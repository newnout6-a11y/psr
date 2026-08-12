from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from src.platforms.kwork_buyer.supervisor import (
    CANARY_WORKER_CAPS,
    BuyerDiscoveryCapacityMode,
    BuyerDiscoveryFleetState,
    BuyerDiscoverySupervisor,
    BuyerDiscoveryWorkerState,
)
from src.platforms.kwork_buyer.worker import (
    BuyerDiscoveryIdentity,
    BuyerDiscoveryOutcome,
    BuyerDiscoveryRunResult,
    BuyerDiscoverySourceError,
)


def identity(index: int, *, account: int | None = None, transport: int | None = None, egress: int | None = None) -> BuyerDiscoveryIdentity:
    return BuyerDiscoveryIdentity(
        worker_id=f"buyer-worker-{index}",
        account_registration_id=f"account-{account or index}",
        transport_id=f"transport-{transport or index}",
        egress_ip=f"203.0.113.{egress or index}",
        route_generation=1,
    )


class IdleRepository:
    def __init__(self) -> None:
        self.lease_calls = 0

    async def lease_query_task(self, *args: Any, **kwargs: Any) -> None:
        self.lease_calls += 1
        return None


class FakeAllocator:
    def __init__(self, identities: Sequence[BuyerDiscoveryIdentity]) -> None:
        self.identities = tuple(identities)
        self.calls: list[dict[str, Any]] = []

    async def allocate(
        self,
        *,
        run_id: str,
        requested_workers: int,
        active_identities: Sequence[BuyerDiscoveryIdentity],
    ) -> Sequence[BuyerDiscoveryIdentity]:
        self.calls.append(
            {
                "run_id": run_id,
                "requested_workers": requested_workers,
                "active": tuple(active_identities),
            }
        )
        return self.identities


class FakeReleaser:
    def __init__(self) -> None:
        self.releases: list[tuple[str, BuyerDiscoveryIdentity, str]] = []

    async def release(self, *, run_id: str, identity: BuyerDiscoveryIdentity, reason: str) -> None:
        self.releases.append((run_id, identity, reason))


class FakeInventory:
    def __init__(self, identities: Sequence[BuyerDiscoveryIdentity]) -> None:
        self.identities = tuple(identities)
        self.calls = 0

    async def inspect(self, *, run_id: str, requested_workers: int) -> Sequence[BuyerDiscoveryIdentity]:
        self.calls += 1
        return self.identities


class FakeSource:
    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"response": []}


class FakeSourceFactory:
    def __init__(self) -> None:
        self.identities: list[BuyerDiscoveryIdentity] = []

    async def create(self, *, run_id: str, identity: BuyerDiscoveryIdentity) -> FakeSource:
        self.identities.append(identity)
        return FakeSource()


