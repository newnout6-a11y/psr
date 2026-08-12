from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_outreach import router
from src.platforms.kwork_buyer.outreach import BuyerProposalSendState
from src.platforms.kwork_buyer.outreach_persistence import SQLiteBuyerOutreachStore
from src.platforms.kwork_buyer.outreach_service import BuyerOutreachService, BuyerRemoteOfferEvidence
from src.platforms.kwork_buyer.proposal_composer import BuyerProposalPreflightInput
from src.platforms.kwork_buyer.proposal_delivery import (
    BuyerAccountBoundProposalDeliveryController,
    BuyerProposalDeliveryError,
    BuyerProposalDeliveryFeatureDisabledError,
    BuyerProposalRemoteDeliveryReceipt,
)


class _ProposalGateway:
    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        assert task == "proposal_writing"
        return {
            "body": "I can deliver the requested integration with clear milestones.",
            "provider": "test",
            "model": "test-model",
            "task": task,
        }


class _DeliveryGateway:
    def __init__(
        self,
        *,
        account_registration_id: str = "account-1",
        receipt: BuyerProposalRemoteDeliveryReceipt | None = None,
        delivery_error: Exception | None = None,
        evidence: BuyerRemoteOfferEvidence | None = None,
    ) -> None:
        self.account_registration_id = account_registration_id
        self.receipt = receipt or BuyerProposalRemoteDeliveryReceipt(
            remote_receipt="offer-123",
            detail={"remote_offer_id": "offer-123"},
        )
        self.delivery_error = delivery_error
        self.evidence = evidence or BuyerRemoteOfferEvidence(
            source="account_bound_offer_read",
            has_offer=True,
            remote_receipt="offer-read-123",
            detail={"remote_offer_id": "offer-read-123"},
        )
        self.deliveries: list[dict[str, Any]] = []
        self.inspections: list[str] = []
        self.closed = 0

    async def deliver_proposal(self, **kwargs: Any) -> BuyerProposalRemoteDeliveryReceipt:
        self.deliveries.append(dict(kwargs))
        if self.delivery_error is not None:
            raise self.delivery_error
        return self.receipt

    async def inspect_send_intent(self, intent) -> BuyerRemoteOfferEvidence:
        self.inspections.append(intent.intent_id)
        return self.evidence

    async def close(self) -> None:
        self.closed += 1


class _GatewayFactory:
    def __init__(self, *gateways: _DeliveryGateway) -> None:
        self.gateways = list(gateways)
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, run_id: str, project_id: str, account_id: str) -> _DeliveryGateway:
        self.calls.append((run_id, project_id, account_id))
        if not self.gateways:
            raise AssertionError("unexpected delivery gateway request")
        return self.gateways.pop(0)


async def _pending_outbox(tmp_path: Path) -> tuple[BuyerOutreachService, SQLiteBuyerOutreachStore, str]:
    store = SQLiteBuyerOutreachStore(tmp_path / "buyer-delivery.sqlite3")
    service = BuyerOutreachService(store, _ProposalGateway())
    await service.promote_project(
        run_id="run-1",
        project_id="project-1",
        sender_account_registration_id="account-1",
        project={
            "remote_project_id": "remote-project-1",
            "title": "Telegram integration",
            "description": "Need a scoped integration with a documented handoff.",
        },
        service_profile={"name": "Automation studio", "skills": ["Python", "Telegram"]},
        score={"total": 91},
    )
    draft = await service.generate_draft(
        platform="kwork",
        run_id="run-1",
        project_id="project-1",
        draft_id="draft-1",
        price=25_000,
        delivery_days=4,
    )
    draft_record = await service.get_draft_record(str(draft["draft_id"]))
    await service.preflight_draft(
        draft_id=draft_record.draft_id,
        evidence=BuyerProposalPreflightInput(
            draft=draft_record,
            project_is_active=True,
            current_project={
                "remote_project_id": "remote-project-1",
                "title": "Telegram integration",
                "description": "Need a scoped integration with a documented handoff.",
            },
            has_offer=False,
            already_work=False,
            duplicate_send_intent=False,
            sender_account_eligible=True,
            account_session_valid=True,
            connects_sufficient=True,
            template_valid=True,
            portfolio_requirements_met=True,
            proposal_send_enabled=True,
        ),
    )
    outbox = await service.create_confirmed_send_intent(
        draft_id=draft_record.draft_id,
        sender_account_registration_id="account-1",
        explicit_confirmation=True,
        confirmation_id="outbox-confirmation-1",
        confirmed_by="operator-1",
    )
    return service, store, str(outbox["send_intent"]["intent_id"])


