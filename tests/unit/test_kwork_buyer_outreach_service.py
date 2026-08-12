from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from src.platforms.kwork_buyer.attachments import parse_attachment
from src.platforms.kwork_buyer.outreach import (
    BuyerProposalOutreach,
    BuyerProposalSendIntent,
    BuyerProposalSendState,
)
from src.platforms.kwork_buyer.outreach_service import (
    BuyerOutreachConfirmation,
    BuyerOutreachConfirmationRequired,
    BuyerOutreachPromotion,
    BuyerOutreachReconciliationGateway,
    BuyerOutreachService,
    BuyerRemoteOfferEvidence,
)
from src.platforms.kwork_buyer.proposal_composer import (
    BuyerComposedProposalDraft,
    BuyerProposalComposer,
    BuyerProposalPreflightInput,
    BuyerProposalPreflightResult,
)


class MemoryOutreachStore:
    def __init__(self) -> None:
        self.promotions: dict[tuple[str, str, str], BuyerOutreachPromotion] = {}
        self.drafts: dict[str, BuyerComposedProposalDraft] = {}
        self.preflights: dict[str, BuyerProposalPreflightResult] = {}
        self.intents: dict[str, BuyerProposalSendIntent] = {}
        self.confirmations: dict[str, BuyerOutreachConfirmation] = {}
        self.events: list[dict[str, Any]] = []

    async def get_promotion(self, *, platform: str, run_id: str, project_id: str) -> BuyerOutreachPromotion | None:
        return self.promotions.get((platform, run_id, project_id))

    async def create_promotion(self, promotion: BuyerOutreachPromotion) -> BuyerOutreachPromotion:
        return self.promotions.setdefault((promotion.platform, promotion.run_id, promotion.project_id), promotion)

    async def get_draft(self, draft_id: str) -> BuyerComposedProposalDraft | None:
        return self.drafts.get(draft_id)

    async def list_drafts(self, *, promotion_id: str) -> Sequence[BuyerComposedProposalDraft]:
        promotion = next(
            (candidate for candidate in self.promotions.values() if candidate.promotion_id == promotion_id),
            None,
        )
        if promotion is None:
            return ()
        drafts = [
            draft
            for draft in self.drafts.values()
            if draft.draft.platform == promotion.platform
            and draft.draft.run_id == promotion.run_id
            and draft.draft.project_id == promotion.project_id
        ]
        return sorted(drafts, key=lambda item: item.version)

    async def create_draft(self, draft: BuyerComposedProposalDraft) -> BuyerComposedProposalDraft:
        existing = self.drafts.get(draft.draft_id)
        if existing is not None:
            return existing
        self.drafts[draft.draft_id] = draft
        return draft

    async def get_preflight(self, draft_id: str) -> BuyerProposalPreflightResult | None:
        return self.preflights.get(draft_id)

    async def save_preflight(self, draft_id: str, result: BuyerProposalPreflightResult) -> BuyerProposalPreflightResult:
        self.preflights[draft_id] = result
        return result

    async def get_send_intent(self, intent_id: str) -> BuyerProposalSendIntent | None:
        return self.intents.get(intent_id)

    async def get_send_intent_by_idempotency_key(self, idempotency_key: str) -> BuyerProposalSendIntent | None:
        return next((intent for intent in self.intents.values() if intent.idempotency_key == idempotency_key), None)

    async def create_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        existing = await self.get_send_intent_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            return existing
        self.intents[intent.intent_id] = intent
        return intent

    async def update_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        self.intents[intent.intent_id] = intent
        return intent

    async def record_confirmation(self, confirmation: BuyerOutreachConfirmation) -> BuyerOutreachConfirmation:
        return self.confirmations.setdefault(confirmation.confirmation_id, confirmation)

    async def get_confirmation(self, confirmation_id: str) -> BuyerOutreachConfirmation | None:
        return self.confirmations.get(confirmation_id)

    async def append_audit_event(
        self,
        *,
        event_type: str,
        run_id: str,
        project_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.events.append({"event_type": event_type, "run_id": run_id, "project_id": project_id, "payload": dict(payload)})


class FakeProposalGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        self.calls.append({"prompt": prompt, "task": task})
        return {
            "body": "I can implement the integration with a clear delivery plan.",
            "provider": "configured-provider",
            "model": "configured-proposal-model",
            "task": task,
        }


class FakeReconciliationGateway:
    def __init__(self, evidence: BuyerRemoteOfferEvidence) -> None:
        self.evidence = evidence
        self.inspected: list[str] = []

    async def inspect_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerRemoteOfferEvidence:
        self.inspected.append(intent.intent_id)
        return self.evidence


def _service() -> tuple[BuyerOutreachService, MemoryOutreachStore, FakeProposalGateway]:
    store = MemoryOutreachStore()
    gateway = FakeProposalGateway()
    return BuyerOutreachService(store, BuyerProposalComposer(gateway)), store, gateway


async def _promote_and_generate(
    service: BuyerOutreachService,
    store: MemoryOutreachStore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    promotion = await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-7",
        project={"title": "Telegram automation", "description": "Need a robust API integration"},
        service_profile={"skills": ["Python", "Telegram"]},
        score={"total": 88.5},
        attachment_results=(parse_attachment(b"Relevant integration contract", filename="brief.txt"),),
    )
    generated = await service.generate_draft(
        platform="kwork",
        run_id="run-1",
        project_id="project-1",
        draft_id="draft-generated",
        price=25_000,
        delivery_days=3,
    )
    assert store.drafts[generated["draft_id"]].draft.context_hash == generated["context_hash"]
    return promotion, generated


def _passing_evidence(draft: BuyerComposedProposalDraft) -> BuyerProposalPreflightInput:
    return BuyerProposalPreflightInput(
        draft=draft,
        project_is_active=True,
        current_project={"title": "Telegram automation", "description": "Need a robust API integration"},
        has_offer=False,
        already_work=False,
        duplicate_send_intent=False,
        sender_account_eligible=True,
        account_session_valid=True,
        connects_sufficient=True,
        template_valid=True,
        portfolio_requirements_met=True,
        proposal_send_enabled=True,
    )


@pytest.mark.asyncio
async def test_promotion_is_idempotent_and_retains_the_first_attachment_aware_snapshot() -> None:
    service, store, _gateway = _service()
    first = await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-7",
        project={"title": "Original project"},
        service_profile={"skills": ["Python"]},
        attachment_results=(parse_attachment(b"Original brief", filename="brief.txt"),),
    )
    second = await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-7",
        project={"title": "Changed after promotion"},
        service_profile={"skills": ["Other"]},
    )

    assert first["promotion_id"] == second["promotion_id"]
    assert second["project"]["title"] == "Original project"
    assert second["attachment_count"] == 1
    assert len(store.promotions) == 1
    assert [event["event_type"] for event in store.events] == ["proposal.project.promoted"]


