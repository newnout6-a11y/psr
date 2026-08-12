"""Pure normalization of Kwork mobile ``/projects`` cards for Buyer Search."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..mapper import (
    MOBILE_PROJECT_SOURCE,
    BuyerMappedProject,
    BuyerProjectMappingError,
    BuyerProjectObservationContext,
    BuyerProjectSnapshot,
    map_buyer_project,
)


def normalize_mobile_project(
    payload: Mapping[str, Any],
    *,
    context: BuyerProjectObservationContext,
) -> BuyerMappedProject:
    """Normalize one mobile project card without performing HTTP or storage I/O."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    remote_project_id = _first_identifier(payload, "id", "project_id", "projectId", "want_id", "wantId")
    if remote_project_id is None:
        raise BuyerProjectMappingError("пустая карточка в мобильной выдаче Kwork: нет ID проекта")
    user = _mapping(payload.get("user"))
    alternative_ids = _all_identifiers(payload, "project_id", "projectId", "want_id", "wantId")
    return map_buyer_project(
        BuyerProjectSnapshot(
            source=MOBILE_PROJECT_SOURCE,
            remote_project_id=remote_project_id,
            title=_first_text(payload, "title", "name") or "",
            description=_first_text(payload, "description", "text", "project_description") or "",
            status=_first_value(payload, "status", "want_status", "want_status_id"),
            category_id=_first_value(payload, "category_id", "categoryId"),
            parent_category_id=_first_value(payload, "parent_category_id", "parentCategoryId"),
            buyer_remote_user_id=_first_value(payload, "user_id", "userId") or _first_value(user, "id", "user_id"),
            buyer_username=_first_text(payload, "username", "user_name") or _first_text(user, "username", "name"),
            budget_min=_first_value(payload, "price", "price_from", "priceFrom", "price_limit"),
            budget_max=_first_value(payload, "possible_price_limit", "possiblePriceLimit", "price_to", "priceTo"),
            offers=_first_value(payload, "offers", "offer_count", "offers_count"),
            views=_first_value(payload, "views"),
            orders=_first_value(payload, "orders"),
            buyer_hired_percent=_first_present(
                _first_value(payload, "user_hired_percent", "buyer_hired_percent"),
                _first_value(user, "hired_percent"),
            ),
            buyer_projects_count=_first_present(
                _first_value(payload, "user_projects_count", "buyer_projects_count"),
                _first_value(user, "projects_count"),
            ),
            buyer_active_projects_count=_first_present(
                _first_value(payload, "user_active_projects_count", "buyer_active_projects_count"),
                _first_value(user, "active_projects_count"),
            ),
            remote_updated_at=_first_value(payload, "date_update", "updated_at", "date_active"),
            canonical_url=_first_text(payload, "url", "project_url", "link"),
            alternative_remote_ids=alternative_ids,
            alternative_urls=_all_text(payload, "url", "project_url", "link"),
        ),
        context=context,
    )


def normalize_mobile_projects(
    payload: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    context: BuyerProjectObservationContext,
) -> tuple[BuyerMappedProject, ...]:
    """Normalize a mobile response page while preserving response positions."""

    cards = extract_mobile_project_cards(payload)
    valid_cards = [
        (position, item)
        for position, item in enumerate(cards)
        if _first_identifier(item, "id", "project_id", "projectId", "want_id", "wantId") is not None
    ]
    if not valid_cards and cards:
        # Keep an entirely malformed envelope retryable instead of silently
        # committing an empty page to the durable run.
        normalize_mobile_project(cards[0], context=context.with_response_position(0))
    return tuple(
        normalize_mobile_project(item, context=context.with_response_position(position))
        for position, item in valid_cards
    )


def extract_mobile_project_cards(payload: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Extract card mappings from known mobile response envelopes without I/O."""

    if isinstance(payload, Mapping):
        for key in ("response", "projects", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping))
        return (payload,)
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes, bytearray)):
        return tuple(item for item in payload if isinstance(item, Mapping))
    raise TypeError("payload must be a mapping or iterable of mappings")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(payload, *keys)
    return str(value) if value is not None else None


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _first_identifier(payload: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(payload, *keys)
    return str(value).strip() if value is not None and str(value).strip() else None


def _all_identifiers(payload: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    return tuple(value for key in keys if (value := _first_identifier(payload, key)) is not None)


def _all_text(payload: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    return tuple(value for key in keys if (value := _first_text(payload, key)) is not None)


__all__ = ["extract_mobile_project_cards", "normalize_mobile_project", "normalize_mobile_projects"]
