"""Account-scoped page adapters used by Buyer Search discovery workers.

These adapters intentionally accept :class:`BuyerReadCapabilities`, never a
general Kwork client.  They normalize the small amount of task metadata a
worker needs into explicit read calls and keep source-specific filter rules in
one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..mapper import MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE
from .capabilities import BuyerReadCapabilities


class BuyerReadPageSourceError(RuntimeError):
    """A retryable source read failure with optional HTTP context."""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class BuyerMobileProjectsPageSource:
    """Read a mobile ``/projects`` page with a strict parameter allow-list."""

    source = MOBILE_PROJECT_SOURCE
    _ALLOWED_FILTERS = frozenset(
        {
            "categories",
            "page",
            "query",
            "price_from",
            "price_to",
            "hiring_from",
            "kworks_filter_from",
            "kworks_filter_to",
            "sort",
            "limit",
            "only_active",
        }
    )

    def __init__(self, capabilities: BuyerReadCapabilities) -> None:
        if not isinstance(capabilities, BuyerReadCapabilities):
            raise TypeError("capabilities must be BuyerReadCapabilities")
        self._capabilities = capabilities

    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_identity_match(self._capabilities, identity)
        params = _mobile_project_params(task, allowed=self._ALLOWED_FILTERS)
        try:
            response = await self._capabilities.fetch_projects(**params)
        except Exception as exc:  # noqa: BLE001 - preserve source error semantics for the fenced worker.
            raise _as_source_error(exc) from exc
        return _response_envelope(response, source=self.source, task=task)


class BuyerWebProjectsPageSource:
    """Read authenticated web state through the explicit web-project capability."""

    source = WEB_PROJECT_SOURCE
    _ALLOWED_FILTERS = frozenset(
        {
            "category_id",
            "page",
            "query",
            "price_from",
            "price_to",
            "hiring_from",
            "kworks_filter_from",
            "kworks_filter_to",
            "prices_filters",
            "sort",
            "status",
        }
    )

    def __init__(self, capabilities: BuyerReadCapabilities) -> None:
        if not isinstance(capabilities, BuyerReadCapabilities):
            raise TypeError("capabilities must be BuyerReadCapabilities")
        self._capabilities = capabilities

    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_identity_match(self._capabilities, identity)
        params = _web_project_params(task, allowed=self._ALLOWED_FILTERS)
        try:
            response = await self._capabilities.fetch_web_projects(**params)
        except Exception as exc:  # noqa: BLE001 - preserve source error semantics for the fenced worker.
            raise _as_source_error(exc) from exc
        return _response_envelope(response, source=self.source, task=task)


def _require_identity_match(capabilities: BuyerReadCapabilities, identity: Mapping[str, Any]) -> None:
    if not isinstance(identity, Mapping):
        raise BuyerReadPageSourceError("worker identity must be a mapping")
    provenance = capabilities.provenance
    expected = {
        "worker_id": provenance.worker_id,
        "account_registration_id": provenance.account_registration_id,
        "transport_id": provenance.transport_id,
        "egress_ip": provenance.egress_ip,
    }
    mismatched = [name for name, value in expected.items() if str(identity.get(name) or "").strip() != value]
    if mismatched:
        raise BuyerReadPageSourceError(f"capability identity mismatch: {', '.join(mismatched)}")


def _mobile_project_params(task: Mapping[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    filters = _task_filters(task)
    params = _filter_params(filters, allowed=allowed)
    params["page"] = _positive_int(task.get("page"), default=1)
    query = _task_query_text(task)
    if query:
        params["query"] = query
    category_id = _task_category_id(task)
    if category_id is not None:
        params["categories"] = str(category_id)
    # The mobile API must never receive web-only filtering fields.
    for forbidden in ("keyword", "kworks_filters", "prices_filters", "classifier", "attributes"):
        params.pop(forbidden, None)
    return params


def _web_project_params(task: Mapping[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    filters = _task_filters(task)
    params = _filter_params(filters, allowed=allowed)
    params["page"] = _positive_int(task.get("page"), default=1)
    query = _task_query_text(task)
    if query:
        params["query"] = query
    category_id = _task_category_id(task)
    if category_id is not None:
        params.setdefault("category_id", category_id)
    return params


def _task_filters(task: Mapping[str, Any]) -> Mapping[str, Any]:
    value = task.get("filters")
    if isinstance(value, Mapping):
        return value
    query = task.get("query")
    if isinstance(query, Mapping) and isinstance(query.get("filters"), Mapping):
        return query["filters"]
    return {}


def _task_query_text(task: Mapping[str, Any]) -> str:
    origin = task.get("query_origin")
    if origin is None and isinstance(task.get("query"), Mapping):
        origin = task["query"].get("origin")
    if str(origin or "").strip().casefold() == "category_browse":
        return ""
    for key in ("query_text", "text", "query"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if key == "query" and isinstance(value, Mapping):
            nested = value.get("text")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _task_category_id(task: Mapping[str, Any]) -> int | None:
    for key in ("category_id", "categoryId"):
        value = task.get(key)
        parsed = _optional_positive_int(value)
        if parsed is not None:
            return parsed
    query = task.get("query")
    if isinstance(query, Mapping):
        return _optional_positive_int(query.get("category_id"))
    return None


def _filter_params(filters: Mapping[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    normalized = dict(filters)
    aliases = {
        "min_budget": "price_from",
        "max_budget": "price_to",
        "min_offers": "kworks_filter_from",
        "max_offers": "kworks_filter_to",
    }
    for source_key, target_key in aliases.items():
        if target_key not in normalized and normalized.get(source_key) not in (None, ""):
            normalized[target_key] = normalized[source_key]
    return {key: value for key, value in normalized.items() if key in allowed and value not in (None, "")}


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _response_envelope(response: Any, *, source: str, task: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(response, Mapping):
        envelope = dict(response)
    elif isinstance(response, tuple) and len(response) == 2 and isinstance(response[1], Mapping):
        cards, metadata = response
        envelope = {**metadata, "response": list(cards) if isinstance(cards, (list, tuple)) else cards}
    elif isinstance(response, (list, tuple)):
        envelope = {"response": list(response)}
    else:
        raise BuyerReadPageSourceError("read capability returned an unsupported response shape")
    envelope.setdefault("source", source)
    envelope.setdefault("page", _positive_int(task.get("page"), default=1))
    envelope.setdefault("observed_at", datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    return envelope


def _as_source_error(exc: Exception) -> BuyerReadPageSourceError:
    if isinstance(exc, BuyerReadPageSourceError):
        return exc
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    retry_after = getattr(exc, "retry_after_seconds", None)
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool) or retry_after <= 0:
        retry_after = None
    return BuyerReadPageSourceError(str(exc) or type(exc).__name__, status_code=status_code, retry_after_seconds=retry_after)


__all__ = [
    "BuyerMobileProjectsPageSource",
    "BuyerReadPageSourceError",
    "BuyerWebProjectsPageSource",
]