@pytest.mark.asyncio
async def test_generated_and_manual_drafts_are_versioned_and_auditable() -> None:
    service, store, gateway = _service()
    promotion, generated = await _promote_and_generate(service, store)
    repeated = await service.generate_draft(
        platform="kwork",
        run_id="run-1",
        project_id="project-1",
        draft_id="draft-generated",
        price=25_000,
        delivery_days=3,
    )
    edited = await service.edit_draft(
        draft_id="draft-generated",
        revised_draft_id="draft-edited",
        body="Edited copy with the agreed delivery milestones.",
        price=27_000,
        delivery_days=4,
    )

    assert generated["promotion_id"] == promotion["promotion_id"]
    assert generated["resolved_model"] == "configured-proposal-model"
    assert generated["context_hash"]
    assert repeated == generated
    assert len(gateway.calls) == 1
    assert edited["version"] == 2
    assert edited["parent_draft_id"] == "draft-generated"
    assert edited["source"] == "manual"
    assert edited["generated_body"] == generated["body"]
    assert edited["body"] != edited["generated_body"]
    assert [event["event_type"] for event in store.events][-2:] == ["proposal.draft.created", "proposal.draft.edited"]


@pytest.mark.asyncio
async def test_preflight_is_persisted_with_the_current_context_hash() -> None:
    service, store, _gateway = _service()
    _promotion, generated = await _promote_and_generate(service, store)

    result = await service.preflight_draft(
        draft_id=generated["draft_id"],
        evidence=_passing_evidence(store.drafts[generated["draft_id"]]),
    )

    assert result["preflight"]["passed"] is True
    assert result["draft"]["context_hash"] == generated["context_hash"]
    assert store.preflights[generated["draft_id"]].passed
    assert store.events[-1]["event_type"] == "proposal.preflight.completed"


