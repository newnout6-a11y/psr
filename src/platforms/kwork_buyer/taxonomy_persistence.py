"""SQLite persistence for immutable Buyer Search taxonomy snapshots.

This store persists observations that have already been read through an
account-bound capability.  It intentionally owns no HTTP client, credentials,
or remote mutation capability.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from .taxonomy import (
    BuyerTaxonomyConflictError,
    BuyerTaxonomyError,
    BuyerTaxonomySnapshot,
)


class BuyerTaxonomyPersistenceError(BuyerTaxonomyError):
    """Raised when an on-disk taxonomy record is invalid or unavailable."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_taxonomy_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_revision TEXT,
    captured_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    category_count INTEGER NOT NULL,
    attribute_count INTEGER NOT NULL,
    filter_count INTEGER NOT NULL,
    term_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source, source_revision)
);

CREATE INDEX IF NOT EXISTS buyer_taxonomy_snapshots_recent_idx
    ON buyer_taxonomy_snapshots(source, captured_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS buyer_taxonomy_categories (
    snapshot_id TEXT NOT NULL REFERENCES buyer_taxonomy_snapshots(snapshot_id) ON DELETE RESTRICT,
    category_id INTEGER NOT NULL CHECK(category_id > 0),
    name TEXT NOT NULL,
    parent_category_id INTEGER,
    rubric_id INTEGER,
    classifier_id INTEGER,
    category_path_json TEXT NOT NULL,
    source_path TEXT,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, category_id)
);

CREATE INDEX IF NOT EXISTS buyer_taxonomy_categories_parent_idx
    ON buyer_taxonomy_categories(snapshot_id, parent_category_id, category_id);

CREATE INDEX IF NOT EXISTS buyer_taxonomy_categories_name_idx
    ON buyer_taxonomy_categories(snapshot_id, name COLLATE NOCASE, category_id);

CREATE TABLE IF NOT EXISTS buyer_taxonomy_descriptors (
    snapshot_id TEXT NOT NULL REFERENCES buyer_taxonomy_snapshots(snapshot_id) ON DELETE RESTRICT,
    category_id INTEGER NOT NULL CHECK(category_id > 0),
    kind TEXT NOT NULL CHECK(kind IN ('attribute', 'filter')),
    descriptor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    values_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, category_id, kind, descriptor_id)
);

CREATE INDEX IF NOT EXISTS buyer_taxonomy_descriptors_category_idx
    ON buyer_taxonomy_descriptors(snapshot_id, category_id, kind, descriptor_id);

CREATE TABLE IF NOT EXISTS buyer_taxonomy_terms (
    snapshot_id TEXT NOT NULL REFERENCES buyer_taxonomy_snapshots(snapshot_id) ON DELETE RESTRICT,
    category_id INTEGER NOT NULL DEFAULT 0 CHECK(category_id >= 0),
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, category_id, kind, normalized_text)
);

CREATE INDEX IF NOT EXISTS buyer_taxonomy_terms_scope_idx
    ON buyer_taxonomy_terms(snapshot_id, category_id, kind, normalized_text);
