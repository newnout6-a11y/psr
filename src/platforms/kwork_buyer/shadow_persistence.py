"""Durable audit storage for Buyer Search shadow reports and rollout gates."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4


class BuyerShadowPersistenceError(RuntimeError):
    """Raised when immutable shadow evidence cannot be stored or read."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_shadow_reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT,
    comparison_json TEXT NOT NULL,
    acceptance_json TEXT,
    operator_accepted INTEGER NOT NULL DEFAULT 0,
    rollback_documented INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buyer_shadow_reports_run_created_idx
    ON buyer_shadow_reports(run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS buyer_shadow_rollout_gates (
    gate_id TEXT PRIMARY KEY,
    run_id TEXT,
    target_workers INTEGER NOT NULL,
    allowed INTEGER NOT NULL,
    blockers_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    report_id TEXT REFERENCES buyer_shadow_reports(report_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buyer_shadow_gates_run_target_idx
    ON buyer_shadow_rollout_gates(run_id, target_workers, created_at DESC);
"""


class SQLiteBuyerShadowStore:
    """Small additive SQLite store for immutable comparison and gate evidence."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def close(self) -> None:
        return None

    async def save_report(
        self,
        comparison: Mapping[str, Any],
        *,
        run_id: str | None = None,
        acceptance: Mapping[str, Any] | None = None,
        operator_accepted: bool = False,
        rollback_documented: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_initialized()
        return await asyncio.to_thread(
            self._save_report_sync,
            dict(comparison),
            _optional_text(run_id),
            dict(acceptance) if acceptance is not None else None,
            bool(operator_accepted),
            bool(rollback_documented),
        )

    async def save_gate(
        self,
        gate: Mapping[str, Any],
        *,
        run_id: str | None = None,
        evidence: list[Mapping[str, Any]] | None = None,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_initialized()
        return await asyncio.to_thread(
            self._save_gate_sync,
            dict(gate),
            _optional_text(run_id),
            [dict(item) for item in evidence or ()],
            _optional_text(report_id),
        )

    async def latest_approved_gate(self, run_id: str, *, target_workers: int) -> dict[str, Any] | None:
        await self._ensure_initialized()
        return await asyncio.to_thread(self._latest_approved_gate_sync, _required_text(run_id, "run_id"), int(target_workers))

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _save_report_sync(
        self,
        comparison: Mapping[str, Any],
        run_id: str | None,
        acceptance: Mapping[str, Any] | None,
        operator_accepted: bool,
        rollback_documented: bool,
    ) -> dict[str, Any]:
        report_id = f"buyer-shadow-report-{uuid4()}"
        created_at = _now()
        payload = {
            "report_id": report_id,
            "run_id": run_id,
            "comparison": _jsonable(comparison, "comparison"),
            "acceptance": _jsonable(acceptance, "acceptance") if acceptance is not None else None,
            "operator_accepted": operator_accepted,
            "rollback_documented": rollback_documented,
            "created_at": created_at,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO buyer_shadow_reports (
                    report_id, run_id, comparison_json, acceptance_json,
                    operator_accepted, rollback_documented, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    run_id,
                    _json(payload["comparison"]),
                    _json(payload["acceptance"]) if payload["acceptance"] is not None else None,
                    int(operator_accepted),
                    int(rollback_documented),
                    created_at,
                ),
            )
        return payload

    def _save_gate_sync(
        self,
        gate: Mapping[str, Any],
        run_id: str | None,
        evidence: list[Mapping[str, Any]],
        report_id: str | None,
    ) -> dict[str, Any]:
        target_workers = _positive_int(gate.get("target_workers"), "gate.target_workers")
        allowed = bool(gate.get("allowed"))
        blockers = gate.get("blockers")
        if not isinstance(blockers, list):
            blockers = []
        gate_id = f"buyer-shadow-gate-{uuid4()}"
        payload = {
            "gate_id": gate_id,
            "run_id": run_id,
            "target_workers": target_workers,
            "allowed": allowed,
            "blockers": [str(item) for item in blockers],
            "evidence": [_jsonable(item, "evidence") for item in evidence],
            "report_id": report_id,
            "created_at": _now(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO buyer_shadow_rollout_gates (
                    gate_id, run_id, target_workers, allowed, blockers_json,
                    evidence_json, report_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gate_id,
                    run_id,
                    target_workers,
                    int(allowed),
                    _json(payload["blockers"]),
                    _json(payload["evidence"]),
                    report_id,
                    payload["created_at"],
                ),
            )
        return payload

    def _latest_approved_gate_sync(self, run_id: str, target_workers: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM buyer_shadow_rollout_gates
                WHERE run_id = ? AND target_workers = ? AND allowed = 1
                ORDER BY created_at DESC, gate_id DESC LIMIT 1
                """,
                (run_id, target_workers),
            ).fetchone()
        if row is None:
            return None
        return {
            "gate_id": row["gate_id"],
            "run_id": row["run_id"],
            "target_workers": int(row["target_workers"]),
            "allowed": bool(row["allowed"]),
            "blockers": _from_json(row["blockers_json"], []),
            "evidence": _from_json(row["evidence_json"], []),
            "report_id": row["report_id"],
            "created_at": row["created_at"],
        }


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _from_json(value: object, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _jsonable(value: Any, name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BuyerShadowPersistenceError(f"{name} must be JSON serializable") from exc
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerShadowPersistenceError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise BuyerShadowPersistenceError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerShadowPersistenceError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise BuyerShadowPersistenceError(f"{name} must be a positive integer")
    return parsed


__all__ = ["BuyerShadowPersistenceError", "SQLiteBuyerShadowStore"]
