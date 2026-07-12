"""REST and WebSocket control plane for durable Kwork market jobs.

The handlers deliberately keep orchestration out of the HTTP layer.  The
``MarketScanCoordinator`` stored on ``app.state.market_jobs`` owns every state
transition, while this module only validates wire payloads, maps domain errors,
and exposes replayable event delivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.handoff import MarketRecommendationHandoffService
from src.platforms.kwork_listing import KworkWebListingClient
from src.platforms.kwork import get_kwork_service
from src.platforms.kwork_supply.models import (
    JobState,
    MarketJobCreate,
    MarketScope,
    NetworkPolicy,
    OperationState,
    SourcePolicy,
    WorkerCommandKind,
)
from src.platforms.kwork_supply.metrics import market_metrics_to_wire, project_market_metrics
from src.platforms.kwork_market_supply import MarketAssistant, MarketAssistantStore
from src.platforms.kwork_supply.repository import (
    MarketCommitConflictError,
    MarketJobNotFoundError,
    MarketJobRepositoryError,
    MarketJobRevisionConflictError,
    MarketOperationLeaseError,
    MarketOperationNotFoundError,
)


router = APIRouter(tags=["kwork-market-jobs"])


# A WebSocket client gets a compact durable replay. Larger backlogs are
# handled as a snapshot resync rather than silently truncating the history
# between the replay query and the live subscription.
_STREAM_REPLAY_LIMIT = 1_000

_T = TypeVar("_T")


class MarketScopeRequest(BaseModel):
    """Serializable scope that starts a durable market job."""

    model_config = ConfigDict(extra="forbid")

    category_id: int = Field(..., ge=1)
    category_name: str = Field(default="", max_length=500)
    classifier_id: int | None = Field(default=None, ge=1)
    classifier_name: str = Field(default="", max_length=500)
    canonical_alias: str | None = Field(default=None, min_length=1, max_length=240)
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_alias")
    @classmethod
    def normalize_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        alias = value.strip()
        if not alias:
            raise ValueError("canonical_alias cannot be blank")
        return alias

    def to_domain(self) -> MarketScope:
        return MarketScope(
            category_id=self.category_id,
            category_name=self.category_name,
            classifier_id=self.classifier_id,
            classifier_name=self.classifier_name,
            canonical_alias=self.canonical_alias,
            filters=self.filters,
        )


class MarketJobCreateRequest(BaseModel):
    """Request accepted immediately before workers begin asynchronous work."""

    model_config = ConfigDict(extra="forbid")

    scope: MarketScopeRequest
    profile: str = Field(default="working", min_length=1, max_length=100)
    target_unique_cards: int = Field(default=60, ge=1, le=10_000)
    desired_workers: int = Field(default=2, ge=1, le=10)
    network_policy: NetworkPolicy = NetworkPolicy.PREFER_VPNTE
    source_policy: SourcePolicy = SourcePolicy.VALIDATED_ONLY
    include_ai: bool = True
    request_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)

    @field_validator("profile")
    @classmethod
    def normalize_profile(cls, value: str) -> str:
        profile = value.strip()
        if not profile:
            raise ValueError("profile cannot be blank")
        return profile

    def to_domain(self) -> MarketJobCreate:
        return MarketJobCreate(
            scope=self.scope.to_domain(),
            profile=self.profile.strip(),
            target_unique_cards=self.target_unique_cards,
            desired_workers=self.desired_workers,
            network_policy=self.network_policy,
            source_policy=self.source_policy,
            include_ai=self.include_ai,
            request_budget=self.request_budget,
            time_budget_seconds=self.time_budget_seconds,
        )


class MarketJobAcceptedResponse(BaseModel):
    """Small acknowledgement returned before any network collection occurs."""

    job_id: str
    state: str
    phase: str
    revision: int
    status_url: str
    events_url: str
    initial_operation: dict[str, Any]


class MarketJobPatchRequest(BaseModel):
    """Mutable execution controls; omitted values are left untouched."""

    model_config = ConfigDict(extra="forbid")

    desired_workers: int | None = Field(default=None, ge=1, le=10)
    target_unique_cards: int | None = Field(default=None, ge=1, le=10_000)
    profile: str | None = Field(default=None, min_length=1, max_length=100)
    request_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=1)

    def has_changes(self) -> bool:
        return bool(
            self.model_fields_set
            - {
                "expected_revision",
            }
        )


class JobRevisionRequest(BaseModel):
    """Optional optimistic-concurrency revision for a state transition."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


