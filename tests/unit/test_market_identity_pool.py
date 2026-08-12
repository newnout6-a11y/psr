from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.platforms.kwork_account_store import KworkAccountStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.identity_pool import MarketIdentityPool
from src.platforms.kwork_supply.models import (
    JobKind,
    MarketJobCreate,
    MarketScope,
    TransportHealth,
    TransportKind,
    TransportSnapshot,
)
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.transports.vpnte import VpnteTransportManager


class FakeVpnteProvider:
    def __init__(self, instances: list[dict[str, Any]], *, on_rotate=None) -> None:
        self.instances_data = deepcopy(instances)
        self.on_rotate = on_rotate
        self.instances_calls = 0

    def instances(self) -> list[dict[str, Any]]:
        self.instances_calls += 1
        return deepcopy(self.instances_data)

    def list(self) -> list[dict[str, Any]]:
        return [{"profileId": f"profile-{index}"} for index in range(160)]

    def status(self, slot: int) -> dict[str, Any]:
        return deepcopy(next(item for item in self.instances_data if item["slot"] == slot))

    def start(self, slot: int, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"unexpected start for slot {slot}")

    def rotate(self, slot: int, **_kwargs: Any) -> dict[str, Any]:
        if self.on_rotate is None:
            raise AssertionError(f"unexpected rotate for slot {slot}")
        self.on_rotate(slot, self.instances_data)
        return self.status(slot)

    def stop(self, slot: int) -> dict[str, Any]:
        raise AssertionError(f"unexpected stop for slot {slot}")


def _instance(slot: int) -> dict[str, Any]:
    return {
        "slot": slot,
        "running": True,
        "proxyUrl": f"http://127.0.0.1:{19000 + slot}",
        "profileId": f"profile-{slot}",
        "profileName": f"Profile {slot}",
    }


def _save_account(store: KworkAccountStore, *, index: int, signup_ip: str, slot: int) -> str:
    registration_id = f"registration-{index}"
    store.save(
        registration_id=registration_id,
        email=f"account{index}@catchmail.io",
        username=f"account{index}",
        user_type=1,
        mail_provider="catchmail",
        status="activated",
        registration_started_at="2026-07-14T10:00:00Z",
        password=f"Password{index}!23",
        session={
            "cookies": [{"name": "PHPSESSID", "value": f"session-{index}", "domain": ".kwork.ru"}],
            "proxy_url": f"http://127.0.0.1:{19000 + slot}",
        },
        signup_ip=signup_ip,
        registration_slot=slot,
    )
    return registration_id


async def _worker(repository: MarketJobRepository, job_id: str, worker_id: str) -> None:
    await repository.upsert_worker(
        {
            "worker_id": worker_id,
            "generation": 1,
            "desired_state": "running",
            "actual_state": "starting",
            "runtime_kind": "test",
        },
        job_id=job_id,
    )


async def _pool(
    tmp_path: Path,
    *,
    slots: list[int],
    egress: dict[int, str],
    on_rotate=None,
    account_registration_ids: tuple[str, ...] = (),
    job_kind: JobKind = JobKind.SUPPLY,
) -> tuple[MarketIdentityPool, MarketJobRepository, str, KworkAccountStore, VpnteTransportManager]:
    store = KworkAccountStore(db_path=tmp_path / "accounts.sqlite3", key_path=tmp_path / "unused.key")
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    created = await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=1),
            account_registration_ids=account_registration_ids,
            job_kind=job_kind,
        ),
        job_id="identity-job",
    )
    manager = VpnteTransportManager(
        FakeVpnteProvider([_instance(slot) for slot in slots], on_rotate=on_rotate)
    )

    async def probe(proxy_url: str) -> str | None:
        slot = int(proxy_url.rsplit(":", 1)[1]) - 19000
        return egress.get(slot)

    return MarketIdentityPool(repository, manager, account_store=store, exit_ip_probe=probe), repository, created["job"]["job_id"], store, manager


