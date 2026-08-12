"""Explicit proposal-workspace API for Buyer Search projects.

Draft, preflight, and outbox routes stay local.  The narrow ``/delivery``
route is separately wired through an account-bound, feature-gated controller
and performs a remote request only after another explicit operator
confirmation.  It never schedules automatic delivery.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.platforms.kwork_buyer.outreach import BuyerProposalReconciliationOutcome
from src.platforms.kwork_buyer.outreach_controller import BuyerOutreachController, BuyerOutreachControllerError
from src.platforms.kwork_buyer.outreach_service import BuyerOutreachNotFoundError, BuyerOutreachServiceError
from src.platforms.kwork_buyer.proposal_delivery import (
    BuyerAccountBoundProposalDeliveryController,
    BuyerProposalDeliveryError,
    BuyerProposalDeliveryFeatureDisabledError,
)
from src.platforms.kwork_buyer.service import BuyerSearchRunNotFoundError


router = APIRouter(prefix="/api/kwork/buyer-search", tags=["kwork-buyer-outreach"])


class BuyerPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_account_registration_id: str = Field(..., min_length=1, max_length=200)
    service_profile: dict[str, Any] = Field(...)
    additional_context: dict[str, Any] = Field(default_factory=dict)


class BuyerDraftGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float | None = Field(default=None, ge=0)
    delivery_days: int | None = Field(default=None, ge=1)
    currency: str = Field(default="RUB", min_length=3, max_length=8)
    model_alias: str | None = Field(default=None, max_length=200)
    additional_context: dict[str, Any] = Field(default_factory=dict)


class BuyerDraftEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(..., min_length=1, max_length=20_000)
    price: float | None = Field(default=None, ge=0)
    delivery_days: int | None = Field(default=None, ge=1)
    currency: str | None = Field(default=None, min_length=3, max_length=8)


class BuyerPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_is_active: bool | None = None
    has_offer: bool | None = None
    already_work: bool | None = None
    duplicate_send_intent: bool = False
    sender_account_eligible: bool | None = None
    account_session_valid: bool | None = None
    connects_sufficient: bool | None = None
    template_valid: bool | None = None
    portfolio_requirements_met: bool | None = None
    outgoing_attachment_count: int = Field(default=0, ge=0)
    attachment_upload_capability_verified: bool = False


class BuyerSendConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_account_registration_id: str = Field(..., min_length=1, max_length=200)
    confirmed_by: str = Field(..., min_length=1, max_length=200)
    confirmation_id: str | None = Field(default=None, max_length=200)


class BuyerSendReconciliationRequest(BaseModel):
    """Evidence-backed resolution for an existing unknown delivery outcome."""

    model_config = ConfigDict(extra="forbid")

    outcome: BuyerProposalReconciliationOutcome
    remote_receipt: str | None = Field(default=None, max_length=1_000)
    reason: str | None = Field(default=None, max_length=4_000)
    evidence: dict[str, Any] | None = None


class BuyerProposalDeliveryRequest(BaseModel):
    """A manual request to perform one already-confirmed pending delivery."""

    model_config = ConfigDict(extra="forbid")

    sender_account_registration_id: str = Field(..., min_length=1, max_length=200)
    confirmed_by: str = Field(..., min_length=1, max_length=200)
    delivery_confirmation_id: str = Field(..., min_length=1, max_length=200)
    explicit_delivery_confirmation: Literal[True]


class BuyerProposalDeliveryReconciliationRequest(BaseModel):
    """Account-bound, read-only reconciliation for one unknown delivery."""

    model_config = ConfigDict(extra="forbid")

    sender_account_registration_id: str = Field(..., min_length=1, max_length=200)


def _controller(app: Any) -> BuyerOutreachController:
    controller = getattr(app.state, "buyer_outreach", None)
    if not isinstance(controller, BuyerOutreachController):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer outreach is unavailable")
    return controller


def _delivery_controller(app: Any) -> BuyerAccountBoundProposalDeliveryController:
    controller = getattr(app.state, "buyer_proposal_delivery", None)
    if not isinstance(controller, BuyerAccountBoundProposalDeliveryController):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Buyer proposal delivery is unavailable")
    return controller


async def _call(operation: Any) -> Any:
    try:
        return await operation
    except BuyerOutreachNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerSearchRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerProposalDeliveryFeatureDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BuyerProposalDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (BuyerOutreachControllerError, BuyerOutreachServiceError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/runs/{run_id}/projects/{project_id}/outreach/promote", status_code=status.HTTP_201_CREATED)
async def promote_project(
    run_id: str,
    project_id: str,
    payload: BuyerPromotionRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _controller(request.app).promote(
            run_id=run_id,
            project_id=project_id,
            sender_account_registration_id=payload.sender_account_registration_id,
            service_profile=payload.service_profile,
            additional_context=payload.additional_context,
        )
    )


@router.get("/runs/{run_id}/projects/{project_id}/outreach")
async def get_promotion(run_id: str, project_id: str, request: Request) -> dict[str, Any]:
    return await _call(
        _controller(request.app).outreach_service.get_promotion(platform="kwork", run_id=run_id, project_id=project_id)
    )


@router.get("/runs/{run_id}/projects/{project_id}/outreach/drafts")
async def list_drafts(run_id: str, project_id: str, request: Request) -> list[dict[str, Any]]:
    return await _call(
        _controller(request.app).outreach_service.list_drafts(platform="kwork", run_id=run_id, project_id=project_id)
    )


@router.post("/runs/{run_id}/projects/{project_id}/outreach/drafts", status_code=status.HTTP_201_CREATED)
async def generate_draft(
    run_id: str,
    project_id: str,
    payload: BuyerDraftGenerateRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _controller(request.app).generate_draft(
            run_id=run_id,
            project_id=project_id,
            price=payload.price,
            delivery_days=payload.delivery_days,
            currency=payload.currency,
            model_alias=payload.model_alias,
            additional_context=payload.additional_context,
        )
    )


@router.get("/outreach/drafts/{draft_id}")
async def get_draft(draft_id: str, request: Request) -> dict[str, Any]:
    return await _call(_controller(request.app).outreach_service.get_draft(draft_id))


@router.patch("/outreach/drafts/{draft_id}")
async def edit_draft(draft_id: str, payload: BuyerDraftEditRequest, request: Request) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    return await _call(
        _controller(request.app).edit_draft(
            draft_id=draft_id,
            body=payload.body,
            **{key: value for key, value in changes.items() if key != "body"},
        )
    )


@router.get("/outreach/drafts/{draft_id}/preflight")
async def get_preflight(draft_id: str, request: Request) -> dict[str, Any] | None:
    return await _call(_controller(request.app).outreach_service.get_preflight(draft_id))


@router.post("/outreach/drafts/{draft_id}/preflight")
async def preflight_draft(draft_id: str, payload: BuyerPreflightRequest, request: Request) -> dict[str, Any]:
    return await _call(_controller(request.app).preflight(draft_id=draft_id, evidence=payload.model_dump()))


@router.post("/outreach/drafts/{draft_id}/send-intents", status_code=status.HTTP_201_CREATED)
async def create_send_intent(
    draft_id: str,
    payload: BuyerSendConfirmationRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _controller(request.app).create_confirmed_send_intent(
            draft_id=draft_id,
            sender_account_registration_id=payload.sender_account_registration_id,
            confirmation_id=payload.confirmation_id or f"buyer-confirmation-{uuid4()}",
            confirmed_by=payload.confirmed_by,
        )
    )


@router.get("/outreach/send-intents/{intent_id}")
async def get_send_intent(intent_id: str, request: Request) -> dict[str, Any]:
    return await _call(_controller(request.app).outreach_service.get_send_intent(intent_id))


@router.post("/outreach/send-intents/{intent_id}/reconcile")
async def reconcile_send_intent(
    intent_id: str,
    payload: BuyerSendReconciliationRequest,
    request: Request,
) -> dict[str, Any]:
    """Reconcile an unknown outbox attempt; this never performs a send."""

    return await _call(
        _controller(request.app).outreach_service.reconcile_send_intent(
            intent_id=intent_id,
            outcome=payload.outcome,
            remote_receipt=payload.remote_receipt,
            reason=payload.reason,
            evidence=payload.evidence,
        )
    )


@router.post("/outreach/send-intents/{intent_id}/delivery")
async def deliver_send_intent(
    intent_id: str,
    payload: BuyerProposalDeliveryRequest,
    request: Request,
) -> dict[str, Any]:
    """Perform one explicit account-bound delivery; no background send exists."""

    return await _call(
        _delivery_controller(request.app).deliver(
            intent_id=intent_id,
            sender_account_registration_id=payload.sender_account_registration_id,
            delivery_confirmation_id=payload.delivery_confirmation_id,
            confirmed_by=payload.confirmed_by,
            explicit_delivery_confirmation=payload.explicit_delivery_confirmation,
        )
    )


@router.post("/outreach/send-intents/{intent_id}/delivery/reconcile")
async def reconcile_delivery_remotely(
    intent_id: str,
    payload: BuyerProposalDeliveryReconciliationRequest,
    request: Request,
) -> dict[str, Any]:
    """Read remote evidence for an unknown delivery without issuing a send."""

    return await _call(
        _delivery_controller(request.app).reconcile_unknown(
            intent_id=intent_id,
            sender_account_registration_id=payload.sender_account_registration_id,
        )
    )


__all__ = ["router"]