@pytest.mark.asyncio
async def test_send_intent_needs_confirmation_and_only_creates_a_pending_outbox_record() -> None:
    service, store, _gateway = _service()
    _promotion, generated = await _promote_and_generate(service, store)
    await service.preflight_draft(
        draft_id=generated["draft_id"],
        evidence=_passing_evidence(store.drafts[generated["draft_id"]]),
    )

    with pytest.raises(BuyerOutreachConfirmationRequired):
        await service.create_confirmed_send_intent(
            draft_id=generated["draft_id"],
            sender_account_registration_id="account-7",
            explicit_confirmation=False,
            confirmation_id="confirm-1",
            confirmed_by="operator-1",
        )

    first = await service.create_confirmed_send_intent(
        draft_id=generated["draft_id"],
        sender_account_registration_id="account-7",
        explicit_confirmation=True,
        confirmation_id="confirm-1",
        confirmed_by="operator-1",
    )
    second = await service.create_confirmed_send_intent(
        draft_id=generated["draft_id"],
        sender_account_registration_id="account-7",
        explicit_confirmation=True,
        confirmation_id="confirm-2",
        confirmed_by="operator-1",
    )

    assert first["auto_send"] is False
    assert first["outbox_only"] is True
    assert first["send_intent"]["state"] == BuyerProposalSendState.PENDING_SEND.value
    assert first["send_intent"]["intent_id"] == second["send_intent"]["intent_id"]
    assert len(store.intents) == 1
    assert len(store.confirmations) == 2


@pytest.mark.asyncio
async def test_unknown_intent_is_reconciled_from_read_only_gateway_evidence() -> None:
    service, store, _gateway = _service()
    _promotion, generated = await _promote_and_generate(service, store)
    await service.preflight_draft(
        draft_id=generated["draft_id"],
        evidence=_passing_evidence(store.drafts[generated["draft_id"]]),
    )
    created = await service.create_confirmed_send_intent(
        draft_id=generated["draft_id"],
        sender_account_registration_id="account-7",
        explicit_confirmation=True,
        confirmation_id="confirm-1",
        confirmed_by="operator-1",
    )
    pending = store.intents[created["send_intent"]["intent_id"]]
    unknown = BuyerProposalOutreach.mark_unknown(
        BuyerProposalOutreach.begin_send(pending, explicit_confirmation=True),
        reason="timeout after remote request",
    )
    store.intents[unknown.intent_id] = unknown
    gateway = FakeReconciliationGateway(
        BuyerRemoteOfferEvidence(
            source="offers-read-api",
            has_offer=True,
            remote_receipt="offer-123",
            detail={"remote_offer_id": "offer-123"},
        )
    )

    assert isinstance(gateway, BuyerOutreachReconciliationGateway)
    result = await service.reconcile_from_gateway(intent_id=unknown.intent_id, gateway=gateway)

    assert gateway.inspected == [unknown.intent_id]
    assert result["send_intent"]["state"] == BuyerProposalSendState.ACCEPTED.value
    assert result["send_intent"]["remote_receipt"] == "offer-123"
    assert result["auto_send"] is False
