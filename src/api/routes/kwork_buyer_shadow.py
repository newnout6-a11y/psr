"""Buyer Search shadow comparison, durable acceptance, and rollout evidence.

The endpoints accept already captured artifacts only and never make Kwork
requests.  When the application provides the dedicated shadow store, reports
and gate decisions are appended durably so a live-discovery canary can check
the exact accepted evidence that authorised it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.platforms.kwork_buyer.shadow import (
    CANARY_WORKER_TARGETS,
    DEFAULT_BUYER_SHADOW_FIELDS,
    DEFAULT_REQUIRED_BUYER_SHADOW_FIELDS,
    BuyerShadowInputError,
    BuyerShadowRolloutConfig,
    compare_buyer_endpoint_drift,
    compare_buyer_shadow,
    evaluate_buyer_shadow_rollout_gate,
    summarize_buyer_shadow_acceptance,
)
from src.platforms.kwork_buyer.shadow_persistence import SQLiteBuyerShadowStore


router = APIRouter(prefix="/api/kwork/buyer-search/shadow", tags=["kwork-buyer-shadow"])


class BuyerShadowComparisonRequest(BaseModel):
    """Immutable legacy/new captures used to calculate parity."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, max_length=200)
    legacy_projects: list[dict[str, Any]] = Field(default_factory=list)
    new_projects: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[str] | None = None
    legacy_endpoints: Any = Field(default_factory=list)
    new_endpoints: Any = Field(default_factory=list)
    endpoint_aliases: dict[str, str] | None = None


class BuyerShadowEndpointDriftRequest(BaseModel):
    """Endpoint samples captured from the legacy and Buyer flows."""

    model_config = ConfigDict(extra="forbid")

    legacy_endpoints: Any = Field(default_factory=list)
    new_endpoints: Any = Field(default_factory=list)
    endpoint_aliases: dict[str, str] | None = None


class BuyerShadowRolloutConfigRequest(BaseModel):
    """Serializable, fail-closed configuration for one rollout check."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    live_discovery: bool = False
    max_workers: int = Field(default=2, ge=1, le=30)
    canary_targets: list[int] = Field(default_factory=lambda: list(CANARY_WORKER_TARGETS))
    completed_canary_targets: list[int] = Field(default_factory=list)
    require_shadow_acceptance: bool = True
    require_verified_egress: bool = True
    require_unique_identity_evidence: bool = True
    legacy_retirement_enabled: bool = False

    def to_domain(self) -> BuyerShadowRolloutConfig:
        return BuyerShadowRolloutConfig(
            enabled=self.enabled,
            live_discovery=self.live_discovery,
            max_workers=self.max_workers,
            canary_targets=tuple(self.canary_targets),
            completed_canary_targets=tuple(self.completed_canary_targets),
            require_shadow_acceptance=self.require_shadow_acceptance,
            require_verified_egress=self.require_verified_egress,
            require_unique_identity_evidence=self.require_unique_identity_evidence,
            legacy_retirement_enabled=self.legacy_retirement_enabled,
        )


class BuyerShadowRolloutGateRequest(BaseModel):
    """Evidence and an optional comparison used to assess one canary stage."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, max_length=200)
    target_workers: int = Field(..., ge=1, le=30)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    config: BuyerShadowRolloutConfigRequest = Field(default_factory=BuyerShadowRolloutConfigRequest)
    comparison: BuyerShadowComparisonRequest | None = None
    operator_accepted: bool = False
    rollback_documented: bool = False
    required_fields: list[str] | None = None


def _comparison(payload: BuyerShadowComparisonRequest) -> Any:
    return compare_buyer_shadow(
        payload.legacy_projects,
        payload.new_projects,
        fields=tuple(payload.fields) if payload.fields is not None else DEFAULT_BUYER_SHADOW_FIELDS,
        legacy_endpoints=payload.legacy_endpoints,
        new_endpoints=payload.new_endpoints,
        endpoint_aliases=payload.endpoint_aliases,
    )


def _validation_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _store(app: Any) -> SQLiteBuyerShadowStore | None:
    store = getattr(app.state, "buyer_shadow", None)
    return store if isinstance(store, SQLiteBuyerShadowStore) else None


@router.post("/compare")
async def compare(payload: BuyerShadowComparisonRequest, request: Request) -> dict[str, Any]:
    """Return a deterministic parity report for two captured project sets."""

    try:
        comparison = _comparison(payload)
        result = comparison.as_dict()
        store = _store(request.app)
        if store is not None:
            report = await store.save_report(result, run_id=payload.run_id)
            result["report"] = report
        return result
    except (BuyerShadowInputError, TypeError, ValueError) as exc:
        raise _validation_error(exc) from exc


@router.post("/endpoint-drift")
async def endpoint_drift(payload: BuyerShadowEndpointDriftRequest) -> dict[str, Any]:
    """Aggregate captured endpoint differences without making remote calls."""

    try:
        drifts = compare_buyer_endpoint_drift(
            payload.legacy_endpoints,
            payload.new_endpoints,
            endpoint_aliases=payload.endpoint_aliases,
        )
        return {"items": [item.as_dict() for item in drifts]}
    except (BuyerShadowInputError, TypeError, ValueError) as exc:
        raise _validation_error(exc) from exc


@router.post("/rollout-gate")
async def rollout_gate(payload: BuyerShadowRolloutGateRequest, request: Request) -> dict[str, Any]:
    """Evaluate staged canary evidence, optionally deriving shadow acceptance."""

    try:
        acceptance = None
        if payload.comparison is not None:
            comparison = _comparison(payload.comparison)
            acceptance = summarize_buyer_shadow_acceptance(
                comparison,
                operator_accepted=payload.operator_accepted,
                rollback_documented=payload.rollback_documented,
                required_fields=(
                    tuple(payload.required_fields)
                    if payload.required_fields is not None
                    else DEFAULT_REQUIRED_BUYER_SHADOW_FIELDS
                ),
            )
        gate = evaluate_buyer_shadow_rollout_gate(
            payload.target_workers,
            payload.evidence,
            config=payload.config.to_domain(),
            acceptance=acceptance,
        )
        result: dict[str, Any] = {"gate": gate.as_dict()}
        report_id: str | None = None
        if acceptance is not None:
            result["acceptance"] = acceptance.as_dict()
            store = _store(request.app)
            if store is not None:
                report = await store.save_report(
                    comparison.as_dict(),
                    run_id=payload.run_id,
                    acceptance=acceptance.as_dict(),
                    operator_accepted=payload.operator_accepted,
                    rollback_documented=payload.rollback_documented,
                )
                report_id = report["report_id"]
                result["report"] = report
        store = _store(request.app)
        if store is not None:
            persisted = await store.save_gate(
                gate.as_dict(),
                run_id=payload.run_id,
                evidence=payload.evidence,
                report_id=report_id,
            )
            result["persisted_gate"] = persisted
        return result
    except (BuyerShadowInputError, TypeError, ValueError) as exc:
        raise _validation_error(exc) from exc


@router.get("/runs/{run_id}/latest-approved-gate/{target_workers}")
async def latest_approved_gate(run_id: str, target_workers: int, request: Request) -> dict[str, Any]:
    store = _store(request.app)
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer shadow persistence is unavailable")
    gate = await store.latest_approved_gate(run_id, target_workers=target_workers)
    return {"gate": gate}


__all__ = ["router"]