class StopJobRequest(JobRevisionRequest):
    force: bool = False


class WorkerPoolRequest(JobRevisionRequest):
    desired_workers: int = Field(..., ge=0, le=10)


class WorkerCommandRequest(BaseModel):
    """Worker-command payload kept extensible for transport-specific knobs."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] = Field(default_factory=dict)


class MarketAssistantQuestionRequest(BaseModel):
    """Question scoped to the durable evidence of one market collection job."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=8_000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message cannot be blank")
        return message


class MarketRecommendationCreateRequest(BaseModel):
    """One Terra-approved offer proposed for a completed market job."""

    model_config = ConfigDict(extra="forbid")

    category_id: int = Field(..., ge=1)
    classifier_id: int | None = Field(default=None, ge=1)
    service_summary: str = Field(..., min_length=1, max_length=2_000)
    price: int = Field(..., ge=1, le=150_000)
    work_time: int = Field(..., ge=1, le=365)
    source_cluster_id: str | None = Field(default=None, max_length=256)
    terra_result: dict[str, Any]


class MarketRecommendationTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


class MarketHandoffManifestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lang: str = Field(default="ru", min_length=2, max_length=16)


class MarketHandoffSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_hash: str = Field(..., min_length=8, max_length=128)
    selection: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class MarketHandoffDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_options: dict[str, Any] = Field(default_factory=dict)


class MarketPageResponse(BaseModel):
    """Uniform pagination envelope for durable operational views."""

    items: list[dict[str, Any]]
    limit: int
    cursor: str | int | None = None
    next_cursor: str | int | None = None


def _market_jobs_from_app(app: Any) -> MarketScanCoordinator:
    coordinator = getattr(app.state, "market_jobs", None)
    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kwork market job runtime is not initialized",
        )
    return coordinator


def _coordinator(request: Request) -> MarketScanCoordinator:
    """Read the process-local coordinator without constructing work per request."""

    return _market_jobs_from_app(request.app)


async def _load_handoff_manifest(
    category_id: int,
    classifier_id: int | None,
    selection: dict[str, Any],
    lang: str,
) -> dict[str, Any]:
    """Read a current Kwork form snapshot for a durable handoff."""

    cookies: dict[str, str] = {}
    try:
        cookies = await get_kwork_service()._fetch_session_hub_cookies()
    except Exception:
        # The listing client returns a structured login/protection response when
        # the anonymous form cannot be read, which is more useful than hiding it.
        pass
    client = KworkWebListingClient(cookies=cookies)
    return await client.build_attribute_manifest(
        category_id,
        selection=selection,
        classifier_id=classifier_id,
        lang=lang,
    )


def _handoff_service(request: Request) -> MarketRecommendationHandoffService:
    return MarketRecommendationHandoffService(_coordinator(request).repository, _load_handoff_manifest)


