from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.platforms.kwork_buyer.conversation_send import BuyerConversationSendCapabilityError
from src.platforms.kwork_buyer.conversation_send_runtime import (
    AccountBoundBuyerConversationSendCapabilitiesFactory,
)
from src.platforms.kwork_buyer.runtime_composition import AccountBoundBuyerKworkClient
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
        return SimpleNamespace(identity=self.identity, token="conversation-send-reservation-1")

    async def release_attachment_identity(self, reservation: object) -> None:
        self.release_calls.append(str(reservation.token))  # type: ignore[attr-defined]


class _IdentityPool:
    def __init__(self) -> None:
        self.context_calls: list[str] = []

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        self.context_calls.append(worker_id)
        return _context()


class _SendClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.close_calls = 0

    async def send_conversation_message(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "remote_dialog_id": remote_dialog_id,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        return {
            "endpoint": "inboxCreate",
            "request": {"user_id": int(remote_dialog_id)},
            "idempotency_key": idempotency_key,
            "remote_response": {
                "success": True,
                "request_id": "request-7",
                "response": {"MID": "message-7", "token": "must-not-persist"},
            },
        }

    async def close(self) -> None:
        self.close_calls += 1


class _ClientFactory:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity]] = []

    async def __call__(self, context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> object:
        self.calls.append((context, identity))
        return self.client


@pytest.mark.asyncio
async def test_conversation_sender_uses_one_active_account_lease_and_persists_remote_receipt_evidence() -> None:
    supervisor = _Supervisor()
    identity_pool = _IdentityPool()
    sender = _SendClient()
    client_factory = _ClientFactory(sender)
    factory = AccountBoundBuyerConversationSendCapabilitiesFactory(
        supervisor,
        identity_pool,  # type: ignore[arg-type]
        client_factory,  # type: ignore[arg-type]
    )

    capabilities = await factory("account-1")
    receipt = await capabilities.send(
        remote_dialog_id="701",
        body="I can start tomorrow.",
        idempotency_key="intent-key-7",
    )
    await capabilities.close()
    await capabilities.close()

    assert capabilities.provenance.to_payload() == {
        "worker_id": "buyer-worker-1",
        "account_registration_id": "account-1",
        "transport_id": "vpnte-slot-1",
        "egress_ip": "198.51.100.10",
        "source": "conversation_send",
    }
    assert receipt.accepted is True
    assert receipt.remote_message_id == "message-7"
    assert receipt.remote_receipt == "request-7"
    assert receipt.metadata["endpoint"] == "inboxCreate"
    assert receipt.metadata["idempotency_key"] == "intent-key-7"
    assert receipt.metadata["remote_response"]["response"]["token"] == "[redacted]"
    assert sender.calls == [
        {
            "remote_dialog_id": "701",
            "body": "I can start tomorrow.",
            "idempotency_key": "intent-key-7",
        }
    ]
    assert supervisor.reserve_calls == ["account-1"]
    assert supervisor.release_calls == ["conversation-send-reservation-1"]
    assert identity_pool.context_calls == ["buyer-worker-1"]
    assert [identity.account_registration_id for _, identity in client_factory.calls] == ["account-1"]
    assert sender.close_calls == 1


@pytest.mark.asyncio
async def test_conversation_sender_closes_and_releases_when_exact_client_lacks_the_narrow_send_method() -> None:
    class _ReadOnlyClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    supervisor = _Supervisor()
    identity_pool = _IdentityPool()
    read_only_client = _ReadOnlyClient()
    client_factory = _ClientFactory(read_only_client)
    factory = AccountBoundBuyerConversationSendCapabilitiesFactory(
        supervisor,
        identity_pool,  # type: ignore[arg-type]
        client_factory,  # type: ignore[arg-type]
    )

    with pytest.raises(BuyerConversationSendCapabilityError, match="does not expose explicit conversation send"):
        await factory("account-1")

    assert read_only_client.close_calls == 1
    assert supervisor.release_calls == ["conversation-send-reservation-1"]


class _InboxApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request_with_body(self, endpoint: str, **params: object) -> dict[str, object]:
        self.calls.append((endpoint, params))
        return {"success": True, "response": {"MID": "remote-message-101"}, "request_id": "request-101"}


class _AccountMobileClient:
    def __init__(self, api: _InboxApi) -> None:
        self.api = api

    async def _get_api(self) -> _InboxApi:
        return self.api


@pytest.mark.asyncio
async def test_account_bound_client_sends_only_documented_inbox_create_payload_and_keeps_local_idempotency_metadata() -> None:
    api = _InboxApi()
    client = object.__new__(AccountBoundBuyerKworkClient)
    client._mobile_client = _AccountMobileClient(api)

    result = await client.send_conversation_message(
        remote_dialog_id="701",
        body="A confirmed reply.",
        idempotency_key="immutable-intent-key",
    )

    assert api.calls == [
        (
            "inboxCreate",
            {
                "use_token": True,
                "retry": False,
                "body": {"text": "A confirmed reply."},
                "user_id": 701,
            },
        )
    ]
    assert result == {
        "endpoint": "inboxCreate",
        "request": {"user_id": 701},
        "idempotency_key": "immutable-intent-key",
        "remote_response": {
            "success": True,
            "response": {"MID": "remote-message-101"},
            "request_id": "request-101",
        },
    }