@pytest.mark.asyncio
async def test_pool_assigns_one_account_and_one_distinct_ip_to_each_parallel_worker(tmp_path: Path):
    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1, 2],
        egress={1: "203.0.113.1", 2: "203.0.113.2"},
    )
    first = _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    second = _save_account(store, index=2, signup_ip="203.0.113.2", slot=2)
    await asyncio.gather(_worker(repository, job_id, "worker-1"), _worker(repository, job_id, "worker-2"))

    leases = await asyncio.gather(pool.acquire("worker-1", job_id), pool.acquire("worker-2", job_id))

    assert all(lease is not None for lease in leases)
    assigned = [lease for lease in leases if lease is not None]
    assert {lease.registration_id for lease in assigned} == {first, second}
    assert {lease.transport_id for lease in assigned} == {"vpnte-slot-1", "vpnte-slot-2"}
    assert {lease.egress_ip for lease in assigned} == {"203.0.113.1", "203.0.113.2"}
    assert {lease.binding_mode for lease in assigned} == {"preferred_ip"}
    contexts = await asyncio.gather(pool.context_for_worker("worker-1"), pool.context_for_worker("worker-2"))
    assert {context.registration_id for context in contexts if context is not None} == {first, second}
    assert {context.cookies["PHPSESSID"] for context in contexts if context is not None} == {"session-1", "session-2"}
    assert len({context.persona_id for context in contexts if context is not None}) == 2


@pytest.mark.asyncio
async def test_pool_creates_a_durable_worker_row_for_a_buyer_identity_lease(tmp_path: Path):
    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1],
        egress={1: "203.0.113.1"},
        job_kind=JobKind.BUYER_SEARCH,
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)

    lease = await pool.acquire("buyer-discovery-worker", job_id)

    assert lease is not None
    worker = await repository.get_worker("buyer-discovery-worker")
    assert worker is not None
    assert worker["job_id"] == job_id
    assert worker["runtime_kind"] == "identity_lease"


