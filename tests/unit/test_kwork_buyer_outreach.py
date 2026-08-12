from __future__ import annotations

import pytest

from src.platforms.kwork_buyer import (
    BuyerProposalConfirmationRequired,
    BuyerProposalDraft,
    BuyerProposalOutreach,
    BuyerProposalPreflight,
    BuyerProposalReconciliationOutcome,
    BuyerProposalSendState,
    BuyerProposalTransitionError,
    proposal_idempotency_key,
    validate_sender_account_registration_id,
)


def draft(*, context: dict[str, object] | None = None, body: str = "I can implement this.") -> BuyerProposalDraft:
    return BuyerProposalDraft(
        draft_id="draft-1",
        run_id="run-1",
        project_id="project-1",
        body=body,
        price=2_500,
        delivery_days=3,
        context=context or {"project": {"title": "Telegram bot"}, "attachments": []},
        model_alias="gpt-5.6-sol",
    )


def passing_preflight() -> BuyerProposalPreflight:
    return BuyerProposalPreflight(passed=True, checks={"project_active": True, "has_offer": True})


def ready_intent():
    intent = BuyerProposalOutreach.create_intent(draft(), sender_account_registration_id="account-42")
    intent = BuyerProposalOutreach.start_preflight(intent)
    return BuyerProposalOutreach.finish_preflight(intent, passing_preflight())


def test_draft_hashes_are_deterministic_and_context_is_detached_from_caller_mutation() -> None:
    context = {"project": {"title": "Telegram bot"}}
    first = draft(context=context)
    second = draft(context={"project": {"title": "Telegram bot"}})
    changed_body = draft(context={"project": {"title": "Telegram bot"}}, body="Different proposal")

    context["project"]["title"] = "changed"  # type: ignore[index]

    assert first.context_hash == second.context_hash
    assert first.draft_hash == second.draft_hash
    assert first.draft_hash != changed_body.draft_hash
    assert first.context["project"]["title"] == "Telegram bot"  # type: ignore[index]


def test_sender_account_is_explicit_and_part_of_idempotency_key() -> None:
    proposal = draft()
    first = BuyerProposalOutreach.create_intent(proposal, sender_account_registration_id="account-a")
    second = BuyerProposalOutreach.create_intent(proposal, sender_account_registration_id="account-a")
    third = BuyerProposalOutreach.create_intent(proposal, sender_account_registration_id="account-b")

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key != third.idempotency_key
    assert first.idempotency_key == proposal_idempotency_key(
        platform="kwork",
        project_id="project-1",
        sender_account_registration_id="account-a",
        draft_hash=proposal.draft_hash,
    )
    with pytest.raises(ValueError):
        validate_sender_account_registration_id(" ")
    with pytest.raises(ValueError):
        validate_sender_account_registration_id("global session")


def test_explicit_confirmation_is_required_before_the_only_send_transition() -> None:
    intent = ready_intent()

    assert intent.state is BuyerProposalSendState.PENDING_SEND
    with pytest.raises(BuyerProposalConfirmationRequired):
        BuyerProposalOutreach.begin_send(intent)

    sending = BuyerProposalOutreach.begin_send(intent, explicit_confirmation=True)
    assert sending.state is BuyerProposalSendState.SENDING
    assert sending.send_attempts == 1


def test_happy_path_reaches_accepted_with_remote_receipt() -> None:
    accepted = BuyerProposalOutreach.mark_accepted(
        BuyerProposalOutreach.begin_send(ready_intent(), explicit_confirmation=True),
        remote_receipt="offer-123",
    )

    assert accepted.state is BuyerProposalSendState.ACCEPTED
    assert accepted.remote_receipt == "offer-123"
    assert accepted.to_payload()["draft_hash"] == draft().draft_hash


def test_preflight_failure_is_terminal_and_cannot_send() -> None:
    intent = BuyerProposalOutreach.start_preflight(
        BuyerProposalOutreach.create_intent(draft(), sender_account_registration_id="account-42")
    )
    failed = BuyerProposalOutreach.finish_preflight(
        intent,
        BuyerProposalPreflight(passed=False, checks={"has_offer": False}, failures=("has_offer",)),
    )

    assert failed.state is BuyerProposalSendState.FAILED
    with pytest.raises(BuyerProposalTransitionError):
        BuyerProposalOutreach.begin_send(failed, explicit_confirmation=True)


def test_unknown_outcome_requires_reconciliation_and_never_blind_retries() -> None:
    unknown = BuyerProposalOutreach.mark_unknown(
        BuyerProposalOutreach.begin_send(ready_intent(), explicit_confirmation=True),
        reason="timeout after request",
    )

    assert unknown.state is BuyerProposalSendState.UNKNOWN
    with pytest.raises(BuyerProposalTransitionError):
        BuyerProposalOutreach.begin_send(unknown, explicit_confirmation=True)

    not_sent = BuyerProposalOutreach.reconcile(unknown, outcome=BuyerProposalReconciliationOutcome.NOT_SENT)
    assert not_sent.state is BuyerProposalSendState.PENDING_SEND
    assert BuyerProposalOutreach.begin_send(not_sent, explicit_confirmation=True).send_attempts == 2


def test_reconciliation_can_accept_or_fail_a_timeout() -> None:
    unknown = BuyerProposalOutreach.mark_unknown(
        BuyerProposalOutreach.begin_send(ready_intent(), explicit_confirmation=True),
        reason="timeout after request",
    )

    accepted = BuyerProposalOutreach.reconcile(
        unknown,
        outcome=BuyerProposalReconciliationOutcome.ACCEPTED,
        remote_receipt="offer-123",
    )
    failed = BuyerProposalOutreach.reconcile(
        unknown,
        outcome=BuyerProposalReconciliationOutcome.FAILED,
        reason="remote rejected",
    )

    assert accepted.state is BuyerProposalSendState.ACCEPTED
    assert failed.state is BuyerProposalSendState.FAILED
