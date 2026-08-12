from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from src.platforms.kwork_buyer.conversation_persistence import (
    BuyerConversationPersistenceConflictError,
    SQLiteBuyerConversationStore,
)
from src.platforms.kwork_buyer.conversations import (
    BuyerConversation,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationKey,
    build_conversation_sync_batch,
    create_outgoing_draft,
    normalize_incoming_message,
)


def _key(account_registration_id: str = "account-1") -> BuyerConversationKey:
    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id="dialog-101",
    )


def _batch(
    key: BuyerConversationKey,
    *,
    cursor: str,
    revision: int = 0,
    remote_message_id: str = "message-1",
    body: str = "Can you help with the project?",
):
    context = BuyerConversationContext(
        project_id="project-7",
        proposal_intent_id="proposal-intent-1",
        buyer_remote_user_id="buyer-9",
        metadata={"origin": "inbox-sync"},
    )
    conversation = BuyerConversation(
        key=key,
        context=context,
        remote_title="Buyer chat",
        last_remote_message_at="2026-07-16T12:00:00Z",
        synced_at="2026-07-16T12:01:00Z",
    )
    message = normalize_incoming_message(
        {
            "id": remote_message_id,
            "text": body,
            "sender_id": "buyer-9",
            "created_at": "2026-07-16T12:00:00Z",
            "received_at": "2026-07-16T12:01:00Z",
            "attachments": [
                {
                    "id": "attachment-1",
                    "name": "brief.pdf",
                    "type": "application/pdf",
                    "url": "https://files.example.test/brief.pdf",
                    "page_count": 3,
                }
            ],
        },
        key=key,
        context=context,
    )
    return build_conversation_sync_batch(
        cursor=BuyerConversationCursor.for_conversation(
            key,
            cursor=cursor,
            watermark="2026-07-16T12:00:00Z",
            revision=revision,
            updated_at="2026-07-16T12:01:00Z",
        ),
        conversations=[conversation],
        messages=[message],
    )


@pytest.mark.asyncio
async def test_sqlite_conversation_store_commits_idempotent_batch_and_draft(tmp_path: Path) -> None:
    db_path = tmp_path / "buyer-conversations.sqlite3"
    store = SQLiteBuyerConversationStore(db_path)
    key = _key()
    batch = _batch(key, cursor="page-1")

    assert await store.commit_sync_batch(batch) == batch
    assert await store.commit_sync_batch(batch) == batch

    reopened = SQLiteBuyerConversationStore(db_path)
    cursor = await reopened.get_cursor(platform="kwork", account_registration_id="account-1")
    conversation = await reopened.get_conversation(key)
    messages = await reopened.list_messages(key=key)

    assert cursor == batch.cursor
    assert conversation is not None
    assert conversation.to_payload() == batch.conversations[0].to_payload()
    assert [message.to_payload() for message in messages] == [batch.messages[0].to_payload()]
    assert messages[0].attachments[0].to_payload() == batch.messages[0].attachments[0].to_payload()

    draft = create_outgoing_draft(
        draft_id="reply-draft-1",
        key=key,
        sender_account_registration_id="account-1",
        body="I can deliver the first implementation in three days.",
        context=batch.conversations[0].context,
        created_at="2026-07-16T12:02:00Z",
    )
    assert await store.save_draft(draft) == draft
    assert await reopened.save_draft(draft) == draft
    assert await reopened.get_draft(key=key, draft_id=draft.draft_id) == draft
    assert await reopened.list_drafts(key=key) == (draft,)

    updated_message = replace(batch.messages[0], attachments=())
    updated_batch = build_conversation_sync_batch(
        cursor=BuyerConversationCursor.for_conversation(
            key,
            cursor="page-2",
            watermark="2026-07-16T12:02:00Z",
            revision=1,
            updated_at="2026-07-16T12:03:00Z",
        ),
        conversations=batch.conversations,
        messages=[updated_message],
    )
    assert await reopened.commit_sync_batch(updated_batch) == updated_batch
    assert (await reopened.get_message(key=key, remote_message_id="message-1")).attachments == ()  # type: ignore[union-attr]

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM buyer_conversation_cursors").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM buyer_conversations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM buyer_conversation_messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM buyer_conversation_attachments").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM buyer_conversation_drafts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_sqlite_conversation_store_keeps_same_remote_dialog_isolated_by_account(tmp_path: Path) -> None:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    first_key = _key("account-1")
    second_key = _key("account-2")
    first_batch = _batch(first_key, cursor="account-1-page", body="First account's buyer message")
    second_batch = _batch(second_key, cursor="account-2-page", body="Second account's buyer message")

    await store.commit_sync_batch(first_batch)
    await store.commit_sync_batch(second_batch)

    assert first_key.remote_dialog_id == second_key.remote_dialog_id
    assert first_key.conversation_id != second_key.conversation_id
    assert (await store.get_cursor(platform="kwork", account_registration_id="account-1")) == first_batch.cursor
    assert (await store.get_cursor(platform="kwork", account_registration_id="account-2")) == second_batch.cursor
    assert [item.conversation_id for item in await store.list_conversations(
        platform="kwork", account_registration_id="account-1"
    )] == [first_key.conversation_id]
    assert [item.conversation_id for item in await store.list_conversations(
        platform="kwork", account_registration_id="account-2"
    )] == [second_key.conversation_id]
    assert (await store.get_message(key=first_key, remote_message_id="message-1")).body == "First account's buyer message"  # type: ignore[union-attr]
    assert (await store.get_message(key=second_key, remote_message_id="message-1")).body == "Second account's buyer message"  # type: ignore[union-attr]

    draft = create_outgoing_draft(
        draft_id="account-1-draft",
        key=first_key,
        sender_account_registration_id="account-1",
        body="A private account-one draft.",
    )
    await store.save_draft(draft)
    assert await store.get_draft(key=second_key, draft_id=draft.draft_id) is None


@pytest.mark.asyncio
async def test_sqlite_conversation_store_rejects_ambiguous_cursor_and_mutable_draft_id(tmp_path: Path) -> None:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    key = _key()
    await store.commit_sync_batch(_batch(key, cursor="page-1"))

    conflicting = _batch(
        key,
        cursor="different-page-with-same-revision",
        remote_message_id="must-not-persist",
        body="This must be rolled back with the cursor conflict.",
    )
    with pytest.raises(BuyerConversationPersistenceConflictError, match="equally-versioned"):
        await store.commit_sync_batch(conflicting)
    assert await store.get_message(key=key, remote_message_id="must-not-persist") is None

    draft = create_outgoing_draft(
        draft_id="reply-draft-1",
        key=key,
        sender_account_registration_id="account-1",
        body="First durable editor version.",
    )
    assert await store.save_draft(draft) == draft
    changed = replace(draft, body="An unsafe in-place overwrite.")
    with pytest.raises(BuyerConversationPersistenceConflictError, match="different immutable"):
        await store.save_draft(changed)
    assert await store.get_draft(key=key, draft_id=draft.draft_id) == draft
