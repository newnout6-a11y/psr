"""Durable, read-only taxonomy snapshots for Buyer Search planning.

The module deliberately accepts already-observed catalog data.  It does not
know how to call Kwork and therefore cannot mutate remote state.  A
capability-scoped reader may collect ``catalogRubrics``, ``catalogCategories``,
``categoryAttributes``, ``catalogFilters``, catalog seeds, and suggestions,
then commit their normalized snapshot here for later operator and planner use.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any, Protocol
from uuid import uuid4


class BuyerTaxonomyError(RuntimeError):
    """Base exception for Buyer Search taxonomy operations."""


class BuyerTaxonomyNotFoundError(BuyerTaxonomyError):
    """Raised when a requested durable taxonomy record is absent."""


class BuyerTaxonomyConflictError(BuyerTaxonomyError):
    """Raised when a supposedly immutable snapshot is changed."""


JsonDict = dict[str, Any]


@dataclass(frozen=True, slots=True)
class BuyerTaxonomySnapshot:
    """A normalized immutable capture that can be used by category mode.

    ``categories`` must use real positive Kwork category IDs.  Attributes,
    filters, catalog seeds, and suggestions are scoped to a category whenever
    that source supplied one; a ``None`` scope is allowed for global terms.
    """

    snapshot_id: str
    source: str
    captured_at: str
    source_revision: str | None
    provenance: JsonDict
    categories: tuple[JsonDict, ...]
    attributes: tuple[JsonDict, ...]
    filters: tuple[JsonDict, ...]
    terms: tuple[JsonDict, ...]
    content_hash: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BuyerTaxonomySnapshot":
        """Validate an observed taxonomy payload and make it deterministic."""

        if not isinstance(payload, Mapping):
            raise TypeError("taxonomy snapshot payload must be a mapping")
        source = _required_text(payload.get("source"), "source")
        snapshot_id = _optional_text(payload.get("snapshot_id")) or f"buyer-taxonomy-{uuid4().hex}"
        captured_at = _optional_text(payload.get("captured_at")) or _now()
        source_revision = _optional_text(payload.get("source_revision"))
        provenance = _mapping(payload.get("provenance"), "provenance")
        categories = _normalize_categories(payload.get("categories", ()))
        if not categories:
            raise ValueError("taxonomy snapshot requires at least one category")
        category_ids = {category["category_id"] for category in categories}
        attributes = _normalize_descriptors(payload.get("attributes", ()), kind="attribute", category_ids=category_ids)
        filters = _normalize_descriptors(payload.get("filters", ()), kind="filter", category_ids=category_ids)
        terms = _normalize_terms(payload, category_ids=category_ids)
        canonical = {
            "source": source,
            "source_revision": source_revision,
            "provenance": provenance,
            "categories": categories,
            "attributes": attributes,
            "filters": filters,
            "terms": terms,
        }
        return cls(
            snapshot_id=snapshot_id,
            source=source,
            captured_at=captured_at,
            source_revision=source_revision,
            provenance=provenance,
            categories=tuple(categories),
            attributes=tuple(attributes),
            filters=tuple(filters),
            terms=tuple(terms),
            content_hash=_content_hash(canonical),
        )

    def to_payload(self) -> JsonDict:
        """Return a JSON-safe snapshot shape for a store or audit artifact."""

        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "captured_at": self.captured_at,
            "source_revision": self.source_revision,
            "provenance": dict(self.provenance),
            "categories": [dict(item) for item in self.categories],
            "attributes": [dict(item) for item in self.attributes],
            "filters": [dict(item) for item in self.filters],
            "terms": [dict(item) for item in self.terms],
            "content_hash": self.content_hash,
        }


class BuyerTaxonomyStore(Protocol):
    """Persistence operations required by :class:`BuyerTaxonomyService`."""

    async def record_snapshot(self, snapshot: BuyerTaxonomySnapshot) -> JsonDict: ...

    async def get_snapshot(self, snapshot_id: str) -> JsonDict | None: ...

    async def get_latest_snapshot(self, *, source: str | None = None) -> JsonDict | None: ...

    async def list_snapshots(self, *, source: str | None = None, limit: int = 100) -> tuple[JsonDict, ...]: ...

    async def list_categories(
        self,
        snapshot_id: str,
        *,
        parent_category_id: int | None = None,
        query: str | None = None,
        after_category_id: int | None = None,
        limit: int = 100,
    ) -> tuple[JsonDict, ...]: ...

    async def get_category(self, snapshot_id: str, category_id: int) -> JsonDict | None: ...

    async def list_descriptors(
        self,
        snapshot_id: str,
        category_id: int,
        *,
        kind: str,
    ) -> tuple[JsonDict, ...]: ...

    async def list_terms(
        self,
        snapshot_id: str,
        *,
        category_ids: Sequence[int],
        limit: int = 500,
    ) -> tuple[JsonDict, ...]: ...


class BuyerTaxonomyService:
    """Read/write local snapshot facade used by the Buyer Search control plane.

    The only write is a local immutable capture.  Neither this service nor its
    store receives a Kwork client, so a discovery worker cannot use it to call
    a mutation endpoint accidentally.
    """

    def __init__(self, store: BuyerTaxonomyStore) -> None:
        self.store = store

    async def record_snapshot(self, payload: Mapping[str, Any]) -> JsonDict:
        return await self.store.record_snapshot(BuyerTaxonomySnapshot.from_payload(payload))

    async def get_snapshot(self, snapshot_id: str) -> JsonDict:
        record = await self.store.get_snapshot(_required_text(snapshot_id, "snapshot_id"))
        if record is None:
            raise BuyerTaxonomyNotFoundError("taxonomy snapshot was not found")
        return record

    async def list_snapshots(self, *, source: str | None = None, limit: int = 100) -> tuple[JsonDict, ...]:
        return await self.store.list_snapshots(source=_optional_text(source), limit=_bounded_limit(limit))

    async def list_categories(
        self,
        *,
        snapshot_id: str | None = None,
        source: str | None = None,
        parent_category_id: int | None = None,
        query: str | None = None,
        after_category_id: int | None = None,
        limit: int = 100,
    ) -> JsonDict:
        snapshot = await self._resolve_snapshot(snapshot_id=snapshot_id, source=source)
        rows = await self.store.list_categories(
            snapshot["snapshot_id"],
            parent_category_id=_positive_int_or_none(parent_category_id),
            query=_optional_text(query),
            after_category_id=_positive_int_or_none(after_category_id),
            limit=_bounded_limit(limit),
        )
        return {
            "snapshot": snapshot,
            "items": list(rows),
            "next_after_category_id": rows[-1]["category_id"] if len(rows) == _bounded_limit(limit) else None,
        }

    async def get_category_context(
        self,
        category_id: int,
        *,
        snapshot_id: str | None = None,
        source: str | None = None,
        child_limit: int = 200,
        term_limit: int = 500,
    ) -> JsonDict:
        """Build an auditable category-first planner context from one snapshot."""

        resolved_category_id = _positive_int(category_id, "category_id")
        snapshot = await self._resolve_snapshot(snapshot_id=snapshot_id, source=source)
        node = await self.store.get_category(snapshot["snapshot_id"], resolved_category_id)
        if node is None:
            raise BuyerTaxonomyNotFoundError("taxonomy category was not found in the selected snapshot")

        ancestors = await self._ancestors(snapshot["snapshot_id"], node)
        children = await self.store.list_categories(
            snapshot["snapshot_id"],
            parent_category_id=resolved_category_id,
            limit=_bounded_limit(child_limit),
        )
        attributes = await self.store.list_descriptors(snapshot["snapshot_id"], resolved_category_id, kind="attribute")
        filters = await self.store.list_descriptors(snapshot["snapshot_id"], resolved_category_id, kind="filter")
        scope_ids = [resolved_category_id, *(item["category_id"] for item in children)]
        terms = await self.store.list_terms(snapshot["snapshot_id"], category_ids=scope_ids, limit=_bounded_limit(term_limit))
        vocabulary = _build_vocabulary(node, ancestors, children, attributes, filters, terms)
        category_path = [*(item["category_id"] for item in ancestors), resolved_category_id]
        return {
            "snapshot": snapshot,
            "category": node,
            "ancestors": list(ancestors),
            "children": list(children),
            "attributes": list(attributes),
            "filters": list(filters),
            "terms": list(terms),
            "vocabulary": vocabulary,
            "generation_defaults": {
                "category_id": resolved_category_id,
                "category_path": category_path,
                "taxonomy_snapshot_id": snapshot["snapshot_id"],
                "taxonomy_source": snapshot["source"],
            },
        }

    async def _resolve_snapshot(self, *, snapshot_id: str | None, source: str | None) -> JsonDict:
        if snapshot_id is not None:
            return await self.get_snapshot(snapshot_id)
        snapshot = await self.store.get_latest_snapshot(source=_optional_text(source))
        if snapshot is None:
            raise BuyerTaxonomyNotFoundError("no durable taxonomy snapshot is available")
        return snapshot

    async def _ancestors(self, snapshot_id: str, node: Mapping[str, Any]) -> tuple[JsonDict, ...]:
        ancestors: list[JsonDict] = []
        seen = {node["category_id"]}
        parent_id = node.get("parent_category_id")
        while parent_id is not None:
            parent = await self.store.get_category(snapshot_id, _positive_int(parent_id, "parent_category_id"))
            if parent is None or parent["category_id"] in seen:
                break
            ancestors.append(parent)
            seen.add(parent["category_id"])
            parent_id = parent.get("parent_category_id")
            if len(ancestors) >= 32:
                break
        ancestors.reverse()
        return tuple(ancestors)


def _normalize_categories(value: Any) -> list[JsonDict]:
    values = _sequence(value, "categories")
    result: list[JsonDict] = []
    seen: set[int] = set()
    for item in values:
        raw = _mapping(item, "category")
        category_id = _positive_int(raw.get("category_id", raw.get("id")), "category_id")
        if category_id in seen:
            raise BuyerTaxonomyConflictError(f"duplicate category_id in snapshot: {category_id}")
        seen.add(category_id)
        metadata = _merged_metadata(raw, {
            "category_id",
            "id",
            "name",
            "title",
            "parent_category_id",
            "parent_id",
            "rubric_id",
            "rubricId",
            "classifier_id",
            "classifierId",
            "category_path",
            "path",
            "source_path",
            "metadata",
        })
        category_path = _positive_int_sequence(raw.get("category_path", raw.get("path", ())), "category_path")
        if category_path and category_path[-1] != category_id:
            raise ValueError("category_path must end with category_id")
        result.append(
            {
                "category_id": category_id,
                "name": _required_text(raw.get("name", raw.get("title")), "category name"),
                "parent_category_id": _positive_int_or_none(raw.get("parent_category_id", raw.get("parent_id"))),
                "rubric_id": _positive_int_or_none(raw.get("rubric_id", raw.get("rubricId"))),
                "classifier_id": _positive_int_or_none(raw.get("classifier_id", raw.get("classifierId"))),
                "category_path": category_path,
                "source_path": _optional_text(raw.get("source_path")),
                "metadata": metadata,
            }
        )
    result.sort(key=lambda item: item["category_id"])
    return result


def _normalize_descriptors(value: Any, *, kind: str, category_ids: set[int]) -> list[JsonDict]:
    values = _sequence(value, f"{kind}s")
    result: list[JsonDict] = []
    seen: set[tuple[int, str]] = set()
    id_keys = (f"{kind}_id", "id", "key", "code", "name", "title")
    for item in values:
        raw = _mapping(item, kind)
        category_id = _positive_int(raw.get("category_id", raw.get("categoryId")), f"{kind}.category_id")
        if category_id not in category_ids:
            raise ValueError(f"{kind} category_id is absent from snapshot categories: {category_id}")
        descriptor_id = ""
        for key in id_keys:
            descriptor_id = _optional_text(raw.get(key)) or ""
            if descriptor_id:
                break
        if not descriptor_id:
            raise ValueError(f"{kind} requires an id, key, code, or name")
        name = _optional_text(raw.get("name", raw.get("title"))) or descriptor_id
        identity = (category_id, descriptor_id.casefold())
        if identity in seen:
            raise BuyerTaxonomyConflictError(f"duplicate {kind} key in category {category_id}: {descriptor_id}")
        seen.add(identity)
        metadata = _merged_metadata(raw, {
            "category_id",
            "categoryId",
            f"{kind}_id",
            "id",
            "key",
            "code",
            "name",
            "title",
            "values",
            "options",
            "metadata",
        })
        values_payload = raw.get("values", raw.get("options", ()))
        result.append(
            {
                "category_id": category_id,
                "descriptor_id": descriptor_id,
                "name": name,
                "values": _json_list(values_payload, f"{kind}.values"),
                "metadata": metadata,
            }
        )
    result.sort(key=lambda item: (item["category_id"], item["descriptor_id"].casefold()))
    return result


def _normalize_terms(payload: Mapping[str, Any], *, category_ids: set[int]) -> list[JsonDict]:
    term_groups = (
        ("catalog_seed", payload.get("catalog_seeds", ())),
        ("suggestion", payload.get("suggestions", ())),
        ("term", payload.get("terms", ())),
    )
    result: list[JsonDict] = []
    seen: set[tuple[str, int | None, str]] = set()
    for default_kind, raw_values in term_groups:
        for value in _sequence(raw_values, f"{default_kind}s"):
            raw = {"text": value} if isinstance(value, str) else _mapping(value, default_kind)
            text = _required_text(raw.get("text", raw.get("name", raw.get("title"))), f"{default_kind} text")
            kind = _optional_text(raw.get("kind")) or default_kind
            category_id = _positive_int_or_none(raw.get("category_id", raw.get("categoryId")))
            if category_id is not None and category_id not in category_ids:
                raise ValueError(f"term category_id is absent from snapshot categories: {category_id}")
            key = (kind.casefold(), category_id, text.casefold())
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "kind": kind,
                    "category_id": category_id,
                    "text": text,
                    "metadata": _merged_metadata(
                        raw,
                        {"kind", "category_id", "categoryId", "text", "name", "title", "metadata"},
                    ),
                }
            )
    result.sort(key=lambda item: (item["kind"].casefold(), item["category_id"] or 0, item["text"].casefold()))
    return result


def _build_vocabulary(
    node: Mapping[str, Any],
    ancestors: Sequence[Mapping[str, Any]],
    children: Sequence[Mapping[str, Any]],
    attributes: Sequence[Mapping[str, Any]],
    filters: Sequence[Mapping[str, Any]],
    terms: Sequence[Mapping[str, Any]],
) -> list[JsonDict]:
    """Return ordered source-labelled terms rather than silent query expansion."""

    vocabulary: list[JsonDict] = []
    seen: set[str] = set()

    def add(text: Any, source: str, *, category_id: int | None = None, metadata: Mapping[str, Any] | None = None) -> None:
        if not isinstance(text, str):
            return
        clean = text.strip()
        key = clean.casefold()
        if not clean or key in seen:
            return
        seen.add(key)
        vocabulary.append(
            {
                "text": clean,
                "source": source,
                "category_id": category_id,
                "metadata": dict(metadata or {}),
            }
        )

    add(node.get("name"), "category", category_id=node.get("category_id"))
    for ancestor in ancestors:
        add(ancestor.get("name"), "ancestor", category_id=ancestor.get("category_id"))
    for child in children:
        add(child.get("name"), "child_category", category_id=child.get("category_id"))
    for descriptor in attributes:
        source = "attribute"
        add(descriptor.get("name"), source, category_id=descriptor.get("category_id"))
        for option in descriptor.get("values", ()):
            if isinstance(option, str):
                add(option, f"{source}_value", category_id=descriptor.get("category_id"))
            elif isinstance(option, Mapping):
                add(
                    option.get("name", option.get("title", option.get("label", option.get("value")))),
                    f"{source}_value",
                    category_id=descriptor.get("category_id"),
                )
    for descriptor in filters:
        source = "filter"
        add(descriptor.get("name"), source, category_id=descriptor.get("category_id"))
        for option in descriptor.get("values", ()):
            if isinstance(option, str):
                add(option, f"{source}_value", category_id=descriptor.get("category_id"))
            elif isinstance(option, Mapping):
                add(
                    option.get("name", option.get("title", option.get("label", option.get("value")))),
                    f"{source}_value",
                    category_id=descriptor.get("category_id"),
                )
    for term in terms:
        add(
            term.get("text"),
            str(term.get("kind") or "term"),
            category_id=term.get("category_id"),
            metadata=term.get("metadata") if isinstance(term.get("metadata"), Mapping) else None,
        )
    return vocabulary


def _content_hash(value: Mapping[str, Any]) -> str:
    return sha256(_dump_json(value).encode("utf-8")).hexdigest()


def _merged_metadata(raw: Mapping[str, Any], reserved: set[str]) -> JsonDict:
    metadata = _mapping(raw.get("metadata"), "metadata") if raw.get("metadata") is not None else {}
    for key, value in raw.items():
        if key not in reserved:
            metadata[key] = value
    _dump_json(metadata)
    return metadata


def _json_list(value: Any, name: str) -> list[Any]:
    values = _sequence(value, name)
    clean = list(values)
    _dump_json(clean)
    return clean


def _mapping(value: Any, name: str) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    result = dict(value)
    _dump_json(result)
    return result


def _sequence(value: Any, name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be an array")
    return tuple(value)


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
    if value is None or value == "":
        return None
    return _positive_int(value, "category_id")


def _positive_int_sequence(value: Any, name: str) -> list[int]:
    return [_positive_int(item, name) for item in _sequence(value, name)]


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} cannot be blank")
    return clean


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("text value must be a string")
    clean = value.strip()
    return clean or None


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("limit must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("limit must be an integer") from exc
    return max(1, min(parsed, 1_000))


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("taxonomy value is not JSON serializable") from exc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "BuyerTaxonomyConflictError",
    "BuyerTaxonomyError",
    "BuyerTaxonomyNotFoundError",
    "BuyerTaxonomyService",
    "BuyerTaxonomySnapshot",
    "BuyerTaxonomyStore",
]
