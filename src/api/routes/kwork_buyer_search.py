"""REST and WebSocket control plane for durable Buyer Search runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.platforms.kwork_buyer.enrichment_controller import (
    BuyerAttachmentEnrichmentController,
    BuyerAttachmentEnrichmentControllerError,
)
from src.platforms.kwork_buyer.export import BuyerExportFormat
from src.platforms.kwork_buyer.service import (
    BuyerSearchRunNotFoundError,
    BuyerSearchService,
    BuyerSearchServiceError,
    BuyerSearchStateError,
)


router = APIRouter(prefix="/api/kwork/buyer-search", tags=["kwork-buyer-search"])
ws_router = APIRouter(tags=["kwork-buyer-search"])


class BuyerSearchRunCreateRequest(BaseModel):
    """Wire input for a new, durable Buyer Search query plan."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=240)
    mode: str
    brief: str | None = Field(default=None, max_length=8_000)
    category_scope: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    requested_workers: int = Field(default=1, ge=1, le=30)
    query_batch_size: int = Field(default=1, ge=1, le=10)
    target_unique_projects: int = Field(default=100, ge=1, le=1_000_000)
    enrichment_policy: dict[str, Any] = Field(default_factory=dict)
    scoring_profile_id: str | None = Field(default=None, max_length=200)
    queries: list[dict[str, Any] | str] = Field(default_factory=list, max_length=300)
    account_registration_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("name", "brief", "scoring_profile_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class BuyerSearchRunPatchRequest(BaseModel):
    """Narrow operator mutations, including explicit lifecycle commands."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=240)
    filters: dict[str, Any] | None = None
    requested_workers: int | None = Field(default=None, ge=1, le=30)
    state: str | None = None

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class BuyerSearchExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: BuyerExportFormat = BuyerExportFormat.ZIP
    filters: dict[str, Any] = Field(default_factory=dict)
    selected_project_ids: list[str] = Field(default_factory=list, max_length=10_000)
    include_attachments: bool = False
    include_raw: bool = False


class BuyerSearchBatchActionRequest(BaseModel):
    """Explicit, durable shortlist operations for a stable project-ID set."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., pattern="^(shortlist|unshortlist)$")
    project_ids: list[str] = Field(..., min_length=1, max_length=10_000)
    tags: list[str] | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2_000)
    selected_by: str = Field(default="api", min_length=1, max_length=200)


