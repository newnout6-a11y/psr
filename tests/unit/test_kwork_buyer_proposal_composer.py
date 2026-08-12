from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.proposal_composer import (
    BuyerProposalComposeRequest,
    BuyerProposalComposer,
    BuyerProposalComposerError,
    BuyerProposalPreflightInput,
    LLMRouterBuyerProposalGateway,
    PROPOSAL_WRITING_TASK,
    build_buyer_proposal_context,
    evaluate_buyer_proposal_preflight,
    revise_buyer_proposal_draft,
)
from src.platforms.kwork_buyer.attachments import build_attachment_context, parse_attachment


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str):
        self.calls.append({"prompt": prompt, "task": task})
        return {
            "body": "I can deliver the Telegram bot integration in three days.",
            "provider": "configured-provider",
            "model": "configured-strongest-alias",
            "task": task,
        }


def context():
    attachment = parse_attachment(b"token=should-not-leak\nRelevant API contract", filename="brief.txt")
    return build_buyer_proposal_context(
        run_id="run-1",
        project_id="project-1",
        project={"title": "Telegram bot", "description": "Need an integration"},
        service_profile={"skills": ["Python", "Telegram"]},
        attachment_context=build_attachment_context((attachment,)),
        score={"total": 87.5},
        prompt_version="buyer-proposal-v7",
    )


@pytest.mark.asyncio
async def test_composer_records_task_specific_resolved_route_and_attachment_context() -> None:
    gateway = FakeGateway()
    manifest = context()
    result = await BuyerProposalComposer(gateway).compose(
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=manifest,
            sender_account_registration_id="account-7",
            price=25_000,
            delivery_days=3,
            currency="rub",
        )
    )

    assert gateway.calls[0]["task"] == PROPOSAL_WRITING_TASK
    assert "should-not-leak" not in gateway.calls[0]["prompt"]
    assert result.resolved_provider == "configured-provider"
    assert result.resolved_model == "configured-strongest-alias"
    assert result.task == PROPOSAL_WRITING_TASK
    assert result.currency == "RUB"
    assert result.draft.context_hash == manifest.context_hash
    assert result.to_payload()["context_manifest"]["context_hash"] == manifest.context_hash


@pytest.mark.asyncio
async def test_manual_revision_is_versioned_and_keeps_generated_text() -> None:
    generated = await BuyerProposalComposer(FakeGateway()).compose(
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=context(),
            sender_account_registration_id="account-7",
            price=25_000,
            delivery_days=3,
        )
    )

    revised = revise_buyer_proposal_draft(
        generated,
        draft_id="draft-2",
        body="Edited proposal with a concrete implementation plan.",
        price=27_000,
        delivery_days=4,
    )

    assert revised.version == generated.version + 1
    assert revised.parent_draft_id == generated.draft_id
    assert revised.source == "manual"
    assert revised.generated_body == generated.draft.body
    assert revised.draft.body != revised.generated_body
    preserved_terms = revise_buyer_proposal_draft(
        generated,
        draft_id="draft-3",
        body="Only wording changed.",
    )
    assert preserved_terms.terms == generated.terms


@pytest.mark.asyncio
async def test_preflight_exposes_stale_duplicate_and_feature_gate_diagnostics() -> None:
    draft = await BuyerProposalComposer(FakeGateway()).compose(
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=context(),
            sender_account_registration_id="account-7",
            price=25_000,
            delivery_days=3,
        )
    )
    result = evaluate_buyer_proposal_preflight(
        BuyerProposalPreflightInput(
            draft=draft,
            project_is_active=True,
            current_project={"title": "Telegram bot", "description": "Changed after generation"},
            has_offer=False,
            already_work=False,
            duplicate_send_intent=True,
            sender_account_eligible=True,
            account_session_valid=True,
            connects_sufficient=True,
            template_valid=True,
            portfolio_requirements_met=True,
            proposal_send_enabled=False,
            outgoing_attachment_count=1,
            attachment_upload_enabled=False,
            attachment_upload_capability_verified=False,
        )
    )

    assert not result.passed
    assert result.is_stale_project
    assert {"stale_project_context", "duplicate_send_intent", "proposal_send_feature_enabled", "attachment_upload_capability"} <= set(result.failures)


@pytest.mark.asyncio
async def test_preflight_passes_only_with_fresh_project_and_all_positive_evidence() -> None:
    draft = await BuyerProposalComposer(FakeGateway()).compose(
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=context(),
            sender_account_registration_id="account-7",
            price=25_000,
            delivery_days=3,
        )
    )
    result = evaluate_buyer_proposal_preflight(
        BuyerProposalPreflightInput(
            draft=draft,
            project_is_active=True,
            current_project={"title": "Telegram bot", "description": "Need an integration"},
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
    )

    assert result.passed
    assert result.failures == ()
    assert result.preflight.passed


def test_invalid_terms_and_implicit_sender_are_rejected() -> None:
    with pytest.raises(BuyerProposalComposerError):
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=context(),
            sender_account_registration_id="account 7",
            price=0,
            delivery_days=3,
        )
    with pytest.raises(BuyerProposalComposerError):
        BuyerProposalComposeRequest(
            draft_id="draft-1",
            context=context(),
            sender_account_registration_id="account-7",
            price=25_000,
            delivery_days=0,
        )


@pytest.mark.asyncio
async def test_router_adapter_uses_task_routing_without_passing_a_model() -> None:
    class Router:
        def __init__(self) -> None:
            self.kwargs: dict[str, str] = {}

        async def generate(self, prompt: str, **kwargs: str) -> str:
            self.kwargs = {"prompt": prompt, **kwargs}
            return "Proposal body"

        def get_last_route(self) -> dict[str, str]:
            return {"provider": "openai", "model": "env-selected-model", "task": PROPOSAL_WRITING_TASK}

    router = Router()
    response = await LLMRouterBuyerProposalGateway(router).generate(prompt="prompt", task=PROPOSAL_WRITING_TASK)

    assert router.kwargs["task"] == PROPOSAL_WRITING_TASK
    assert "model" not in router.kwargs
    assert response.model == "env-selected-model"