@pytest.mark.asyncio
async def test_explicit_delivery_is_account_bound_idempotent_and_persists_remote_receipt(tmp_path: Path) -> None:
    service, store, intent_id = await _pending_outbox(tmp_path)
    gateway = _DeliveryGateway()
    factory = _GatewayFactory(gateway)
    controller = BuyerAccountBoundProposalDeliveryController(service, factory, delivery_enabled=True)

    delivered = await controller.deliver(
        intent_id=intent_id,
        sender_account_registration_id="account-1",
        delivery_confirmation_id="delivery-confirmation-1",
        confirmed_by="operator-1",
        explicit_delivery_confirmation=True,
    )

    assert delivered["send_intent"]["state"] == BuyerProposalSendState.ACCEPTED.value
    assert delivered["send_intent"]["remote_receipt"] == "offer-123"
    assert delivered["delivery"] == {
        "mode": "explicit_operator_request",
        "delivery_confirmation_id": "delivery-confirmation-1",
        "idempotent_replay": False,
        "remote_request_performed": True,
        "requires_reconciliation": False,
    }
    assert delivered["auto_send"] is False
    assert factory.calls == [("run-1", "project-1", "account-1")]
    assert gateway.deliveries == [
        {
            "remote_project_id": "remote-project-1",
            "proposal_body": "I can deliver the requested integration with clear milestones.",
            "price": 25_000,
            "delivery_days": 4,
            "currency": "RUB",
            "idempotency_key": delivered["send_intent"]["idempotency_key"],
        }
    ]
    assert gateway.closed == 1

    replay = await controller.deliver(
        intent_id=intent_id,
        sender_account_registration_id="account-1",
        delivery_confirmation_id="delivery-confirmation-1",
        confirmed_by="operator-1",
        explicit_delivery_confirmation=True,
    )

    assert replay["send_intent"] == delivered["send_intent"]
    assert replay["delivery"]["idempotent_replay"] is True
    assert replay["delivery"]["remote_request_performed"] is False
    assert len(gateway.deliveries) == 1
    persisted = await store.get_send_intent(intent_id)
    assert persisted is not None
    assert persisted.state is BuyerProposalSendState.ACCEPTED
    events = await store.list_audit_events(run_id="run-1", project_id="project-1")
    assert [event["event_type"] for event in events][-2:] == ["proposal.delivery.started", "proposal.delivery.accepted"]


@pytest.mark.asyncio
async def test_timeout_becomes_unknown_then_requires_explicit_read_only_reconciliation(tmp_path: Path) -> None:
    service, store, intent_id = await _pending_outbox(tmp_path)
    timeout_gateway = _DeliveryGateway(delivery_error=TimeoutError("simulated timeout"))
    reconciliation_gateway = _DeliveryGateway()
    factory = _GatewayFactory(timeout_gateway, reconciliation_gateway)
    controller = BuyerAccountBoundProposalDeliveryController(service, factory, delivery_enabled=True)

    unknown = await controller.deliver(
        intent_id=intent_id,
        sender_account_registration_id="account-1",
        delivery_confirmation_id="delivery-confirmation-timeout",
        confirmed_by="operator-1",
        explicit_delivery_confirmation=True,
    )

    assert unknown["send_intent"]["state"] == BuyerProposalSendState.UNKNOWN.value
    assert unknown["delivery"]["requires_reconciliation"] is True
    assert timeout_gateway.closed == 1

    reconciled = await controller.reconcile_unknown(
        intent_id=intent_id,
        sender_account_registration_id="account-1",
    )

    assert reconciled["send_intent"]["state"] == BuyerProposalSendState.ACCEPTED.value
    assert reconciled["send_intent"]["remote_receipt"] == "offer-read-123"
    assert reconciled["delivery"]["mode"] == "explicit_remote_reconciliation"
    assert reconciliation_gateway.deliveries == []
    assert reconciliation_gateway.inspections == [intent_id]
    assert reconciliation_gateway.closed == 1
    persisted = await store.get_send_intent(intent_id)
    assert persisted is not None
    assert persisted.state is BuyerProposalSendState.ACCEPTED


@pytest.mark.asyncio
async def test_delivery_is_feature_gated_and_rejects_a_different_sender_account(tmp_path: Path) -> None:
    service, _store, intent_id = await _pending_outbox(tmp_path)
    factory = _GatewayFactory(_DeliveryGateway())
    disabled = BuyerAccountBoundProposalDeliveryController(service, factory, delivery_enabled=False)

    with pytest.raises(BuyerProposalDeliveryFeatureDisabledError):
        await disabled.deliver(
            intent_id=intent_id,
            sender_account_registration_id="account-1",
            delivery_confirmation_id="delivery-confirmation-disabled",
            confirmed_by="operator-1",
            explicit_delivery_confirmation=True,
        )
    assert factory.calls == []

    enabled = BuyerAccountBoundProposalDeliveryController(service, factory, delivery_enabled=True)
    with pytest.raises(BuyerProposalDeliveryError, match="pinned"):
        await enabled.deliver(
            intent_id=intent_id,
            sender_account_registration_id="account-2",
            delivery_confirmation_id="delivery-confirmation-wrong-account",
            confirmed_by="operator-1",
            explicit_delivery_confirmation=True,
        )
    assert factory.calls == []


def test_delivery_route_requires_app_state_controller_and_explicit_confirmation(tmp_path: Path) -> None:
    service, _store, intent_id = asyncio.run(_pending_outbox(tmp_path))
    gateway = _DeliveryGateway()
    app = FastAPI()
    app.state.buyer_proposal_delivery = BuyerAccountBoundProposalDeliveryController(
        service,
        _GatewayFactory(gateway),
        delivery_enabled=True,
    )
    app.include_router(router)

    with TestClient(app) as client:
        rejected = client.post(
            f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}/delivery",
            json={
                "sender_account_registration_id": "account-1",
                "confirmed_by": "operator-1",
                "delivery_confirmation_id": "delivery-confirmation-route",
                "explicit_delivery_confirmation": False,
            },
        )
        assert rejected.status_code == 422
        delivered = client.post(
            f"/api/kwork/buyer-search/outreach/send-intents/{intent_id}/delivery",
            json={
                "sender_account_registration_id": "account-1",
                "confirmed_by": "operator-1",
                "delivery_confirmation_id": "delivery-confirmation-route",
                "explicit_delivery_confirmation": True,
            },
        )
        assert delivered.status_code == 200
        assert delivered.json()["send_intent"]["state"] == BuyerProposalSendState.ACCEPTED.value
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/kwork/buyer-search/outreach/send-intents/{intent_id}/delivery" in paths
        assert "/api/kwork/buyer-search/outreach/send-intents/{intent_id}/delivery/reconcile" in paths
