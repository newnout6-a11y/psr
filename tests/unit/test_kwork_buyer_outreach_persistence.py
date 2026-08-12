from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.platforms.kwork_buyer.attachments import parse_attachment
from src.platforms.kwork_buyer.outreach import BuyerProposalOutreach, BuyerProposalSendState
from src.platforms.kwork_buyer.outreach_persistence import (
    BuyerOutreachPersistenceConflictError,
    BuyerOutreachPersistenceNotFoundError,
    SQLiteBuyerOutreachStore,
)
from src.platforms.kwork_buyer.outreach_service import (
    BuyerOutreachConfirmation,
    BuyerOutreachPromotionConflict,
    BuyerOutreachService,
    BuyerOutreachStore,
)
from src.platforms.kwork_buyer.proposal_composer import (
    BuyerProposalComposer,
    BuyerProposalPreflightInput,
)


class DeterministicProposalGateway:
    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        assert "CONTEXT_JSON:" in prompt
        return {
            "body": "I can deliver the requested automation with a clear implementation plan.",
            "provider": "test-provider",
            "model": "test-proposal-model",
            "task": task,
        }


def _service(db_path: Path) -> tuple[BuyerOutreachService, SQLiteBuyerOutreachStore]:
    store = SQLiteBuyerOutreachStore(db_path)
    return BuyerOutreachService(store, BuyerProposalComposer(DeterministicProposalGateway())), store


async def _promote_and_generate(
    service: BuyerOutreachService,
    *,
    draft_id: str = "draft-1",
) -> tuple[dict[str, object], dict[str, object]]:
    promotion = await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-7",
        project={"title": "Telegram automation", "description": "Need a durable API integration"},
        service_profile={"skills": ["Python", "Telegram"]},
        score={"total": 88.5, "confidence": 0.92},
        additional_context={"operator_note": "Use the existing webhook pattern"},
        attachment_results=(parse_attachment(b"Contract requirements", filename="brief.txt"),),
    )
    generated = await service.generate_draft(
        platform="kwork",
        run_id="run-1",
        project_id="project-1",
        draft_id=draft_id,
        price=25_000,
        delivery_days=3,
    )
    return promotion, generated


