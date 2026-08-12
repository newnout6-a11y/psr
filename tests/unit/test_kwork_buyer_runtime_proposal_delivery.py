from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import src.platforms.kwork_buyer.proposal_delivery_runtime as delivery_runtime
from src.platforms.kwork_buyer.outreach import BuyerProposalSendIntent
from src.platforms.kwork_buyer.proposal_delivery import BuyerProposalDeliveryRejectedError
from src.platforms.kwork_buyer.proposal_delivery_runtime import (
    AccountBoundBuyerProposalDeliveryGatewayFactory,
    BuyerProposalDeliveryRuntimeError,
    KWORK_CREATE_OFFER_URL,
)
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext


def _context(*, registration_id: str = "account-1") -> MarketAccountContext:
    return MarketAccountContext(
        registration_id=registration_id,
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session", "csrf_user_token": "csrf-token"},
        headers={"X-Persona": "buyer-test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )


class _Supervisor:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple[str, str]] = []
        self.release_calls: list[str] = []
        self.identity = BuyerDiscoveryIdentity(
            worker_id="buyer-worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="198.51.100.10",
            route_generation=3,
        )

    async def reserve_attachment_identity(self, run_id: str, account_registration_id: str) -> object:
        self.reserve_calls.append((run_id, account_registration_id))
        return SimpleNamespace(
            run_id=run_id,
            worker_id=self.identity.worker_id,
            identity=self.identity,
            token=f"proposal-reservation-{len(self.reserve_calls)}",
        )

    async def release_attachment_identity(self, reservation: object) -> None:
        self.release_calls.append(str(reservation.token))  # type: ignore[attr-defined]


class _IdentityPool:
    def __init__(self, context: MarketAccountContext) -> None:
        self.context = context
        self.calls: list[str] = []

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext:
        self.calls.append(worker_id)
        return self.context


class _AccountClient:
    def __init__(self, context: MarketAccountContext) -> None:
        self.context = context
        self.proxy_url = "http://127.0.0.1:19001"
        self.project_reads: list[str] = []
        self.want_reads: list[str] = []
        self.close_calls = 0
        self.project_detail: dict[str, object] = {"has_offer": True, "offer": {"id": "offer-42"}}
        self.want_detail: dict[str, object] = {"has_offer": False}

    async def fetch_project_detail(self, remote_project_id: str) -> dict[str, object]:
        self.project_reads.append(remote_project_id)
        return self.project_detail

    async def fetch_want_detail(self, remote_project_id: str) -> dict[str, object]:
        self.want_reads.append(remote_project_id)
        return self.want_detail

    async def close(self) -> None:
        self.close_calls += 1


class _CloseFailingAccountClient(_AccountClient):
    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("simulated client close failure")


class _ClientFactory:
    def __init__(self, client: _AccountClient | Exception) -> None:
        self.client = client
        self.calls: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity]] = []

    async def __call__(self, context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> _AccountClient:
        self.calls.append((context, identity))
        if isinstance(self.client, Exception):
            raise self.client
        return self.client


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeAsyncClient:
    response = _FakeResponse(status_code=201, payload={"success": True, "offer": {"id": "offer-42"}})
    constructions: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructions.append(kwargs)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).posts.append({"url": url, **kwargs})
        return type(self).response


def _intent() -> BuyerProposalSendIntent:
    return BuyerProposalSendIntent(
        intent_id="intent-1",
        draft_id="draft-1",
        project_id="project-1",
        platform="kwork",
        sender_account_registration_id="account-1",
        idempotency_key="idem-1",
        context_hash="a" * 64,
        draft_hash="b" * 64,
    )


def _factory(
    client: _AccountClient | Exception,
    *,
    project: dict[str, object] | None = None,
) -> tuple[AccountBoundBuyerProposalDeliveryGatewayFactory, _Supervisor, _IdentityPool, _ClientFactory]:
    supervisor = _Supervisor()
    identity_pool = _IdentityPool(_context())
    client_factory = _ClientFactory(client)

    async def resolve_project(run_id: str, project_id: str) -> dict[str, object]:
        assert (run_id, project_id) == ("run-1", "project-1")
        return project or {"remote_project_id": "42"}

    return (
        AccountBoundBuyerProposalDeliveryGatewayFactory(
            supervisor,
            identity_pool,  # type: ignore[arg-type]
            client_factory,  # type: ignore[arg-type]
            project_resolver=resolve_project,
        ),
        supervisor,
        identity_pool,
        client_factory,
    )