class BuyerSearchFinalScoreRequest(BaseModel):
    """Versioned, explicitly requested model-scoring pass."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str | None = Field(default=None, max_length=200)
    profile_version: str = Field(default="1", min_length=1, max_length=100)


class BuyerSearchQueryGenerateRequest(BaseModel):
    """Append a reviewable query generation batch to one existing run."""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
    brief: str | None = Field(default=None, max_length=8_000)
    category_scope: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    queries: list[dict[str, Any] | str] = Field(default_factory=list, max_length=300)
    requested_workers: int | None = Field(default=None, ge=1, le=30)
    query_batch_size: int | None = Field(default=None, ge=1, le=10)
    minimum_count: int | None = Field(default=None, ge=1, le=300)
    auto_approve: bool = False


class BuyerSearchCollisionRegenerateRequest(BuyerSearchQueryGenerateRequest):
    """Replacement candidates for one or more durable planner collisions."""

    collision_query_ids: list[str] = Field(default_factory=list, max_length=300)


class BuyerSearchQueryPlanCompactRequest(BaseModel):
    """Archive redundant phrases while retaining source coverage."""

    model_config = ConfigDict(extra="forbid")

    maximum_queries: int = Field(default=30, ge=1, le=300)


class BuyerSearchQueryPatchRequest(BaseModel):
    """Operator-controlled query approval, edit, and disable payload."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=1_000)
    filters: dict[str, Any] | None = None
    category_id: int | None = Field(default=None, ge=1)
    category_path: list[int] | None = None
    rationale: str | None = Field(default=None, max_length=2_000)
    origin: str | None = Field(default=None, max_length=100)
    parent_query_id: str | None = Field(default=None, max_length=200)
    priority: int | None = Field(default=None, ge=-100_000, le=100_000)
    predicted_total: int | None = Field(default=None, ge=0)
    approved: bool | None = None
    enabled: bool | None = None
    state: str | None = Field(default=None, max_length=100)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class BuyerSearchAttachmentRequest(BaseModel):
    """Metadata from an account-bound enrichment reader, never upload bytes."""

    model_config = ConfigDict(extra="forbid")

    attachment_id: str | None = Field(default=None, max_length=200)
    source_observation_id: str | None = Field(default=None, max_length=200)
    remote_url: str | None = Field(default=None, max_length=4_000)
    filename: str | None = Field(default=None, max_length=1_000)
    content_type: str | None = Field(default=None, max_length=300)
    detected_type: str | None = Field(default=None, max_length=100)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, max_length=200)
    state: str = Field(default="discovered", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BuyerSearchAttachmentDerivativeRequest(BaseModel):
    """Bounded parser/OCR/vision result for a previously stored attachment."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=100)
    parser_name: str | None = Field(default=None, max_length=200)
    parser_version: str | None = Field(default=None, max_length=200)
    content_hash: str | None = Field(default=None, max_length=200)
    extracted_text: str | None = Field(default=None, max_length=200_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_count: int | None = Field(default=None, ge=0)
    state: str = Field(default="parsed", max_length=100)


class BuyerSearchEnrichmentRequest(BaseModel):
    """Explicit account selection for a read-only attachment enrichment pass."""

    model_config = ConfigDict(extra="forbid")

    account_registration_id: str = Field(..., min_length=1, max_length=200)


class BuyerSearchWorkspaceBuilderState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=240)
    brief: str = Field(default="", max_length=8_000)
    exact_queries: list[str] = Field(default_factory=list, max_length=300)
    taxonomy_selections: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    min_budget: str = Field(default="", max_length=40)
    max_budget: str = Field(default="", max_length=40)
    max_offers: str = Field(default="", max_length=40)
    min_buyer_hired_percent: str = Field(default="", max_length=40)
    max_age_hours: str = Field(default="", max_length=40)
    target_projects: int = Field(default=100, ge=1, le=1_000_000)
    workers: int = Field(default=2, ge=1, le=30)
    query_batch_size: int = Field(default=1, ge=1, le=10)
    account_registration_ids: list[str] = Field(default_factory=list, max_length=30)
    enrichment_enabled: bool = False
    scoring_profile_id: str = Field(default="", max_length=200)
    advanced_open: bool = False
    preview_queries: list[dict[str, Any]] = Field(default_factory=list, max_length=300)


class BuyerSearchWorkspaceViewState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_section: str = Field(default="setup", pattern="^(setup|projects|shortlist|outreach|monitoring)$")
    selected_run_id: str | None = Field(default=None, max_length=200)
    selected_project_id: str | None = Field(default=None, max_length=200)
    project_filters: dict[str, Any] = Field(default_factory=dict)
    project_sort: str = Field(default="score_desc", max_length=100)
    cursor: str | None = Field(default=None, max_length=4_000)
    cursor_history: list[str] = Field(default_factory=list, max_length=1_000)
    selected_project_ids: list[str] = Field(default_factory=list, max_length=10_000)
    shortlist_tags: str = Field(default="", max_length=2_000)
    shortlist_note: str = Field(default="", max_length=2_000)
    include_attachments: bool = False
    inspector_tab: str = Field(default="details", pattern="^(details|proposal|conversation)$")
    inspector_open: bool = True
    runs_pane_width: int = Field(default=272, ge=220, le=520)
    inspector_width: int = Field(default=390, ge=320, le=720)


class BuyerSearchWorkspaceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    builder: BuyerSearchWorkspaceBuilderState = Field(default_factory=BuyerSearchWorkspaceBuilderState)
    view: BuyerSearchWorkspaceViewState = Field(default_factory=BuyerSearchWorkspaceViewState)


class BuyerSearchWorkspacePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    state: BuyerSearchWorkspaceState


def _service(app: Any) -> BuyerSearchService:
    service = getattr(app.state, "buyer_search", None)
    if not isinstance(service, BuyerSearchService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer Search is unavailable")
    return service


@router.get("/workspace")
async def get_workspace(request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).get_workspace_state())


@router.put("/workspace")
async def put_workspace(payload: BuyerSearchWorkspacePutRequest, request: Request) -> dict[str, Any]:
    return await _call(
        _service(request.app).put_workspace_state(
            payload.state.model_dump(),
            schema_version=payload.schema_version,
        )
    )


def _enrichment_controller(app: Any) -> BuyerAttachmentEnrichmentController:
    controller = getattr(app.state, "buyer_enrichment", None)
    if not isinstance(controller, BuyerAttachmentEnrichmentController):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer attachment enrichment is unavailable")
    return controller


async def _call(operation: Any) -> Any:
    try:
        return await operation
    except BuyerSearchRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerSearchStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BuyerSearchServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _call_enrichment(operation: Any) -> Any:
    try:
        return await operation
    except BuyerAttachmentEnrichmentControllerError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(payload: BuyerSearchRunCreateRequest, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).create_run(payload.model_dump()))


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _call(_service(request.app).list_runs(limit=limit, cursor=cursor))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).get_run(run_id))


@router.patch("/runs/{run_id}")
async def patch_run(run_id: str, payload: BuyerSearchRunPatchRequest, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).update_run(run_id, payload.changes()))


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, request: Request) -> dict[str, Any]:
    """Remove a stopped or terminal Buyer Search run from the workspace."""

    return await _call(_service(request.app).delete_run(run_id))


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).update_run(run_id, {"state": "paused"}))


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).update_run(run_id, {"state": "running"}))


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).update_run(run_id, {"state": "stopped"}))


@router.post("/runs/{run_id}/restart")
async def restart_run(run_id: str, request: Request) -> dict[str, Any]:
    """Fence prior task attempts and return the durable run to ready state."""

    return await _call(_service(request.app).restart_run(run_id))


@router.post("/runs/{run_id}/continue")
async def continue_to_target(run_id: str, request: Request) -> dict[str, Any]:
    """Add fresh queries to a terminal run that has not reached its target."""

    return await _call(_service(request.app).continue_to_target(run_id))


@router.get("/runs/{run_id}/fleet")
async def get_fleet(run_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).get_fleet(run_id))


@router.get("/runs/{run_id}/queries")
async def list_queries(
    run_id: str,
    request: Request,
    state_filter: str | None = Query(default=None, alias="state"),
    include_disabled: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=1000),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _call(
        _service(request.app).list_queries(
            run_id,
            state=state_filter,
            include_disabled=include_disabled,
            limit=limit,
            cursor=cursor,
        )
    )


@router.post("/runs/{run_id}/queries/generate", status_code=status.HTTP_201_CREATED)
async def generate_queries(
    run_id: str,
    payload: BuyerSearchQueryGenerateRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(_service(request.app).generate_queries(run_id, payload.model_dump(exclude_none=True)))


@router.post("/runs/{run_id}/queries/distribute")
async def distribute_queries(
    run_id: str,
    request: Request,
    query_ids: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    return await _call(_service(request.app).distribute_queries(run_id, query_ids=query_ids))


@router.post("/runs/{run_id}/queries/compact")
async def compact_query_plan(
    run_id: str,
    payload: BuyerSearchQueryPlanCompactRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _service(request.app).compact_query_plan(run_id, maximum_queries=payload.maximum_queries)
    )


@router.get("/runs/{run_id}/queries/collisions")
async def get_query_collisions(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2_000),
) -> dict[str, Any]:
    return await _call(_service(request.app).get_query_collisions(run_id, after_seq=after_seq, limit=limit))


@router.post("/runs/{run_id}/queries/regenerate-collisions", status_code=status.HTTP_201_CREATED)
async def regenerate_query_collisions(
    run_id: str,
    payload: BuyerSearchCollisionRegenerateRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _service(request.app).regenerate_query_collisions(
            run_id,
            payload.model_dump(exclude_none=True),
        )
    )


@router.get("/runs/{run_id}/query-bundles")
async def get_query_bundles(
    run_id: str,
    request: Request,
    include_disabled: bool = Query(default=False),
) -> dict[str, Any]:
    return await _call(_service(request.app).get_query_bundles(run_id, include_disabled=include_disabled))


@router.patch("/runs/{run_id}/queries/{query_id}")
async def patch_query(
    run_id: str,
    query_id: str,
    payload: BuyerSearchQueryPatchRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(_service(request.app).update_query(run_id, query_id, payload.changes()))


@router.get("/runs/{run_id}/projects")
async def list_projects(
    run_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    sort: str | None = Query(default=None),
    text: str | None = Query(default=None, max_length=1_000),
    shortlist: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
    max_score: float | None = Query(default=None),
    min_budget: float | None = Query(default=None, ge=0),
    max_budget: float | None = Query(default=None, ge=0),
    min_offers: int | None = Query(default=None, ge=0),
    max_offers: int | None = Query(default=None, ge=0),
    min_views: int | None = Query(default=None, ge=0),
    max_views: int | None = Query(default=None, ge=0),
    min_buyer_hired_percent: float | None = Query(default=None, ge=0, le=100),
    min_age_seconds: int | None = Query(default=None, ge=0),
    max_age_seconds: int | None = Query(default=None, ge=0),
    category_id: int | None = Query(default=None, ge=1),
    has_attachments: bool | None = Query(default=None),
    attachment_parse_state: Annotated[list[str] | None, Query()] = None,
    attachment_type: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    proposal_state: Annotated[list[str] | None, Query()] = None,
    conversation_state: Annotated[list[str] | None, Query()] = None,
    unseen: bool | None = Query(default=None),
) -> dict[str, Any]:
    filters = {
        key: value
        for key, value in {
            "text": text,
            "shortlist_state": shortlist,
            "min_score": min_score,
            "max_score": max_score,
            "min_budget": min_budget,
            "max_budget": max_budget,
            "min_offers": min_offers,
            "max_offers": max_offers,
            "min_views": min_views,
            "max_views": max_views,
            "min_buyer_hired_percent": min_buyer_hired_percent,
            "min_age_seconds": min_age_seconds,
            "max_age_seconds": max_age_seconds,
            "category_id": category_id,
            "has_attachments": has_attachments,
            "attachment_parse_state": attachment_parse_state,
            "attachment_type": attachment_type,
            "tag": tag,
            "proposal_state": proposal_state,
            "conversation_state": conversation_state,
            "unseen": unseen,
        }.items()
        if value is not None
    }
    return await _call(_service(request.app).list_projects(run_id, filters=filters, sort=sort, cursor=cursor, limit=limit))


@router.get("/runs/{run_id}/facets")
async def get_facets(run_id: str, request: Request, shortlist: str | None = Query(default=None)) -> dict[str, Any]:
    filters = {"shortlist_state": shortlist} if shortlist is not None else {}
    return await _call(_service(request.app).get_facets(run_id, filters=filters))


@router.get("/runs/{run_id}/projects/{project_id}")
async def get_project(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).get_project(run_id, project_id))


@router.post("/runs/{run_id}/projects/batch-action")
async def batch_project_action(
    run_id: str,
    payload: BuyerSearchBatchActionRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _service(request.app).batch_project_action(
            run_id,
            action=payload.action,
            project_ids=payload.project_ids,
            tags=payload.tags,
            note=payload.note,
            selected_by=payload.selected_by,
        )
    )


@router.post("/runs/{run_id}/projects/{project_id}/rescore")
async def rescore_project(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
    return await _call(_service(request.app).rescore_project(run_id, project_id))


@router.post("/runs/{run_id}/projects/{project_id}/score-final")
async def score_project_final(
    run_id: str,
    project_id: str,
    payload: BuyerSearchFinalScoreRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _service(request.app).final_score_project(
            run_id,
            project_id,
            profile_id=payload.profile_id,
            profile_version=payload.profile_version,
        )
    )


@router.post("/runs/{run_id}/projects/{project_id}/enrich")
async def enrich_project(
    run_id: str,
    project_id: str,
    payload: BuyerSearchEnrichmentRequest,
    request: Request,
) -> dict[str, Any]:
    """Run a feature-gated, account/VPNTE-bound attachment-only enrichment pass."""

    return await _call_enrichment(
        _enrichment_controller(request.app).enrich_project(
            run_id=run_id,
            project_id=project_id,
            account_registration_id=payload.account_registration_id,
        )
    )


@router.post("/runs/{run_id}/projects/{project_id}/attachments", status_code=status.HTTP_201_CREATED)
async def record_attachment(
    run_id: str,
    project_id: str,
    payload: BuyerSearchAttachmentRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(_service(request.app).record_attachment(run_id, project_id, payload.model_dump(exclude_none=True)))


@router.post("/runs/{run_id}/projects/{project_id}/attachments/{attachment_id}/derivatives", status_code=status.HTTP_201_CREATED)
async def record_attachment_derivative(
    run_id: str,
    project_id: str,
    attachment_id: str,
    payload: BuyerSearchAttachmentDerivativeRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _service(request.app).record_attachment_derivative(
            run_id,
            project_id,
            attachment_id,
            payload.model_dump(exclude_none=True),
        )
    )


@router.get("/runs/{run_id}/events")
async def replay_events(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    events = await _call(_service(request.app).replay_events(run_id, after_seq=after_seq, limit=limit))
    return {"items": events, "next_after_seq": events[-1].get("seq", after_seq) if events else after_seq}


@router.post("/runs/{run_id}/exports", status_code=status.HTTP_201_CREATED)
async def create_export(run_id: str, payload: BuyerSearchExportRequest, request: Request) -> dict[str, Any]:
    return await _call(
        _service(request.app).create_export(
            run_id,
            format=payload.format,
            filters=payload.filters,
            selected_project_ids=payload.selected_project_ids,
            include_attachments=payload.include_attachments,
            include_raw=payload.include_raw,
        )
    )


@router.get("/runs/{run_id}/exports/{export_id}")
async def get_export(run_id: str, export_id: str, request: Request) -> dict[str, Any]:
    service = _service(request.app)
    return await _call(service.get_export(run_id, export_id))


@router.get("/runs/{run_id}/exports/{export_id}/download")
async def download_export(run_id: str, export_id: str, request: Request) -> FileResponse:
    service = _service(request.app)
    exported = await _call(service.get_export(run_id, export_id))
    path = service.export_path(export_id)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer Search export not found")
    return FileResponse(Path(path), filename=str(exported.get("filename") or path.name))


@ws_router.websocket("/ws/kwork-buyer-search/runs/{run_id}")
async def stream_run_events(websocket: WebSocket, run_id: str, after_seq: int = 0) -> None:
    """Replay durable events first, then poll the append-only event sequence.

    The poll is read-only and protects correctness across process restarts; the
    desktop can reconcile the authoritative list/detail snapshot at any time.
    """

    await websocket.accept()
    try:
        service = _service(websocket.app)
        run = await service.get_run(run_id)
        sequence = max(0, after_seq)
        await websocket.send_json({"type": "snapshot", "run": run, "after_seq": sequence})
        while True:
            events = await service.replay_events(run_id, after_seq=sequence, limit=1_000)
            for event in events:
                sequence = max(sequence, int(event.get("seq") or sequence))
                await websocket.send_json({"type": "event", "event": event})
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=0.75)
            except TimeoutError:
                continue
            if message.strip().casefold() in {"close", "disconnect"}:
                return
    except WebSocketDisconnect:
        return
    except BuyerSearchRunNotFoundError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except Exception:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)


__all__ = ["router", "ws_router"]