def _passing_evidence(draft) -> BuyerProposalPreflightInput:
    return BuyerProposalPreflightInput(
        draft=draft,
        project_is_active=True,
        current_project={"title": "Telegram automation", "description": "Need a durable API integration"},
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
async def test_sqlite_outreach_store_round_trips_account_bound_outbox_and_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "buyer-outreach.sqlite3"
    service, store = _service(db_path)
    assert isinstance(store, BuyerOutreachStore)

    promotion, generated = await _promote_and_generate(service)
    draft = await store.get_draft(str(generated["draft_id"]))
    assert draft is not None
    assert draft.to_payload()["context_hash"] == generated["context_hash"]

    preflight = await service.preflight_draft(draft_id=draft.draft_id, evidence=_passing_evidence(draft))
    confirmed = await service.create_confirmed_send_intent(
        draft_id=draft.draft_id,
        sender_account_registration_id="account-7",
        explicit_confirmation=True,
        confirmation_id="confirmation-1",
        confirmed_by="operator@example.test",
    )

    reopened = SQLiteBuyerOutreachStore(db_path)
    restored_promotion = await reopened.get_promotion(platform="kwork", run_id="run-1", project_id="project-1")
    restored_draft = await reopened.get_draft(draft.draft_id)
    restored_preflight = await reopened.get_preflight(draft.draft_id)
    restored_intent = await reopened.get_send_intent(str(confirmed["send_intent"]["intent_id"]))
    confirmations = await reopened.list_confirmations(intent_id=str(confirmed["send_intent"]["intent_id"]))
    events = await reopened.list_audit_events(run_id="run-1", project_id="project-1")

    assert restored_promotion is not None
    assert restored_promotion.to_payload() == promotion
    assert restored_promotion.attachment_results[0].text == "Contract requirements"
    assert restored_draft is not None
    assert restored_draft.to_payload()["draft_hash"] == generated["draft_hash"]
    assert restored_preflight is not None
    assert restored_preflight.to_payload() == preflight["preflight"]
    assert restored_intent is not None
    assert restored_intent.state is BuyerProposalSendState.PENDING_SEND
    assert [item.confirmation_id for item in confirmations] == ["confirmation-1"]
    assert [event["event_type"] for event in events] == [
        "proposal.project.promoted",
        "proposal.draft.created",
        "proposal.preflight.completed",
        "proposal.send.confirmed",
    ]
    assert {event["sender_account_registration_id"] for event in events} == {"account-7"}
    assert events[-1]["payload"]["idempotency_key"] == restored_intent.idempotency_key


@pytest.mark.asyncio
async def test_sqlite_outreach_store_keeps_promotions_and_draft_versions_immutable(tmp_path: Path) -> None:
    service, store = _service(tmp_path / "buyer-outreach.sqlite3")
    first_promotion, generated = await _promote_and_generate(service)

    repeated = await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-7",
        project={"title": "Changed project"},
        service_profile={"skills": ["Other"]},
    )
    assert repeated["promotion_id"] == first_promotion["promotion_id"]
    assert repeated["project"]["title"] == "Telegram automation"

    with pytest.raises(BuyerOutreachPromotionConflict):
        await service.promote_project(
            run_id="run-1",
            project_id="project-1",
            sender_account_registration_id="account-9",
            project={"title": "Changed project"},
            service_profile={"skills": ["Other"]},
        )

    first = await store.get_draft(str(generated["draft_id"]))
    assert first is not None
    colliding_base = replace(first.draft, draft_id="draft-collision")
    colliding_version = replace(first, draft=colliding_base)
    with pytest.raises(BuyerOutreachPersistenceConflictError):
        await store.create_draft(colliding_version)

    revised = await service.edit_draft(
        draft_id=first.draft_id,
        revised_draft_id="draft-2",
        body="Edited text retains the original snapshot and adds delivery milestones.",
        price=27_000,
        delivery_days=4,
    )
    drafts = await store.list_drafts(promotion_id=str(first_promotion["promotion_id"]))
    assert [item.draft_id for item in drafts] == [first.draft_id, "draft-2"]
    assert [item.version for item in drafts] == [1, 2]
    assert drafts[0].draft.draft_hash == first.draft.draft_hash
    assert drafts[1].draft.draft_hash == revised["draft_hash"]


@pytest.mark.asyncio
async def test_sqlite_outreach_store_enforces_outbox_transition_and_confirmation_identity(tmp_path: Path) -> None:
    service, store = _service(tmp_path / "buyer-outreach.sqlite3")
    _promotion, generated = await _promote_and_generate(service)
    draft = await store.get_draft(str(generated["draft_id"]))
    assert draft is not None
    await service.preflight_draft(draft_id=draft.draft_id, evidence=_passing_evidence(draft))
    created = await service.create_confirmed_send_intent(
        draft_id=draft.draft_id,
        sender_account_registration_id="account-7",
        explicit_confirmation=True,
        confirmation_id="confirmation-1",
        confirmed_by="operator@example.test",
    )
    pending = await store.get_send_intent(str(created["send_intent"]["intent_id"]))
    assert pending is not None

    sending = BuyerProposalOutreach.begin_send(pending, explicit_confirmation=True)
    persisted = await store.update_send_intent(sending)
    assert persisted.state is BuyerProposalSendState.SENDING
    with pytest.raises(BuyerOutreachPersistenceConflictError):
        await store.update_send_intent(pending)

    confirmation = BuyerOutreachConfirmation(
        confirmation_id="confirmation-2",
        intent_id=pending.intent_id,
        confirmed_by="operator@example.test",
        confirmed_at="2026-07-16T00:00:00Z",
    )
    assert await store.record_confirmation(confirmation) == confirmation
    assert await store.record_confirmation(confirmation) == confirmation
    with pytest.raises(BuyerOutreachPersistenceConflictError):
        await store.record_confirmation(replace(confirmation, confirmed_by="different@example.test"))
    with pytest.raises(BuyerOutreachPersistenceNotFoundError):
        await store.append_audit_event(
            event_type="proposal.invalid",
            run_id="run-unknown",
            project_id="project-unknown",
            payload={"reason": "no promotion"},
        )