@pytest.mark.asyncio
async def test_pool_reuses_route_inventory_and_persists_release(tmp_path: Path):
    pool, repository, job_id, store, manager = await _pool(
        tmp_path,
        slots=[1, 2],
        egress={1: "203.0.113.1", 2: "203.0.113.2"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    _save_account(store, index=2, signup_ip="203.0.113.2", slot=2)
    await asyncio.gather(_worker(repository, job_id, "worker-1"), _worker(repository, job_id, "worker-2"))

    first, second = await asyncio.gather(pool.acquire("worker-1", job_id), pool.acquire("worker-2", job_id))

    assert first is not None and second is not None
    provider = manager._provider
    assert isinstance(provider, FakeVpnteProvider)
    assert provider.instances_calls == 1
    leased = manager.get(first.transport_id)
    assert leased is not None
    await repository.upsert_transport(leased)

    retained = await pool.acquire("worker-1", job_id)
    assert retained is not None
    assert retained.transport_id == first.transport_id

    await pool.release("worker-1", reason="test_complete")

    transports = await repository.list_transports(limit=10)
    released = next(item for item in transports if item["transport_id"] == first.transport_id)
    assert released["lease_owner"] is None


@pytest.mark.asyncio
async def test_pool_reconciles_bindings_for_terminal_jobs(tmp_path: Path):
    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1],
        egress={1: "203.0.113.1"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    await _worker(repository, job_id, "worker-1")
    lease = await pool.acquire("worker-1", job_id)
    assert lease is not None

    released_count = await pool.release_inactive_job_bindings(set())

    binding = await repository.get_market_identity_binding("worker-1")
    transport = next(
        item for item in await repository.list_transports(limit=10) if item["transport_id"] == lease.transport_id
    )
    assert released_count == 1
    assert binding is not None and binding["state"] == "released"
    assert transport["lease_owner"] is None


@pytest.mark.asyncio
async def test_pool_reclaims_orphaned_buyer_bindings_for_a_restarted_runtime(tmp_path: Path):
    pool, repository, job_id, store, manager = await _pool(
        tmp_path,
        slots=[1],
        egress={1: "203.0.113.1"},
        job_kind=JobKind.BUYER_SEARCH,
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    await _worker(repository, job_id, "old-buyer-worker")
    old_lease = await pool.acquire("old-buyer-worker", job_id)
    assert old_lease is not None

    provider = manager._provider
    assert isinstance(provider, FakeVpnteProvider)

    async def probe(_proxy_url: str) -> str:
        return "203.0.113.1"

    restarted_pool = MarketIdentityPool(
        repository,
        VpnteTransportManager(provider),
        account_store=store,
        exit_ip_probe=probe,
    )

    assert await restarted_pool.reclaim_unowned_buyer_bindings() == 1
    binding = await repository.get_market_identity_binding("old-buyer-worker")
    assert binding is not None and binding["state"] == "released"

    await _worker(repository, job_id, "new-buyer-worker")
    new_lease = await restarted_pool.acquire("new-buyer-worker", job_id)
    assert new_lease is not None
    assert new_lease.registration_id == old_lease.registration_id


@pytest.mark.asyncio
async def test_pool_falls_back_to_free_ip_then_returns_account_to_preferred_ip_on_rebind(tmp_path: Path):
    pool, repository, job_id, store, manager = await _pool(
        tmp_path,
        slots=[1, 2],
        egress={1: "203.0.113.1", 2: "203.0.113.2"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    await _worker(repository, job_id, "worker-1")
    assert manager.acquire_slot("other-worker", 1) is not None

    fallback = await pool.acquire("worker-1", job_id)

    assert fallback is not None
    assert fallback.transport_id == "vpnte-slot-2"
    assert fallback.egress_ip == "203.0.113.2"
    assert fallback.binding_mode == "fallback"
    manager.release("vpnte-slot-1", "other-worker")

    rebound = await pool.rebind("worker-1", job_id, reason="test")

    assert rebound is not None
    assert rebound.transport_id == "vpnte-slot-1"
    assert rebound.egress_ip == "203.0.113.1"
    assert rebound.binding_mode == "preferred_ip"


@pytest.mark.asyncio
async def test_pool_never_leases_two_slots_with_the_same_egress_ip_and_caps_by_accounts(tmp_path: Path):
    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1, 2, 3],
        egress={1: "203.0.113.1", 2: "203.0.113.1", 3: "203.0.113.3"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    _save_account(store, index=2, signup_ip="203.0.113.2", slot=2)
    await asyncio.gather(_worker(repository, job_id, "worker-1"), _worker(repository, job_id, "worker-2"))

    assert await pool.capacity_for_job(job_id) == 2
    first, second = await asyncio.gather(pool.acquire("worker-1", job_id), pool.acquire("worker-2", job_id))

    assert first is not None and second is not None
    assert {first.egress_ip, second.egress_ip} == {"203.0.113.1", "203.0.113.3"}
    assert {first.transport_id, second.transport_id} == {"vpnte-slot-1", "vpnte-slot-3"}


@pytest.mark.asyncio
async def test_pool_only_leases_accounts_selected_for_the_job(tmp_path: Path):
    selected_account = "registration-2"
    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1, 2],
        egress={1: "203.0.113.1", 2: "203.0.113.2"},
        account_registration_ids=(selected_account,),
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    _save_account(store, index=2, signup_ip="203.0.113.2", slot=2)
    await asyncio.gather(_worker(repository, job_id, "worker-1"), _worker(repository, job_id, "worker-2"))

    assert await pool.capacity_for_job(job_id) == 1
    first, second = await asyncio.gather(pool.acquire("worker-1", job_id), pool.acquire("worker-2", job_id))

    assigned = [lease for lease in (first, second) if lease is not None]
    assert len(assigned) == 1
    assert assigned[0].registration_id == selected_account
    snapshot = await pool.snapshot(job_id=job_id)
    selected = {item["registration_id"] for item in snapshot["accounts"] if item["selected_for_job"]}
    assert selected == {selected_account}
    assert snapshot["summary"]["account_selection_mode"] == "manual"


@pytest.mark.asyncio
async def test_snapshot_reads_accounts_and_cached_routes_without_refreshing_vpnte(tmp_path: Path, monkeypatch):
    pool, repository, _job_id, store, manager = await _pool(
        tmp_path,
        slots=[1],
        egress={1: "203.0.113.1"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    await repository.upsert_transport(
        TransportSnapshot(
            transport_id="vpnte-slot-1",
            kind=TransportKind.VPNTE,
            health=TransportHealth.HEALTHY,
            slot=1,
            proxy_url="http://127.0.0.1:19001",
            egress_ip="203.0.113.1",
        )
    )

    def unexpected_refresh():
        raise AssertionError("fast account-pool snapshot must not refresh VPNTE")

    monkeypatch.setattr(manager, "refresh", unexpected_refresh)

    snapshot = await pool.snapshot(refresh_routes=False)

    assert [account["registration_id"] for account in snapshot["accounts"]] == ["registration-1"]
    assert snapshot["summary"]["effective_capacity"] == 1
    assert snapshot["routes"][0]["egress_ip"] == "203.0.113.1"


@pytest.mark.asyncio
async def test_live_snapshot_stops_durable_routes_missing_from_vpnte(tmp_path: Path):
    pool, repository, _job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1],
        egress={1: "203.0.113.1"},
    )
    _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    await repository.upsert_transport(
        TransportSnapshot(
            transport_id="vpnte-slot-2",
            kind=TransportKind.VPNTE,
            health=TransportHealth.HEALTHY,
            slot=2,
            proxy_url="http://127.0.0.1:19002",
            egress_ip="203.0.113.2",
        )
    )

    live = await pool.snapshot(refresh_routes=True)
    cached = await pool.snapshot(refresh_routes=False)

    durable = {item["transport_id"]: item for item in await repository.list_transports(limit=10)}
    assert live["summary"]["routes_healthy"] == 1
    assert live["summary"]["provider_profiles_total"] == 160
    assert live["summary"]["egress_ips_distinct"] == 1
    assert cached["summary"]["routes_healthy"] == 1
    assert cached["summary"]["egress_ips_distinct"] == 1
    assert durable["vpnte-slot-2"]["health"] == "stopped"
    assert durable["vpnte-slot-2"]["proxy_url"] is None


@pytest.mark.asyncio
async def test_pool_rotation_rebinds_when_vpnte_returns_another_workers_ip(tmp_path: Path):
    def rotate_slot(slot: int, instances: list[dict[str, Any]]) -> None:
        assert slot == 1
        next(item for item in instances if item["slot"] == slot)["proxyUrl"] = "http://127.0.0.1:19004"

    pool, repository, job_id, store, _manager = await _pool(
        tmp_path,
        slots=[1, 2, 3],
        egress={
            1: "203.0.113.1",
            2: "203.0.113.2",
            3: "203.0.113.3",
            4: "203.0.113.2",
        },
        on_rotate=rotate_slot,
    )
    first_account = _save_account(store, index=1, signup_ip="203.0.113.1", slot=1)
    _save_account(store, index=2, signup_ip="203.0.113.2", slot=2)
    await asyncio.gather(_worker(repository, job_id, "worker-1"), _worker(repository, job_id, "worker-2"))

    first, second = await asyncio.gather(pool.acquire("worker-1", job_id), pool.acquire("worker-2", job_id))
    assert first is not None and second is not None
    assert first.transport_id == "vpnte-slot-1"
    assert second.transport_id == "vpnte-slot-2"

    rotated = await pool.rotate("worker-1", job_id)

    assert rotated is not None
    assert rotated.registration_id == first_account
    assert rotated.transport_id == "vpnte-slot-3"
    assert rotated.egress_ip == "203.0.113.3"
    active = await repository.list_market_identity_bindings(job_id=job_id, active_only=True)
    assert {item["current_egress_ip"] for item in active} == {"203.0.113.2", "203.0.113.3"}
