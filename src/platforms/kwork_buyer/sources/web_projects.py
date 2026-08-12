"""Pure normalization of authenticated Kwork web project state for Buyer Search."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..mapper import (
    WEB_PROJECT_SOURCE,
    BuyerMappedProject,
    BuyerProjectMappingError,
    BuyerProjectObservationContext,
    BuyerProjectSnapshot,
    map_buyer_project,
)


def normalize_web_project(
    payload: Mapping[str, Any],
    *,
    context: BuyerProjectObservationContext,
) -> BuyerMappedProject:
    """Normalize one authenticated web card/state entry without any network call."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    remote_project_id = _first_identifier(payload, "id", "want_id", "wantId", "project_id", "projectId")
    if remote_project_id is None:
        raise BuyerProjectMappingError("пустая карточка в веб-выдаче Kwork: нет ID проекта")
    buyer = _mapping(payload.get("user") or payload.get("buyer") or payload.get("userData"))
    alternative_ids = _all_identifiers(payload, "project_id", "projectId", "want_id", "wantId")
    return map_buyer_project(
        BuyerProjectSnapshot(
            source=WEB_PROJECT_SOURCE,
            remote_project_id=remote_project_id,
            title=_first_text(payload, "title", "name") or "",
            description=_first_text(payload, "description", "full_description", "text") or "",
            status=_first_value(payload, "status", "want_status", "wantStatus", "want_status_id"),
            category_id=_first_value(payload, "category_id", "categoryId"),
            parent_category_id=_first_value(payload, "parent_category_id", "parentCategoryId"),
            buyer_remote_user_id=_first_value(payload, "user_id", "userId", "buyer_id")
            or _first_value(buyer, "id", "user_id", "userId"),
            buyer_username=_first_text(payload, "username", "user_name") or _first_text(buyer, "username", "name", "login"),
            budget_min=_first_value(payload, "priceLimit", "price_limit", "price", "priceFrom", "price_from"),
            budget_max=_first_value(payload, "possiblePriceLimit", "possible_price_limit", "priceTo", "price_to"),
            offers=_first_value(payload, "kwork_count", "kworkCount", "offers", "offer_count"),
            views=_first_value(payload, "views_dirty", "views", "view_count"),
            orders=_first_value(payload, "orders", "order_count"),
            buyer_hired_percent=_first_present(
                _first_value(payload, "user_hired_percent", "buyer_hired_percent"),
                _first_value(buyer, "hired_percent", "hiredPercent"),
            ),
            buyer_projects_count=_first_present(
                _first_value(payload, "user_projects_count", "buyer_projects_count"),
                _first_value(buyer, "projects_count", "projectsCount"),
            ),
            buyer_active_projects_count=_first_present(
                _first_value(payload, "user_active_projects_count", "buyer_active_projects_count"),
                _first_value(buyer, "active_projects_count", "activeProjectsCount"),
            ),
            remote_updated_at=_first_value(payload, "date_update", "updated_at", "date_active", "dateActive"),
            canonical_url=_first_text(payload, "url", "project_url", "link", "canonical_url"),
            alternative_remote_ids=alternative_ids,
            alternative_urls=_all_text(payload, "url", "project_url", "link", "canonical_url"),
        ),
        context=context,
    )


def normalize_web_projects(
    payload: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    context: BuyerProjectObservationContext,
) -> tuple[BuyerMappedProject, ...]:
    """Normalize a web state page while preserving each source response position."""

    cards = extract_web_project_cards(payload)
    valid_cards = [
        (position, item)
        for position, item in enumerate(cards)
        if _first_identifier(item, "id", "want_id", "wantId", "project_id", "projectId") is not None
    ]
    if not valid_cards and cards:
        normalize_web_project(cards[0], context=context.with_response_position(0))
    return tuple(
        normalize_web_project(item, context=context.with_response_position(position))
        for position, item in valid_cards
    )


def extract_web_project_cards(payload: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Extract cards from common stateData envelopes without remote side effects."""

    if isinstance(payload, Mapping):
        for key in ("projects", "items", "data", "wants"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping))
            if isinstance(value, Mapping):
                nested = value.get("data") or value.get("items") or value.get("projects")
                if isinstance(nested, list):
                    return tuple(item for item in nested if isinstance(item, Mapping))
        pagination = payload.get("pagination")
        if isinstance(pagination, Mapping) and isinstance(pagination.get("data"), list):
            return tuple(item for item in pagination["data"] if isinstance(item, Mapping))
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


__all__ = ["extract_web_project_cards", "normalize_web_project", "normalize_web_projects"]
