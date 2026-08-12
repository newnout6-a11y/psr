from __future__ import annotations

from typing import Any

import pytest

from src.platforms.kwork_buyer.conversation_copilot import (
    CONVERSATION_REPLY_TASK,
    BuyerConversationCopilotContextError,
    BuyerConversationCopilotController,
    BuyerConversationCopilotNotFoundError,
    LLMRouterBuyerConversationReplyGateway,
)
from src.platforms.kwork_buyer.conversation_persistence import SQLiteBuyerConversationStore
from src.platforms.kwork_buyer.conversations import (
    BuyerConversation,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationKey,
    build_conversation_sync_batch,
    normalize_incoming_message,
    normalize_outgoing_message,
)


class _ProjectLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_project(self, run_id: str, project_id: str) -> dict[str, Any]:
        self.calls.append((run_id, project_id))
        assert (run_id, project_id) == ("run-7", "project-7")
        return {
            "project_id": project_id,
            "title": "Telegram bot integration",
            "description": "Buyer needs a scoped implementation and handoff.",
            "budget_max": 30_000,
        }


class _ProposalLoader:
    async def get_draft_record(self, draft_id: str) -> dict[str, Any]:
        assert draft_id == "proposal-draft-7"
        return {
            "draft_id": draft_id,
            "run_id": "run-7",
            "project_id": "project-7",
            "body": "Proposal context: staged delivery and documented handoff.",
        }

    async def get_send_intent(self, intent_id: str) -> dict[str, Any]:
        assert intent_id == "proposal-intent-7"
        return {
            "intent_id": intent_id,
            "run_id": "run-7",
            "project_id": "project-7",
            "state": "pending_send",
        }


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str) -> dict[str, str]:
        self.calls.append({"prompt": prompt, "task": task})
        return {
            "body": "Yes, I can confirm the integration scope and share a staged delivery plan.",
            "provider": "test-provider",
            "model": "test-conversation-model",
            "task": task,
        }


def _key(account_registration_id: str = "account-7") -> BuyerConversationKey:
    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id="dialog-7",
    )


async def _seed(store: SQLiteBuyerConversationStore, key: BuyerConversationKey) -> None:
    context = BuyerConversationContext(
        project_id="project-7",
        proposal_draft_id="proposal-draft-7",
        proposal_intent_id="proposal-intent-7",
        buyer_remote_user_id="buyer-7",
        metadata={"run_id": "run-7", "origin": "account-inbox-sync"},
    )
    conversation = BuyerConversation(
        key=key,
        context=context,
        remote_title="Buyer conversation",
        last_remote_message_at="2026-07-16T12:00:00Z",
        synced_at="2026-07-16T12:01:00Z",
    )
    incoming = normalize_incoming_message(
        key=key,
        remote_message_id="message-incoming-1",
        body="Could you clarify the implementation milestones?",
        remote_sender_id="buyer-7",
        remote_created_at="2026-07-16T11:59:00Z",
        observed_at="2026-07-16T12:00:00Z",
        context=context,
    )
    message_context = BuyerConversationContext(project_id="project-7")
    outgoing = normalize_outgoing_message(
        key=key,
        remote_message_id="message-outgoing-1",
        body="I can outline the stages after I confirm the API details.",
        sender_account_registration_id=key.account_registration_id,
        remote_created_at="2026-07-16T12:00:00Z",
        observed_at="2026-07-16T12:01:00Z",
        context=message_context,
    )
    await store.commit_sync_batch(
        build_conversation_sync_batch(
            cursor=BuyerConversationCursor.for_conversation(
                key,
                cursor="inbox-page-7",
                watermark="2026-07-16T12:01:00Z",
                revision=1,
                updated_at="2026-07-16T12:01:00Z",
            ),
            conversations=[conversation],
            messages=[incoming, outgoing],
        )
    )


@pytest.mark.asyncio
async def test_copilot_uses_full_account_scoped_context_and_persists_only_a_local_draft(tmp_path) -> None:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    key = _key()
    await _seed(store, key)
    project_loader = _ProjectLoader()
    gateway = _Gateway()
    controller = BuyerConversationCopilotController(
        store,
        gateway,
        project_loader=project_loader,
        proposal_loader=_ProposalLoader(),
    )

    result = await controller.generate_reply_draft(
        key=key,
        draft_id="reply-draft-7",
        operator_instruction="Keep it concise and ask one clarifying question.",
    )

    assert gateway.calls[0]["task"] == CONVERSATION_REPLY_TASK
    assert "Could you clarify the implementation milestones?" in gateway.calls[0]["prompt"]
    assert "I can outline the stages after I confirm the API details." in gateway.calls[0]["prompt"]
    assert "Telegram bot integration" in gateway.calls[0]["prompt"]
    assert "staged delivery and documented handoff" in gateway.calls[0]["prompt"]
    assert project_loader.calls == [("run-7", "project-7")]
    assert result.draft.state == "draft"
    assert result.draft.source == "ai_copilot"
    assert result.draft.sender_account_registration_id == "account-7"
    assert result.resolved_provider == "test-provider"
    assert result.resolved_model == "test-conversation-model"
    assert result.task == CONVERSATION_REPLY_TASK
    assert result.context_manifest.context_hash == result.draft.context.metadata["conversation_copilot"]["context_hash"]
    assert result.to_payload()["outbox_only"] is True
    assert result.to_payload()["auto_send"] is False
    assert await store.list_drafts(key=key) == (result.draft,)
    assert len(await store.list_messages(key=key)) == 2

    repeated = await controller.generate_reply_draft(
        key=key,
        draft_id="reply-draft-7",
        operator_instruction="Keep it concise and ask one clarifying question.",
    )
    assert repeated == result
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_copilot_requires_loaders_for_linked_project_and_proposal_context(tmp_path) -> None:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    key = _key()
    await _seed(store, key)

    with pytest.raises(BuyerConversationCopilotContextError, match="proposal_loader"):
        await BuyerConversationCopilotController(store, _Gateway()).generate_reply_draft(key=key, draft_id="reply-draft-7")


@pytest.mark.asyncio
async def test_copilot_cannot_read_a_same_remote_dialog_through_another_account_scope(tmp_path) -> None:
    store = SQLiteBuyerConversationStore(tmp_path / "buyer-conversations.sqlite3")
    await _seed(store, _key("account-7"))
    gateway = _Gateway()
    controller = BuyerConversationCopilotController(
        store,
        gateway,
        project_loader=_ProjectLoader(),
        proposal_loader=_ProposalLoader(),
    )

    with pytest.raises(BuyerConversationCopilotNotFoundError):
        await controller.generate_reply_draft(key=_key("account-8"), draft_id="reply-draft-8")
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_router_adapter_uses_conversation_reply_task_without_explicit_model() -> None:
    class _Router:
        def __init__(self) -> None:
            self.kwargs: dict[str, str] = {}

        async def generate(self, prompt: str, **kwargs: str) -> str:
            self.kwargs = {"prompt": prompt, **kwargs}
            return "Draft reply"

        def get_last_route(self) -> dict[str, str]:
            return {"provider": "openai", "model": "task-configured-model", "task": CONVERSATION_REPLY_TASK}

    router = _Router()
    result = await LLMRouterBuyerConversationReplyGateway(router).generate(
        prompt="prompt",
        task=CONVERSATION_REPLY_TASK,
    )

    assert router.kwargs["task"] == CONVERSATION_REPLY_TASK
    assert "model" not in router.kwargs
    assert result.model == "task-configured-model"
