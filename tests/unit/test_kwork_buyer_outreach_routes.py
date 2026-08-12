from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_outreach import router
from src.platforms.kwork_buyer.outreach import BuyerProposalOutreach
from src.platforms.kwork_buyer.outreach_controller import BuyerOutreachController
from src.platforms.kwork_buyer.outreach_persistence import SQLiteBuyerOutreachStore
from src.platforms.kwork_buyer.outreach_service import BuyerOutreachService
from src.platforms.kwork_buyer.service import BuyerSearchSettings


class _SearchService:
    async def get_project(self, run_id: str, project_id: str) -> dict[str, object]:
        assert (run_id, project_id) == ("run-1", "project-1")
        return {
            "project_id": project_id,
            "remote_project_id": "remote-project-1",
            "title": "Telegram integration",
            "description": "Build a scoped Telegram integration with a handoff.",
            "budget_min": 10_000,
            "budget_max": 20_000,
            "attachments": [],
            "scores": [{"score_kind": "preliminary", "total_score": 82}],
        }


class _ProposalGateway:
    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        assert "CONTEXT_JSON" in prompt
        assert task == "proposal_writing"
        return {
            "body": "I can deliver this integration in clear milestones with a documented handoff.",
            "provider": "test-provider",
            "model": "test-proposal-model",
            "task": task,
        }


async def _verified_preflight(_account_id: str, _run_id: str, _project_id: str, _project: dict[str, object]) -> dict[str, bool]:
    return {
        "project_is_active": True,
        "has_offer": False,
        "already_work": False,
        "sender_account_eligible": True,
        "account_session_valid": True,
        "connects_sufficient": True,
    }


def _controller(tmp_path: Path) -> tuple[BuyerOutreachController, SQLiteBuyerOutreachStore]:
    store = SQLiteBuyerOutreachStore(tmp_path / "buyer-outreach.sqlite3")
    outreach = BuyerOutreachService(store, _ProposalGateway())
    return (
        BuyerOutreachController(
            _SearchService(),
            outreach,
            settings=BuyerSearchSettings(proposal_send=True),
            preflight_evidence_verifier=_verified_preflight,
        ),
        store,
    )


async def _seed_unknown_send_intent(
    controller: BuyerOutreachController,
    store: SQLiteBuyerOutreachStore,
) -> str:
    await controller.promote(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-1",
        service_profile={"name": "Automation studio", "skills": ["Telegram", "Python"]},
    )
    draft = await controller.generate_draft(run_id="run-1", project_id="project-1", price=18_000, delivery_days=7)
    await controller.preflight(
        draft_id=draft["draft_id"],
        evidence={
            "template_valid": True,
            "portfolio_requirements_met": True,
        },
    )
    outbox = await controller.create_confirmed_send_intent(
        draft_id=draft["draft_id"],
        sender_account_registration_id="account-1",
        confirmation_id="confirmation-1",
        confirmed_by="operator",
    )
    intent = outbox["send_intent"]
    durable_intent = await store.get_send_intent(intent["intent_id"])
    assert durable_intent is not None

    # This test only prepares an existing ambiguous outbox record.  The API
    # below reconciles it; it never invokes a remote send operation.
    sending = BuyerProposalOutreach.begin_send(durable_intent, explicit_confirmation=True)
    await store.update_send_intent(sending)
    unknown = BuyerProposalOutreach.mark_unknown(
        sending,
        reason="simulated remote timeout",
    )
    await store.update_send_intent(unknown)
    return unknown.intent_id


def test_reconcile_unknown_send_intent_route_is_durable_and_has_no_send_endpoint(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    intent_id = asyncio.run(_seed_unknown_send_intent(controller, store))
    app = FastAPI()
    app.state.buyer_outreach = controller
    app.include_router(router)

    with TestClient(app) as client:
        reconciled = client.post(
            f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}/reconcile",
            json={
                "outcome": "accepted",
                "remote_receipt": "offer-remote-1",
                "evidence": {
                    "source": "operator_review",
                    "has_offer": True,
                    "remote_receipt": "offer-remote-1",
                    "detail": {"checked_at": "2026-07-16T12:00:00Z"},
                },
            },
        )
        assert reconciled.status_code == 200
        payload = reconciled.json()
        assert payload["send_intent"]["state"] == "accepted"
        assert payload["send_intent"]["remote_receipt"] == "offer-remote-1"
        assert payload["evidence"]["source"] == "operator_review"
        assert payload["auto_send"] is False

        persisted = client.get(f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}")
        assert persisted.status_code == 200
        assert persisted.json()["state"] == "accepted"

        repeated = client.post(
            f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}/reconcile",
            json={"outcome": "failed", "reason": "must not overwrite resolved state"},
        )
        assert repeated.status_code == 422

        send_attempt = client.post(f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}/send")
        assert send_attempt.status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/kwork/buyer-search/outreach/send-intents/{intent_id}/send" not in paths
