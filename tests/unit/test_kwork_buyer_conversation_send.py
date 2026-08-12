from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_conversations import router
from src.platforms.kwork_buyer.conversation_persistence import SQLiteBuyerConversationStore
from src.platforms.kwork_buyer.conversation_send import (
    BuyerConversationRemoteSendReceipt,
    BuyerConversationSendCapabilities,
    BuyerConversationSendCapabilityError,
    BuyerConversationSendController,
    BuyerConversationSendDisabledError,
    BuyerConversationSendProvenance,
)
from src.platforms.kwork_buyer.conversations import (
    BuyerConversationContext,
    BuyerConversationKey,
    create_outgoing_draft,
)


def _key(account_registration_id: str = "account-1") -> BuyerConversationKey:
    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id="dialog-101",
    )


async def _seed_draft(
    store: SQLiteBuyerConversationStore,
    key: BuyerConversationKey,
    *,
    draft_id: str = "reply-draft-1",
) -> None:
    await store.save_draft(
        create_outgoing_draft(
            draft_id=draft_id,
            key=key,
            sender_account_registration_id=key.account_registration_id,
            body="I can start tomorrow and will share the first milestone on Friday.",
            context=BuyerConversationContext(project_id="project-7", buyer_remote_user_id="buyer-9"),
            source="operator",
        )
    )


