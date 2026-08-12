"""Deterministic query and filter normalization for Buyer Search."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
import json
import math
from typing import Any, TypeAlias
import unicodedata


QueryKey: TypeAlias = tuple[int | None, str, str]


def normalize_query_text(value: str) -> str:
    """Return the exact-deduplication representation of a search query.

    The display text remains the caller's responsibility.  This value is only
    for matching and hashing, so compatibility Unicode forms, casing, and
    whitespace cannot create separate durable queries.
    """

    if not isinstance(value, str):
        raise TypeError("query text must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def canonical_filter_json(filters: Mapping[str, Any] | None = None) -> str:
    """Serialize filters deterministically for durable exact-query identity."""

    normalized = _normalize_filter_value({} if filters is None else filters)
    if not isinstance(normalized, dict):
        raise TypeError("filters must be a mapping")
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def filter_hash(filters: Mapping[str, Any] | None = None) -> str:
    """Return the SHA-256 digest of canonical filter JSON."""

    return sha256(canonical_filter_json(filters).encode("utf-8")).hexdigest()


def stable_query_key(
    value: str,
    *,
    category_id: int | None,
    filters: Mapping[str, Any] | None = None,
) -> QueryKey:
    """Return the tuple used by the run/category/query/filter uniqueness rule."""

    _validate_category_id(category_id)
    return (category_id, normalize_query_text(value), filter_hash(filters))


def stable_query_hash(
    value: str,
    *,
    category_id: int | None,
    filters: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable hash of :func:`stable_query_key` for logs and indexes."""

    key = stable_query_key(value, category_id=category_id, filters=filters)
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def deduplicate_queries(values: Iterable[str]) -> list[str]:
    """Keep the first non-empty display query for every normalized exact match."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("query values must be strings")
        normalized = normalize_query_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value.strip())
    return result


def _validate_category_id(category_id: int | None) -> None:
    if category_id is None:
        return
    if isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0:
        raise ValueError("category_id must be a positive integer or None")


def _normalize_filter_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("filter keys must be strings")
            normalized[key] = _normalize_filter_value(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_filter_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("filter numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported filter value type: {type(value).__name__}")


__all__ = [
    "QueryKey",
    "canonical_filter_json",
    "deduplicate_queries",
    "filter_hash",
    "normalize_query_text",
    "stable_query_hash",
    "stable_query_key",
]
