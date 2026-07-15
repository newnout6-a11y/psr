import asyncio

import pytest

from src.api.routes import dashboard


@pytest.mark.asyncio
async def test_conversation_history_route_wraps_messages(monkeypatch):
    rows = [{"message_id": 1, "sender": "customer", "message_text": "hello", "created_at": "2026-01-01 10:00:00"}]
    monkeypatch.setattr(dashboard.q, "conversation_history", lambda project_id, platform: rows)
    calls: list[tuple[str, str | int]] = []
    monkeypatch.setattr(dashboard, "_schedule_kwork_dialog_sync", lambda limit: calls.append(("sync", limit)))
    monkeypatch.setattr(dashboard, "_schedule_kwork_dialog_read", lambda project_id: calls.append(("read", project_id)))

    result = await dashboard.conversation_history_route("project-1", "kwork")

    assert result == {"messages": rows, "username": "", "read_state": {"ok": None, "pending": True}}
    assert calls == [("sync", 100), ("read", "project-1")]


@pytest.mark.asyncio
async def test_conversations_return_cached_rows_without_waiting_for_slow_kwork_sync(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    previous_task = dashboard._kwork_dialog_sync_task
    dashboard._kwork_dialog_sync_task = None

    async def slow_sync(_limit: int, timeout: float = 8.0) -> None:
        del timeout
        started.set()
        await release.wait()

    monkeypatch.setattr(dashboard, "_sync_kwork_dialogs_safe", slow_sync)
    monkeypatch.setattr(dashboard.q, "active_conversations", lambda limit: [{"project_id": "cached", "limit": limit}])

    try:
        result = await dashboard.conversations(20)
        task = dashboard._kwork_dialog_sync_task
        assert result == [{"project_id": "cached", "limit": 20}]
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=0.1)
        assert not task.done()
    finally:
        release.set()
        if dashboard._kwork_dialog_sync_task is not None:
            await dashboard._kwork_dialog_sync_task
        dashboard._kwork_dialog_sync_task = previous_task


@pytest.mark.asyncio
async def test_mark_kwork_dialog_read_uses_web_read_state(monkeypatch):

    class FakeService:
        async def mark_web_dialog_read(self, project_id):
            return {"ok": True, "web_opened": True, "project_id": project_id}

    monkeypatch.setattr("src.platforms.kwork.get_kwork_service", lambda: FakeService())

    result = await dashboard._mark_kwork_dialog_read("12345")

    assert result["ok"] is True
    assert result["web_opened"] is True


@pytest.mark.asyncio
async def test_mark_kwork_dialog_read_uses_project_title_fallback(monkeypatch):
    calls = []

    class FakeService:
        async def mark_web_dialog_read(self, recipient):
            calls.append(recipient)
            if recipient == "client_name":
                return {"ok": True, "web_opened": True, "username": recipient}
            return {"ok": False, "web_opened": False}

    class FakeDB:
        def get_conversation(self, project_id, platform):
            return {"project_title": "client_name"}

    monkeypatch.setattr("src.platforms.kwork.get_kwork_service", lambda: FakeService())
    monkeypatch.setattr("src.action.proposal_db.ProposalDB", lambda: FakeDB())

    result = await dashboard._mark_kwork_dialog_read("12345")

    assert calls == ["12345", "client_name"]
    assert result["ok"] is True
    assert result["fallback"]["username"] == "client_name"


async def _noop_async():
    return None


def test_dialog_last_message_supports_kwork_model_shape():
    class LastMessage:
        message = "latest"

    class Dialog:
        last_message = ""
        last_message_obj = LastMessage()

    assert dashboard._dialog_last_message(Dialog()) == "latest"


def test_dialog_sync_status_uses_unread_and_system_dialogs():
    assert dashboard._dialog_sync_status({"sender": "customer", "unread_count": 2}, "Vladimir") == "awaiting_reply"
    assert dashboard._dialog_sync_status({"sender": "customer", "unread_count": 0}, "Vladimir") == "read"
    assert dashboard._dialog_sync_status({"sender": "freelancer", "unread_count": 0}, "Vladimir") == "replied"
    assert dashboard._dialog_sync_status({"sender": "customer", "unread_count": 5}, "Support") == "system"


