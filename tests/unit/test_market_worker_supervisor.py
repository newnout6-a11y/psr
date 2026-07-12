from __future__ import annotations

import asyncio

import pytest

from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, NetworkPolicy, OperationKind, OperationState, TransportHealth, TransportKind, TransportSnapshot, WorkerState
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor
from src.platforms.kwork_supply.worker import MarketWorker


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def make_coordinator(tmp_path) -> MarketScanCoordinator:
    return MarketScanCoordinator(MarketJobRepository(tmp_path / "market-workers.sqlite3"))


@pytest.mark.asyncio
async def test_supervisor_runs_stable_worker_and_recovers_initial_mapping_operation(tmp_path):
    coordinator = make_coordinator(tmp_path)
    handled: list[str] = []

    async def handle_mapping(worker, operation) -> None:
        handled.append(operation["operation_id"])

    await coordinator.create_job(
        MarketJobCreate(scope=MarketScope(category_id=38, canonical_alias="website-repair"), desired_workers=1),
        job_id="job_worker",
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers={OperationKind.MAP_SCOPE: handle_mapping},
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        async def completed() -> bool:
            operations = await coordinator.repository.list_operations("job_worker")
            return bool(operations) and operations[0]["state"] == OperationState.SUCCEEDED.value

        await wait_until(completed)
        workers = await coordinator.repository.list_workers(job_id="job_worker")
        assert handled
        assert workers[0]["worker_id"] == "worker-job_worker-01"
        assert workers[0]["generation"] == 1
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_pause_drains_workers_without_leasing_new_work(tmp_path):
    coordinator = make_coordinator(tmp_path)

    async def handle_mapping(worker, operation) -> None:
        await asyncio.sleep(0.2)

    await coordinator.create_job(
        MarketJobCreate(scope=MarketScope(category_id=38, canonical_alias="website-repair"), desired_workers=1),
        job_id="job_paused",
    )
    await coordinator.pause_job("job_paused")
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers={OperationKind.MAP_SCOPE: handle_mapping},
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        async def paused() -> bool:
            job = await coordinator.repository.get_job("job_paused")
            return job is not None and job["state"] == "paused"

        await wait_until(paused)
        operation = (await coordinator.repository.list_operations("job_paused"))[0]
        assert operation["state"] == OperationState.QUEUED.value
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_supervisor_applies_protection_and_healthy_transport_caps_without_mutating_configuration(tmp_path):
    coordinator = make_coordinator(tmp_path)
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            desired_workers=2,
            network_policy=NetworkPolicy.VPNTE_ONLY,
        ),
        job_id="job_adaptive",
    )
    await coordinator.repository.upsert_transport(
        TransportSnapshot(
            transport_id="vpnte-slot-1",
            kind=TransportKind.VPNTE,
            health=TransportHealth.HEALTHY,
            slot=1,
            proxy_url="http://127.0.0.1:17990",
        )
    )
    initial = await coordinator.repository.lease_operation("worker_protection", job_id="job_adaptive")
    assert initial is not None
    await coordinator.repository.fail_operation(
        initial["operation_id"],
        "worker_protection",
        "HTTP 429",
        failure_kind="rate_limited",
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers={},
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        assert supervisor.worker_ids == ("worker-job_adaptive-01",)
        job = await coordinator.repository.get_job("job_adaptive")
        events = await coordinator.replay_events("job_adaptive")
        assert job is not None and job["desired_workers"] == 2
        advice = [event for event in events if event["type"] == "worker.concurrency_recommended"]
        assert advice[-1]["payload"]["effective_workers"] == 1
        assert advice[-1]["payload"]["configured_workers"] == 2
        assert advice[-1]["payload"]["advisory"] is True
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_idle_worker_polls_do_not_flood_durable_events(tmp_path):
    coordinator = make_coordinator(tmp_path)
    await coordinator.create_job(
        MarketJobCreate(scope=MarketScope(category_id=38, canonical_alias="website-repair"), desired_workers=1),
        job_id="job_worker_events",
    )
    worker = MarketWorker(
        coordinator,
        job_id="job_worker_events",
        worker_id="worker-job_worker_events-01",
        generation=1,
        handlers={},
    )

    await worker._persist_state(WorkerState.IDLE)
    await worker._persist_state(WorkerState.LEASING)
    await worker._persist_state(WorkerState.IDLE)
    await worker._persist_state(WorkerState.BUSY)
    await worker._persist_state(WorkerState.BUSY)

    events = await coordinator.replay_events("job_worker_events")
    state_events = [event for event in events if event["type"] == "worker.state_changed"]
    assert len(state_events) == 1
    assert state_events[0]["payload"]["actual_state"] == WorkerState.BUSY.value
