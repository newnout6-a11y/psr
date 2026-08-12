"""Buyer Search taxonomy reads, local ingestion, and account-bound refresh.

The legacy snapshot endpoint persists an already-observed local capture. The
separate refresh router accepts a run/account scope and delegates remote reads
to an injected read-only capability; neither router exposes a Kwork mutation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.platforms.kwork_buyer.taxonomy import (
    BuyerTaxonomyConflictError,
    BuyerTaxonomyError,
    BuyerTaxonomyNotFoundError,
    BuyerTaxonomyService,
)
from src.platforms.kwork_buyer.taxonomy_refresh import (
    BuyerTaxonomyRefreshCapabilityError,
    BuyerTaxonomyRefreshController,
    BuyerTaxonomyRefreshDisabledError,
    BuyerTaxonomyRefreshError,
)


router = APIRouter(prefix="/api/kwork/buyer-search/taxonomy", tags=["kwork-buyer-taxonomy"])
refresh_router = APIRouter(
    prefix="/api/kwork/buyer-search/accounts/{account_registration_id}/taxonomy",
    tags=["kwork-buyer-taxonomy"],
)


class BuyerTaxonomySnapshotRequest(BaseModel):
    """Normalized immutable capture produced by a read-only taxonomy reader."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str | None = Field(default=None, min_length=1, max_length=200)
    source: str = Field(..., min_length=1, max_length=200)
    source_revision: str | None = Field(default=None, max_length=500)
    captured_at: str | None = Field(default=None, max_length=100)
    provenance: dict[str, Any] = Field(default_factory=dict)
    categories: list[dict[str, Any]] = Field(..., min_length=1, max_length=20_000)
    attributes: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    catalog_seeds: list[dict[str, Any] | str] = Field(default_factory=list, max_length=100_000)
    suggestions: list[dict[str, Any] | str] = Field(default_factory=list, max_length=100_000)
    terms: list[dict[str, Any] | str] = Field(default_factory=list, max_length=100_000)


class BuyerTaxonomyRefreshRequest(BaseModel):
    """Bounded, account/run-scoped catalog capture controls."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=200)
    rubric_ids: list[int] = Field(default_factory=list, max_length=20)
    category_ids: list[int] = Field(default_factory=list, max_length=80)
    rubric_limit: int = Field(default=20, ge=1, le=20)
    category_limit: int = Field(default=80, ge=1, le=80)
    include_attributes: bool = True
    include_filters: bool = True
    catalog_seed_limit: int = Field(default=80, ge=0, le=200)


def _service(app: Any) -> BuyerTaxonomyService:
    service = getattr(app.state, "buyer_taxonomy", None)
    if not isinstance(service, BuyerTaxonomyService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer taxonomy is unavailable")
    return service


def _refresh_controller(app: Any) -> BuyerTaxonomyRefreshController:
    controller = getattr(app.state, "buyer_taxonomy_refresh", None)
    if not isinstance(controller, BuyerTaxonomyRefreshController):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer taxonomy refresh is unavailable")
    return controller


async def _call(operation: Any) -> Any:
    try:
        return await operation
    except BuyerTaxonomyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerTaxonomyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BuyerTaxonomyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _call_refresh(operation: Any) -> Any:
    try:
        return await operation
    except (BuyerTaxonomyRefreshDisabledError, BuyerTaxonomyRefreshCapabilityError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BuyerTaxonomyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BuyerTaxonomyRefreshError, BuyerTaxonomyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/snapshots", status_code=status.HTTP_201_CREATED)
async def record_snapshot(payload: BuyerTaxonomySnapshotRequest, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).record_snapshot(payload.model_dump(exclude_none=True)))


@refresh_router.post("/refresh", status_code=status.HTTP_201_CREATED)
async def refresh_taxonomy(
    account_registration_id: str,
    payload: BuyerTaxonomyRefreshRequest,
    request: Request,
) -> dict[str, Any]:
    """Capture one bounded taxonomy snapshot through the requested live lease."""

    return await _call_refresh(
        _refresh_controller(request.app).refresh(
            account_registration_id=account_registration_id,
            **payload.model_dump(),
        )
    )


@router.get("/snapshots")
async def list_snapshots(
    request: Request,
    source: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> dict[str, Any]:
    items = await _call(_service(request.app).list_snapshots(source=source, limit=limit))
    return {"items": list(items)}


@router.get("/categories")
async def list_categories(
    request: Request,
    snapshot_id: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, max_length=200),
    parent_category_id: int | None = Query(default=None, ge=1),
    query: str | None = Query(default=None, min_length=1, max_length=500),
    after_category_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> dict[str, Any]:
    return await _call(
        _service(request.app).list_categories(
            snapshot_id=snapshot_id,
            source=source,
            parent_category_id=parent_category_id,
            query=query,
            after_category_id=after_category_id,
            limit=limit,
        )
    )


@router.get("/categories/{category_id}/generation-context")
async def get_category_generation_context(
    category_id: int,
    request: Request,
    snapshot_id: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, max_length=200),
    child_limit: int = Query(default=200, ge=1, le=1_000),
    term_limit: int = Query(default=500, ge=1, le=1_000),
) -> dict[str, Any]:
    return await _call(
        _service(request.app).get_category_context(
            category_id,
            snapshot_id=snapshot_id,
            source=source,
            child_limit=child_limit,
            term_limit=term_limit,
        )
    )


@router.get("/categories/{category_id}")
async def get_category(
    category_id: int,
    request: Request,
    snapshot_id: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    context = await _call(
        _service(request.app).get_category_context(
            category_id,
            snapshot_id=snapshot_id,
            source=source,
            child_limit=1,
            term_limit=1,
        )
    )
    return {"snapshot": context["snapshot"], "category": context["category"]}


__all__ = ["refresh_router", "router"]
