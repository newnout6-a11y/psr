from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.platforms.kwork_buyer.runtime_adapters import (
    AccountBoundBuyerReadSourceFactory,
    BuyerAccountContextUnavailableError,
    BuyerRuntimeAdapterError,
    MarketIdentityPoolDiscoveryAdapter,
)
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext, MarketIdentityLease


class _Pool:
    def __init__(self) -> None:
        self.acquire_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str | None]] = []
        self.renew_calls: list[str] = []
        self.reclaim_calls: list[str] = []
        self.reclaim_buyer_calls = 0
        self.snapshot_calls: list[tuple[str, bool]] = []
        self.context_calls: list[str] = []
        self.renew_result = True
        self.snapshot_payload: Mapping[str, Any] = {"accounts": [], "routes": []}
        self.context: MarketAccountContext | None = None
        self._leases: list[MarketIdentityLease | None] = []

    async def acquire(self, worker_id: str, job_id: str) -> MarketIdentityLease | None:
        self.acquire_calls.append((worker_id, job_id))
        if self._leases:
            return self._leases.pop(0)
        return _lease(worker_id, job_id)

    async def release(self, worker_id: str, *, reason: str | None = None) -> None:
        self.release_calls.append((worker_id, reason))

    async def renew(self, worker_id: str) -> bool:
        self.renew_calls.append(worker_id)
        return self.renew_result

    async def reclaim_unowned_job_bindings(self, job_id: str) -> int:
        self.reclaim_calls.append(job_id)
        return 0

    async def reclaim_unowned_buyer_bindings(self) -> int:
        self.reclaim_buyer_calls += 1
        return 0

    async def snapshot(self, *, job_id: str | None = None, refresh_routes: bool = True) -> Mapping[str, Any]:
        self.snapshot_calls.append((str(job_id), refresh_routes))
        return self.snapshot_payload

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        self.context_calls.append(worker_id)
        return self.context


class _ReadClient:
    def __init__(self) -> None:
        self.mobile_params: dict[str, object] | None = None
        self.web_params: dict[str, object] | None = None
        self.send_calls = 0

    async def fetch_buyer_projects(self, **params: object) -> dict[str, object]:
        self.mobile_params = params
        return {"response": [{"id": "1", "title": "Mobile project"}]}

    async def fetch_web_projects(self, **params: object) -> dict[str, object]:
        self.web_params = params
        return {"items": [{"id": "2", "title": "Web project"}]}

    async def send_message(self, *_args: object, **_kwargs: object) -> None:
        self.send_calls += 1
        raise AssertionError("Buyer read source must not call mutation methods")


def _lease(worker_id: str, job_id: str, *, account: str = "account-1") -> MarketIdentityLease:
    return MarketIdentityLease(
        worker_id=worker_id,
        job_id=job_id,
        registration_id=account,
        username="buyer",
        persona_id="persona-1",
        transport_id="vpnte-slot-1",
        slot=1,
        proxy_url="http://127.0.0.1:19001",
        egress_ip="198.51.100.10",
        signup_ip="198.51.100.10",
        preferred_slot=1,
        binding_mode="preferred_ip",
        lease_token="lease-token",
        lease_deadline="2026-07-16T12:00:00Z",
    )


def _identity() -> BuyerDiscoveryIdentity:
    return BuyerDiscoveryIdentity(
        worker_id="buyer-worker-1",
        account_registration_id="account-1",
        transport_id="vpnte-slot-1",
        egress_ip="198.51.100.10",
    )


@pytest.mark.asyncio
async def test_market_identity_adapter_maps_leases_and_renews_only_owned_run_identities() -> None:
    pool = _Pool()
    adapter = MarketIdentityPoolDiscoveryAdapter(
        pool,  # type: ignore[arg-type]
        job_id_for_run=lambda run_id: f"market-job:{run_id}",
        worker_id_factory=lambda _run_id, ordinal: f"buyer-worker-{ordinal}",
    )

    allocated = await adapter.allocate(run_id="run-1", requested_workers=2, active_identities=())

    assert [identity.worker_id for identity in allocated] == ["buyer-worker-1", "buyer-worker-2"]
    assert [identity.account_registration_id for identity in allocated] == ["account-1", "account-1"]
    assert pool.acquire_calls == [
        ("buyer-worker-1", "market-job:run-1"),
        ("buyer-worker-2", "market-job:run-1"),
    ]
    assert await adapter.heartbeat("run-1", allocated[0], {}) is True
    assert pool.renew_calls == ["buyer-worker-1"]

    await adapter.release(run_id="run-1", identity=allocated[0], reason="operator_stop")

    assert pool.release_calls == [("buyer-worker-1", "operator_stop")]
    assert await adapter.heartbeat("run-1", allocated[0], {}) is False
    with pytest.raises(BuyerRuntimeAdapterError, match="does not belong"):
        await adapter.release(run_id="other-run", identity=allocated[1], reason="operator_stop")


