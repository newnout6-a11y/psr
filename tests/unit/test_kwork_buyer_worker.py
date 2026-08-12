from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from src.platforms.kwork_buyer.mapper import MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE
from src.platforms.kwork_buyer.worker import (
    BuyerDiscoveryFailureKind,
    BuyerDiscoveryIdentity,
    BuyerDiscoveryOutcome,
    BuyerDiscoverySourceError,
    BuyerDiscoveryWorker,
    BuyerDiscoveryWorkerError,
)


def task(
    *,
    source: str = MOBILE_PROJECT_SOURCE,
    lease_fence: int = 7,
    retry_count: int = 0,
    cursor: Mapping[str, Any] | None = None,
    query_origin: str | None = None,
) -> dict[str, Any]:
    payload = {
        "task_id": "task-1",
        "query_id": "query-1",
        "attempt_id": "attempt-1",
        "lease_fence": lease_fence,
        "retry_count": retry_count,
        "source": source,
        "page": 1,
        "cursor": cursor,
        "raw_artifact_id": "artifact-1",
    }
    if query_origin is not None:
        payload["query_origin"] = query_origin
    return payload


class FakeRepository:
    def __init__(self, tasks: list[Mapping[str, Any] | None]) -> None:
        self.tasks = tasks
        self.leases: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.commits: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.retries: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.failures: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.run_updates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def lease_query_task(self, *args: Any, **kwargs: Any) -> Mapping[str, Any] | None:
        self.leases.append((args, kwargs))
        return self.tasks.pop(0) if self.tasks else None

    async def commit_observed_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.commits.append((args, kwargs))
        return {"ok": True}

    async def append_event(self, *args: Any, **kwargs: Any) -> None:
        self.events.append((args, kwargs))

    async def retry_query_task(self, *args: Any, **kwargs: Any) -> None:
        self.retries.append((args, kwargs))

    async def fail_query_task(self, *args: Any, **kwargs: Any) -> None:
        self.failures.append((args, kwargs))

    async def update_run(self, *args: Any, **kwargs: Any) -> None:
        self.run_updates.append((args, kwargs))


class FakeSource:
    def __init__(self, response: Mapping[str, Any] | Exception) -> None:
        self.response = response
        self.calls = 0
        self.max_in_flight = 0
        self._in_flight = 0

    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0)
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        finally:
            self._in_flight -= 1


class RetryableAdapterError(RuntimeError):
    retry_after_seconds = 4


def worker(repository: FakeRepository, source: FakeSource) -> BuyerDiscoveryWorker:
    return BuyerDiscoveryWorker(
        repository,
        source,
        run_id="run-1",
        identity=BuyerDiscoveryIdentity(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="203.0.113.10",
            route_generation=3,
        ),
    )


@pytest.mark.asyncio
async def test_worker_leases_readonly_mobile_page_and_commits_fenced_canonical_payloads() -> None:
    repository = FakeRepository([task()])
    source = FakeSource(
        {
            "observed_at": "2026-07-16T12:00:00Z",
            "response": [{"id": "101", "title": "Telegram bot", "description": "Build a bot", "price": 1_000}],
        }
    )

    result = await worker(repository, source).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    assert repository.leases[0][1]["identity"]["account_registration_id"] == "account-1"
    assert repository.commits[0][0] == ()
    assert repository.commits[0][1]["task_id"] == "task-1"
    assert repository.commits[0][1]["worker_id"] == "worker-1"
    assert repository.commits[0][1]["attempt_id"] == "attempt-1"
    assert repository.commits[0][1]["lease_fence"] == 7
    assert isinstance(repository.commits[0][1]["latency_ms"], int)
    assert repository.commits[0][1]["latency_ms"] >= 0
    project = repository.commits[0][1]["projects"][0]
    assert project["canonical"]["remote_project_id"] == "101"
    assert project["observation"]["account_registration_id"] == "account-1"
    assert project["observation"]["route_generation"] == 3
    assert source.calls == 1


