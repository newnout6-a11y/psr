"""Pure parsing and contract validation for the Kwork web catalog stream."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
import re
from typing import Any, Protocol

from ..contracts import (
    BatchRequest,
    BatchResult,
    BatchState,
    ContractState,
    ContractVerdict,
    CursorKind,
    ProtectionStatus,
    SourceCapabilities,
    SourceCursor,
    account_novelty,
    card_identity,
    fingerprint_for_cards,
)


WEB_CATALOG_CAPABILITIES = SourceCapabilities(
    supports_scope_mapping=False,
    supports_continuation=True,
    supports_page_pagination=False,
    reports_cursor=False,
    emits_raw_artifacts=True,
)

_CATALOG_PATH_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,120}$")
_RESERVED_FILTER_KEYS = {"excludeIds", "onePage", "page", "pageSize"}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: object) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _response_size(payload: object) -> int | None:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _protection_status(status_code: int) -> ProtectionStatus:
    if status_code == 403:
        return ProtectionStatus.BLOCKED
    if status_code == 429:
        return ProtectionStatus.RATE_LIMITED
    if status_code >= 400:
        return ProtectionStatus.HTTP_ERROR
    return ProtectionStatus.OK


def _normalize_catalog_path(alias: str) -> str:
    """Accept a canonical relative catalog path and reject URL-like input."""

    value = alias.strip()
    if not value or "?" in value or "#" in value or "://" in value or value.startswith("//"):
        raise ValueError("invalid Kwork catalog path")
    value = value.strip("/")
    segments = value.split("/")
    if not segments or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid Kwork catalog path")
    if not all(_CATALOG_PATH_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ValueError("invalid Kwork catalog path")
    return "/".join(segments)


def _scope_filters(filters: Mapping[str, object] | None) -> dict[str, object]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise TypeError("filters must be a mapping")
    normalized: dict[str, object] = {}
    for key, value in filters.items():
        name = str(key).strip()
        if not name:
            raise ValueError("filter keys cannot be blank")
        if name in _RESERVED_FILTER_KEYS:
            raise ValueError(f"{name!r} is managed by the web catalog continuation contract")
        normalized[name] = value
    return normalized


def _payload_parts(
    payload: object,
) -> tuple[
    tuple[Mapping[str, Any], ...],
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    str | None,
]:
    """Extract the known card shape, returning an explicit error for bad input."""

    if not isinstance(payload, Mapping):
        return (), {}, {}, {}, "payload_not_mapping"
    if payload.get("success") is not True:
        return (), {}, {}, {}, "response_not_successful"
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return (), {}, {}, {}, "state_data_missing"
    state = data.get("stateData")
    if not isinstance(state, Mapping):
        return (), {}, {}, {}, "state_data_missing"
    view = state.get("viewData")
    if not isinstance(view, Mapping):
        return (), {}, {}, {}, "view_data_missing"
    filters = view.get("filters")
    filters = filters if isinstance(filters, Mapping) else {}
    kworks = view.get("kworks")
    if not isinstance(kworks, Mapping):
        return (), filters, {}, {}, "kworks_missing"
    posts = kworks.get("posts")
    if not isinstance(posts, Mapping):
        return (), filters, kworks, {}, "posts_missing"
    raw_cards = posts.get("data")
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, (str, bytes, bytearray)):
        return (), filters, kworks, posts, "posts_data_missing"
    if not all(isinstance(card, Mapping) for card in raw_cards):
        return (), filters, kworks, posts, "posts_data_not_mapping_list"
    return tuple(raw_cards), filters, kworks, posts, None


def _next_cursor(
    cursor: SourceCursor | None,
    cards: Sequence[Mapping[str, Any]],
) -> SourceCursor | None:
    if not cards:
        return None
    prior_ids = cursor.exclude_ids if cursor and cursor.kind is CursorKind.EXCLUDE_IDS else ()
    identifiers = (*prior_ids, *(card_identity(card) for card in cards))
    return SourceCursor.exclude_ids_cursor(identifiers)


@dataclass(frozen=True, slots=True)
class WebCatalogSource:
    """Adapt already-fetched web catalog JSON without performing HTTP calls."""

    name: str = "web_catalog"
    capabilities: SourceCapabilities = WEB_CATALOG_CAPABILITIES

    def build_request(
        self,
        *,
        alias: str,
        category_id: int,
        cursor: SourceCursor | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> BatchRequest:
        clean_alias = _normalize_catalog_path(alias)
        if category_id <= 0:
            raise ValueError("category_id must be positive")
        if cursor is not None and cursor.kind is not CursorKind.EXCLUDE_IDS:
            raise ValueError("web catalog continuation requires an exclude_ids cursor")
        return BatchRequest(
            source=self.name,
            shard_key=f"category:{category_id}:alias:{clean_alias}",
            cursor=cursor,
            scope={"alias": clean_alias, "category_id": category_id, "filters": _scope_filters(filters)},
        )

    @staticmethod
    def request_params(
        cursor: SourceCursor | None,
        *,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Merge shard filters with the exact accepted continuation cursor."""

        params = _scope_filters(filters)
        if cursor is None:
            return params
        if cursor.kind is not CursorKind.EXCLUDE_IDS:
            raise ValueError("web catalog continuation requires an exclude_ids cursor")
        params.update({"excludeIds": ",".join(cursor.exclude_ids), "onePage": 1})
        return params

    def parse_batch(
        self,
        request: BatchRequest,
        payload: object,
        *,
        status_code: int = 200,
        raw_response_ref: str | None = None,
        timing_ms: int | None = None,
        response_bytes: int | None = None,
    ) -> BatchResult:
        """Create an evidence-rich batch result from one decoded web response."""

        protection_status = _protection_status(status_code)
        if protection_status.blocks_collection:
            return BatchResult(
                source=self.name,
                requested_cursor=request.cursor,
                reported_cursor=request.cursor,
                protection_status=protection_status,
                raw_response_ref=raw_response_ref,
                timing_ms=timing_ms,
                response_bytes=response_bytes if response_bytes is not None else _response_size(payload),
                metadata={"shape_valid": False, "shape_error": f"http_{status_code}", "active_category_id": None},
            )

        cards, filters, kworks, posts, shape_error = _payload_parts(payload)
        active_category_id = _first_int(filters.get("activeCategoryId"), filters.get("categoryId"))
        source_total = _first_int(kworks.get("total"), posts.get("total"))
        source_total_found = _first_int(
            kworks.get("total_found"),
            kworks.get("totalFound"),
            posts.get("total_found"),
            posts.get("totalFound"),
            filters.get("kworksCount"),
        )
        metadata = {
            "shape_valid": shape_error is None,
            "shape_error": shape_error,
            "active_category_id": active_category_id,
        }
        return BatchResult(
            source=self.name,
            requested_cursor=request.cursor,
            reported_cursor=request.cursor,
            cards=cards,
            actual_item_count=len(cards),
            source_total=source_total,
            source_total_found=source_total_found,
            next_cursor=_next_cursor(request.cursor, cards) if shape_error is None else None,
            protection_status=protection_status,
            raw_response_ref=raw_response_ref,
            timing_ms=timing_ms,
            response_bytes=response_bytes if response_bytes is not None else _response_size(payload),
            metadata=metadata,
        )

    def validate_batch(
        self,
        request: BatchRequest,
        response: BatchResult,
        previous: BatchState | None,
    ) -> ContractVerdict:
        """Accept only a recognized, correctly scoped, novel web batch."""

        if response.protection_status.blocks_collection:
            return ContractVerdict(state=ContractState.BLOCKED, reason_codes=("protection_signal",))

        reasons: list[str] = []
        if request.source != self.name or response.source != self.name:
            reasons.append("source_mismatch")
        if response.requested_cursor != request.cursor:
            reasons.append("requested_cursor_mismatch")
        if request.cursor is not None and request.cursor.kind is not CursorKind.EXCLUDE_IDS:
            reasons.append("unsupported_cursor_kind")
        if response.reported_cursor != request.cursor:
            reasons.append("reported_cursor_mismatch")
        if response.metadata.get("shape_valid") is not True:
            reasons.append("card_shape_invalid")

        expected_category_id = _as_int(request.scope.get("category_id"))
        reported_category_id = _as_int(response.metadata.get("active_category_id"))
        if expected_category_id is not None and reported_category_id != expected_category_id:
            reasons.append("active_category_mismatch")

        expected_fingerprint = fingerprint_for_cards(response.cards)
        if response.fingerprint != expected_fingerprint:
            reasons.append("fingerprint_mismatch")
        if response.actual_item_count != len(response.cards):
            reasons.append("actual_item_count_mismatch")

        novelty = account_novelty(response.cards, seen_ids=(previous.seen_card_ids if previous else ()))
        if response.cards and novelty.new_unique == 0:
            reasons.append("zero_novelty")
        if previous and response.cards and previous.last_fingerprint == response.fingerprint:
            reasons.append("repeated_fingerprint")
        if response.cards and (response.next_cursor is None or response.next_cursor.kind is not CursorKind.EXCLUDE_IDS):
            reasons.append("next_cursor_missing")

        if reasons:
            return ContractVerdict(
                state=ContractState.CONTRACT_VIOLATION,
                reason_codes=tuple(dict.fromkeys(reasons)),
                novelty=novelty,
            )
        if not response.cards:
            return ContractVerdict(state=ContractState.EXHAUSTED, novelty=novelty)
        return ContractVerdict(state=ContractState.ACCEPTED, novelty=novelty)

    @staticmethod
    def normalize(raw_card: Mapping[str, Any]) -> Mapping[str, Any]:
        """Project the catalog fields used by durable listing views."""

        card = dict(raw_card)
        listing_id = _first_int(card.get("listing_id"), card.get("id"), card.get("PID"))
        if listing_id is not None:
            card["listing_key"] = str(listing_id)

        title = _first_text(card.get("title"), card.get("gtitle"), card.get("name"))
        if title is not None:
            card["title"] = title

        seller = _first_text(
            card.get("seller_key"),
            card.get("seller"),
            card.get("username"),
            card.get("userName"),
            card.get("user_name"),
        )
        if seller is not None:
            card["seller_key"] = seller

        seller_id = _first_int(card.get("seller_id"), card.get("user_id"), card.get("userId"))
        if seller_id is not None:
            card["seller_id"] = seller_id
        return card


