"""Synthetic crash-recovery coverage for the durable market-job path."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor


def raw_catalog(cards: list[Mapping[str, object]]) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "stateData": {
                "viewData": {
                    "filters": {"activeCategoryId": 38, "kworksCount": 100},
                    "kworks": {"total": 20, "total_found": 100, "posts": {"data": cards}},
                }
            }
        },
    }


class FakeWebClient:
    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw

    async def get_web_catalog_filters(self, alias: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "endpoint": f"/catalog_kworks_filters/{alias}",
            "url": f"https://kwork.ru/catalog_kworks_filters/{alias}",
            "status_code": 200,
            "content_type": "application/json",
            "bytes": 100,
            "request_params": {"page": 1, "pageSize": 10, **dict(kwargs.get("filters") or {})},
            "protection_status": "ok",
            "raw": self.raw,
        }


class FakeClientFactory:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses

    def __call__(self, _proxy_url: str | None) -> FakeWebClient:
        return FakeWebClient(self.responses.pop(0))


async def wait_for_completed(coordinator: MarketScanCoordinator, job_id: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        job = await coordinator.repository.get_job(job_id)
        if job is not None and job["state"] == "completed":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("market job did not complete")


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_and_job_continues_without_duplicate_cards(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=4,
            desired_workers=1,
            request_budget=2,
        ),
        job_id="job_resume",
    )
    # This simulates a process that died while holding the initial mapping lease.
    leased = await repository.lease_operation(
        "worker-crashed",
        job_id="job_resume",
        lease_seconds=1,
        now="2020-01-01T00:00:00Z",
    )
    assert leased is not None

    executor = MarketOperationExecutor(
        coordinator,
        client_factory=FakeClientFactory(
            [
                raw_catalog([{"id": 1}, {"id": 2}]),
                raw_catalog([{"id": 1}, {"id": 2}]),
                raw_catalog([{"id": 3}, {"id": 4}]),
            ]
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers=executor.handlers,
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        job = await wait_for_completed(coordinator, "job_resume")
    finally:
        await supervisor.close()

    listings = await repository.list_listings("job_resume")
    events = await coordinator.replay_events("job_resume")
    assert job["counters"]["unique_cards"] == 4
    assert [listing["listing_key"] for listing in listings] == ["1", "2", "3", "4"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert len(list((tmp_path / "artifacts" / "job_resume" / "raw").glob("*.json.gz"))) == 3
    exports_dir = tmp_path / "artifacts" / "job_resume" / "exports"
    assert (exports_dir / "summary.json").exists()
    assert (exports_dir / "listings.jsonl.gz").exists()
    assert any(event["type"] == "job.metrics" for event in events)
    assert any(event["type"] == "result.ready" for event in events)
    checkpoints = await repository.list_checkpoints("job_resume", limit=1)
    assert checkpoints[0]["metrics"]["enrichment_selection"]["selected_count"] == 4
    assert checkpoints[0]["metrics"]["ai_evidence"]["sample_based"] is True


@pytest.mark.asyncio
async def test_completed_job_resumes_the_same_shard_to_a_deeper_target(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=4,
            desired_workers=1,
            request_budget=2,
        ),
        job_id="job_deepen_resume",
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=FakeClientFactory(
            [
                raw_catalog([{"id": 1}, {"id": 2}]),
                raw_catalog([{"id": 1}, {"id": 2}]),
                raw_catalog([{"id": 3}, {"id": 4}]),
                raw_catalog([{"id": 5}, {"id": 6}]),
            ]
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers=executor.handlers,
        reconcile_interval_seconds=0.01,
        worker_poll_interval_seconds=0.01,
    )
    await supervisor.start()
    try:
        completed = await wait_for_completed(coordinator, "job_deepen_resume")
        reconfigured = await coordinator.update_job(
            "job_deepen_resume",
            target_unique_cards=6,
            expected_revision=completed["revision"],
        )
        resumed = await coordinator.resume_job(
            "job_deepen_resume",
            expected_revision=reconfigured["revision"],
        )
        deep_completed = await wait_for_completed(coordinator, "job_deepen_resume")
    finally:
        await supervisor.close()

    listings = await repository.list_listings("job_deepen_resume")
    operations = await repository.list_operations("job_deepen_resume")
    checkpoints = await repository.list_checkpoints("job_deepen_resume", limit=10)
    assert resumed["state"] == "running"
    assert resumed["phase"] == "collect"
    assert deep_completed["counters"]["unique_cards"] == 6
    assert [listing["listing_key"] for listing in listings] == ["1", "2", "3", "4", "5", "6"]
    assert len([operation for operation in operations if operation["kind"] == "analyze_snapshot"]) == 2
    assert len([operation for operation in operations if operation["kind"] == "export_snapshot"]) == 2
    assert len(checkpoints) >= 2