@pytest.mark.asyncio
async def test_market_identity_adapter_inventory_uses_only_eligible_distinct_unleased_routes() -> None:
    pool = _Pool()
    pool.snapshot_payload = {
        "accounts": [
            {"registration_id": "account-2", "market_enabled": True, "status": "activated", "session_cookie_count": 1},
            {"registration_id": "account-1", "market_enabled": True, "status": "activated", "session_cookie_count": 2},
            {"registration_id": "disabled", "market_enabled": False, "status": "activated", "session_cookie_count": 3},
            {
                "registration_id": "not-selected",
                "market_enabled": True,
                "status": "activated",
                "session_cookie_count": 3,
                "selected_for_job": False,
            },
        ],
        "routes": [
            {"transport_id": "vpnte-slot-2", "slot": 2, "health": "healthy", "egress_ip": "203.0.113.2"},
            {"transport_id": "vpnte-slot-1", "slot": 1, "health": "healthy", "egress_ip": "203.0.113.1"},
            {"transport_id": "duplicate-ip", "slot": 3, "health": "healthy", "egress_ip": "203.0.113.1"},
            {"transport_id": "unhealthy", "slot": 4, "health": "unhealthy", "egress_ip": "203.0.113.4"},
            {
                "transport_id": "leased",
                "slot": 5,
                "health": "healthy",
                "egress_ip": "203.0.113.5",
                "lease_owner": "other-worker",
            },
        ],
    }
    adapter = MarketIdentityPoolDiscoveryAdapter(pool, job_id_for_run=lambda _run_id: "market-job")  # type: ignore[arg-type]

    inventory = await adapter.inspect(run_id="run-1", requested_workers=5)

    assert pool.reclaim_buyer_calls == 1
    assert pool.reclaim_calls == []
    assert pool.snapshot_calls == [("market-job", True)]
    assert [(item.account_registration_id, item.transport_id, item.egress_ip) for item in inventory] == [
        ("account-1", "vpnte-slot-1", "203.0.113.1"),
        ("account-2", "vpnte-slot-2", "203.0.113.2"),
    ]
    assert all(":inventory:run-1:" in item.worker_id for item in inventory)


@pytest.mark.asyncio
async def test_account_bound_source_factory_reads_with_context_bound_capabilities_only() -> None:
    pool = _Pool()
    pool.context = MarketAccountContext(
        registration_id="account-1",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"User-Agent": "test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )
    client = _ReadClient()
    received: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity]] = []

    async def client_factory(
        context: MarketAccountContext,
        identity: BuyerDiscoveryIdentity,
    ) -> _ReadClient:
        received.append((context, identity))
        return client

    factory = AccountBoundBuyerReadSourceFactory(pool, account_client_factory=client_factory)  # type: ignore[arg-type]
    identity = _identity()
    source = await factory.create(run_id="run-1", identity=identity)

    mobile_response = await source.fetch_page(
        task={"source": "mobile", "page": 2, "query_text": "CRM", "category_id": 7},
        identity=identity.to_payload(),
    )
    web_response = await source.fetch_page(
        task={"source": "web_projects", "page": 3, "query_text": "Landing", "category_id": 9},
        identity=identity.to_payload(),
    )

    assert pool.context_calls == ["buyer-worker-1"]
    assert received == [(pool.context, identity)]
    assert client.mobile_params == {"page": 2, "query": "CRM", "categories": "7"}
    assert client.web_params == {"page": 3, "query": "Landing", "category_id": 9}
    assert mobile_response["source"] == "mobile_projects"
    assert web_response["source"] == "web_projects"
    assert client.send_calls == 0
    assert not hasattr(source, "send_message")


@pytest.mark.asyncio
async def test_account_bound_source_factory_rejects_mismatched_or_missing_context_before_client_creation() -> None:
    pool = _Pool()
    created = False

    def client_factory(_context: MarketAccountContext, _identity: BuyerDiscoveryIdentity) -> _ReadClient:
        nonlocal created
        created = True
        return _ReadClient()

    factory = AccountBoundBuyerReadSourceFactory(pool, account_client_factory=client_factory)  # type: ignore[arg-type]
    with pytest.raises(BuyerAccountContextUnavailableError, match="no active account context"):
        await factory.create(run_id="run-1", identity=_identity())
    assert created is False

    pool.context = MarketAccountContext(
        registration_id="other-account",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"User-Agent": "test"},
        persona_id="persona-1",
        signup_ip=None,
        preferred_slot=None,
    )
    with pytest.raises(BuyerAccountContextUnavailableError, match="does not match"):
        await factory.create(run_id="run-1", identity=_identity())
    assert created is False