async def _call(operation: Awaitable[_T]) -> _T:
    """Map durable control-plane failures to stable HTTP errors."""

    try:
        return await operation
    except (MarketJobNotFoundError, MarketOperationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MarketJobRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (MarketCommitConflictError, MarketOperationLeaseError, MarketJobRepositoryError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _require_job(coordinator: MarketScanCoordinator, job_id: str) -> dict[str, Any]:
    job = await _call(coordinator.repository.get_job(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"market job {job_id!r} was not found")
    return job


async def _require_recommendation(
    coordinator: MarketScanCoordinator,
    job_id: str,
    recommendation_id: str,
) -> dict[str, Any]:
    recommendation = await _call(coordinator.repository.get_recommendation(recommendation_id))
    if recommendation is None or recommendation["job_id"] != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"market recommendation {recommendation_id!r} was not found")
    return recommendation


async def _require_handoff(
    coordinator: MarketScanCoordinator,
    job_id: str,
    handoff_id: str,
) -> dict[str, Any]:
    handoff = await _call(coordinator.repository.get_draft_handoff(handoff_id))
    if handoff is None or handoff["job_id"] != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"market draft handoff {handoff_id!r} was not found")
    return handoff


def _next_cursor(items: list[dict[str, Any]], key: str) -> str | int | None:
    if not items:
        return None
    value = items[-1].get(key)
    return value if isinstance(value, (str, int)) else None


async def _market_job_assistant_snapshot(
    coordinator: MarketScanCoordinator,
    job_id: str,
) -> dict[str, Any]:
    """Build an assistant context from the durable semantic job result."""

    job = await _require_job(coordinator, job_id)
    checkpoints, semantic = await asyncio.gather(
        coordinator.repository.list_checkpoints(job_id, limit=1),
        coordinator.repository.get_semantic_analysis(job_id),
    )
    metric_inputs = await coordinator.repository.list_listing_metrics_inputs(job_id)
    aggregate_total = (job.get("counters") or {}).get("aggregate_scope_total")
    metrics = market_metrics_to_wire(project_market_metrics(metric_inputs, aggregate_scope_total=aggregate_total))
    latest_checkpoint = checkpoints[0] if checkpoints else None
    stored_metrics = latest_checkpoint.get("metrics") if isinstance(latest_checkpoint, Mapping) else {}
    raw_listings: list[dict[str, Any]] = []
    seen_listing_ids: set[int] = set()
    for group in semantic.get("dossier", {}).get("groups", []):
        if not isinstance(group, Mapping):
            continue
        for representative in group.get("representatives", []):
            if not isinstance(representative, Mapping):
                continue
            listing_id = representative.get("listing_id")
            if not isinstance(listing_id, int) or listing_id in seen_listing_ids:
                continue
            seen_listing_ids.add(listing_id)
            raw_listings.append(
                {
                    "id": listing_id,
                    "gtitle": representative.get("title"),
                    "price": representative.get("price"),
                    "url": representative.get("url"),
                    "description": representative.get("description"),
                    "seller": representative.get("seller_key"),
                    "evidence_id": representative.get("evidence_id"),
                    "bounded_representative": True,
                }
            )
    return {
        "source": "psr.durable_market_job",
        "assistant_mode": "durable_semantic_dossier",
        "job_id": job_id,
        "scope": dict(job.get("scope") or {}),
        "coverage": {
            "state": job["state"],
            "phase": job["phase"],
            "target_unique_cards": job["target_unique_cards"],
            "observed_unique_cards": (job.get("counters") or {}).get("unique_cards", 0),
            "raw_listing_count": (job.get("counters") or {}).get("unique_cards", 0),
            "semantic_cluster_count": semantic.get("cluster_count", 0),
            "bounded_representative_count": len(raw_listings),
        },
        "slices": [],
        "sample": {"counters": job.get("counters") or {}, "metrics": metrics},
        "analysis": dict(stored_metrics) if isinstance(stored_metrics, Mapping) else {},
        "semantic_dossier": semantic.get("dossier", {}),
        "raw_listings": raw_listings,
    }


@router.post(
    "/api/kwork/market/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MarketJobAcceptedResponse,
)
async def create_market_job(request: Request, payload: MarketJobCreateRequest) -> MarketJobAcceptedResponse:
    """Persist a job and return before the supervisor performs network work."""

    coordinator = _coordinator(request)
    try:
        create = payload.to_domain()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    created = await _call(coordinator.create_job(create))
    job = created["job"]
    return MarketJobAcceptedResponse(
        job_id=job["job_id"],
        state=job["state"],
        phase=job["phase"],
        revision=job["revision"],
        status_url=created["status_url"],
        events_url=created["events_url"],
        initial_operation=created["initial_operation"],
    )


@router.get("/api/kwork/market/jobs", response_model=MarketPageResponse)
async def list_market_jobs(
    request: Request,
    state: Annotated[list[JobState] | None, Query()] = None,
    limit: int = Query(default=100, ge=1, le=1_000),
    offset: int = Query(default=0, ge=0),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    items = await _call(coordinator.repository.list_jobs(states=state, limit=limit, offset=offset))
    return MarketPageResponse(
        items=items,
        limit=limit,
        cursor=offset,
        next_cursor=offset + len(items) if len(items) == limit else None,
    )


@router.get("/api/kwork/market/jobs/{job_id}")
async def get_market_job(request: Request, job_id: str) -> dict[str, Any]:
    return await _call(_coordinator(request).snapshot(job_id))


@router.patch("/api/kwork/market/jobs/{job_id}")
async def update_market_job(
    request: Request,
    job_id: str,
    payload: MarketJobPatchRequest,
) -> dict[str, Any]:
    if not payload.has_changes():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="at least one mutable field is required")

    kwargs: dict[str, Any] = {"expected_revision": payload.expected_revision}
    fields = payload.model_fields_set
    if "desired_workers" in fields:
        kwargs["desired_workers"] = payload.desired_workers
    if "target_unique_cards" in fields:
        kwargs["target_unique_cards"] = payload.target_unique_cards
    if "profile" in fields:
        kwargs["profile"] = payload.profile.strip() if payload.profile is not None else payload.profile
    if "request_budget" in fields:
        kwargs["request_budget"] = payload.request_budget
        kwargs["clear_request_budget"] = payload.request_budget is None
    if "time_budget_seconds" in fields:
        kwargs["time_budget_seconds"] = payload.time_budget_seconds
        kwargs["clear_time_budget"] = payload.time_budget_seconds is None
    return await _call(_coordinator(request).update_job(job_id, **kwargs))


@router.post("/api/kwork/market/jobs/{job_id}/pause")
async def pause_market_job(
    request: Request,
    job_id: str,
    payload: JobRevisionRequest | None = None,
) -> dict[str, Any]:
    return await _call(
        _coordinator(request).pause_job(job_id, expected_revision=payload.expected_revision if payload else None)
    )


@router.post("/api/kwork/market/jobs/{job_id}/resume")
async def resume_market_job(
    request: Request,
    job_id: str,
    payload: JobRevisionRequest | None = None,
) -> dict[str, Any]:
    return await _call(
        _coordinator(request).resume_job(job_id, expected_revision=payload.expected_revision if payload else None)
    )


@router.post("/api/kwork/market/jobs/{job_id}/stop")
async def stop_market_job(
    request: Request,
    job_id: str,
    payload: StopJobRequest | None = None,
) -> dict[str, Any]:
    stop = payload or StopJobRequest()
    return await _call(
        _coordinator(request).stop_job(
            job_id,
            force=stop.force,
            expected_revision=stop.expected_revision,
        )
    )


@router.patch("/api/kwork/market/jobs/{job_id}/worker-pool")
async def set_market_worker_pool(
    request: Request,
    job_id: str,
    payload: WorkerPoolRequest,
) -> dict[str, Any]:
    return await _call(
        _coordinator(request).set_worker_pool(
            job_id,
            payload.desired_workers,
            expected_revision=payload.expected_revision,
        )
    )


@router.get("/api/kwork/market/jobs/{job_id}/workers", response_model=MarketPageResponse)
async def list_market_workers(
    request: Request,
    job_id: str,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_workers(job_id=job_id, cursor=cursor, limit=limit))
    return MarketPageResponse(items=items, limit=limit, cursor=cursor, next_cursor=_next_cursor(items, "worker_id"))


@router.get("/api/kwork/market/jobs/{job_id}/transports", response_model=MarketPageResponse)
async def list_market_transports(
    request: Request,
    job_id: str,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_transports(job_id=job_id, cursor=cursor, limit=limit))
    return MarketPageResponse(items=items, limit=limit, cursor=cursor, next_cursor=_next_cursor(items, "transport_id"))


@router.get("/api/kwork/market/jobs/{job_id}/shards", response_model=MarketPageResponse)
async def list_market_shards(
    request: Request,
    job_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_shards(job_id, offset=cursor, limit=limit))
    return MarketPageResponse(
        items=items,
        limit=limit,
        cursor=cursor,
        next_cursor=cursor + len(items) if len(items) == limit else None,
    )


@router.get("/api/kwork/market/jobs/{job_id}/operations", response_model=MarketPageResponse)
async def list_market_operations(
    request: Request,
    job_id: str,
    cursor: str | None = Query(default=None, max_length=256),
    state: Annotated[OperationState | None, Query()] = None,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_operations(job_id, cursor=cursor, state=state, limit=limit))
    return MarketPageResponse(items=items, limit=limit, cursor=cursor, next_cursor=_next_cursor(items, "operation_id"))


@router.get("/api/kwork/market/jobs/{job_id}/operations/{operation_id}/attempts", response_model=MarketPageResponse)
async def list_market_operation_attempts(
    request: Request,
    job_id: str,
    operation_id: str,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    operation = await _call(coordinator.repository.get_operation(operation_id))
    if operation is None or operation["job_id"] != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"operation {operation_id!r} was not found")
    items = await _call(coordinator.repository.list_operation_attempts(operation_id, limit=limit))
    return MarketPageResponse(items=items, limit=limit, cursor=None, next_cursor=None)


@router.get("/api/kwork/market/jobs/{job_id}/listings", response_model=MarketPageResponse)
async def list_market_listings(
    request: Request,
    job_id: str,
    cursor: int | None = Query(default=None, ge=0),
    shard: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=1_000),
) -> MarketPageResponse:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_listings(job_id, cursor=cursor, shard_id=shard, limit=limit))
    return MarketPageResponse(items=items, limit=limit, cursor=cursor, next_cursor=_next_cursor(items, "listing_id"))


