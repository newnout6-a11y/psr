from __future__ import annotations

import asyncio

import pytest

from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, OperationKind
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.worker import MarketWorker


@pytest.mark.asyncio
async def test_worker_renews_long_running_operation_lease(tmp_path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    job_id = "job_long_operation"
    await coordinator.create_job(
        MarketJobCreate(scope=MarketScope(category_id=1)),
        job_id=job_id,
    )
    for worker_id in ("worker-1", "worker-2"):
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
    operation = await repository.lease_operation("worker-1", job_id=job_id, lease_seconds=1)
    assert operation is not None
    entered = asyncio.Event()

    async def long_handler(_worker, _operation):
        entered.set()
        await asyncio.sleep(1.4)

    worker = MarketWorker(
        coordinator,
        job_id=job_id,
        worker_id="worker-1",
        generation=1,
        handlers={OperationKind.MAP_SCOPE: long_handler},
        lease_seconds=1,
    )
    running = asyncio.create_task(worker._execute(operation))
    await entered.wait()
    await asyncio.sleep(1.1)

    stolen = await repository.lease_operation("worker-2", job_id=job_id, lease_seconds=1)

    assert stolen is None
    await running
    completed = await repository.get_operation(str(operation["operation_id"]))
    assert completed is not None
    assert completed["state"] == "succeeded"
