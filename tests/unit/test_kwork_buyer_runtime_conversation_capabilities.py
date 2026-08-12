from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import src.platforms.kwork_buyer.runtime_composition as runtime_composition
from src.platforms.kwork_buyer.conversation_sync import BuyerConversationReadCapabilities
from src.platforms.kwork_buyer.runtime_composition import (
    AccountBoundBuyerConversationCapabilitiesFactory,
    AccountBoundBuyerKworkClient,
)
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext


def _context() -> MarketAccountContext:
    return MarketAccountContext(
        registration_id="account-1",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"X-Persona": "buyer-test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )


class _Supervisor:
    def __init__(self) -> None:
        self.reserve_calls: list[str] = []
        self.release_calls: list[str] = []
        self.identity = BuyerDiscoveryIdentity(
            worker_id="buyer-worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="198.51.100.10",
        )

    async def reserve_account_identity(self, account_registration_id: str) -> object:
        self.reserve_calls.append(account_registration_id)
        return SimpleNamespace(
            run_id="run-1",
            worker_id=self.identity.worker_id,
            identity=self.identity,
            token="conversation-reservation-1",
        )

    async def release_attachment_identity(self, reservation: object) -> None:
        self.release_calls.append(str(reservation.token))  # type: ignore[attr-defined]


class _IdentityPool:
    def __init__(self) -> None:
        self.context_calls: list[str] = []

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        self.context_calls.append(worker_id)
        return _context()


class _ConversationClient:
    def __init__(self) -> None:
        self.dialog_calls: list[dict[str, object]] = []
        self.message_calls: list[dict[str, object]] = []
        self.send_calls = 0
        self.close_calls = 0

    async def fetch_inbox_dialogs(self, *, cursor: str | None, limit: int) -> dict[str, object]:
        self.dialog_calls.append({"cursor": cursor, "limit": limit})
        return {"dialogs": []}

    async def fetch_inbox_messages(
        self,
        *,
        dialog: dict[str, object],
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        self.message_calls.append({"dialog": dialog, "cursor": cursor, "limit": limit})
        return {"messages": []}

    async def send_message(self) -> None:
        self.send_calls += 1
        raise AssertionError("conversation capability must not expose remote send")

    async def close(self) -> None:
        self.close_calls += 1


class _ClientFactory:
    def __init__(self, client: _ConversationClient) -> None:
        self.client = client
        self.calls: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity]] = []

    async def __call__(self, context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> _ConversationClient:
        self.calls.append((context, identity))
        return self.client


@pytest.mark.asyncio
async def test_conversation_capability_uses_active_account_lease_and_exposes_only_inbox_reads() -> None:
    supervisor = _Supervisor()
    identity_pool = _IdentityPool()
    client = _ConversationClient()
    client_factory = _ClientFactory(client)
    factory = AccountBoundBuyerConversationCapabilitiesFactory(
        supervisor,
        identity_pool,  # type: ignore[arg-type]
        client_factory,  # type: ignore[arg-type]
    )

    capabilities = await factory("account-1")

    assert isinstance(capabilities, BuyerConversationReadCapabilities)
    assert capabilities.provenance.as_dict() == {
        "worker_id": "buyer-worker-1",
        "account_registration_id": "account-1",
        "transport_id": "vpnte-slot-1",
        "egress_ip": "198.51.100.10",
        "source": "conversation_sync",
    }
    assert not hasattr(capabilities, "send_message")

    await capabilities.fetch_dialogs(cursor="watermark-1", limit=10)
    await capabilities.fetch_messages(dialog={"username": "buyer-one"}, cursor=None, limit=20)
    await capabilities.close()
    await capabilities.close()

    assert supervisor.reserve_calls == ["account-1"]
    assert supervisor.release_calls == ["conversation-reservation-1"]
    assert identity_pool.context_calls == ["buyer-worker-1"]
    assert [identity.account_registration_id for _, identity in client_factory.calls] == ["account-1"]
    assert client.dialog_calls == [{"cursor": "watermark-1", "limit": 10}]
    assert client.message_calls == [{"dialog": {"username": "buyer-one"}, "cursor": None, "limit": 20}]
    assert client.close_calls == 1
    assert client.send_calls == 0


class _WebInboxResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200
        self.url = "https://kwork.ru/inbox"
        self.headers: dict[str, str] = {}


class _WebInboxAsyncClient:
    response = _WebInboxResponse("")
    constructions: list[dict[str, Any]] = []
    get_calls: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructions.append(kwargs)

    async def __aenter__(self) -> "_WebInboxAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str) -> _WebInboxResponse:
        type(self).get_calls.append(url)
        return type(self).response


class _MobileInboxClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request(self, endpoint: str, **params: object) -> dict[str, object]:
        self.calls.append((endpoint, params))
        page = int(params["page"])
        if page == 1:
            return {
                "response": [
                    {
                        "MID": "message-1",
                        "MSGFROM": "worker-id",
                        "message": "I can help.",
                        "time": "2026-07-16T11:59:00Z",
                    }
                ],
                "paging": {"pages": 2},
            }
        return {
            "response": [
                {
                    "MID": "message-2",
                    "MSGFROM": "buyer-id",
                    "message": "Please send the scope.",
                    "time": "2026-07-16T12:00:00Z",
                }
            ],
            "paging": {"pages": 2},
        }


def _account_client() -> AccountBoundBuyerKworkClient:
    client = object.__new__(AccountBoundBuyerKworkClient)
    client.context = _context()
    client.proxy_url = "http://127.0.0.1:19001"
    client._mobile_client = _MobileInboxClient()
    return client


@pytest.mark.asyncio
async def test_account_bound_client_reads_web_inbox_and_mobile_history_without_a_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _WebInboxAsyncClient.constructions = []
    _WebInboxAsyncClient.get_calls = []
    _WebInboxAsyncClient.response = _WebInboxResponse(
        "<script>window.chatList=[{\"username\":\"buyer-one\",\"user_id\":\"buyer-id\","
        "\"member_id\":\"worker-id\",\"lastMessage\":{\"MID\":\"latest-message\","
        "\"time\":\"2026-07-16T12:00:00Z\"}}];</script>"
    )
    monkeypatch.setattr(runtime_composition.httpx, "AsyncClient", _WebInboxAsyncClient)
    client = _account_client()

    dialogs = await client.fetch_inbox_dialogs(cursor=None, limit=10)
    messages = await client.fetch_inbox_messages(
        dialog=dialogs["dialogs"][0],  # type: ignore[index]
        cursor=None,
        limit=10,
    )

    assert dialogs["dialogs"] == [
        {
            "remote_dialog_id": "buyer-id",
            "dialog_id": "buyer-id",
            "username": "buyer-one",
            "user_id": "buyer-id",
            "remote_title": "buyer-one",
            "last_message_at": "2026-07-16T12:00:00Z",
            "unread_count": 0,
            "member_id": "worker-id",
        }
    ]
    assert dialogs["watermark"] == "2026-07-16T12:00:00Z"
    assert [message["remote_message_id"] for message in messages["messages"]] == ["message-1", "message-2"]  # type: ignore[index]
    assert [message.get("direction") for message in messages["messages"]] == ["outgoing", None]  # type: ignore[index]
    assert _WebInboxAsyncClient.get_calls == ["https://kwork.ru/inbox"]
    assert _WebInboxAsyncClient.constructions[0]["proxy"] == "http://127.0.0.1:19001"
    assert _WebInboxAsyncClient.constructions[0]["trust_env"] is False
    assert _WebInboxAsyncClient.constructions[0]["cookies"] == {"PHPSESSID": "session"}
    assert client._mobile_client.calls == [  # type: ignore[attr-defined]
        ("inboxes", {"use_token": True, "username": "buyer-one", "page": 1}),
        ("inboxes", {"use_token": True, "username": "buyer-one", "page": 2}),
    ]