@router.get("/api/kwork/market/jobs/{job_id}/events", response_model=MarketPageResponse)
async def replay_market_events(
    request: Request,
    job_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1_000),
) -> MarketPageResponse:
    items = await _call(_coordinator(request).replay_events(job_id, after_seq=after_seq, limit=limit))
    next_cursor = int(items[-1]["seq"]) if items else after_seq
    return MarketPageResponse(items=items, limit=limit, cursor=after_seq, next_cursor=next_cursor)


@router.get("/api/kwork/market/jobs/{job_id}/results")
async def get_market_results(request: Request, job_id: str) -> dict[str, Any]:
    coordinator = _coordinator(request)
    job = await _require_job(coordinator, job_id)
    checkpoints = await _call(coordinator.repository.list_checkpoints(job_id, limit=1))
    metric_inputs = await _call(coordinator.repository.list_listing_metrics_inputs(job_id))
    aggregate_total = (job.get("counters") or {}).get("aggregate_scope_total")
    metrics = project_market_metrics(metric_inputs, aggregate_scope_total=aggregate_total)
    latest_checkpoint = checkpoints[0] if checkpoints else None
    stored_metrics = latest_checkpoint.get("metrics") if isinstance(latest_checkpoint, dict) else None
    analysis = {
        key: stored_metrics[key]
        for key in ("enrichment_selection", "ai_evidence")
        if isinstance(stored_metrics, dict) and key in stored_metrics
    }
    return {
        "job_id": job_id,
        "revision": job["revision"],
        "state": job["state"],
        "phase": job["phase"],
        "target_unique_cards": job["target_unique_cards"],
        "counters": job["counters"],
        "latest_checkpoint": latest_checkpoint,
        "metrics": market_metrics_to_wire(metrics),
        "analysis": analysis,
    }


