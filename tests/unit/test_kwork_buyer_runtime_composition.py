from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from src.platforms.kwork_buyer.runtime_composition import (
    AccountBoundBuyerKworkClientFactory,
    BuyerBackingJobError,
    BuyerRunMarketJobResolver,
    BuyerTransportUnavailableError,
    _web_project_params,
)
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext
from src.platforms.kwork_supply.models import (
    JobKind,
    MarketJobCreate,
    MarketScope,
    TransportHealth,
    TransportKind,
    TransportSnapshot,
)
from src.platforms.kwork_supply.repository import MarketJobRepository


class _TransportManager:
    def __init__(self, snapshot: TransportSnapshot | None) -> None:
        self.snapshot = snapshot
        self.refresh_calls = 0
        self.get_calls: list[str] = []

    def refresh(self) -> Sequence[TransportSnapshot]:
        self.refresh_calls += 1
        return (self.snapshot,) if self.snapshot is not None else ()

    def get(self, transport_id: str) -> TransportSnapshot | None:
        self.get_calls.append(transport_id)
        return self.snapshot if self.snapshot is not None and self.snapshot.transport_id == transport_id else None


def _context() -> MarketAccountContext:
    return MarketAccountContext(
        registration_id="account-1",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"User-Agent": "buyer-test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )


def _identity() -> BuyerDiscoveryIdentity:
    return BuyerDiscoveryIdentity(
        worker_id="buyer-worker-1",
        account_registration_id="account-1",
        transport_id="vpnte-slot-1",
        egress_ip="198.51.100.10",
        route_generation=4,
    )


def _snapshot(**changes: Any) -> TransportSnapshot:
    values = {
        "transport_id": "vpnte-slot-1",
        "kind": TransportKind.VPNTE,
        "health": TransportHealth.HEALTHY,
        "slot": 1,
        "proxy_url": "http://127.0.0.1:19001",
        "generation": 4,
        "lease_owner": "buyer-worker-1",
        "egress_ip": "198.51.100.10",
    }
    values.update(changes)
    return TransportSnapshot(**values)


@pytest.mark.asyncio
async def test_backing_job_resolver_creates_one_durable_buyer_job_without_operations(tmp_path) -> None:
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    resolver = BuyerRunMarketJobResolver(repository)
    run = {
        "run_id": "run-1",
        "category_scope": {"category_id": 38, "category_name": "Website work"},
        "filters": {"budget_max": 10_000},
        "requested_workers": 3,
        "target_unique_projects": 400,
        "account_registration_ids": ["account-a", " account-b ", "account-a"],
    }

    job_id = await resolver.ensure_for_run(run)
    created = await repository.get_job(job_id)

    assert job_id == "buyer-search:run-1"
    assert created is not None
    assert created["job_kind"] == JobKind.BUYER_SEARCH.value
    assert created["network_policy"] == "vpnte_only"
    assert created["desired_workers"] == 3
    assert created["account_registration_ids"] == ["account-a", "account-b"]
    assert created["workflow_config"]["buyer_run_id"] == "run-1"
    assert created["workflow_config"]["owns_supply_operations"] is False
    assert await repository.list_operations(job_id) == []
    assert await resolver.resolve("run-1") == job_id
    assert await resolver.ensure_for_run(run) == job_id


@pytest.mark.asyncio
async def test_backing_job_resolver_rejects_a_deterministic_id_owned_by_another_workflow(tmp_path) -> None:
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    resolver = BuyerRunMarketJobResolver(repository)
    await repository.initialize()
    await repository.create_job(
        MarketJobCreate(scope=MarketScope(category_id=1)),
        job_id="buyer-search:run-1",
    )

    with pytest.raises(BuyerBackingJobError, match="non-Buyer"):
        await resolver.resolve("run-1")


@pytest.mark.asyncio
async def test_backing_job_resolver_finalizes_terminal_buyer_lease_container(tmp_path) -> None:
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    resolver = BuyerRunMarketJobResolver(repository)
    run = {"run_id": "run-terminal", "requested_workers": 2, "target_unique_projects": 100}
    job_id = await resolver.ensure_for_run(run)

    assert await resolver.finalize_for_run("run-terminal", "buyer_run_stopped") == job_id

    finalized = await repository.get_job(job_id)
    assert finalized is not None
    assert finalized["state"] == "stopped"
    assert finalized["finished_at"] is not None
    assert finalized["last_warning"] == "buyer_run_stopped"


@pytest.mark.asyncio
async def test_account_bound_factory_uses_refreshed_owned_vpnte_snapshot_and_never_a_direct_route() -> None:
    transport_manager = _TransportManager(_snapshot())
    received: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity, TransportSnapshot]] = []
    expected_client = object()

    async def client_builder(
        context: MarketAccountContext,
        identity: BuyerDiscoveryIdentity,
        snapshot: TransportSnapshot,
    ) -> object:
        received.append((context, identity, snapshot))
        return expected_client

    factory = AccountBoundBuyerKworkClientFactory(transport_manager, client_builder=client_builder)  # type: ignore[arg-type]

    client = await factory(_context(), _identity())

    assert client is expected_client
    assert transport_manager.refresh_calls == 1
    assert transport_manager.get_calls == ["vpnte-slot-1"]
    assert received == [(_context(), _identity(), _snapshot())]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"health": TransportHealth.DEGRADED},
        {"proxy_url": None},
        {"kind": TransportKind.DIRECT},
        {"lease_owner": "other-worker"},
        {"egress_ip": "203.0.113.10"},
    ],
)
async def test_account_bound_factory_fails_closed_without_the_exact_healthy_vpnte_route(changes: dict[str, Any]) -> None:
    transport_manager = _TransportManager(_snapshot(**changes))
    factory = AccountBoundBuyerKworkClientFactory(
        transport_manager,  # type: ignore[arg-type]
        client_builder=lambda *_args: object(),
    )

    with pytest.raises(BuyerTransportUnavailableError):
        await factory(_context(), _identity())


def test_web_project_params_maps_documented_kwork_filter_names() -> None:
    params = _web_project_params(
        {
            "page": 3,
            "category_id": 37,
            "query": "создание сайта",
            "price_from": 5_000,
            "price_to": 100_000,
            "hiring_from": 80,
            "kworks_filter_from": 5,
            "kworks_filter_to": 10,
            "prices_filters": "fixed",
        }
    )

    assert params == {
        "page": 3,
        "c": 37,
        "keyword": "создание сайта",
        "price-from": 5_000,
        "price-to": 100_000,
        "hiring-from": 80,
        "kworks-filters": "1",
        "prices-filters": "fixed",
    }
