"""Mobile first-page source adapter without unproven continuation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..contracts import (
    MOBILE_FIRST_PAGE_CAPABILITIES,
    BatchRequest,
    BatchResult,
    BatchState,
    ContractVerdict,
    CursorKind,
    ProtectionStatus,
    SourceCapabilities,
    SourceCursor,
    validate_mobile_page,
)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MobileKworksClient(Protocol):
    """Subset of the existing market client used for the mobile first page."""

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
    ) -> Mapping[str, Any]:
        """Return a mobile catalog response."""


@dataclass(slots=True)
class KworkMobileKworksAdapter:
    """Adapt the current mobile helper while forbidding unvalidated pagination."""

    client: MobileKworksClient
    name: str = "mobile_kworks"
    capabilities: SourceCapabilities = MOBILE_FIRST_PAGE_CAPABILITIES

    def build_request(
        self,
        *,
        category_id: int,
        classifier_id: int | None = None,
        cursor: SourceCursor | None = None,
    ) -> BatchRequest:
        if category_id <= 0:
            raise ValueError("category_id must be positive")
        if classifier_id is not None and classifier_id <= 0:
            raise ValueError("classifier_id must be positive")
        if cursor is not None and (cursor.kind is not CursorKind.PAGE or cursor.page != 1):
            raise ValueError("mobile source supports only page 1")
        return BatchRequest(
            source=self.name,
            shard_key=f"category:{category_id}:classifier:{classifier_id or 'root'}",
            cursor=SourceCursor.page_cursor(1),
            scope={"category_id": category_id, "classifier_id": classifier_id},
        )

    async def fetch_batch(self, request: BatchRequest) -> BatchResult:
        """Fetch page one and preserve the response's reported page separately."""

        if request.source != self.name:
            raise ValueError("mobile request source mismatch")
        if request.cursor is None or request.cursor.kind is not CursorKind.PAGE or request.cursor.page != 1:
            raise ValueError("mobile source supports only page 1")
        category_id = _as_int(request.scope.get("category_id"))
        classifier_id = _as_int(request.scope.get("classifier_id"))
        if category_id is None or category_id <= 0:
            raise ValueError("mobile request requires category_id")

        catalog = await self.client.get_kworks(
            category_id=category_id,
            classifier_id=classifier_id,
            page=1,
        )
        if not isinstance(catalog, Mapping):
            raise TypeError("mobile client must return a mapping")

        raw_cards = catalog.get("kworks")
        shape_valid = isinstance(raw_cards, Sequence) and not isinstance(raw_cards, (str, bytes, bytearray))
        if shape_valid and all(isinstance(card, Mapping) for card in raw_cards):
            cards = tuple(raw_cards)
            shape_error: str | None = None
        elif shape_valid:
            cards = ()
            shape_error = "kworks_not_mapping_list"
            shape_valid = False
        else:
            cards = ()
            shape_error = "kworks_missing"

        paging = catalog.get("paging")
        reported_page = _as_int(paging.get("page")) if isinstance(paging, Mapping) else None
        reported_cursor = SourceCursor.page_cursor(reported_page) if reported_page and reported_page > 0 else None
        request_params = catalog.get("_request_params")
        return BatchResult(
            source=self.name,
            requested_cursor=request.cursor,
            reported_cursor=reported_cursor,
            cards=cards,
            actual_item_count=len(cards),
            source_total=_as_int(catalog.get("kworks_count") or catalog.get("count")),
            source_total_found=(
                _as_int(paging.get("total")) if isinstance(paging, Mapping) else _as_int(catalog.get("total_found"))
            ),
            next_cursor=None,
            protection_status=ProtectionStatus.OK,
            metadata={
                "shape_valid": shape_valid,
                "shape_error": shape_error,
                "reported_page": reported_page,
                "request_params": dict(request_params) if isinstance(request_params, Mapping) else {},
                # The worker writes this before committing observations so the
                # bounded page can be replayed from durable evidence.
                "_raw_payload": dict(catalog),
            },
        )

    def validate_batch(
        self,
        request: BatchRequest,
        response: BatchResult,
        previous: BatchState | None,
    ) -> ContractVerdict:
        return validate_mobile_page(request, response, previous, capabilities=self.capabilities)

    @staticmethod
    def normalize(raw_card: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(raw_card)