@pytest.mark.asyncio
async def test_delivery_gateway_uses_exact_leased_vpnte_identity_posts_explicit_offer_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.constructions = []
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.response = _FakeResponse(
        status_code=201,
        payload={"success": True, "data": {"offer": {"id": "offer-42"}}},
    )
    monkeypatch.setattr(delivery_runtime.httpx, "AsyncClient", _FakeAsyncClient)
    client = _AccountClient(_context())
    factory, supervisor, identity_pool, client_factory = _factory(client)

    gateway = await factory("run-1", "project-1", "account-1")
    receipt = await gateway.deliver_proposal(
        remote_project_id="42",
        proposal_body="I can deliver this integration.",
        price=25_000,
        delivery_days=4,
        currency="RUB",
        idempotency_key="proposal-idempotency-1",
    )
    evidence = await gateway.inspect_send_intent(_intent())
    await gateway.close()
    await gateway.close()

    assert receipt.to_payload() == {
        "remote_receipt": "offer-42",
        "source": "kwork_offer_api",
        "detail": {
            "http_status": 201,
            "remote_project_id": "42",
            "remote_offer_id": "offer-42",
            "offer_type": "custom",
        },
    }
    assert evidence.to_payload() == {
        "source": "account_bound_offer_detail",
        "has_offer": True,
        "remote_receipt": "offer-42",
        "detail": {
            "run_id": "run-1",
            "project_id": "project-1",
            "remote_project_id": "42",
            "remote_offer_id": "offer-42",
            "has_offer": True,
        },
    }
    assert supervisor.reserve_calls == [("run-1", "account-1")]
    assert supervisor.release_calls == ["proposal-reservation-1"]
    assert identity_pool.calls == ["buyer-worker-1"]
    assert [identity.account_registration_id for _, identity in client_factory.calls] == ["account-1"]
    assert client.project_reads == ["42"]
    assert client.want_reads == []
    assert client.close_calls == 1
    assert _FakeAsyncClient.posts == [
        {
            "url": KWORK_CREATE_OFFER_URL,
            "files": {
                "wantId": (None, "42"),
                "offerType": (None, "custom"),
                "description": (None, "I can deliver this integration."),
                "kwork_duration": (None, "4"),
                "kwork_price": (None, "25000"),
                "kwork_name": (None, "Custom offer"),
                "csrftoken": (None, "csrf-token"),
                "draftKey": (None, "2fa363d2"),
            },
        }
    ]
    assert _FakeAsyncClient.constructions == [
        {
            "headers": {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "User-Agent": "PSR-BuyerProposalDelivery/1.0",
                "Origin": "https://kwork.ru",
                "Referer": "https://kwork.ru/new_offer?project=42",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": "csrf-token",
                "Idempotency-Key": "proposal-idempotency-1",
                "X-Persona": "buyer-test",
            },
            "cookies": {"PHPSESSID": "session", "csrf_user_token": "csrf-token"},
            "timeout": 15.0,
            "follow_redirects": False,
            "proxy": "http://127.0.0.1:19001",
            "trust_env": False,
        }
    ]


@pytest.mark.asyncio
async def test_delivery_gateway_classifies_definitive_remote_rejection_without_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.constructions = []
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.response = _FakeResponse(
        status_code=422,
        payload={"success": False, "message": "offer already exists"},
    )
    monkeypatch.setattr(delivery_runtime.httpx, "AsyncClient", _FakeAsyncClient)
    client = _AccountClient(_context())
    factory, supervisor, _identity_pool, _client_factory = _factory(client)
    gateway = await factory("run-1", "project-1", "account-1")

    with pytest.raises(BuyerProposalDeliveryRejectedError, match="offer already exists"):
        await gateway.deliver_proposal(
            remote_project_id="42",
            proposal_body="I can deliver this integration.",
            price=25_000,
            delivery_days=4,
            currency="RUB",
            idempotency_key="proposal-idempotency-2",
        )
    await gateway.close()

    assert len(_FakeAsyncClient.posts) == 1
    assert supervisor.release_calls == ["proposal-reservation-1"]


@pytest.mark.asyncio
async def test_factory_releases_the_fence_when_account_client_construction_fails() -> None:
    factory, supervisor, identity_pool, client_factory = _factory(RuntimeError("VPNTE client build failed"))

    with pytest.raises(RuntimeError, match="VPNTE client build failed"):
        await factory("run-1", "project-1", "account-1")

    assert supervisor.reserve_calls == [("run-1", "account-1")]
    assert supervisor.release_calls == ["proposal-reservation-1"]
    assert identity_pool.calls == ["buyer-worker-1"]
    assert len(client_factory.calls) == 1


@pytest.mark.asyncio
async def test_gateway_releases_the_fence_even_when_its_client_close_fails() -> None:
    client = _CloseFailingAccountClient(_context())
    factory, supervisor, _identity_pool, _client_factory = _factory(client)
    gateway = await factory("run-1", "project-1", "account-1")

    with pytest.raises(RuntimeError, match="simulated client close failure"):
        await gateway.close()
    await gateway.close()

    assert client.close_calls == 1
    assert supervisor.release_calls == ["proposal-reservation-1"]


@pytest.mark.asyncio
async def test_factory_rejects_non_numeric_remote_project_before_reserving_a_route() -> None:
    client = _AccountClient(_context())
    factory, supervisor, _identity_pool, _client_factory = _factory(client, project={"remote_project_id": "project-opaque"})

    with pytest.raises(BuyerProposalDeliveryRuntimeError, match="positive decimal"):
        await factory("run-1", "project-1", "account-1")

    assert supervisor.reserve_calls == []
    assert supervisor.release_calls == []
