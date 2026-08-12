from __future__ import annotations

from types import MappingProxyType

import pytest

from src.platforms.kwork_buyer.conversations import (
    BuyerConversation,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationDeliveryState,
    BuyerConversationDraft,
    BuyerConversationError,
    BuyerConversationKey,
    BuyerConversationMessageDirection,
    advance_account_cursor,
    build_conversation_sync_batch,
    create_outgoing_draft,
    merge_account_cursors,
    normalize_incoming_message,
    normalize_outgoing_message,
)


def conversation_key(account: str = "account-1") -> BuyerConversationKey:
    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account,
        remote_dialog_id="dialog-101",
    )


def test_conversation_identity_includes_sender_account_and_sync_batch_rejects_cross_account_data() -> None:
    first_key = conversation_key("account-1")
    second_key = conversation_key("account-2")

    assert first_key.conversation_id != second_key.conversation_id

    cursor = BuyerConversationCursor.for_conversation(first_key, cursor="page-1")
    wrong_account_message = normalize_incoming_message(
        key=second_key,
        remote_message_id="message-1",
        body="Hello",
    )

    with pytest.raises(BuyerConversationError, match="sync cursor account"):
        build_conversation_sync_batch(cursor=cursor, messages=[wrong_account_message])


def test_cursors_are_account_scoped_and_only_merge_same_scope() -> None:
    key = conversation_key()
    current = BuyerConversationCursor.for_conversation(key, cursor="opaque-1", watermark="2026-07-16T12:00:00Z")
    advanced = advance_account_cursor(current, cursor="opaque-2", updated_at="2026-07-16T12:01:00Z")

    assert advanced.account_scope == key.account_scope
    assert advanced.revision == 1
    assert advanced.watermark == "2026-07-16T12:00:00Z"
    assert merge_account_cursors(current, advanced) is advanced

    other_account = BuyerConversationCursor.for_conversation(conversation_key("account-2"), cursor="opaque-2")
    with pytest.raises(BuyerConversationError, match="different account scopes"):
        merge_account_cursors(advanced, other_account)


def test_incoming_and_outgoing_messages_are_normalized_immutable_and_account_pinned() -> None:
    key = conversation_key()
    context = BuyerConversationContext(
        project_id="project-7",
        proposal_draft_id="draft-1",
        proposal_intent_id="intent-1",
        metadata={"matched_queries": ["bot"]},
    )
    incoming = normalize_incoming_message(
        {
            "id": "remote-1",
            "text": "  Hello,\r\n\r\n  can you help?  ",
            "sender_id": "buyer-9",
            "status": "received",
            "files": [{"id": "file-1", "name": "brief.pdf", "type": "APPLICATION/PDF", "size": 4}],
        },
        key=key,
        context=context,
    )
    outgoing = normalize_outgoing_message(
        {
            "id": "remote-2",
            "body": "  Yes, I can help.  ",
            "state": "sent",
        },
        key=key,
        sender_account_registration_id="account-1",
        context=context,
    )

    assert incoming.direction is BuyerConversationMessageDirection.INCOMING
    assert incoming.body == "Hello,\n\ncan you help?"
    assert incoming.sender_account_registration_id is None
    assert incoming.attachments[0].content_type == "application/pdf"
    assert incoming.delivery_state is BuyerConversationDeliveryState.RECEIVED
    assert outgoing.direction is BuyerConversationMessageDirection.OUTGOING
    assert outgoing.sender_account_registration_id == "account-1"
    assert outgoing.delivery_state is BuyerConversationDeliveryState.SENT
    assert incoming.context.project_id == "project-7"
    assert isinstance(incoming.context.metadata, MappingProxyType)
    assert incoming.message_id != outgoing.message_id

    with pytest.raises(TypeError):
        incoming.context.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(AttributeError):
        incoming.attachments += ()  # type: ignore[misc]


def test_outgoing_history_and_editor_drafts_require_an_explicit_matching_sender_account() -> None:
    key = conversation_key()

    with pytest.raises(BuyerConversationError, match="must match"):
        normalize_outgoing_message(
            key=key,
            remote_message_id="remote-2",
            body="A response",
            sender_account_registration_id="account-2",
        )

    with pytest.raises(BuyerConversationError, match="must match"):
        create_outgoing_draft(
            draft_id="draft-1",
            key=key,
            body="A suggested response",
            sender_account_registration_id="account-2",
        )


def test_ai_reply_is_only_a_draft_and_retains_project_proposal_context_for_persistence() -> None:
    key = conversation_key()
    context = BuyerConversationContext(project_id="project-7", proposal_intent_id="intent-1")
    draft = create_outgoing_draft(
        draft_id="reply-draft-1",
        key=key,
        sender_account_registration_id="account-1",
        body="  I can deliver this in three days.  ",
        context=context,
        source="ai",
    )

    assert isinstance(draft, BuyerConversationDraft)
    assert draft.state == "draft"
    assert draft.body == "I can deliver this in three days."
    payload = draft.to_payload()
    assert payload["state"] == "draft"
    assert payload["context"]["project_id"] == "project-7"
    assert payload["context"]["proposal_intent_id"] == "intent-1"
    assert "delivery_state" not in payload


def test_full_history_batch_retains_context_and_serializes_by_account() -> None:
    key = conversation_key()
    context = BuyerConversationContext(project_id="project-7", proposal_intent_id="intent-1")
    conversation = BuyerConversation(key=key, context=context, remote_title="Buyer chat")
    message = normalize_incoming_message(
        key=key,
        remote_message_id="message-1",
        body="Can we start?",
        context=context,
    )
    batch = build_conversation_sync_batch(
        cursor=BuyerConversationCursor.for_conversation(key, cursor="opaque-2"),
        conversations=[conversation],
        messages=[message],
    )

    payload = batch.to_payload()
    assert payload["cursor"]["account_registration_id"] == "account-1"
    assert payload["conversations"][0]["context"]["project_id"] == "project-7"
    assert payload["messages"][0]["context"]["proposal_intent_id"] == "intent-1"
