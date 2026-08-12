"""Application orchestration for Buyer Search outreach.

The module owns durable promotion, proposal-version, preflight, and outbox
state.  It deliberately never performs a proposal HTTP request.  A separate,
explicitly authorised delivery component may consume the persisted outbox
later; timeout reconciliation is limited to an injected read-only evidence
gateway.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, uuid4, uuid5

from .attachments import BuyerAttachmentContext, BuyerAttachmentParseResult
from .outreach import (
    BuyerProposalOutreach,
    BuyerProposalReconciliationOutcome,
    BuyerProposalSendIntent,
    BuyerProposalSendState,
    validate_sender_account_registration_id,
)
from .proposal_composer import (
    BuyerComposedProposalDraft,
    BuyerProposalComposeRequest,
    BuyerProposalComposer,
    BuyerProposalLLMGateway,
    BuyerProposalPreflightInput,
    BuyerProposalPreflightResult,
    build_buyer_proposal_context,
    revise_buyer_proposal_draft,
)


_UNSET = object()


class BuyerOutreachServiceError(ValueError):
    """Raised when an operator request violates the Buyer outreach contract."""


class BuyerOutreachNotFoundError(BuyerOutreachServiceError):
    """Raised when a durable Buyer outreach record is unavailable."""


class BuyerOutreachPromotionConflict(BuyerOutreachServiceError):
    """Raised when an existing promotion is asked to silently change account."""


class BuyerOutreachConfirmationRequired(BuyerOutreachServiceError):
    """Raised when a caller tries to create a send outbox item without consent."""


@dataclass(frozen=True, slots=True)
class BuyerOutreachPromotion:
    """An immutable project snapshot promoted into the outreach domain.

    The key intentionally excludes the sender account: a project should not
    become two outreach records merely because somebody changes a UI selector.
    A different sender is rejected explicitly instead of silently weakening
    account attribution.
    """

    run_id: str
    project_id: str
    sender_account_registration_id: str
    project: Mapping[str, Any]
    service_profile: Mapping[str, Any]
    platform: str = "kwork"
    score: Mapping[str, Any] = field(default_factory=dict)
    additional_context: Mapping[str, Any] = field(default_factory=dict)
    attachment_context: BuyerAttachmentContext | None = None
    attachment_results: tuple[BuyerAttachmentParseResult, ...] = ()
    promotion_id: str = field(init=False)
    project_hash: str = field(init=False)

    def __post_init__(self) -> None:
        run_id = _required_text(self.run_id, "run_id")
        project_id = _required_text(self.project_id, "project_id")
        platform = _required_text(self.platform, "platform")
        if platform != "kwork":
            raise BuyerOutreachServiceError("Buyer outreach currently supports only the kwork platform")
        sender = validate_sender_account_registration_id(self.sender_account_registration_id)
        project = _normalize_mapping(self.project, "project")
        service_profile = _normalize_mapping(self.service_profile, "service_profile")
        score = _normalize_mapping(self.score, "score")
        additional_context = _normalize_mapping(self.additional_context, "additional_context")
        if self.attachment_context is not None and self.attachment_results:
            raise BuyerOutreachServiceError("provide attachment_context or attachment_results, not both")
        if self.attachment_context is not None and not isinstance(self.attachment_context, BuyerAttachmentContext):
            raise TypeError("attachment_context must be a BuyerAttachmentContext or None")
        attachment_results = tuple(self.attachment_results)
        if not all(isinstance(result, BuyerAttachmentParseResult) for result in attachment_results):
            raise TypeError("attachment_results must contain BuyerAttachmentParseResult values")

        promotion_id = _promotion_id(platform, run_id, project_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "sender_account_registration_id", sender)
        object.__setattr__(self, "project", _freeze_json_value(project))
        object.__setattr__(self, "service_profile", _freeze_json_value(service_profile))
        object.__setattr__(self, "score", _freeze_json_value(score))
        object.__setattr__(self, "additional_context", _freeze_json_value(additional_context))
        object.__setattr__(self, "attachment_results", attachment_results)
        object.__setattr__(self, "promotion_id", promotion_id)
        object.__setattr__(self, "project_hash", _hash_json(project))

    def build_context(self, *, additional_context: Mapping[str, Any] | None = None):
        """Build the exact attachment-aware context used by a new draft."""

        merged_context = _thaw_json_value(self.additional_context)
        if additional_context is not None:
            merged_context.update(_normalize_mapping(additional_context, "additional_context"))
        kwargs: dict[str, Any] = {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "project": _thaw_json_value(self.project),
            "service_profile": _thaw_json_value(self.service_profile),
            "score": _thaw_json_value(self.score),
            "additional_context": merged_context,
        }
        if self.attachment_context is not None:
            kwargs["attachment_context"] = self.attachment_context
        elif self.attachment_results:
            kwargs["attachment_results"] = self.attachment_results
        return build_buyer_proposal_context(**kwargs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "promotion_id": self.promotion_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "platform": self.platform,
            "sender_account_registration_id": self.sender_account_registration_id,
            "project": _thaw_json_value(self.project),
            "project_hash": self.project_hash,
            "service_profile": _thaw_json_value(self.service_profile),
            "score": _thaw_json_value(self.score),
            "additional_context": _thaw_json_value(self.additional_context),
            "attachment_context_hash": self.attachment_context.context_hash if self.attachment_context is not None else None,
            "attachment_count": len(self.attachment_results)
            if self.attachment_context is None
            else len(self.attachment_context.manifest),
        }


@dataclass(frozen=True, slots=True)
class BuyerOutreachConfirmation:
    """Durable evidence that an operator explicitly requested outbox delivery."""

    confirmation_id: str
    intent_id: str
    confirmed_by: str
    confirmed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmation_id", _required_text(self.confirmation_id, "confirmation_id"))
        object.__setattr__(self, "intent_id", _required_text(self.intent_id, "intent_id"))
        object.__setattr__(self, "confirmed_by", _required_text(self.confirmed_by, "confirmed_by"))
        object.__setattr__(self, "confirmed_at", _required_text(self.confirmed_at, "confirmed_at"))

    def to_payload(self) -> dict[str, str]:
        return {
            "confirmation_id": self.confirmation_id,
            "intent_id": self.intent_id,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True, slots=True)
class BuyerOutreachDeliveryAttempt:
    """The durable state reserved for one explicitly requested delivery.

    This is deliberately transport-free.  A separately injected delivery
    controller may use the immutable draft and promotion evidence to make one
    remote request after this record has transitioned the outbox to
    ``sending``.
    """

    intent: BuyerProposalSendIntent
    draft: BuyerComposedProposalDraft
    promotion: BuyerOutreachPromotion
    confirmation: BuyerOutreachConfirmation

    def __post_init__(self) -> None:
        if not isinstance(self.intent, BuyerProposalSendIntent):
            raise TypeError("intent must be a BuyerProposalSendIntent")
        if not isinstance(self.draft, BuyerComposedProposalDraft):
            raise TypeError("draft must be a BuyerComposedProposalDraft")
        if not isinstance(self.promotion, BuyerOutreachPromotion):
            raise TypeError("promotion must be a BuyerOutreachPromotion")
        if not isinstance(self.confirmation, BuyerOutreachConfirmation):
            raise TypeError("confirmation must be a BuyerOutreachConfirmation")


@dataclass(frozen=True, slots=True)
class BuyerRemoteOfferEvidence:
    """Read-only evidence used to reconcile an indeterminate send attempt."""

    source: str
    has_offer: bool | None
    remote_receipt: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = _required_text(self.source, "source")
        if self.has_offer is not None and not isinstance(self.has_offer, bool):
            raise TypeError("has_offer must be a boolean or None")
        receipt = _optional_text(self.remote_receipt, "remote_receipt")
        if not isinstance(self.detail, Mapping):
            raise TypeError("detail must be a mapping")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "remote_receipt", receipt)
        object.__setattr__(self, "detail", _freeze_json_value(_normalize_mapping(self.detail, "detail")))

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "has_offer": self.has_offer,
            "remote_receipt": self.remote_receipt,
            "detail": _thaw_json_value(self.detail),
        }


@runtime_checkable
class BuyerOutreachStore(Protocol):
    """Persistence boundary for outreach; implementations own atomic writes."""

    async def get_promotion(self, *, platform: str, run_id: str, project_id: str) -> BuyerOutreachPromotion | None:
        """Return the project promotion keyed by platform/run/project."""

    async def create_promotion(self, promotion: BuyerOutreachPromotion) -> BuyerOutreachPromotion:
        """Insert or return the existing idempotent promotion."""

    async def get_draft(self, draft_id: str) -> BuyerComposedProposalDraft | None:
        """Return one immutable draft version."""

    async def list_drafts(self, *, promotion_id: str) -> Sequence[BuyerComposedProposalDraft]:
        """Return all versions for one promoted project in persistent order."""

    async def create_draft(self, draft: BuyerComposedProposalDraft) -> BuyerComposedProposalDraft:
        """Persist a draft with unique draft ID and project/version constraints."""

    async def get_preflight(self, draft_id: str) -> BuyerProposalPreflightResult | None:
        """Return the latest durable preflight for a draft version."""

    async def save_preflight(self, draft_id: str, result: BuyerProposalPreflightResult) -> BuyerProposalPreflightResult:
        """Persist a preflight result without changing the immutable draft."""

    async def get_send_intent(self, intent_id: str) -> BuyerProposalSendIntent | None:
        """Return one send outbox item."""

    async def get_send_intent_by_idempotency_key(self, idempotency_key: str) -> BuyerProposalSendIntent | None:
        """Return an outbox item by its platform/project/account/draft key."""

    async def create_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        """Create one unique outbox record."""

    async def update_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        """Persist an allowed state transition for a previously created intent."""

    async def record_confirmation(self, confirmation: BuyerOutreachConfirmation) -> BuyerOutreachConfirmation:
        """Persist an operator confirmation idempotently by confirmation ID."""

    async def get_confirmation(self, confirmation_id: str) -> BuyerOutreachConfirmation | None:
        """Return one immutable confirmation, including delivery confirmations."""

    async def append_audit_event(
        self,
        *,
        event_type: str,
        run_id: str,
        project_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append immutable operator and reconciliation audit evidence."""


