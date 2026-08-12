from __future__ import annotations

from pathlib import Path

import pytest

from src.platforms.kwork_buyer.conversation_persistence import SQLiteBuyerConversationStore
from src.platforms.kwork_buyer.conversation_sync import (
    BuyerConversationReadCapabilities,
    BuyerConversationSyncController,
    BuyerConversationSyncDisabledError,
    BuyerConversationSyncError,
)
from src.platforms.kwork_buyer.conversations import BuyerConversationKey
from src.platforms.kwork_buyer.service import BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadProvenance


class _InboxClient:
    def __init__(self, *, malformed_message: bool = False) -> None:
        self.malformed_message = malformed_message
        self.dialog_calls: list[dict[str, object]] = []
        self.message_calls: list[dict[str, object]] = []
        self.close_calls = 0
        self.send_calls = 0

    async def fetch_inbox_dialogs(self, *, cursor: str | None, limit: int) -> dict[str, object]:
        self.dialog_calls.append({"cursor": cursor, "limit": limit})
        return {
            "dialogs": [
                {
                    "dialog_id": "dialog-1",
                    "username": "buyer-one",
                    "user_id": "buyer-id",
                    "remote_title": "Buyer chat",
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
        messages: list[dict[str, object]] = [
            {
                "id": "message-1",
                "text": "Can you help with the handoff?",
                "sender_id": "buyer-id",
                "created_at": "2026-07-16T11:59:00Z",
            },
            {
                "id": "message-2",
                "text": "Yes, I can help.",
                "direction": "outgoing",
                "created_at": "2026-07-16T12:00:00Z",
            },
        ]
        if self.malformed_message:
            messages.append({"text": "Missing a stable remote message ID"})
        return {"messages": messages}

    async def send_message(self, **_kwargs: object) -> None:
        self.send_calls += 1
        raise AssertionError("conversation sync must not send a remote message")

    async def close(self) -> None:
        self.close_calls += 1


class _ReaderFactory:
    def __init__(self, client: _InboxClient) -> None:
        self.client = client
        self.accounts: list[str] = []

    async def __call__(self, account_registration_id: str) -> BuyerConversationReadCapabilities:
        self.accounts.append(account_registration_id)
        return BuyerConversationReadCapabilities(
            self.client,
            BuyerReadProvenance(
                worker_id="worker-1",
                account_registration_id=account_registration_id,
                transport_id="vpnte-slot-1",
                egress_ip="198.51.100.10",
                source="conversation_sync",
            ),
        )


async def _controller(
    tmp_path: Path,
    *,
    enabled: bool = True,
    malformed_message: bool = False,
) -> tuple[BuyerConversationSyncController, SQLiteBuyerConversationStore, _InboxClient, _ReaderFactory]:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversation-sync.sqlite3")
    await store.initialize()
    client = _InboxClient(malformed_message=malformed_message)
    factory = _ReaderFactory(client)
    controller = BuyerConversationSyncController(
        store,
        reader_factory=factory,
        settings=BuyerSearchSettings(conversation_sync=enabled),
    )
    return controller, store, client, factory


@pytest.mark.asyncio
async def test_sync_persists_full_account_bound_history_atomically(tmp_path: Path) -> None:
    controller, store, client, factory = await _controller(tmp_path)

    result = await controller.sync_account(
        account_registration_id="account-1",
        dialog_limit=10,
        message_limit=20,
    )

    key = BuyerConversationKey(
        platform="kwork",
        account_registration_id="account-1",
        remote_dialog_id="dialog-1",
    )
    conversation = await store.get_conversation(key)
    messages = await store.list_messages(key=key)
    cursor = await store.get_cursor(platform="kwork", account_registration_id="account-1")

    assert result["dialog_count"] == 1
    assert result["message_count"] == 2
    assert result["auto_send"] is False
    assert result["provenance"]["account_registration_id"] == "account-1"
    assert factory.accounts == ["account-1"]
    assert client.dialog_calls == [{"cursor": None, "limit": 10}]
    assert client.message_calls[0]["limit"] == 20
    assert client.close_calls == 1
    assert client.send_calls == 0
    assert conversation is not None
    assert conversation.key.account_registration_id == "account-1"
    assert [message.remote_message_id for message in messages] == ["message-1", "message-2"]
    assert [message.direction.value for message in messages] == ["incoming", "outgoing"]
    assert cursor is not None
    assert cursor.watermark == "2026-07-16T12:00:00Z"


@pytest.mark.asyncio
async def test_sync_rejects_disabled_feature_without_borrowing_a_reader(tmp_path: Path) -> None:
    controller, _store, client, factory = await _controller(tmp_path, enabled=False)

    with pytest.raises(BuyerConversationSyncDisabledError, match="disabled"):
        await controller.sync_account(account_registration_id="account-1")

    assert factory.accounts == []
    assert client.dialog_calls == []
    assert client.close_calls == 0


@pytest.mark.asyncio
async def test_sync_does_not_commit_a_partial_batch_when_history_normalization_fails(tmp_path: Path) -> None:
    controller, store, client, _factory = await _controller(tmp_path, malformed_message=True)

    with pytest.raises(BuyerConversationSyncError, match="remote_message_id"):
        await controller.sync_account(account_registration_id="account-1")

    key = BuyerConversationKey(
        platform="kwork",
        account_registration_id="account-1",
        remote_dialog_id="dialog-1",
    )
    assert await store.get_cursor(platform="kwork", account_registration_id="account-1") is None
    assert await store.get_conversation(key) is None
    assert await store.list_messages(key=key) == ()
    assert client.close_calls == 1