class _Sender:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []
        self.close_calls = 0

    async def send_conversation_message(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> object:
        self.calls.append(
            {
                "remote_dialog_id": remote_dialog_id,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def close(self) -> None:
        self.close_calls += 1


class _SenderFactory:
    def __init__(self, sender: _Sender, *, capability_account: str | None = None) -> None:
        self.sender = sender
        self.capability_account = capability_account
        self.accounts: list[str] = []

    async def __call__(self, account_registration_id: str) -> BuyerConversationSendCapabilities:
        self.accounts.append(account_registration_id)
        return BuyerConversationSendCapabilities(
            self.sender,
            BuyerConversationSendProvenance(
                worker_id="worker-send-1",
                account_registration_id=self.capability_account or account_registration_id,
                transport_id="vpnte-slot-1",
                egress_ip="198.51.100.22",
            ),
        )


def _controller(
    tmp_path: Path,
    *,
    outcome: object,
    enabled: bool = True,
    capability_account: str | None = None,
) -> tuple[SQLiteBuyerConversationStore, BuyerConversationSendController, _Sender, _SenderFactory]:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    sender = _Sender(outcome)
    factory = _SenderFactory(sender, capability_account=capability_account)
    return store, BuyerConversationSendController(store, sender_factory=factory, enabled=enabled), sender, factory


def test_explicit_account_bound_send_persists_receipt_audit_and_outgoing_message(tmp_path: Path) -> None:
    store, controller, sender, factory = _controller(
        tmp_path,
        outcome={"accepted": True, "message_id": "remote-message-7", "receipt": "request-7"},
    )
    key = _key()
    asyncio.run(_seed_draft(store, key))

    result = asyncio.run(
        controller.send_draft(
            key=key,
            draft_id="reply-draft-1",
            requested_by="operator-1",
            command_id="send-command-1",
            explicit_operator_command=True,
        )
    )

    assert result["auto_send"] is False
    assert result["idempotent"] is False
    assert result["send_intent"]["state"] == "sent"
    assert result["send_intent"]["remote_message_id"] == "remote-message-7"
    assert factory.accounts == ["account-1"]
    assert sender.calls[0]["remote_dialog_id"] == "dialog-101"
    assert sender.calls[0]["body"].startswith("I can start tomorrow")
    assert sender.close_calls == 1

    repeated = asyncio.run(
        controller.send_draft(
            key=key,
            draft_id="reply-draft-1",
            requested_by="operator-2",
            command_id="send-command-retry",
            explicit_operator_command=True,
        )
    )
    assert repeated["idempotent"] is True
    assert len(sender.calls) == 1

    messages = asyncio.run(store.list_messages(key=key))
    assert messages[-1].remote_message_id == "remote-message-7"
    assert messages[-1].sender_account_registration_id == "account-1"
    audits = asyncio.run(store.list_send_audit_events(key=key, intent_id=result["send_intent"]["intent_id"]))
    assert [event["event_type"] for event in audits] == [
        "conversation.send.requested",
        "conversation.send.dispatch_started",
        "conversation.send.sent",
    ]
    assert audits[1]["payload"]["provenance"]["account_registration_id"] == "account-1"


def test_ambiguous_transport_outcome_never_retries_and_can_be_reconciled(tmp_path: Path) -> None:
    store, controller, sender, _factory = _controller(tmp_path, outcome=TimeoutError("timed out"))
    key = _key()
    asyncio.run(_seed_draft(store, key))

    unknown = asyncio.run(
        controller.send_draft(
            key=key,
            draft_id="reply-draft-1",
            requested_by="operator-1",
            command_id="send-command-1",
            explicit_operator_command=True,
        )
    )
    assert unknown["send_intent"]["state"] == "unknown"
    assert unknown["requires_reconciliation"] is True
    assert len(sender.calls) == 1

    repeated = asyncio.run(
        controller.send_draft(
            key=key,
            draft_id="reply-draft-1",
            requested_by="operator-1",
            command_id="send-command-2",
            explicit_operator_command=True,
        )
    )
    assert repeated["idempotent"] is True
    assert len(sender.calls) == 1

    reconciled = asyncio.run(
        controller.reconcile_send_intent(
            key=key,
            intent_id=unknown["send_intent"]["intent_id"],
            requested_by="operator-1",
            command_id="reconcile-command-1",
            explicit_operator_command=True,
            receipt=BuyerConversationRemoteSendReceipt(
                accepted=True,
                remote_message_id="remote-message-8",
                remote_receipt="receipt-8",
            ),
        )
    )
    assert reconciled["send_intent"]["state"] == "sent"
    assert reconciled["requires_reconciliation"] is False
    assert [message.remote_message_id for message in asyncio.run(store.list_messages(key=key))] == ["remote-message-8"]


def test_sender_scope_mismatch_is_closed_before_any_remote_send(tmp_path: Path) -> None:
    store, controller, sender, factory = _controller(
        tmp_path,
        outcome={"accepted": True, "message_id": "remote-message-7"},
        capability_account="account-2",
    )
    key = _key()
    asyncio.run(_seed_draft(store, key))

    with pytest.raises(BuyerConversationSendCapabilityError):
        asyncio.run(
            controller.send_draft(
                key=key,
                draft_id="reply-draft-1",
                requested_by="operator-1",
                command_id="send-command-1",
                explicit_operator_command=True,
            )
        )

    assert factory.accounts == ["account-1"]
    assert sender.calls == []
    assert sender.close_calls == 1
    intent = asyncio.run(store.get_send_intent_by_draft(key=key, draft_id="reply-draft-1"))
    assert intent is not None
    assert intent.state.value == "pending"


def test_send_route_requires_explicit_confirmation_and_reconciles_account_scope(tmp_path: Path) -> None:
    store, controller, sender, _factory = _controller(
        tmp_path,
        outcome={"accepted": None, "receipt": "gateway-ack-without-message-id"},
    )
    key = _key()
    asyncio.run(_seed_draft(store, key))
    app = FastAPI()
    app.state.buyer_conversations = store
    app.state.buyer_conversation_send = controller
    app.include_router(router)
    base = "/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101"

    with TestClient(app) as client:
        rejected = client.post(
            f"{base}/drafts/reply-draft-1/send",
            json={"requested_by": "operator-1", "command_id": "send-command-1", "confirm_send": False},
        )
        assert rejected.status_code == 422
        assert sender.calls == []

        unknown = client.post(
            f"{base}/drafts/reply-draft-1/send",
            json={"requested_by": "operator-1", "command_id": "send-command-1", "confirm_send": True},
        )
        assert unknown.status_code == 200
        assert unknown.json()["send_intent"]["state"] == "unknown"
        intent_id = unknown.json()["send_intent"]["intent_id"]

        reconciled = client.post(
            f"{base}/send-intents/{intent_id}/reconcile",
            json={
                "requested_by": "operator-1",
                "command_id": "reconcile-command-1",
                "confirm_reconciliation": True,
                "remote_message_id": "remote-message-9",
                "remote_receipt": "receipt-9",
            },
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["send_intent"]["state"] == "sent"

        audit = client.get(f"{base}/send-intents/{intent_id}/audit")
        assert audit.status_code == 200
        assert [event["event_type"] for event in audit.json()["items"]] == [
            "conversation.send.requested",
            "conversation.send.dispatch_started",
            "conversation.send.unknown",
            "conversation.send.reconciled_sent",
        ]

        wrong_account = client.get(
            f"/api/kwork/buyer-search/accounts/account-2/conversations/dialog-101/send-intents/{intent_id}"
        )
        assert wrong_account.status_code == 404

    assert len(sender.calls) == 1


def test_disabled_controller_does_not_construct_a_sender(tmp_path: Path) -> None:
    store, controller, sender, factory = _controller(
        tmp_path,
        outcome={"accepted": True, "message_id": "remote-message-7"},
        enabled=False,
    )
    key = _key()
    asyncio.run(_seed_draft(store, key))

    with pytest.raises(BuyerConversationSendDisabledError):
        asyncio.run(
            controller.send_draft(
                key=key,
                draft_id="reply-draft-1",
                requested_by="operator-1",
                command_id="send-command-1",
                explicit_operator_command=True,
            )
        )
    assert factory.accounts == []
    assert sender.calls == []