@router.get("/api/kwork/market/jobs/{job_id}/recommendations")
async def list_market_recommendations(request: Request, job_id: str) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_job(coordinator, job_id)
    items = await _call(coordinator.repository.list_recommendations(job_id))
    return {"job_id": job_id, "items": items}


@router.post("/api/kwork/market/jobs/{job_id}/recommendations", status_code=status.HTTP_201_CREATED)
async def create_market_recommendation(
    request: Request,
    job_id: str,
    payload: MarketRecommendationCreateRequest,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    job = await _require_job(coordinator, job_id)
    scope = job.get("scope") if isinstance(job.get("scope"), Mapping) else {}
    if payload.category_id != scope.get("category_id"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recommendation category_id must match the market job category",
        )
    recommendation = await _call(
        _handoff_service(request).propose(
            job_id,
            category_id=payload.category_id,
            classifier_id=payload.classifier_id,
            service_summary=payload.service_summary,
            price=payload.price,
            work_time=payload.work_time,
            source_cluster_id=payload.source_cluster_id,
            terra_result=payload.terra_result,
        )
    )
    return {"recommendation": recommendation}


@router.post("/api/kwork/market/jobs/{job_id}/recommendations/{recommendation_id}/confirm")
async def confirm_market_recommendation(
    request: Request,
    job_id: str,
    recommendation_id: str,
    payload: MarketRecommendationTransitionRequest | None = None,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_recommendation(coordinator, job_id, recommendation_id)
    return await _call(
        _handoff_service(request).confirm_recommendation(
            recommendation_id,
            expected_revision=payload.expected_revision if payload else None,
        )
    )


@router.post("/api/kwork/market/jobs/{job_id}/recommendations/{recommendation_id}/reject")
async def reject_market_recommendation(
    request: Request,
    job_id: str,
    recommendation_id: str,
    payload: MarketRecommendationTransitionRequest | None = None,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_recommendation(coordinator, job_id, recommendation_id)
    return await _call(
        _handoff_service(request).reject_recommendation(
            recommendation_id,
            expected_revision=payload.expected_revision if payload else None,
        )
    )


@router.get("/api/kwork/market/jobs/{job_id}/draft-handoffs/{handoff_id}")
async def get_market_draft_handoff(request: Request, job_id: str, handoff_id: str) -> dict[str, Any]:
    return await _require_handoff(_coordinator(request), job_id, handoff_id)


@router.post("/api/kwork/market/jobs/{job_id}/draft-handoffs/{handoff_id}/refresh-manifest")
async def refresh_market_draft_handoff_manifest(
    request: Request,
    job_id: str,
    handoff_id: str,
    payload: MarketHandoffManifestRequest | None = None,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_handoff(coordinator, job_id, handoff_id)
    handoff = await _call(_handoff_service(request).refresh_manifest(handoff_id, lang=payload.lang if payload else "ru"))
    return {"handoff": handoff}


@router.put("/api/kwork/market/jobs/{job_id}/draft-handoffs/{handoff_id}/selection")
async def update_market_draft_handoff_selection(
    request: Request,
    job_id: str,
    handoff_id: str,
    payload: MarketHandoffSelectionRequest,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_handoff(coordinator, job_id, handoff_id)
    handoff = await _call(
        _handoff_service(request).update_selection(
            handoff_id,
            selection=payload.selection,
            expected_manifest_hash=payload.manifest_hash,
            confirm=payload.confirm,
        )
    )
    return {"handoff": handoff}


@router.post("/api/kwork/market/jobs/{job_id}/draft-handoffs/{handoff_id}/draft")
async def generate_market_draft_handoff_draft(
    request: Request,
    job_id: str,
    handoff_id: str,
    payload: MarketHandoffDraftRequest | None = None,
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    await _require_handoff(coordinator, job_id, handoff_id)
    return await _call(
        _handoff_service(request).generate_draft(
            handoff_id,
            generation_options=payload.generation_options if payload else {},
        )
    )


@router.post("/api/kwork/market/jobs/{job_id}/assistant")
async def ask_market_job_assistant(
    request: Request,
    job_id: str,
    payload: MarketAssistantQuestionRequest,
) -> dict[str, Any]:
    """Answer against this durable job instead of an unrelated legacy market scan."""

    coordinator = _coordinator(request)
    snapshot = await _market_job_assistant_snapshot(coordinator, job_id)
    context_id = f"market_job_{job_id}"
    MarketAssistantStore.upsert(context_id, snapshot)
    try:
        return await MarketAssistant().ask(context_id, payload.message)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _command_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    command: WorkerCommandKind,
    payload: WorkerCommandRequest | None,
) -> dict[str, Any]:
    result = await _call(
        _coordinator(request).command_worker(
            job_id,
            worker_id,
            command,
            payload=payload.payload if payload else None,
        )
    )
    return result


@router.post("/api/kwork/market/jobs/{job_id}/workers/{worker_id}/drain", status_code=status.HTTP_202_ACCEPTED)
async def drain_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    payload: WorkerCommandRequest | None = None,
) -> dict[str, Any]:
    return await _command_market_worker(request, job_id, worker_id, WorkerCommandKind.DRAIN, payload)


@router.post("/api/kwork/market/jobs/{job_id}/workers/{worker_id}/restart", status_code=status.HTTP_202_ACCEPTED)
async def restart_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    payload: WorkerCommandRequest | None = None,
) -> dict[str, Any]:
    return await _command_market_worker(request, job_id, worker_id, WorkerCommandKind.RESTART, payload)


@router.post("/api/kwork/market/jobs/{job_id}/workers/{worker_id}/disable", status_code=status.HTTP_202_ACCEPTED)
async def disable_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    payload: WorkerCommandRequest | None = None,
) -> dict[str, Any]:
    return await _command_market_worker(request, job_id, worker_id, WorkerCommandKind.DISABLE, payload)


@router.post("/api/kwork/market/jobs/{job_id}/workers/{worker_id}/rotate", status_code=status.HTTP_202_ACCEPTED)
async def rotate_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    payload: WorkerCommandRequest | None = None,
) -> dict[str, Any]:
    return await _command_market_worker(request, job_id, worker_id, WorkerCommandKind.ROTATE, payload)


@router.post("/api/kwork/market/jobs/{job_id}/workers/{worker_id}/reconnect", status_code=status.HTTP_202_ACCEPTED)
async def reconnect_market_worker(
    request: Request,
    job_id: str,
    worker_id: str,
    payload: WorkerCommandRequest | None = None,
) -> dict[str, Any]:
    return await _command_market_worker(request, job_id, worker_id, WorkerCommandKind.RECONNECT, payload)


@router.post("/api/kwork/market/jobs/{job_id}/operations/{operation_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_market_operation(request: Request, job_id: str, operation_id: str) -> dict[str, Any]:
    return await _call(_coordinator(request).retry_operation(job_id, operation_id))


@router.websocket("/ws/kwork-market/jobs/{job_id}")
async def stream_market_job_events(websocket: WebSocket, job_id: str) -> None:
    """Replay durable events, then deliver fresh committed events without gaps."""

    try:
        coordinator = _market_jobs_from_app(websocket.app)
    except HTTPException:
        await websocket.close(code=1013)
        return

    raw_after_seq = websocket.query_params.get("after_seq", "0")
    try:
        after_seq = int(raw_after_seq)
        if after_seq < 0:
            raise ValueError
    except (TypeError, ValueError):
        await websocket.close(code=1008)
        return

    job = await coordinator.repository.get_job(job_id)
    if job is None:
        await websocket.close(code=4404)
        return

    queue = coordinator.event_hub.subscribe(job_id)
    await websocket.accept()
    last_seq = after_seq
    try:
        # Subscribe first.  Events committed while replay runs are queued and
        # de-duplicated below by their durable monotonic sequence number.
        event_bounds = await coordinator.repository.get_event_sequence_bounds(job_id)
        first_available_sequence = event_bounds["first_sequence"]
        last_available_sequence = event_bounds["last_sequence"]
        if first_available_sequence is not None and after_seq < first_available_sequence - 1:
            await websocket.send_json(
                {
                    "schema_version": 1,
                    "seq": last_available_sequence or 0,
                    "job_id": job_id,
                    "type": "resync_required",
                    "payload": {
                        "reason": "event_retention_gap",
                        "after_seq": after_seq,
                        "first_available_seq": first_available_sequence,
                    },
                }
            )
            return
        if last_available_sequence is not None and last_available_sequence - after_seq > _STREAM_REPLAY_LIMIT:
            await websocket.send_json(
                {
                    "schema_version": 1,
                    "seq": last_available_sequence,
                    "job_id": job_id,
                    "type": "resync_required",
                    "payload": {
                        "reason": "event_replay_window_exceeded",
                        "after_seq": after_seq,
                        "replay_limit": _STREAM_REPLAY_LIMIT,
                    },
                }
            )
            return
        replayed = await coordinator.replay_events(job_id, after_seq=after_seq, limit=_STREAM_REPLAY_LIMIT)
        first_replayed_sequence = int(replayed[0]["seq"]) if replayed else None
        if first_replayed_sequence is not None and first_replayed_sequence != after_seq + 1:
            await websocket.send_json(
                {
                    "schema_version": 1,
                    "seq": int(replayed[-1]["seq"]),
                    "job_id": job_id,
                    "type": "resync_required",
                    "payload": {
                        "reason": "event_retention_gap",
                        "after_seq": after_seq,
                        "first_available_seq": first_replayed_sequence,
                    },
                }
            )
            return
        if len(replayed) > _STREAM_REPLAY_LIMIT:
            await websocket.send_json(
                {
                    "schema_version": 1,
                    "seq": int(replayed[-1]["seq"]),
                    "job_id": job_id,
                    "type": "resync_required",
                    "payload": {
                        "reason": "event_replay_window_exceeded",
                        "after_seq": after_seq,
                        "replay_limit": _STREAM_REPLAY_LIMIT,
                    },
                }
            )
            return
        for event in replayed:
            sequence = int(event["seq"])
            if sequence <= last_seq:
                continue
            await websocket.send_json(event)
            last_seq = sequence

        while True:
            event_task = asyncio.create_task(queue.get())
            receive_task = asyncio.create_task(websocket.receive())
            done, pending = await asyncio.wait(
                {event_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if receive_task in done:
                message = receive_task.result()
                if message["type"] == "websocket.disconnect":
                    return
            if event_task not in done:
                continue
            event = event_task.result()
            sequence = int(event.get("seq", 0))
            if sequence <= last_seq:
                continue
            await websocket.send_json(event)
            last_seq = sequence
    except WebSocketDisconnect:
        return
    finally:
        coordinator.event_hub.unsubscribe(job_id, queue)
