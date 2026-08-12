"""Application bridge between Buyer Search project evidence and outreach state.

This module composes two already-separated domains without giving either one a
remote send capability.  It promotes a durable project snapshot, carries only
bounded attachment excerpts into proposal context, and evaluates a preflight
against the latest Buyer Search projection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import inspect
from typing import Any

from .outreach_service import BuyerOutreachService
from .proposal_composer import BuyerProposalPreflightInput
from .service import BuyerSearchSettings


class BuyerOutreachControllerError(ValueError):
    """Raised for malformed Buyer Search outreach API input."""


_UNSET = object()
BuyerPreflightEvidenceVerifier = Callable[
    [str, str, str, Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class BuyerOutreachController:
    """Promote selected Buyer projects and keep their proposal context auditable."""

    def __init__(
        self,
        search_service: object,
        outreach_service: BuyerOutreachService,
        *,
        settings: BuyerSearchSettings,
        preflight_evidence_verifier: BuyerPreflightEvidenceVerifier | None = None,
    ) -> None:
        if not callable(getattr(search_service, "get_project", None)):
            raise TypeError("search_service must expose get_project")
        if not isinstance(outreach_service, BuyerOutreachService):
            raise TypeError("outreach_service must be BuyerOutreachService")
        if not isinstance(settings, BuyerSearchSettings):
            raise TypeError("settings must be BuyerSearchSettings")
        if preflight_evidence_verifier is not None and not callable(preflight_evidence_verifier):
            raise TypeError("preflight_evidence_verifier must be callable")
        self.search_service = search_service
        self.outreach_service = outreach_service
        self.settings = settings
        self.preflight_evidence_verifier = preflight_evidence_verifier

    async def promote(
        self,
        *,
        run_id: str,
        project_id: str,
        sender_account_registration_id: str,
        service_profile: Mapping[str, Any],
        additional_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = await self.search_service.get_project(_required_text(run_id, "run_id"), _required_text(project_id, "project_id"))
        if not isinstance(project, Mapping):
            raise BuyerOutreachControllerError("Buyer Search project detail must be an object")
        return await self.outreach_service.promote_project(
            run_id=run_id,
            project_id=project_id,
            sender_account_registration_id=_required_text(sender_account_registration_id, "sender_account_registration_id"),
            project=dict(project),
            service_profile=_mapping(service_profile, "service_profile"),
            score=_project_score(project),
            additional_context={**_attachment_context(project), **_mapping_or_empty(additional_context)},
        )

    async def generate_draft(
        self,
        *,
        run_id: str,
        project_id: str,
        price: int | float | None = None,
        delivery_days: int | None = None,
        currency: str = "RUB",
        model_alias: str | None = None,
        additional_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.outreach_service.generate_draft(
            platform="kwork",
            run_id=_required_text(run_id, "run_id"),
            project_id=_required_text(project_id, "project_id"),
            price=price,
            delivery_days=delivery_days,
            currency=_required_text(currency, "currency"),
            model_alias=_optional_text(model_alias),
            additional_context=_mapping_or_empty(additional_context),
        )

    async def edit_draft(
        self,
        *,
        draft_id: str,
        body: str,
        price: int | float | None | object = _UNSET,
        delivery_days: int | None | object = _UNSET,
        currency: str | object = _UNSET,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"draft_id": _required_text(draft_id, "draft_id"), "body": _required_text(body, "body")}
        if price is not _UNSET:
            kwargs["price"] = price
        if delivery_days is not _UNSET:
            kwargs["delivery_days"] = delivery_days
        if currency is not _UNSET:
            kwargs["currency"] = _required_text(currency, "currency")
        return await self.outreach_service.edit_draft(**kwargs)

    async def preflight(self, *, draft_id: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        draft = await self.outreach_service.get_draft_record(_required_text(draft_id, "draft_id"))
        project = await self.search_service.get_project(draft.draft.run_id, draft.draft.project_id)
        data = _mapping(evidence, "evidence")
        verified = await self._verified_preflight_evidence(
            draft.sender_account_registration_id,
            draft.draft.run_id,
            draft.draft.project_id,
            project if isinstance(project, Mapping) else {},
        )
        input_value = BuyerProposalPreflightInput(
            draft=draft,
            # Remote/account evidence never comes from a browser checkbox.  In
            # the absence of a leased account-bound verifier these values stay
            # unknown and preflight remains non-passing.
            project_is_active=_optional_bool(verified.get("project_is_active"), "project_is_active"),
            current_project=dict(project) if isinstance(project, Mapping) else None,
            has_offer=_optional_bool(verified.get("has_offer"), "has_offer"),
            already_work=_optional_bool(verified.get("already_work"), "already_work"),
            duplicate_send_intent=bool(verified.get("duplicate_send_intent", False)),
            sender_account_eligible=_optional_bool(verified.get("sender_account_eligible"), "sender_account_eligible"),
            account_session_valid=_optional_bool(verified.get("account_session_valid"), "account_session_valid"),
            connects_sufficient=_optional_bool(verified.get("connects_sufficient"), "connects_sufficient"),
            template_valid=_optional_bool(data.get("template_valid"), "template_valid"),
            portfolio_requirements_met=_optional_bool(data.get("portfolio_requirements_met"), "portfolio_requirements_met"),
            proposal_send_enabled=self.settings.proposal_send,
            outgoing_attachment_count=_nonnegative_int(data.get("outgoing_attachment_count", 0), "outgoing_attachment_count"),
            attachment_upload_enabled=self.settings.proposal_attachments,
            attachment_upload_capability_verified=bool(verified.get("attachment_upload_capability_verified", False)),
        )
        return await self.outreach_service.preflight_draft(draft_id=draft.draft_id, evidence=input_value)

    async def _verified_preflight_evidence(
        self,
        sender_account_registration_id: str,
        run_id: str,
        project_id: str,
        project: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.preflight_evidence_verifier is None:
            return {}
        value = self.preflight_evidence_verifier(sender_account_registration_id, run_id, project_id, dict(project))
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, Mapping):
            raise BuyerOutreachControllerError("preflight_evidence_verifier must return an object")
        return dict(value)

    async def create_confirmed_send_intent(
        self,
        *,
        draft_id: str,
        sender_account_registration_id: str,
        confirmation_id: str,
        confirmed_by: str,
    ) -> dict[str, Any]:
        return await self.outreach_service.create_confirmed_send_intent(
            draft_id=_required_text(draft_id, "draft_id"),
            sender_account_registration_id=_required_text(sender_account_registration_id, "sender_account_registration_id"),
            explicit_confirmation=True,
            confirmation_id=_required_text(confirmation_id, "confirmation_id"),
            confirmed_by=_required_text(confirmed_by, "confirmed_by"),
        )


def _project_score(project: Mapping[str, Any]) -> dict[str, Any]:
    scores = project.get("scores")
    if isinstance(scores, Sequence) and not isinstance(scores, (str, bytes, bytearray)):
        for score in scores:
            if isinstance(score, Mapping):
                return dict(score)
    return {
        "preliminary_score": project.get("preliminary_score"),
        "final_score": project.get("final_score"),
    }


def _attachment_context(project: Mapping[str, Any]) -> dict[str, Any]:
    attachments = project.get("attachments")
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes, bytearray)):
        return {"attachment_manifest": [], "attachment_excerpts": []}
    manifest: list[dict[str, Any]] = []
    excerpts: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        manifest.append(
            {
                "attachment_id": attachment.get("attachment_id"),
                "filename": attachment.get("filename"),
                "detected_type": attachment.get("detected_type"),
                "sha256": attachment.get("sha256"),
                "state": attachment.get("state"),
            }
        )
        derivatives = attachment.get("derivatives")
        if not isinstance(derivatives, Sequence) or isinstance(derivatives, (str, bytes, bytearray)):
            continue
        for derivative in derivatives:
            if not isinstance(derivative, Mapping) or str(derivative.get("state") or "").casefold() not in {"parsed", "completed"}:
                continue
            text = _optional_text(derivative.get("extracted_text"))
            if text:
                excerpts.append(
                    {
                        "attachment_id": attachment.get("attachment_id"),
                        "parser": derivative.get("parser_name"),
                        "text": text[:12_000],
                    }
                )
    return {"attachment_manifest": manifest, "attachment_excerpts": excerpts}


def _mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BuyerOutreachControllerError(f"{name} must be an object")
    return dict(value)


def _mapping_or_empty(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return _mapping(value, "additional_context") if value is not None else {}


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerOutreachControllerError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise BuyerOutreachControllerError(f"{name} must be a boolean")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BuyerOutreachControllerError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachControllerError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise BuyerOutreachControllerError(f"{name} must be a non-negative integer")
    return parsed


__all__ = ["BuyerOutreachController", "BuyerOutreachControllerError", "BuyerPreflightEvidenceVerifier"]
