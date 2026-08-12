from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_conversations import router
from src.platforms.kwork_buyer.conversation_copilot import BuyerConversationCopilotController
from src.platforms.kwork_buyer.conversation_persistence import SQLiteBuyerConversationStore
from src.platforms.kwork_buyer.conversation_sync import (
    BuyerConversationReadCapabilities,
    BuyerConversationSyncController,
)
from src.platforms.kwork_buyer.conversations import (
    BuyerConversation,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationKey,
    build_conversation_sync_batch,
    normalize_incoming_message,
)
from src.platforms.kwork_buyer.service import BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadProvenance


def _key(account_registration_id: str) -> BuyerConversationKey:
    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id="dialog-101",
    )


def _batch(key: BuyerConversationKey, *, body: str, cursor: str):
    context = BuyerConversationContext(project_id="project-7", buyer_remote_user_id="buyer-9")
    conversation = BuyerConversation(
        key=key,
        context=context,
        remote_title=f"{key.account_registration_id} buyer chat",
        last_remote_message_at="2026-07-16T12:00:00Z",
    )
    message = normalize_incoming_message(
        {
            "id": "message-1",
            "text": body,
            "sender_id": "buyer-9",
            "created_at": "2026-07-16T12:00:00Z",
        },
        key=key,
        context=context,
    )
    return build_conversation_sync_batch(
        cursor=BuyerConversationCursor.for_conversation(
            key,
            cursor=cursor,
            watermark="2026-07-16T12:00:00Z",
            updated_at="2026-07-16T12:01:00Z",
        ),
        conversations=[conversation],
        messages=[message],
    )


def _client(tmp_path: Path) -> tuple[TestClient, SQLiteBuyerConversationStore]:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    app = FastAPI()
    app.state.buyer_conversations = store
    app.include_router(router)
    return TestClient(app), store


class _CopilotGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        self.calls.append({"prompt": prompt, "task": task})
        return {
            "body": "I can confirm the scope and ask one focused follow-up question.",
            "provider": "test-provider",
            "model": "test-conversation-model",
            "task": task,
        }


def _copilot_client(tmp_path: Path) -> tuple[TestClient, SQLiteBuyerConversationStore, _CopilotGateway]:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    gateway = _CopilotGateway()
    app = FastAPI()
    app.state.buyer_conversations = store
    app.state.buyer_conversation_copilot = BuyerConversationCopilotController(store, gateway)
    app.include_router(router)
    return TestClient(app), store, gateway


def _copilot_batch(key: BuyerConversationKey):
    context = BuyerConversationContext(buyer_remote_user_id="buyer-9")
    conversation = BuyerConversation(
        key=key,
        context=context,
        remote_title="Buyer chat",
        last_remote_message_at="2026-07-16T12:00:00Z",
    )
    message = normalize_incoming_message(
        {
            "id": "message-copilot-1",
            "text": "Could you confirm what the handoff includes?",
            "sender_id": "buyer-9",
            "created_at": "2026-07-16T12:00:00Z",
        },
        key=key,
        context=context,
    )
    return build_conversation_sync_batch(
        cursor=BuyerConversationCursor.for_conversation(key, cursor="copilot-page"),
        conversations=[conversation],
        messages=[message],
    )