class RetryRepository:
    def __init__(self) -> None:
        self._leased = False

    async def lease_query_task(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if self._leased:
            return None
        self._leased = True
        return {
            "task_id": "task-1",
            "query_id": "query-1",
            "attempt_id": "attempt-1",
            "lease_fence": 1,
            "source": "mobile_projects",
            "page": 1,
        }

    async def append_event(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def retry_query_task(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def update_run(self, *args: Any, **kwargs: Any) -> None:
        return None


class RecordingPageRepository:
    def __init__(self) -> None:
        self._leased = False
        self.commits: list[dict[str, Any]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def lease_query_task(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if self._leased:
            return None
        self._leased = True
        return {
            "task_id": "task-1",
            "query_id": "query-1",
            "attempt_id": "attempt-1",
            "lease_fence": 1,
            "source": "mobile_projects",
            "page": 1,
        }

    async def commit_observed_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.commits.append(dict(kwargs))
        return {"ok": True}

    async def append_event(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((run_id, event_type, dict(payload)))

    async def update_run(self, *args: Any, **kwargs: Any) -> None:
        return None


class AttachmentReservationRepository:
    def __init__(self) -> None:
        self.task_available = False
        self._leased = False

    async def lease_query_task(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if not self.task_available or self._leased:
            return None
        self._leased = True
        return {
            "task_id": "task-after-attachment-release",
            "query_id": "query-after-attachment-release",
            "attempt_id": "attempt-after-attachment-release",
            "lease_fence": 1,
            "source": "mobile_projects",
            "page": 1,
        }

    async def append_event(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def retry_query_task(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def update_run(self, *args: Any, **kwargs: Any) -> None:
        return None


class ForbiddenSource:
    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        raise BuyerDiscoverySourceError("forbidden", status_code=403)


class ForbiddenSourceFactory:
    async def create(self, *, run_id: str, identity: BuyerDiscoveryIdentity) -> ForbiddenSource:
        return ForbiddenSource()


class RateLimitedSource:
    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        raise BuyerDiscoverySourceError("rate limited", status_code=429, retry_after_seconds=2)


class RateLimitedSourceFactory:
    async def create(self, *, run_id: str, identity: BuyerDiscoveryIdentity) -> RateLimitedSource:
        return RateLimitedSource()


class AttachmentReservationSource:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        raise BuyerDiscoverySourceError("temporary", status_code=500)


class AttachmentReservationSourceFactory:
    def __init__(self) -> None:
        self.source = AttachmentReservationSource()

    async def create(self, *, run_id: str, identity: BuyerDiscoveryIdentity) -> AttachmentReservationSource:
        return self.source


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        self.items.append((run_id, event_type, dict(payload)))


async def wait_until(predicate: Any, *, timeout: float = 0.75) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for expected supervisor state")


def supervisor(
    allocator: FakeAllocator,
    releaser: FakeReleaser,
    factory: FakeSourceFactory,
    *,
    inventory: FakeInventory | None = None,
    events: Events | None = None,
    heartbeat: Any = None,
    idle_poll_interval_seconds: float = 0.05,
    reconcile_interval_seconds: float = 1.0,
    heartbeat_interval_seconds: float = 0.02,
) -> BuyerDiscoverySupervisor:
    return BuyerDiscoverySupervisor(
        IdleRepository(),
        identity_allocator=allocator,
        identity_releaser=releaser,
        source_factory=factory,
        capacity_inventory=inventory,
        event_hook=events,
        heartbeat_hook=heartbeat,
        idle_poll_interval_seconds=idle_poll_interval_seconds,
        reconcile_interval_seconds=reconcile_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


@pytest.mark.asyncio
async def test_preflight_requires_unique_account_transport_and_egress_and_uses_canary_caps() -> None:
    first = identity(1)
    duplicate_account = identity(2, account=1)
    second = identity(3)
    inventory = FakeInventory([first, duplicate_account, second])
    active_supervisor = supervisor(FakeAllocator([]), FakeReleaser(), FakeSourceFactory(), inventory=inventory)

    preflight = await active_supervisor.preflight("run-1", requested_workers=5, canary_cap=5)

    assert CANARY_WORKER_CAPS == (2, 5, 10, 20, 30)
    assert preflight.eligible_accounts == 2
    assert preflight.healthy_transports == 3
    assert preflight.unique_egress_ips == 3
    assert preflight.effective_capacity == 2
    assert preflight.collision_count == 1
    assert "unique_account_capacity" in preflight.reasons
    assert "identity_collisions" in preflight.reasons
    with pytest.raises(ValueError, match="canary_cap"):
        await active_supervisor.preflight("run-1", requested_workers=2, canary_cap=3)


@pytest.mark.asyncio
async def test_configured_worker_ceiling_accepts_non_canary_limits_and_rejects_higher_requests() -> None:
    identities = [identity(1), identity(2), identity(3)]
    active_supervisor = BuyerDiscoverySupervisor(
        IdleRepository(),
        identity_allocator=FakeAllocator(identities),
        identity_releaser=FakeReleaser(),
        source_factory=FakeSourceFactory(),
        capacity_inventory=FakeInventory(identities),
        max_workers=3,
    )

    preflight = await active_supervisor.preflight("run-1", requested_workers=3)

    assert preflight.canary_cap == 3
    with pytest.raises(ValueError, match="requested_workers"):
        await active_supervisor.preflight("run-1", requested_workers=4)
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_strict_start_blocks_before_allocating_when_inventory_cannot_meet_canary() -> None:
    only_identity = identity(1)
    allocator = FakeAllocator([only_identity])
    events = Events()
    active_supervisor = supervisor(
        allocator,
        FakeReleaser(),
        FakeSourceFactory(),
        inventory=FakeInventory([only_identity]),
        events=events,
    )

    snapshot = await active_supervisor.start(
        "run-1",
        requested_workers=2,
        canary_cap=2,
        capacity_mode=BuyerDiscoveryCapacityMode.STRICT,
    )

    assert snapshot.state is BuyerDiscoveryFleetState.BLOCKED
    assert snapshot.effective_workers == 0
    assert allocator.calls == []
    assert [event_type for _, event_type, _ in events.items] == ["capacity.changed", "run.blocked"]
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_strict_start_releases_partial_allocator_reservations_without_an_inventory() -> None:
    only_identity = identity(1)
    releaser = FakeReleaser()
    events = Events()
    factory = FakeSourceFactory()
    active_supervisor = supervisor(FakeAllocator([only_identity]), releaser, factory, events=events)

    snapshot = await active_supervisor.start(
        "run-1",
        requested_workers=2,
        canary_cap=2,
        capacity_mode=BuyerDiscoveryCapacityMode.STRICT,
    )

    assert snapshot.state is BuyerDiscoveryFleetState.BLOCKED
    assert factory.identities == []
    assert releaser.releases == [("run-1", only_identity, "strict_capacity_shortfall")]
    assert "run.started" not in [event_type for _, event_type, _ in events.items]
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_elastic_start_creates_one_worker_per_unique_identity_and_releases_rejected_reservations() -> None:
    first = identity(1)
    duplicate_account = identity(2, account=1)
    second = identity(3)
    allocator = FakeAllocator([first, duplicate_account, second])
    releaser = FakeReleaser()
    factory = FakeSourceFactory()
    active_supervisor = supervisor(
        allocator,
        releaser,
        factory,
        inventory=FakeInventory([first, duplicate_account, second]),
    )

    snapshot = await active_supervisor.start("run-1", requested_workers=5, canary_cap=5)
    await wait_until(lambda: active_supervisor.snapshot("run-1").effective_workers == 2)
    await wait_until(
        lambda: all(
            worker.state in {BuyerDiscoveryWorkerState.RUNNING, BuyerDiscoveryWorkerState.IDLE}
            for worker in active_supervisor.snapshot("run-1").workers
        )
    )

    assert snapshot.state is BuyerDiscoveryFleetState.RUNNING
    assert {item.account_registration_id for item in factory.identities} == {"account-1", "account-3"}
    assert len(factory.identities) == 2
    assert releaser.releases[0][1] is duplicate_account
    assert releaser.releases[0][2] == "capacity_collision_or_excess"
    assert all(
        worker.state in {BuyerDiscoveryWorkerState.RUNNING, BuyerDiscoveryWorkerState.IDLE}
        for worker in active_supervisor.snapshot("run-1").workers
    )

    stopped = await active_supervisor.stop("run-1")

    assert stopped.state is BuyerDiscoveryFleetState.STOPPED
    released_worker_ids = [item[1].worker_id for item in releaser.releases]
    assert released_worker_ids.count(first.worker_id) == 1
    assert released_worker_ids.count(second.worker_id) == 1
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_repeated_idle_polls_emit_only_one_idle_transition() -> None:
    only_identity = identity(1)
    repository = IdleRepository()
    events = Events()
    active_supervisor = BuyerDiscoverySupervisor(
        repository,
        identity_allocator=FakeAllocator([only_identity]),
        identity_releaser=FakeReleaser(),
        source_factory=FakeSourceFactory(),
        capacity_inventory=FakeInventory([only_identity]),
        event_hook=events,
        idle_poll_interval_seconds=0.01,
        reconcile_interval_seconds=5.0,
        heartbeat_interval_seconds=1.0,
    )

    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: repository.lease_calls >= 5)

    event_types = [event_type for _, event_type, _ in events.items]
    worker_states = [payload["state"] for _, event_type, payload in events.items if event_type == "worker.state"]
    assert event_types.count("worker.idle") == 1
    assert worker_states == ["running", "idle"]

    await active_supervisor.stop("run-1")
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_supervisor_does_not_duplicate_committed_page_outcome_event() -> None:
    only_identity = identity(1)
    repository = RecordingPageRepository()
    results: list[BuyerDiscoveryRunResult] = []

    async def record_result(
        run_id: str,
        current: BuyerDiscoveryIdentity,
        result: BuyerDiscoveryRunResult,
    ) -> None:
        results.append(result)

    active_supervisor = BuyerDiscoverySupervisor(
        repository,
        identity_allocator=FakeAllocator([only_identity]),
        identity_releaser=FakeReleaser(),
        source_factory=FakeSourceFactory(),
        capacity_inventory=FakeInventory([only_identity]),
        event_hook=repository.append_event,
        result_hook=record_result,
        idle_poll_interval_seconds=0.01,
        reconcile_interval_seconds=5.0,
        heartbeat_interval_seconds=1.0,
    )

    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: len(repository.commits) == 1)
    await wait_until(
        lambda: active_supervisor.snapshot("run-1").workers[0].state is BuyerDiscoveryWorkerState.IDLE
    )

    event_types = [event_type for _, event_type, _ in repository.events]
    assert event_types.count("query.page.started") == 1
    assert event_types.count("query.page.completed") == 1
    assert [result.outcome for result in results] == [BuyerDiscoveryOutcome.COMMITTED]

    await active_supervisor.stop("run-1")
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_pause_resume_stop_exposes_heartbeat_and_releases_identity_once() -> None:
    only_identity = identity(1)
    allocator = FakeAllocator([only_identity])
    releaser = FakeReleaser()
    events = Events()
    heartbeat_calls: list[str] = []

    async def heartbeat(run_id: str, current: BuyerDiscoveryIdentity, payload: Mapping[str, Any]) -> bool:
        heartbeat_calls.append(current.worker_id)
        return True

    active_supervisor = supervisor(
        allocator,
        releaser,
        FakeSourceFactory(),
        inventory=FakeInventory([only_identity]),
        events=events,
        heartbeat=heartbeat,
    )
    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: bool(heartbeat_calls))

    await active_supervisor.pause("run-1")
    await wait_until(lambda: active_supervisor.snapshot("run-1").state is BuyerDiscoveryFleetState.PAUSED)
    assert active_supervisor.snapshot("run-1").workers[0].state is BuyerDiscoveryWorkerState.PAUSED

    resumed = await active_supervisor.resume("run-1")
    assert resumed.state is BuyerDiscoveryFleetState.RUNNING
    await wait_until(
        lambda: active_supervisor.snapshot("run-1").workers[0].state
        in {BuyerDiscoveryWorkerState.RUNNING, BuyerDiscoveryWorkerState.IDLE}
    )

    stopped = await active_supervisor.stop("run-1")

    assert stopped.state is BuyerDiscoveryFleetState.STOPPED
    assert [entry[1] for entry in releaser.releases] == [only_identity]
    event_types = [event_type for _, event_type, _ in events.items]
    assert "worker.heartbeat" in event_types
    assert "run.paused" in event_types
    assert "run.resumed" in event_types
    assert "run.stopped" in event_types
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_account_reservation_blocks_discovery_until_the_same_identity_is_released() -> None:
    only_identity = identity(1)
    repository = AttachmentReservationRepository()
    source_factory = AttachmentReservationSourceFactory()
    active_supervisor = BuyerDiscoverySupervisor(
        repository,
        identity_allocator=FakeAllocator([only_identity]),
        identity_releaser=FakeReleaser(),
        source_factory=source_factory,
        capacity_inventory=FakeInventory([only_identity]),
        idle_poll_interval_seconds=0.01,
        reconcile_interval_seconds=5.0,
    )
    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: active_supervisor.snapshot("run-1").workers[0].state is BuyerDiscoveryWorkerState.IDLE)

    reservation = await active_supervisor.reserve_account_identity("account-1")
    repository.task_available = True
    await active_supervisor.wake("run-1")
    await asyncio.sleep(0.05)

    assert source_factory.source.calls == 0

    await active_supervisor.release_attachment_identity(reservation)
    await wait_until(lambda: source_factory.source.calls == 1)
    await active_supervisor.stop("run-1")
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_failed_heartbeat_cancels_worker_and_releases_the_account_transport_egress_lease() -> None:
    only_identity = identity(1)
    releaser = FakeReleaser()
    events = Events()

    async def heartbeat(run_id: str, current: BuyerDiscoveryIdentity, payload: Mapping[str, Any]) -> bool:
        return False

    active_supervisor = supervisor(
        FakeAllocator([only_identity]),
        releaser,
        FakeSourceFactory(),
        inventory=FakeInventory([only_identity]),
        events=events,
        heartbeat=heartbeat,
        reconcile_interval_seconds=5.0,
    )
    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: any(reason == "heartbeat_lost" for _, _, reason in releaser.releases))

    assert any(event_type == "worker.lease_lost" for _, event_type, _ in events.items)
    assert releaser.releases[0][1] is only_identity
    await active_supervisor.stop("run-1")
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_http_403_requests_identity_rebind_after_persisting_retry() -> None:
    only_identity = identity(1)
    releaser = FakeReleaser()
    events = Events()
    active_supervisor = BuyerDiscoverySupervisor(
        RetryRepository(),
        identity_allocator=FakeAllocator([only_identity]),
        identity_releaser=releaser,
        source_factory=ForbiddenSourceFactory(),
        capacity_inventory=FakeInventory([only_identity]),
        event_hook=events,
        idle_poll_interval_seconds=0.05,
        reconcile_interval_seconds=5.0,
    )

    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: any(reason == "http_403" for _, _, reason in releaser.releases))

    assert any(event_type == "identity.rebind_requested" for _, event_type, _ in events.items)
    await active_supervisor.stop("run-1")
    await active_supervisor.close()


@pytest.mark.asyncio
async def test_http_429_temporarily_lowers_capacity_and_recovers_after_backoff_expiry() -> None:
    only_identity = identity(1)
    allocator = FakeAllocator([only_identity])
    releaser = FakeReleaser()
    events = Events()
    active_supervisor = BuyerDiscoverySupervisor(
        RetryRepository(),
        identity_allocator=allocator,
        identity_releaser=releaser,
        source_factory=RateLimitedSourceFactory(),
        capacity_inventory=FakeInventory([only_identity]),
        event_hook=events,
        idle_poll_interval_seconds=0.05,
        reconcile_interval_seconds=5.0,
    )

    await active_supervisor.start("run-1", requested_workers=1, canary_cap=2)
    await wait_until(lambda: any(event_type == "capacity.throttle_applied" for _, event_type, _ in events.items))
    await wait_until(lambda: any(reason == "http_429" for _, _, reason in releaser.releases))

    throttled = active_supervisor.snapshot("run-1")
    assert throttled.adaptive_worker_cap == 1
    assert throttled.throttle_until
    assert throttled.throttle_failures == 1
    assert len(allocator.calls) == 1
    assert any(event_type == "identity.quarantine_requested" for _, event_type, _ in events.items)

    # The reconciler may wake on the release, but the active cooldown keeps it
    # from immediately reacquiring the same route.
    await active_supervisor.reconcile("run-1")
    assert len(allocator.calls) == 1

    active_supervisor._get_fleet("run-1").throttle_until = "2000-01-01T00:00:00.000Z"
    recovered = await active_supervisor.reconcile("run-1")
    await wait_until(lambda: any(event_type == "capacity.throttle_recovered" for _, event_type, _ in events.items))

    assert recovered.adaptive_worker_cap is None
    assert recovered.throttle_until is None
    assert recovered.throttle_failures == 0
    assert len(allocator.calls) == 2
    await active_supervisor.stop("run-1")
    await active_supervisor.close()
