from __future__ import annotations

from typing import Any

import pytest

from src.platforms.kwork_buyer.outreach_controller import BuyerOutreachController
from src.platforms.kwork_buyer.outreach_persistence import SQLiteBuyerOutreachStore
from src.platforms.kwork_buyer.outreach_service import BuyerOutreachService
from src.platforms.kwork_buyer.service import BuyerSearchSettings


class _SearchService:
    def __init__(self) -> None:
        self.project: dict[str, Any] = {
            "project_id": "project-1",
            "remote_project_id": "remote-1",
            "title": "Telegram bot",
            "description": "Need an integration bot with a simple dashboard.",
            "budget_min": 10_000,
            "budget_max": 20_000,
            "attachments": [],
            "scores": [{"score_kind": "preliminary", "total_score": 82}],
        }

    async def get_project(self, run_id: str, project_id: str) -> dict[str, Any]:
        assert (run_id, project_id) == ("run-1", "project-1")
        return dict(self.project)


class _Gateway:
    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        assert "CONTEXT_JSON" in prompt
        assert task == "proposal_writing"
        return {
            "body": "I can deliver the bot in clear milestones with a tested handoff.",
            "provider": "test",
            "model": "gpt-5.6-sol",
            "task": task,
        }


async def _verified_preflight(_account_id: str, _run_id: str, _project_id: str, _project: dict[str, Any]) -> dict[str, bool]:
    return {
        "project_is_active": True,
        "has_offer": False,
        "already_work": False,
        "sender_account_eligible": True,
        "account_session_valid": True,
        "connects_sufficient": True,
    }


@pytest.mark.asyncio
async def test_controller_promotes_generates_preflights_and_creates_outbox_only(tmp_path) -> None:
    store = SQLiteBuyerOutreachStore(tmp_path / "outreach.sqlite3")
    outreach = BuyerOutreachService(store, _Gateway())
    controller = BuyerOutreachController(
        _SearchService(),
        outreach,
        settings=BuyerSearchSettings(proposal_send=True, proposal_attachments=False),
        preflight_evidence_verifier=_verified_preflight,
    )

    promoted = await controller.promote(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-1",
        service_profile={"name": "Automation studio", "skills": ["Telegram", "Python"]},
    )
    assert promoted["sender_account_registration_id"] == "account-1"

    draft = await controller.generate_draft(run_id="run-1", project_id="project-1", price=18_000, delivery_days=7)
    assert draft["resolved_model"] == "gpt-5.6-sol"

    preflight = await controller.preflight(
        draft_id=draft["draft_id"],
        evidence={
            "template_valid": True,
            "portfolio_requirements_met": True,
        },
    )
    assert preflight["preflight"]["passed"] is True

    outbox = await controller.create_confirmed_send_intent(
        draft_id=draft["draft_id"],
        sender_account_registration_id="account-1",
        confirmation_id="confirm-1",
        confirmed_by="operator",
    )
    assert outbox["outbox_only"] is True
    assert outbox["auto_send"] is False
    assert outbox["send_intent"]["state"] == "pending_send"


@pytest.mark.asyncio
async def test_browser_claimed_account_evidence_cannot_pass_preflight_without_verifier(tmp_path) -> None:
    store = SQLiteBuyerOutreachStore(tmp_path / "outreach-unverified.sqlite3")
    outreach = BuyerOutreachService(store, _Gateway())
    controller = BuyerOutreachController(
        _SearchService(),
        outreach,
        settings=BuyerSearchSettings(proposal_send=True),
    )
    await controller.promote(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-1",
        service_profile={"name": "Automation studio"},
    )
    draft = await controller.generate_draft(run_id="run-1", project_id="project-1")

    result = await controller.preflight(
        draft_id=draft["draft_id"],
        evidence={
            "project_is_active": True,
            "has_offer": False,
            "already_work": False,
            "sender_account_eligible": True,
            "account_session_valid": True,
            "connects_sufficient": True,
            "template_valid": True,
            "portfolio_requirements_met": True,
        },
    )

    assert result["preflight"]["passed"] is False
    assert "sender_account_eligible" in result["preflight"]["failures"]
    assert "connects_sufficient" in result["preflight"]["failures"]