class _SyncInboxClient:
    def __init__(self) -> None:
        self.dialog_calls: list[dict[str, object]] = []
        self.message_calls: list[dict[str, object]] = []
        self.close_calls = 0
        self.send_calls = 0

    async def fetch_inbox_dialogs(self, *, cursor: str | None, limit: int) -> dict[str, object]:
        self.dialog_calls.append({"cursor": cursor, "limit": limit})
        return {
            "dialogs": [
                {
                    "dialog_id": "dialog-sync-1",
                    "username": "buyer-one",
                    "user_id": "buyer-1",
                    "remote_title": "Sync buyer",
                    "last_message_at": "2026-07-16T12:00:00Z",
                }
            ],
            "watermark": "2026-07-16T12:00:00Z",
        }

    async def fetch_inbox_messages(
        self,
        *,
        dialog: dict[str, object],
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        self.message_calls.append({"dialog": dialog, "cursor": cursor, "limit": limit})
        return {
            "messages": [
                {
                    "id": "sync-message-1",
                    "text": "Please confirm the next step.",
                    "sender_id": "buyer-1",
                    "created_at": "2026-07-16T12:00:00Z",
                }
            ]
        }

    async def send_message(self) -> None:
        self.send_calls += 1
        raise AssertionError("sync route must not send a message")

    async def close(self) -> None:
        self.close_calls += 1


class _SyncReaderFactory:
    def __init__(self, client: _SyncInboxClient) -> None:
        self.client = client
        self.accounts: list[str] = []

    async def __call__(self, account_registration_id: str) -> BuyerConversationReadCapabilities:
        self.accounts.append(account_registration_id)
        return BuyerConversationReadCapabilities(
            self.client,
            BuyerReadProvenance(
                worker_id="worker-sync-1",
                account_registration_id=account_registration_id,
                transport_id="vpnte-slot-1",
                egress_ip="198.51.100.10",
                source="conversation_sync",
            ),
        )


def _sync_client(
    tmp_path: Path,
    *,
    enabled: bool = True,
) -> tuple[TestClient, SQLiteBuyerConversationStore, _SyncInboxClient, _SyncReaderFactory]:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    inbox_client = _SyncInboxClient()
    factory = _SyncReaderFactory(inbox_client)
    app = FastAPI()
    app.state.buyer_conversations = store
    app.state.buyer_conversation_sync = BuyerConversationSyncController(
        store,
        reader_factory=factory,
        settings=BuyerSearchSettings(conversation_sync=enabled),
    )
    app.include_router(router)
    return TestClient(app), store, inbox_client, factory


def test_account_scoped_cursor_conversation_and_message_routes(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    first = _key("account-1")
    second = _key("account-2")
    asyncio.run(store.commit_sync_batch(_batch(first, body="First account message", cursor="account-1-page")))
    asyncio.run(store.commit_sync_batch(_batch(second, body="Second account message", cursor="account-2-page")))

    with client:
        cursor = client.get("/api/kwork/buyer-search/accounts/account-1/conversation-cursor")
        assert cursor.status_code == 200
        assert cursor.json()["cursor"]["cursor"] == "account-1-page"

        listed = client.get("/api/kwork/buyer-search/accounts/account-1/conversations")
        assert listed.status_code == 200
        assert [item["conversation_id"] for item in listed.json()["items"]] == [first.conversation_id]

        detail = client.get("/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101")
        assert detail.status_code == 200
        assert detail.json()["account_registration_id"] == "account-1"

        messages = client.get("/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101/messages")
        assert messages.status_code == 200
        assert messages.json()["items"][0]["body"] == "First account message"

        message = client.get("/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101/messages/message-1")
        assert message.status_code == 200
        assert message.json()["account_registration_id"] == "account-1"

        missing_other_scope = client.get("/api/kwork/buyer-search/accounts/account-3/conversations/dialog-101")
        assert missing_other_scope.status_code == 404


def test_account_scoped_sync_route_uses_the_injected_read_only_controller(tmp_path: Path) -> None:
    client, store, inbox_client, factory = _sync_client(tmp_path)
    base = "/api/kwork/buyer-search/accounts/account-1/conversations"

    with client:
        synced = client.post(f"{base}/sync", json={"dialog_limit": 7, "message_limit": 12})
        assert synced.status_code == 200
        payload = synced.json()
        assert payload["dialog_count"] == 1
        assert payload["message_count"] == 1
        assert payload["auto_send"] is False
        assert payload["provenance"]["account_registration_id"] == "account-1"

        messages = client.get(f"{base}/dialog-sync-1/messages")
        assert messages.status_code == 200
        assert messages.json()["items"][0]["body"] == "Please confirm the next step."

        send_attempt = client.post(f"{base}/dialog-sync-1/messages", json={"body": "Must remain unavailable"})
        assert send_attempt.status_code == 405

    assert factory.accounts == ["account-1"]
    assert inbox_client.dialog_calls == [{"cursor": None, "limit": 7}]
    assert inbox_client.message_calls[0]["limit"] == 12
    assert inbox_client.close_calls == 1
    assert inbox_client.send_calls == 0
    assert asyncio.run(store.get_cursor(platform="kwork", account_registration_id="account-1")) is not None


def test_sync_route_returns_conflict_while_feature_is_disabled(tmp_path: Path) -> None:
    client, _store, inbox_client, factory = _sync_client(tmp_path, enabled=False)

    with client:
        response = client.post("/api/kwork/buyer-search/accounts/account-1/conversations/sync")

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
    assert factory.accounts == []
    assert inbox_client.dialog_calls == []


def test_sync_route_requires_the_controller(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)

    with client:
        response = client.post("/api/kwork/buyer-search/accounts/account-1/conversations/sync")

    assert response.status_code == 503


def test_account_scoped_draft_routes_are_immutable_and_never_send(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    key = _key("account-1")
    asyncio.run(store.commit_sync_batch(_batch(key, body="Can you start tomorrow?", cursor="page-1")))

    draft_payload = {
        "draft_id": "reply-draft-1",
        "body": "Yes, I can start tomorrow.",
        "context": {"project_id": "project-7", "metadata": {"origin": "operator"}},
    }
    base = "/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101"
    with client:
        created = client.post(f"{base}/drafts", json=draft_payload)
        assert created.status_code == 201
        assert created.json()["state"] == "draft"
        assert created.json()["sender_account_registration_id"] == "account-1"

        listed = client.get(f"{base}/drafts")
        assert listed.status_code == 200
        assert [item["draft_id"] for item in listed.json()["items"]] == ["reply-draft-1"]

        detail = client.get(f"{base}/drafts/reply-draft-1")
        assert detail.status_code == 200
        assert detail.json()["body"] == draft_payload["body"]

        conflicting = client.post(f"{base}/drafts", json={**draft_payload, "body": "Changed immutable reply."})
        assert conflicting.status_code == 409

        wrong_account = client.get(
            "/api/kwork/buyer-search/accounts/account-2/conversations/dialog-101/drafts/reply-draft-1"
        )
        assert wrong_account.status_code == 404

        send_attempt = client.post(f"{base}/messages", json={"body": "This must not send."})
        assert send_attempt.status_code == 405


def test_copilot_reply_draft_route_is_idempotent_account_scoped_and_never_sends(tmp_path: Path) -> None:
    client, store, gateway = _copilot_client(tmp_path)
    key = _key("account-1")
    asyncio.run(store.commit_sync_batch(_copilot_batch(key)))
    base = "/api/kwork/buyer-search/accounts/account-1/conversations/dialog-101"
    payload = {
        "draft_id": "copilot-reply-1",
        "operator_instruction": "Keep it concise and ask one clarifying question.",
    }

    with client:
        created = client.post(f"{base}/reply-drafts", json=payload)
        assert created.status_code == 201
        draft_payload = created.json()
        assert draft_payload["draft"]["state"] == "draft"
        assert draft_payload["draft"]["source"] == "ai_copilot"
        assert draft_payload["draft"]["sender_account_registration_id"] == "account-1"
        assert draft_payload["task"] == "conversation_reply"
        assert draft_payload["outbox_only"] is True
        assert draft_payload["auto_send"] is False
        assert gateway.calls[0]["task"] == "conversation_reply"

        repeated = client.post(f"{base}/reply-drafts", json=payload)
        assert repeated.status_code == 201
        assert repeated.json()["prompt_hash"] == draft_payload["prompt_hash"]
        assert len(gateway.calls) == 1

        stored = client.get(f"{base}/drafts/copilot-reply-1")
        assert stored.status_code == 200
        assert stored.json()["source"] == "ai_copilot"

        messages = client.get(f"{base}/messages")
        assert messages.status_code == 200
        assert [message["remote_message_id"] for message in messages.json()["items"]] == ["message-copilot-1"]

        other_account = client.post(
            "/api/kwork/buyer-search/accounts/account-2/conversations/dialog-101/reply-drafts",
            json={"draft_id": "copilot-reply-2"},
        )
        assert other_account.status_code == 404
        assert len(gateway.calls) == 1

        send_attempt = client.post(f"{base}/messages", json={"body": "This must not send."})
        assert send_attempt.status_code == 405


def test_conversation_routes_require_the_store(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/kwork/buyer-search/accounts/account-1/conversations")

    assert response.status_code == 503