"""


class SQLiteBuyerTaxonomyStore:
    """Short-lived-connection SQLite implementation for local taxonomy state."""

    def __init__(self, db_path: str | Path) -> None:
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a string or Path")
        if not str(db_path).strip():
            raise ValueError("db_path cannot be blank")
        self.db_path = Path(db_path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def close(self) -> None:
        """Close the logical lifecycle; every operation owns its connection."""

        return None

    async def record_snapshot(self, snapshot: BuyerTaxonomySnapshot) -> dict[str, Any]:
        if not isinstance(snapshot, BuyerTaxonomySnapshot):
            raise TypeError("snapshot must be a BuyerTaxonomySnapshot")
        return await self._call(self._record_snapshot_sync, snapshot)

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return await self._call(self._get_snapshot_sync, _required_text(snapshot_id, "snapshot_id"))

    async def get_latest_snapshot(self, *, source: str | None = None) -> dict[str, Any] | None:
        return await self._call(self._get_latest_snapshot_sync, _optional_text(source))

    async def list_snapshots(self, *, source: str | None = None, limit: int = 100) -> tuple[dict[str, Any], ...]:
        return await self._call(self._list_snapshots_sync, _optional_text(source), _bounded_limit(limit))

    async def list_categories(
        self,
        snapshot_id: str,
        *,
        parent_category_id: int | None = None,
        query: str | None = None,
        after_category_id: int | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        return await self._call(
            self._list_categories_sync,
            _required_text(snapshot_id, "snapshot_id"),
            _positive_int_or_none(parent_category_id),
            _optional_text(query),
            _positive_int_or_none(after_category_id),
            _bounded_limit(limit),
        )

    async def get_category(self, snapshot_id: str, category_id: int) -> dict[str, Any] | None:
        return await self._call(
            self._get_category_sync,
            _required_text(snapshot_id, "snapshot_id"),
            _positive_int(category_id, "category_id"),
        )

    async def list_descriptors(
        self,
        snapshot_id: str,
        category_id: int,
        *,
        kind: str,
    ) -> tuple[dict[str, Any], ...]:
        normalized_kind = _descriptor_kind(kind)
        return await self._call(
            self._list_descriptors_sync,
            _required_text(snapshot_id, "snapshot_id"),
            _positive_int(category_id, "category_id"),
            normalized_kind,
        )

    async def list_terms(
        self,
        snapshot_id: str,
        *,
        category_ids: Sequence[int],
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        normalized_ids = tuple(dict.fromkeys(_positive_int(item, "category_ids") for item in category_ids))
        if not normalized_ids:
            return ()
        return await self._call(
            self._list_terms_sync,
            _required_text(snapshot_id, "snapshot_id"),
            normalized_ids,
            _bounded_limit(limit),
        )

    async def _call(self, function: Any, *args: Any) -> Any:
        await self._ensure_initialized()
        return await asyncio.to_thread(function, *args)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _record_snapshot_sync(self, snapshot: BuyerTaxonomySnapshot) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = connection.execute(
                "SELECT * FROM buyer_taxonomy_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                return self._assert_immutable_match(existing, snapshot)

            if snapshot.source_revision is not None:
                existing = connection.execute(
                    "SELECT * FROM buyer_taxonomy_snapshots WHERE source = ? AND source_revision = ?",
                    (snapshot.source, snapshot.source_revision),
                ).fetchone()
                if existing is not None:
                    return self._assert_immutable_match(existing, snapshot)

            connection.execute(
                """
                INSERT INTO buyer_taxonomy_snapshots (
                    snapshot_id, source, source_revision, captured_at, provenance_json, content_hash,
                    category_count, attribute_count, filter_count, term_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.source,
                    snapshot.source_revision,
                    snapshot.captured_at,
                    _dump_json(snapshot.provenance),
                    snapshot.content_hash,
                    len(snapshot.categories),
                    len(snapshot.attributes),
                    len(snapshot.filters),
                    len(snapshot.terms),
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO buyer_taxonomy_categories (
                    snapshot_id, category_id, name, parent_category_id, rubric_id, classifier_id,
                    category_path_json, source_path, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.snapshot_id,
                        item["category_id"],
                        item["name"],
                        item["parent_category_id"],
                        item["rubric_id"],
                        item["classifier_id"],
                        _dump_json(item["category_path"]),
                        item["source_path"],
                        _dump_json(item["metadata"]),
                    )
                    for item in snapshot.categories
                ],
            )
            connection.executemany(
                """
                INSERT INTO buyer_taxonomy_descriptors (
                    snapshot_id, category_id, kind, descriptor_id, name, values_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.snapshot_id,
                        item["category_id"],
                        "attribute",
                        item["descriptor_id"],
                        item["name"],
                        _dump_json(item["values"]),
                        _dump_json(item["metadata"]),
                    )
                    for item in snapshot.attributes
                ]
                + [
                    (
                        snapshot.snapshot_id,
                        item["category_id"],
                        "filter",
                        item["descriptor_id"],
                        item["name"],
                        _dump_json(item["values"]),
                        _dump_json(item["metadata"]),
                    )
                    for item in snapshot.filters
                ],
            )
            connection.executemany(
                """
                INSERT INTO buyer_taxonomy_terms (
                    snapshot_id, category_id, kind, text, normalized_text, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.snapshot_id,
                        item["category_id"] or 0,
                        item["kind"],
                        item["text"],
                        item["text"].casefold(),
                        _dump_json(item["metadata"]),
                    )
                    for item in snapshot.terms
                ],
            )
            row = connection.execute(
                "SELECT * FROM buyer_taxonomy_snapshots WHERE snapshot_id = ?", (snapshot.snapshot_id,)
            ).fetchone()
        if row is None:
            raise BuyerTaxonomyPersistenceError("taxonomy snapshot was not persisted")
        return _snapshot_from_row(row)

    def _assert_immutable_match(self, row: sqlite3.Row, snapshot: BuyerTaxonomySnapshot) -> dict[str, Any]:
        if row["content_hash"] != snapshot.content_hash:
            raise BuyerTaxonomyConflictError("taxonomy snapshot ID or source revision already exists with different content")
        return _snapshot_from_row(row)

    def _get_snapshot_sync(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_taxonomy_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def _get_latest_snapshot_sync(self, source: str | None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if source is None:
                row = connection.execute(
                    """
                    SELECT * FROM buyer_taxonomy_snapshots
                    ORDER BY captured_at DESC, created_at DESC, snapshot_id ASC LIMIT 1
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM buyer_taxonomy_snapshots WHERE source = ?
                    ORDER BY captured_at DESC, created_at DESC, snapshot_id ASC LIMIT 1
                    """,
                    (source,),
                ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def _list_snapshots_sync(self, source: str | None, limit: int) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            if source is None:
                rows = connection.execute(
                    """
                    SELECT * FROM buyer_taxonomy_snapshots
                    ORDER BY captured_at DESC, created_at DESC, snapshot_id ASC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM buyer_taxonomy_snapshots WHERE source = ?
                    ORDER BY captured_at DESC, created_at DESC, snapshot_id ASC LIMIT ?
                    """,
                    (source, limit),
                ).fetchall()
        return tuple(_snapshot_from_row(row) for row in rows)

    def _list_categories_sync(
        self,
        snapshot_id: str,
        parent_category_id: int | None,
        query: str | None,
        after_category_id: int | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        clauses = ["snapshot_id = ?"]
        values: list[Any] = [snapshot_id]
        # Tree browsing starts at roots. A supplied text query intentionally
        # searches the whole immutable snapshot so a picker can find a deep
        # category without knowing its parent first.
        if parent_category_id is None and query is None:
            clauses.append("parent_category_id IS NULL")
        elif parent_category_id is not None:
            clauses.append("parent_category_id = ?")
            values.append(parent_category_id)
        if query is not None:
            clauses.append("name LIKE ? COLLATE NOCASE")
            values.append(f"%{query}%")
        if after_category_id is not None:
            clauses.append("category_id > ?")
            values.append(after_category_id)
        values.append(limit)
        statement = f"""
            SELECT * FROM buyer_taxonomy_categories
            WHERE {' AND '.join(clauses)}
            ORDER BY category_id ASC LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(statement, values).fetchall()
        return tuple(_category_from_row(row) for row in rows)

    def _get_category_sync(self, snapshot_id: str, category_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM buyer_taxonomy_categories
                WHERE snapshot_id = ? AND category_id = ?
                """,
                (snapshot_id, category_id),
            ).fetchone()
        return _category_from_row(row) if row is not None else None

    def _list_descriptors_sync(
        self,
        snapshot_id: str,
        category_id: int,
        kind: str,
    ) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_taxonomy_descriptors
                WHERE snapshot_id = ? AND category_id = ? AND kind = ?
                ORDER BY descriptor_id COLLATE NOCASE ASC
                """,
                (snapshot_id, category_id, kind),
            ).fetchall()
        return tuple(_descriptor_from_row(row) for row in rows)

    def _list_terms_sync(
        self,
        snapshot_id: str,
        category_ids: tuple[int, ...],
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        placeholders = ", ".join("?" for _ in category_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM buyer_taxonomy_terms
                WHERE snapshot_id = ? AND (category_id = 0 OR category_id IN ({placeholders}))
                ORDER BY kind COLLATE NOCASE ASC, category_id ASC, normalized_text ASC
                LIMIT ?
                """,
                (snapshot_id, *category_ids, limit),
            ).fetchall()
        return tuple(_term_from_row(row) for row in rows)


def _snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_id": row["snapshot_id"],
        "source": row["source"],
        "source_revision": row["source_revision"],
        "captured_at": row["captured_at"],
        "provenance": _load_json(row["provenance_json"], "provenance"),
        "content_hash": row["content_hash"],
        "category_count": row["category_count"],
        "attribute_count": row["attribute_count"],
        "filter_count": row["filter_count"],
        "term_count": row["term_count"],
        "created_at": row["created_at"],
    }


def _category_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "category_id": row["category_id"],
        "name": row["name"],
        "parent_category_id": row["parent_category_id"],
        "rubric_id": row["rubric_id"],
        "classifier_id": row["classifier_id"],
        "category_path": _load_json(row["category_path_json"], "category_path"),
        "source_path": row["source_path"],
        "metadata": _load_json(row["metadata_json"], "metadata"),
    }