@runtime_checkable
class BuyerOutreachReconciliationGateway(Protocol):
    """Read-only remote evidence contract; it deliberately has no send method."""

    async def inspect_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerRemoteOfferEvidence | Mapping[str, Any]:
        """Inspect remote offer evidence after a timeout or ambiguous response."""


class BuyerOutreachService:
    """Coordinate draft and outbox records without gaining transport capability."""

    def __init__(self, store: BuyerOutreachStore, composer: BuyerProposalComposer | BuyerProposalLLMGateway) -> None:
        if not isinstance(store, BuyerOutreachStore):
            raise TypeError("store must implement BuyerOutreachStore")
        self._store = store
        self._composer = composer if isinstance(composer, BuyerProposalComposer) else BuyerProposalComposer(composer)

    async def get_promotion(self, *, platform: str, run_id: str, project_id: str) -> dict[str, Any]:
        """Return one account-pinned promotion without exposing store internals."""

        return (await self._require_promotion(platform=platform, run_id=run_id, project_id=project_id)).to_payload()

    async def get_draft_record(self, draft_id: str) -> BuyerComposedProposalDraft:
        """Return an immutable draft object for controlled preflight orchestration."""

        return await self._require_draft(draft_id)

    async def get_draft(self, draft_id: str) -> dict[str, Any]:
        """Return a serialized immutable proposal draft and its promotion key."""

        draft = await self._require_draft(draft_id)
        promotion = await self._require_promotion(
            platform=draft.draft.platform,
            run_id=draft.draft.run_id,
            project_id=draft.draft.project_id,
        )
        return _draft_payload(draft, promotion_id=promotion.promotion_id)

    async def list_drafts(self, *, platform: str, run_id: str, project_id: str) -> list[dict[str, Any]]:
        """List immutable versions in the persisted promotion order."""

        promotion = await self._require_promotion(platform=platform, run_id=run_id, project_id=project_id)
        drafts = await self._store.list_drafts(promotion_id=promotion.promotion_id)
        return [_draft_payload(draft, promotion_id=promotion.promotion_id) for draft in drafts]

    async def get_preflight(self, draft_id: str) -> dict[str, Any] | None:
        """Return the latest durable preflight evidence for a draft version."""

        draft = await self._require_draft(draft_id)
        result = await self._store.get_preflight(draft.draft_id)
        return result.to_payload() if result is not None else None

    async def get_send_intent(self, intent_id: str) -> dict[str, Any]:
        """Return one durable outbox record; this never triggers delivery."""

        return (await self._require_send_intent(intent_id)).to_payload()

    async def get_send_intent_record(self, intent_id: str) -> BuyerProposalSendIntent:
        """Return the durable outbox object for an explicit delivery controller."""

        return await self._require_send_intent(intent_id)

    async def get_confirmation(self, confirmation_id: str) -> BuyerOutreachConfirmation | None:
        """Return durable operator evidence without exposing the persistence layer."""

        return await self._store.get_confirmation(_required_text(confirmation_id, "confirmation_id"))

    async def promote_project(
        self,
        *,
        run_id: str,
        project_id: str,
        sender_account_registration_id: str,
        project: Mapping[str, Any],
        service_profile: Mapping[str, Any],
        platform: str = "kwork",
        score: Mapping[str, Any] | None = None,
        additional_context: Mapping[str, Any] | None = None,
        attachment_context: BuyerAttachmentContext | None = None,
        attachment_results: Sequence[BuyerAttachmentParseResult] = (),
    ) -> dict[str, Any]:
        """Create the project promotion once and return its immutable snapshot."""

        normalized_platform = _required_text(platform, "platform")
        normalized_run_id = _required_text(run_id, "run_id")
        normalized_project_id = _required_text(project_id, "project_id")
        sender = validate_sender_account_registration_id(sender_account_registration_id)
        existing = await self._store.get_promotion(
            platform=normalized_platform,
            run_id=normalized_run_id,
            project_id=normalized_project_id,
        )
        if existing is not None:
            if existing.sender_account_registration_id != sender:
                raise BuyerOutreachPromotionConflict(
                    "a promoted project is pinned to a different sender account; create an explicit account reassignment flow"
                )
            return existing.to_payload()

        promotion = BuyerOutreachPromotion(
            run_id=normalized_run_id,
            project_id=normalized_project_id,
            sender_account_registration_id=sender,
            project=project,
            service_profile=service_profile,
            platform=normalized_platform,
            score=score or {},
            additional_context=additional_context or {},
            attachment_context=attachment_context,
            attachment_results=tuple(attachment_results),
        )
        persisted = await self._store.create_promotion(promotion)
        if persisted.sender_account_registration_id != sender:
            raise BuyerOutreachPromotionConflict("promotion persistence returned a different sender account")
        await self._audit(
            "proposal.project.promoted",
            persisted.run_id,
            persisted.project_id,
            {
                "promotion_id": persisted.promotion_id,
                "project_hash": persisted.project_hash,
                "sender_account_registration_id": persisted.sender_account_registration_id,
            },
        )
        return persisted.to_payload()

    async def generate_draft(
        self,
        *,
        platform: str,
        run_id: str,
        project_id: str,
        draft_id: str | None = None,
        price: int | float | None = None,
        delivery_days: int | None = None,
        currency: str = "RUB",
        model_alias: str | None = None,
        additional_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate one immutable draft version using the task-routed composer."""

        promotion = await self._require_promotion(platform=platform, run_id=run_id, project_id=project_id)
        resolved_draft_id = _required_text(draft_id, "draft_id") if draft_id is not None else _new_id("buyer-draft")
        existing = await self._store.get_draft(resolved_draft_id)
        if existing is not None:
            self._ensure_draft_matches_promotion(existing, promotion)
            return _draft_payload(existing, promotion_id=promotion.promotion_id)

        drafts = await self._store.list_drafts(promotion_id=promotion.promotion_id)
        version = max((draft.version for draft in drafts), default=0) + 1
        composed = await self._composer.compose(
            BuyerProposalComposeRequest(
                draft_id=resolved_draft_id,
                context=promotion.build_context(additional_context=additional_context),
                sender_account_registration_id=promotion.sender_account_registration_id,
                price=price,
                delivery_days=delivery_days,
                currency=currency,
                version=version,
                model_alias=model_alias,
            )
        )
        persisted = await self._store.create_draft(composed)
        self._ensure_draft_matches_promotion(persisted, promotion)
        await self._audit(
            "proposal.draft.created",
            promotion.run_id,
            promotion.project_id,
            _draft_audit_payload(persisted, promotion.promotion_id),
        )
        return _draft_payload(persisted, promotion_id=promotion.promotion_id)

    async def edit_draft(
        self,
        *,
        draft_id: str,
        revised_draft_id: str | None = None,
        body: str,
        price: int | float | None | object = _UNSET,
        delivery_days: int | None | object = _UNSET,
        currency: str | object = _UNSET,
    ) -> dict[str, Any]:
        """Persist a manual version while retaining the original generated text."""

        previous = await self._require_draft(draft_id)
        promotion = await self._require_promotion(
            platform=previous.draft.platform,
            run_id=previous.draft.run_id,
            project_id=previous.draft.project_id,
        )
        new_draft_id = _required_text(revised_draft_id, "revised_draft_id") if revised_draft_id is not None else _new_id("buyer-draft")
        existing = await self._store.get_draft(new_draft_id)
        if existing is not None:
            self._ensure_draft_matches_promotion(existing, promotion)
            return _draft_payload(existing, promotion_id=promotion.promotion_id)

        changes: dict[str, Any] = {}
        if price is not _UNSET:
            changes["price"] = price
        if delivery_days is not _UNSET:
            changes["delivery_days"] = delivery_days
        if currency is not _UNSET:
            changes["currency"] = currency
        revised = revise_buyer_proposal_draft(previous, draft_id=new_draft_id, body=body, **changes)
        persisted = await self._store.create_draft(revised)
        self._ensure_draft_matches_promotion(persisted, promotion)
        await self._audit(
            "proposal.draft.edited",
            promotion.run_id,
            promotion.project_id,
            _draft_audit_payload(persisted, promotion.promotion_id),
        )
        return _draft_payload(persisted, promotion_id=promotion.promotion_id)

    async def preflight_draft(
        self,
        *,
        draft_id: str,
        evidence: BuyerProposalPreflightInput,
    ) -> dict[str, Any]:
        """Persist a fresh preflight without mutating the proposal version."""

        draft = await self._require_draft(draft_id)
        if not isinstance(evidence, BuyerProposalPreflightInput):
            raise TypeError("evidence must be a BuyerProposalPreflightInput")
        if evidence.draft.draft_id != draft.draft_id:
            raise BuyerOutreachServiceError("preflight evidence belongs to a different draft version")
        promotion = await self._require_promotion(
            platform=draft.draft.platform,
            run_id=draft.draft.run_id,
            project_id=draft.draft.project_id,
        )
        result = self._composer.preflight(evidence)
        persisted = await self._store.save_preflight(draft.draft_id, result)
        await self._audit(
            "proposal.preflight.completed",
            promotion.run_id,
            promotion.project_id,
            {
                "draft_id": draft.draft_id,
                "context_hash": draft.draft.context_hash,
                "passed": persisted.passed,
                "failures": list(persisted.failures),
                "current_project_hash": persisted.current_project_hash,
            },
        )
        return {
            "draft": _draft_payload(draft, promotion_id=promotion.promotion_id),
            "preflight": persisted.to_payload(),
        }

    async def create_confirmed_send_intent(
        self,
        *,
        draft_id: str,
        sender_account_registration_id: str,
        explicit_confirmation: bool,
        confirmation_id: str,
        confirmed_by: str,
    ) -> dict[str, Any]:
        """Create a pending outbox item after an explicit operator confirmation.

        This method only makes durable state.  It never calls a Kwork client,
        schedules an HTTP task, or transitions the intent into ``sending``.
        """

        if explicit_confirmation is not True:
            raise BuyerOutreachConfirmationRequired("explicit confirmation is required before creating a send intent")
        draft = await self._require_draft(draft_id)
        sender = validate_sender_account_registration_id(sender_account_registration_id)
        if sender != draft.sender_account_registration_id:
            raise BuyerOutreachServiceError("sender account must match the account pinned to the draft")
        promotion = await self._require_promotion(
            platform=draft.draft.platform,
            run_id=draft.draft.run_id,
            project_id=draft.draft.project_id,
        )
        preflight = await self._store.get_preflight(draft.draft_id)
        if preflight is None:
            raise BuyerOutreachServiceError("a fresh passing preflight is required before creating a send intent")
        if not preflight.passed:
            raise BuyerOutreachServiceError("a failing preflight cannot be confirmed for sending")

        proposed = BuyerProposalOutreach.create_intent(draft.draft, sender_account_registration_id=sender)
        intent = await self._store.get_send_intent_by_idempotency_key(proposed.idempotency_key)
        if intent is None:
            pending = BuyerProposalOutreach.finish_preflight(
                BuyerProposalOutreach.start_preflight(proposed),
                preflight.preflight,
            )
            intent = await self._store.create_send_intent(pending)
        self._ensure_intent_matches_draft(intent, draft)
        confirmation = await self._store.record_confirmation(
            BuyerOutreachConfirmation(
                confirmation_id=confirmation_id,
                intent_id=intent.intent_id,
                confirmed_by=confirmed_by,
                confirmed_at=_utc_now(),
            )
        )
        await self._audit(
            "proposal.send.confirmed",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": intent.intent_id,
                "draft_id": draft.draft_id,
                "state": intent.state.value,
                "idempotency_key": intent.idempotency_key,
                "confirmation": confirmation.to_payload(),
            },
        )
        return {
            "draft": _draft_payload(draft, promotion_id=promotion.promotion_id),
            "preflight": preflight.to_payload(),
            "send_intent": intent.to_payload(),
            "confirmation": confirmation.to_payload(),
            "outbox_only": True,
            "auto_send": False,
        }

    async def begin_explicit_delivery(
        self,
        *,
        intent_id: str,
        sender_account_registration_id: str,
        explicit_confirmation: bool,
        confirmation_id: str,
        confirmed_by: str,
    ) -> BuyerOutreachDeliveryAttempt:
        """Durably reserve one pending outbox item for a manual remote request.

        The method never creates a Kwork client or makes a network request.  It
        only turns ``pending_send`` into ``sending`` after an additional,
        immutable operator confirmation has been recorded.  A delivery
        controller must finish this attempt as accepted, failed, or unknown.
        """

        if explicit_confirmation is not True:
            raise BuyerOutreachConfirmationRequired("explicit confirmation is required before delivering a proposal")
        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.PENDING_SEND:
            raise BuyerOutreachServiceError("only a pending send intent can begin explicit delivery")
        draft = await self._require_draft(intent.draft_id)
        self._ensure_intent_matches_draft(intent, draft)
        sender = validate_sender_account_registration_id(sender_account_registration_id)
        if sender != intent.sender_account_registration_id:
            raise BuyerOutreachServiceError("sender account must match the account pinned to the send intent")
        promotion = await self._require_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        confirmation = await self._store.record_confirmation(
            BuyerOutreachConfirmation(
                confirmation_id=confirmation_id,
                intent_id=intent.intent_id,
                confirmed_by=confirmed_by,
                confirmed_at=_utc_now(),
            )
        )
        sending = BuyerProposalOutreach.begin_send(intent, explicit_confirmation=True)
        persisted = await self._store.update_send_intent(sending)
        await self._audit(
            "proposal.delivery.started",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": persisted.intent_id,
                "draft_id": draft.draft_id,
                "state": persisted.state.value,
                "idempotency_key": persisted.idempotency_key,
                "confirmation": confirmation.to_payload(),
            },
        )
        return BuyerOutreachDeliveryAttempt(
            intent=persisted,
            draft=draft,
            promotion=promotion,
            confirmation=confirmation,
        )

    async def mark_delivery_accepted(
        self,
        *,
        intent_id: str,
        remote_receipt: str,
        evidence: BuyerRemoteOfferEvidence | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a stable remote receipt after an explicit delivery request."""

        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.SENDING:
            raise BuyerOutreachServiceError("only an in-flight send intent can be accepted")
        draft = await self._require_draft(intent.draft_id)
        promotion = await self._require_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        normalized_evidence = _coerce_remote_evidence(evidence) if evidence is not None else None
        accepted = BuyerProposalOutreach.mark_accepted(intent, remote_receipt=remote_receipt)
        persisted = await self._store.update_send_intent(accepted)
        await self._audit(
            "proposal.delivery.accepted",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": persisted.intent_id,
                "draft_id": draft.draft_id,
                "state": persisted.state.value,
                "remote_receipt": persisted.remote_receipt,
                "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            },
        )
        return {
            "send_intent": persisted.to_payload(),
            "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            "auto_send": False,
        }

    async def mark_delivery_unknown(
        self,
        *,
        intent_id: str,
        reason: str,
        evidence: BuyerRemoteOfferEvidence | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an indeterminate delivery and require a read-only reconcile."""

        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.SENDING:
            raise BuyerOutreachServiceError("only an in-flight send intent can become unknown")
        draft = await self._require_draft(intent.draft_id)
        promotion = await self._require_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        normalized_evidence = _coerce_remote_evidence(evidence) if evidence is not None else None
        unknown = BuyerProposalOutreach.mark_unknown(intent, reason=reason)
        persisted = await self._store.update_send_intent(unknown)
        await self._audit(
            "proposal.delivery.unknown",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": persisted.intent_id,
                "draft_id": draft.draft_id,
                "state": persisted.state.value,
                "reason": persisted.failure_reason,
                "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            },
        )
        return {
            "send_intent": persisted.to_payload(),
            "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            "auto_send": False,
        }

    async def mark_delivery_failed(
        self,
        *,
        intent_id: str,
        reason: str,
        evidence: BuyerRemoteOfferEvidence | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a definitive remote rejection of an explicit delivery."""

        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.SENDING:
            raise BuyerOutreachServiceError("only an in-flight send intent can fail")
        draft = await self._require_draft(intent.draft_id)
        promotion = await self._require_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        normalized_evidence = _coerce_remote_evidence(evidence) if evidence is not None else None
        failed = BuyerProposalOutreach.mark_failed(intent, reason=reason)
        persisted = await self._store.update_send_intent(failed)
        await self._audit(
            "proposal.delivery.failed",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": persisted.intent_id,
                "draft_id": draft.draft_id,
                "state": persisted.state.value,
                "reason": persisted.failure_reason,
                "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            },
        )
        return {
            "send_intent": persisted.to_payload(),
            "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            "auto_send": False,
        }

    async def reconcile_send_intent(
        self,
        *,
        intent_id: str,
        outcome: BuyerProposalReconciliationOutcome | str,
        remote_receipt: str | None = None,
        reason: str | None = None,
        evidence: BuyerRemoteOfferEvidence | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a declared reconciliation result to an ``unknown`` outbox item."""

        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.UNKNOWN:
            raise BuyerOutreachServiceError("only an unknown send intent can be reconciled")
        draft = await self._require_draft(intent.draft_id)
        promotion = await self._require_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        normalized_evidence = _coerce_remote_evidence(evidence) if evidence is not None else None
        reconciled = BuyerProposalOutreach.reconcile(
            intent,
            outcome=BuyerProposalReconciliationOutcome(outcome),
            remote_receipt=remote_receipt,
            reason=reason,
        )
        persisted = await self._store.update_send_intent(reconciled)
        await self._audit(
            "proposal.send.reconciled",
            promotion.run_id,
            promotion.project_id,
            {
                "intent_id": persisted.intent_id,
                "draft_id": draft.draft_id,
                "outcome": BuyerProposalReconciliationOutcome(outcome).value,
                "state": persisted.state.value,
                "remote_receipt": persisted.remote_receipt,
                "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            },
        )
        return {
            "send_intent": persisted.to_payload(),
            "evidence": normalized_evidence.to_payload() if normalized_evidence is not None else None,
            "auto_send": False,
        }

    async def reconcile_from_gateway(
        self,
        *,
        intent_id: str,
        gateway: BuyerOutreachReconciliationGateway,
    ) -> dict[str, Any]:
        """Read remote evidence and reconcile; the gateway cannot send a proposal."""

        if not isinstance(gateway, BuyerOutreachReconciliationGateway):
            raise TypeError("gateway must implement BuyerOutreachReconciliationGateway")
        intent = await self._require_send_intent(intent_id)
        if intent.state is not BuyerProposalSendState.UNKNOWN:
            raise BuyerOutreachServiceError("only an unknown send intent can be reconciled")
        evidence = _coerce_remote_evidence(await gateway.inspect_send_intent(intent))
        if evidence.has_offer is True and evidence.remote_receipt:
            return await self.reconcile_send_intent(
                intent_id=intent.intent_id,
                outcome=BuyerProposalReconciliationOutcome.ACCEPTED,
                remote_receipt=evidence.remote_receipt,
                evidence=evidence,
            )
        if evidence.has_offer is False:
            return await self.reconcile_send_intent(
                intent_id=intent.intent_id,
                outcome=BuyerProposalReconciliationOutcome.NOT_SENT,
                evidence=evidence,
            )
        reason = "offer evidence is incomplete; manual review required"
        if evidence.has_offer is True:
            reason = "offer evidence lacks a stable remote receipt; manual review required"
        return await self.reconcile_send_intent(
            intent_id=intent.intent_id,
            outcome=BuyerProposalReconciliationOutcome.MANUAL_REVIEW,
            reason=reason,
            evidence=evidence,
        )

    async def _require_promotion(self, *, platform: str, run_id: str, project_id: str) -> BuyerOutreachPromotion:
        promotion = await self._store.get_promotion(
            platform=_required_text(platform, "platform"),
            run_id=_required_text(run_id, "run_id"),
            project_id=_required_text(project_id, "project_id"),
        )
        if promotion is None:
            raise BuyerOutreachNotFoundError(f"outreach promotion not found: {platform}/{run_id}/{project_id}")
        return promotion

    async def _require_draft(self, draft_id: str) -> BuyerComposedProposalDraft:
        draft = await self._store.get_draft(_required_text(draft_id, "draft_id"))
        if draft is None:
            raise BuyerOutreachNotFoundError(f"proposal draft not found: {draft_id}")
        return draft

    async def _require_send_intent(self, intent_id: str) -> BuyerProposalSendIntent:
        intent = await self._store.get_send_intent(_required_text(intent_id, "intent_id"))
        if intent is None:
            raise BuyerOutreachNotFoundError(f"proposal send intent not found: {intent_id}")
        return intent

    async def _audit(self, event_type: str, run_id: str, project_id: str, payload: Mapping[str, Any]) -> None:
        await self._store.append_audit_event(
            event_type=event_type,
            run_id=run_id,
            project_id=project_id,
            payload=dict(payload),
        )

    @staticmethod
    def _ensure_draft_matches_promotion(draft: BuyerComposedProposalDraft, promotion: BuyerOutreachPromotion) -> None:
        if (
            draft.draft.platform != promotion.platform
            or draft.draft.run_id != promotion.run_id
            or draft.draft.project_id != promotion.project_id
            or draft.sender_account_registration_id != promotion.sender_account_registration_id
        ):
            raise BuyerOutreachServiceError("draft persistence returned a record outside the selected outreach promotion")

    @staticmethod
    def _ensure_intent_matches_draft(intent: BuyerProposalSendIntent, draft: BuyerComposedProposalDraft) -> None:
        if (
            intent.draft_id != draft.draft_id
            or intent.project_id != draft.draft.project_id
            or intent.platform != draft.draft.platform
            or intent.sender_account_registration_id != draft.sender_account_registration_id
            or intent.draft_hash != draft.draft.draft_hash
            or intent.context_hash != draft.draft.context_hash
        ):
            raise BuyerOutreachServiceError("send intent does not match its immutable draft or sender account")


def _promotion_id(platform: str, run_id: str, project_id: str) -> str:
    value = f"buyer-outreach-promotion/v1/{platform}/{run_id}/{project_id}"
    return f"buyer-promotion-{uuid5(NAMESPACE_URL, value).hex}"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def _draft_payload(draft: BuyerComposedProposalDraft, *, promotion_id: str) -> dict[str, Any]:
    payload = draft.to_payload()
    payload["promotion_id"] = promotion_id
    return payload


def _draft_audit_payload(draft: BuyerComposedProposalDraft, promotion_id: str) -> dict[str, Any]:
    return {
        "promotion_id": promotion_id,
        "draft_id": draft.draft_id,
        "version": draft.version,
        "parent_draft_id": draft.parent_draft_id,
        "source": draft.source,
        "context_hash": draft.draft.context_hash,
        "draft_hash": draft.draft.draft_hash,
        "prompt_hash": draft.prompt_hash,
        "resolved_provider": draft.resolved_provider,
        "resolved_model": draft.resolved_model,
        "task": draft.task,
        "sender_account_registration_id": draft.sender_account_registration_id,
    }


def _coerce_remote_evidence(value: BuyerRemoteOfferEvidence | Mapping[str, Any]) -> BuyerRemoteOfferEvidence:
    if isinstance(value, BuyerRemoteOfferEvidence):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("evidence must be a BuyerRemoteOfferEvidence or mapping")
    return BuyerRemoteOfferEvidence(
        source=_required_text(value.get("source"), "source"),
        has_offer=value.get("has_offer"),
        remote_receipt=_optional_text(value.get("remote_receipt"), "remote_receipt"),
        detail=value.get("detail") if isinstance(value.get("detail"), Mapping) else {},
    )


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BuyerOutreachServiceError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _normalize_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized = _normalize_json_value(value, name)
    assert isinstance(normalized, dict)
    return normalized


def _normalize_json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError(f"{name} keys must be strings")
            normalized[key] = _normalize_json_value(value[key], name)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item, name) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BuyerOutreachServiceError(f"{name} numbers must be finite")
        return value
    raise TypeError(f"unsupported {name} value type: {type(value).__name__}")


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _hash_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BuyerOutreachConfirmation",
    "BuyerOutreachConfirmationRequired",
    "BuyerOutreachDeliveryAttempt",
    "BuyerOutreachNotFoundError",
    "BuyerOutreachPromotion",
    "BuyerOutreachPromotionConflict",
    "BuyerOutreachReconciliationGateway",
    "BuyerOutreachService",
    "BuyerOutreachServiceError",
    "BuyerOutreachStore",
    "BuyerRemoteOfferEvidence",
]