@pytest.mark.asyncio
async def test_sync_kwork_dialogs_keeps_dialog_without_project_id(monkeypatch):
    conversations = []
    messages = []

    class FakeDB:
        def get_or_create_conversation(self, project_id, platform, project_title=""):
            conversations.append((project_id, platform, project_title))
            return 42

        def add_conversation_message(self, conversation_id, sender, message_text, platform_message_id=None):
            messages.append((conversation_id, sender, message_text, platform_message_id))

    class Dialog:
        user_id = 12345
        username = "client_name"
        project_name = ""
        last_message = "hello from chat"

    class FakeApi:
        async def get_all_dialogs(self):
            return [Dialog()]

    class FakeService:
        def __init__(self):
            self.synced = False

        async def get_api(self):
            return FakeApi()

        async def _sync_session_hub_cookies(self, api):
            self.synced = True

    service = FakeService()
    monkeypatch.setattr("src.action.proposal_db.ProposalDB", FakeDB)
    monkeypatch.setattr("src.platforms.kwork.get_kwork_service", lambda: service)

    await dashboard._sync_kwork_dialogs(limit=10)

    assert service.synced is True
    assert conversations == [("12345", "kwork", "client_name")]
    assert messages == [(42, "customer", "hello from chat", "12345")]


@pytest.mark.asyncio
async def test_sync_kwork_dialogs_updates_status_for_existing_read_dialog(monkeypatch):
    statuses = []

    class FakeDB:
        def get_or_create_conversation(self, project_id, platform, project_title=""):
            return 42

        def add_conversation_message(self, conversation_id, sender, message_text, platform_message_id=None):
            return False

        def update_conversation_status(self, conversation_id, status):
            statuses.append((conversation_id, status))

    class FakeService:
        async def get_web_dialogs(self, limit):
            return [
                {
                    "user_id": 12345,
                    "username": "client_name",
                    "project_name": "",
                    "last_message": "already read",
                    "last_message_id": "m1",
                    "sender": "customer",
                    "unread_count": 0,
                },
                {
                    "user_id": 71232,
                    "username": "Support",
                    "project_name": "Support",
                    "last_message": "system text",
                    "last_message_id": "m2",
                    "sender": "customer",
                    "unread_count": 0,
                },
            ]

    monkeypatch.setattr("src.action.proposal_db.ProposalDB", FakeDB)
    monkeypatch.setattr("src.platforms.kwork.get_kwork_service", lambda: FakeService())

    await dashboard._sync_kwork_dialogs(limit=10)

    assert statuses == [(42, "read"), (42, "system")]


@pytest.mark.asyncio
async def test_conversation_message_route_sends_and_persists(monkeypatch):
    messages = []

    class FakeDB:
        def get_conversation(self, project_id, platform):
            return {"conversation_id": 77, "project_id": project_id, "platform": platform}

        def add_conversation_message(self, conversation_id, sender, message_text, platform_message_id=None):
            messages.append((conversation_id, sender, message_text, platform_message_id))
            return True

    class FakeService:
        async def send_web_message(self, recipient, text):
            return {"MID": 987, "message": text, "MSGTO": int(recipient)}

    monkeypatch.setattr("src.action.proposal_db.ProposalDB", FakeDB)
    monkeypatch.setattr("src.platforms.kwork.get_kwork_service", lambda: FakeService())

    result = await dashboard.conversation_message_route(
        "12345",
        "kwork",
        dashboard.ConversationMessageRequest(text="reply text"),
    )

    assert result["ok"] is True
    assert result["message_id"] == 987
    assert messages == [(77, "freelancer", "reply text", "987")]


@pytest.mark.asyncio
async def test_conversation_draft_route_uses_llm(monkeypatch):
    rows = [
        {"message_id": 1, "sender": "customer", "message_text": "Здравствуйте, сколько стоит бот?"},
        {"message_id": 2, "sender": "freelancer", "message_text": "Здравствуйте, зависит от задачи."},
        {"message_id": 3, "sender": "customer", "message_text": "Нужны заявки из Telegram."},
    ]

    class FakeDB:
        def get_conversation(self, project_id, platform):
            return {"project_title": "Telegram bot", "project_id": project_id, "platform": platform}

    class FakeRouter:
        async def generate(self, **kwargs):
            assert "Нужны заявки из Telegram" in kwargs["prompt"]
            assert kwargs["task"] == "conversation_reply_draft"
            return "Могу сделать бот для заявок. Уточните, какие поля нужно собирать."

    monkeypatch.setattr(dashboard.q, "conversation_history", lambda project_id, platform: rows)
    monkeypatch.setattr("src.action.proposal_db.ProposalDB", FakeDB)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())

    result = await dashboard.conversation_draft_route(
        "12345",
        "kwork",
        dashboard.ConversationDraftRequest(),
    )

    assert result == {
        "ok": True,
        "text": "Могу сделать бот для заявок. Уточните, какие поля нужно собирать.",
    }
