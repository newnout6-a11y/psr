"""End-to-end coverage for the bounded mobile-only market source policy."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, SourcePolicy
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor


class FakeMobileRuntimeClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.mobile_calls: list[dict[str, int | None]] = []
        self.web_calls = 0

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
    ) -> Mapping[str, Any]:
        self.mobile_calls.append(
            {"category_id": category_id, "classifier_id": classifier_id, "page": page}
        )
        return self.response

    async def get_web_catalog_filters(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.web_calls += 1
        raise AssertionError("mobile_first_page_only must not request web catalog data")


class MobileRuntimeClientFactory:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.clients: list[FakeMobileRuntimeClient] = []

    def __call__(self, _proxy_url: str | None) -> FakeMobileRuntimeClient:
        client = FakeMobileRuntimeClient(self.response)
        self.clients.append(client)
        return client


async def _wait_for_completed(coordinator: MarketScanCoordinator, job_id: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        job = await coordinator.repository.get_job(job_id)
        if job is not None and job["state"] == "completed":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("bounded mobile market job did not complete")


@pytest.mark.asyncio
async def test_mobile_first_page_only_completes_without_web_or_continuation(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = MobileRuntimeClientFactory(
        {
            "kworks_count": 900,
            "paging": {"page": 1, "total": 900},
            "kworks": [
                {"id": 501, "title": "Mobile one", "price": 20_000},
                {"id": 502, "title": "Mobile two", "price": 20_000},
            ],
            "_request_params": {"categoryId": 38, "classifierId": 1271, "page": 1},
        }
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, classifier_id=1271),
            source_policy=SourcePolicy.MOBILE_FIRST_PAGE_ONLY,
            target_unique_cards=10,
            desired_workers=1,
            request_budget=100,
        ),
        job_id="job_mobile_runtime",
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers=executor.handlers,
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        job = await _wait_for_completed(coordinator, "job_mobile_runtime")
    finally:
        await supervisor.close()

    operations = await repository.list_operations("job_mobile_runtime", limit=20)
    fetch = next(operation for operation in operations if operation["kind"] == "fetch_batch")
    attempts = await repository.list_operation_attempts(fetch["operation_id"])
    snapshot = await repository.load_export_snapshot("job_mobile_runtime")
    raw_files = list((tmp_path / "artifacts" / "job_mobile_runtime" / "raw").glob("*.json.gz"))
    assert job["counters"]["unique_cards"] == 2
    assert job["counters"]["requests"] == 1
    assert len(factory.clients) == 1
    assert factory.clients[0].mobile_calls == [{"category_id": 38, "classifier_id": 1271, "page": 1}]
    assert factory.clients[0].web_calls == 0
    assert fetch["payload"]["cursor"]["page"] == 1
    assert fetch["payload"]["remaining_requests"] == 1
    assert attempts[0]["requested_cursor"]["page"] == 1
    assert attempts[0]["reported_cursor"]["page"] == 1
    assert attempts[0]["raw_response_ref"]
    assert len(snapshot["observations"]) == 2
    assert all(row["source"] == "mobile_kworks" for row in snapshot["observations"])
    assert all(row["requested_cursor"]["page"] == 1 for row in snapshot["observations"])
    assert all(row["reported_cursor"]["page"] == 1 for row in snapshot["observations"])
    assert raw_files
    assert all(operation["kind"] != "fetch_batch" or operation["state"] == "succeeded" for operation in operations)