def _descriptor_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "category_id": row["category_id"],
        "descriptor_id": row["descriptor_id"],
        "name": row["name"],
        "values": _load_json(row["values_json"], "descriptor values"),
        "metadata": _load_json(row["metadata_json"], "descriptor metadata"),
    }


def _term_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "category_id": row["category_id"] or None,
        "text": row["text"],
        "metadata": _load_json(row["metadata_json"], "term metadata"),
    }


def _load_json(value: Any, name: str) -> Any:
    if not isinstance(value, str):
        raise BuyerTaxonomyPersistenceError(f"stored taxonomy {name} is invalid")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise BuyerTaxonomyPersistenceError(f"stored taxonomy {name} JSON is invalid") from exc


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BuyerTaxonomyPersistenceError("taxonomy value cannot be persisted as JSON") from exc


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} cannot be blank")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _required_text(value, "text")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return _positive_int(value, "category ID")


def _descriptor_kind(value: Any) -> str:
    if value not in {"attribute", "filter"}:
        raise ValueError("descriptor kind must be attribute or filter")
    return str(value)


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("limit must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("limit must be an integer") from exc
    return max(1, min(result, 1_000))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


BuyerTaxonomySQLiteStore = SQLiteBuyerTaxonomyStore


__all__ = [
    "BuyerTaxonomyPersistenceError",
    "BuyerTaxonomySQLiteStore",
    "SQLiteBuyerTaxonomyStore",
]