class WebCatalogClient(Protocol):
    """The existing client surface used by the HTTP-facing source adapter."""

    async def get_web_catalog_filters(
        self,
        alias: str,
        *,
        page: int = 1,
        page_size: int = 10,
        filters: Mapping[str, object] | None = None,
        include_raw: bool = False,
        cookies: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        """Fetch a web catalog response with the raw payload included."""


@dataclass(slots=True)
class KworkWebCatalogAdapter:
    """Bridge the existing HTTP helper to the pure web source contract."""

    client: WebCatalogClient
    cookies: Mapping[str, str] | None = None
    default_page_size: int = 10
    source: WebCatalogSource = field(default_factory=WebCatalogSource)

    def __post_init__(self) -> None:
        if self.default_page_size < 1:
            raise ValueError("default_page_size must be positive")

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def capabilities(self) -> SourceCapabilities:
        return self.source.capabilities

    def build_request(
        self,
        *,
        alias: str,
        category_id: int,
        cursor: SourceCursor | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> BatchRequest:
        return self.source.build_request(alias=alias, category_id=category_id, cursor=cursor, filters=filters)

    def request_params(
        self,
        cursor: SourceCursor | None,
        *,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self.source.request_params(cursor, filters=filters)

    def parse_batch(self, request: BatchRequest, payload: object, **kwargs: Any) -> BatchResult:
        return self.source.parse_batch(request, payload, **kwargs)

    def validate_batch(
        self,
        request: BatchRequest,
        response: BatchResult,
        previous: BatchState | None,
    ) -> ContractVerdict:
        return self.source.validate_batch(request, response, previous)

    def normalize(self, raw_card: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.source.normalize(raw_card)

    async def fetch_batch(self, request: BatchRequest) -> BatchResult:
        """Fetch exactly one web batch and preserve the helper's response evidence."""

        alias = request.scope.get("alias")
        if not isinstance(alias, str) or not alias:
            raise ValueError("web catalog request requires a canonical alias")
        scope_filters = request.scope.get("filters")
        if scope_filters is not None and not isinstance(scope_filters, Mapping):
            raise ValueError("web catalog request filters must be a mapping")
        continuation_params = self.request_params(request.cursor, filters=scope_filters)
        page_size = request.requested_page_size or self.default_page_size
        helper_response = await self.client.get_web_catalog_filters(
            alias,
            page=1,
            page_size=page_size,
            filters=continuation_params,
            include_raw=True,
            cookies=dict(self.cookies) if self.cookies else None,
        )
        if not isinstance(helper_response, Mapping):
            raise TypeError("web catalog client must return a mapping")

        status_code = _first_int(helper_response.get("status_code")) or 200
        response_bytes = _first_int(helper_response.get("bytes"))
        if response_bytes is not None and response_bytes < 0:
            response_bytes = None
        raw_payload = helper_response.get("raw")
        result = self.source.parse_batch(
            request,
            raw_payload,
            status_code=status_code,
            raw_response_ref=(
                str(helper_response["raw_response_ref"])
                if helper_response.get("raw_response_ref") is not None
                else None
            ),
            response_bytes=response_bytes,
        )
        helper_params = helper_response.get("request_params")
        adapter_metadata = {
            "endpoint": helper_response.get("endpoint"),
            "url": helper_response.get("url"),
            "status_code": status_code,
            "content_type": helper_response.get("content_type"),
            "protection_status": helper_response.get("protection_status"),
            "retry_after": helper_response.get("retry_after"),
            "request_params": dict(helper_params) if isinstance(helper_params, Mapping) else {},
            "continuation_params": continuation_params,
        }
        metadata = dict(result.metadata)
        metadata["adapter"] = adapter_metadata
        # This value is intentionally private to the in-process worker.  It is
        # written to the artifact store before any event or API response is made.
        if raw_payload is not None:
            metadata["_raw_payload"] = raw_payload
        return replace(result, metadata=metadata)