@pytest.mark.asyncio
async def test_worker_maps_web_pages_with_web_provenance() -> None:
    repository = FakeRepository([task(source=WEB_PROJECT_SOURCE)])
    source = FakeSource({"pagination": {"data": [{"id": "101", "title": "Web project", "views_dirty": 8}]}})

    result = await worker(repository, source).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    observation = repository.commits[0][1]["projects"][0]["observation"]
    assert observation["source"] == WEB_PROJECT_SOURCE
    assert observation["views"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_marker", ["refresh_cycle", "refresh_epoch"])
async def test_refresh_page_does_not_schedule_a_second_page(refresh_marker: str) -> None:
    repository = FakeRepository([task(cursor={refresh_marker: 3})])
    source = FakeSource(
        {
            "response": [{"id": "101", "title": "Fresh project"}],
            "paging": {"page": 1, "pages": 2, "total": 13},
        }
    )

    result = await worker(repository, source).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    assert "next_tasks" not in repository.commits[0][1]


@pytest.mark.asyncio
async def test_category_browse_first_page_fans_out_all_explicit_remaining_pages() -> None:
    repository = FakeRepository([task(query_origin="category_browse")])
    source = FakeSource(
        {
            "response": [{"id": "101", "title": "Fresh project"}],
            "paging": {"page": 1, "pages": 4, "total": 37},
        }
    )

    result = await worker(repository, source).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    continuations = repository.commits[0][1]["next_tasks"]
    assert [continuation["page"] for continuation in continuations] == [2, 3, 4]
    assert all(continuation["cursor"] == {"pagination_fanout": True} for continuation in continuations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (BuyerDiscoverySourceError("forbidden", status_code=403), BuyerDiscoveryFailureKind.HTTP_403),
        (BuyerDiscoverySourceError("rate limited", status_code=429, retry_after_seconds=2), BuyerDiscoveryFailureKind.HTTP_429),
        (TimeoutError("slow source"), BuyerDiscoveryFailureKind.TIMEOUT),
        (RetryableAdapterError("temporary adapter failure"), BuyerDiscoveryFailureKind.TRANSIENT),
    ],
)
async def test_worker_classifies_transient_errors_and_schedules_fenced_retry(
    error: Exception,
    expected_kind: BuyerDiscoveryFailureKind,
) -> None:
    repository = FakeRepository([task()])
    result = await worker(repository, FakeSource(error)).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.RETRY
    assert result.failure_kind is expected_kind
    assert repository.commits == []
    assert repository.retries[0][0][:4] == ("task-1", "worker-1", "attempt-1", 7)
    assert repository.retries[0][1]["failure_kind"] == expected_kind.value


@pytest.mark.asyncio
async def test_worker_retries_a_malformed_project_page_instead_of_committing_it_as_empty() -> None:
    repository = FakeRepository([task()])

    result = await worker(repository, FakeSource({"paging": {"total": 1}})).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.RETRY
    assert result.failure_kind is BuyerDiscoveryFailureKind.TRANSIENT
    assert "пустая карточка" in (result.error or "")
    assert repository.commits == []
    assert len(repository.retries) == 1


@pytest.mark.asyncio
async def test_worker_stops_retrying_malformed_pages_after_the_bounded_retry_limit() -> None:
    repository = FakeRepository([task(retry_count=3)])

    result = await worker(repository, FakeSource({"paging": {"total": 1}})).run_once()

    assert result.outcome is BuyerDiscoveryOutcome.FAILED
    assert result.failure_kind is BuyerDiscoveryFailureKind.TRANSIENT
    assert "лимит повторных попыток" in (result.error or "")
    assert repository.retries == []
    assert len(repository.failures) == 1


@pytest.mark.asyncio
async def test_worker_rejects_a_missing_lease_fence_before_reading_the_source() -> None:
    repository = FakeRepository([task(lease_fence=0)])
    source = FakeSource({"response": []})

    with pytest.raises(BuyerDiscoveryWorkerError, match="lease_fence"):
        await worker(repository, source).run_once()

    assert source.calls == 0


@pytest.mark.asyncio
async def test_worker_serializes_concurrent_run_calls_to_one_inflight_source_request() -> None:
    repository = FakeRepository([task(), None])
    source = FakeSource({"response": [{"id": "101", "title": "Project"}]})
    active_worker = worker(repository, source)

    first, second = await asyncio.gather(active_worker.run_once(), active_worker.run_once())

    assert {first.outcome, second.outcome} == {BuyerDiscoveryOutcome.COMMITTED, BuyerDiscoveryOutcome.IDLE}
    assert source.max_in_flight == 1
