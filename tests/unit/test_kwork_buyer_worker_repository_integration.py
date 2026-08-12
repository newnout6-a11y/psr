from __future__ import annotations

from typing import Any, Mapping

import pytest

from src.platforms.kwork_buyer.repository import BuyerSearchRepository
from src.platforms.kwork_buyer.worker import (
    BuyerDiscoveryIdentity,
    BuyerDiscoveryOutcome,
    BuyerDiscoverySourceError,
    BuyerDiscoveryWorker,
)


class _MobilePageSource:
    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        assert task["source"] == "mobile_projects"
        assert task["query_text"] == "telegram bot"
        assert identity["account_registration_id"] == "account-1"
        return {
            "http_status": 200,
            "headers": {"Content-Type": "application/json", "Authorization": "secret"},
            "response": [
                {
                    "id": "remote-42",
                    "title": "Telegram bot",
                    "description": "Need an automation bot",
                    "price": 12_000,
                    "offers": 2,
                }
            ],
            "paging": {"total": 1, "page": 1},
        }


@pytest.mark.asyncio
async def test_worker_commits_mapped_card_and_redacted_raw_artifact(tmp_path) -> None:
    repository = BuyerSearchRepository(tmp_path / "buyer.sqlite3")
    await repository.create_run({"run_id": "run-1", "name": "Worker integration", "mode": "manual"})
    await repository.create_queries(
        "run-1",
        [
            {
                "query_id": "query-1",
                "text": "telegram bot",
                "normalized_text": "telegram bot",
                "origin": "manual",
                "state": "approved",
                "approved": True,
            }
        ],
    )
    await repository.enqueue_query_task(
        "run-1",
        {
            "task_id": "task-1",
            "query_id": "query-1",
            "source": "mobile_projects",
            "page": 1,
            "request_fingerprint": "worker-integration",
        },
    )
    worker = BuyerDiscoveryWorker(
        repository,
        _MobilePageSource(),
        run_id="run-1",
        identity=BuyerDiscoveryIdentity(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="transport-1",
            egress_ip="198.51.100.10",
            route_generation=3,
        ),
    )

    result = await worker.run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    page = await repository.list_run_projects("run-1")
    assert page["items"][0]["remote_project_id"] == "remote-42"
    detail = await repository.get_run_project("run-1", page["items"][0]["project_id"])
    artifact_id = detail["observations"][0]["raw_artifact_id"]
    assert artifact_id
    artifact = await repository.get_raw_artifact(artifact_id)
    assert artifact["body"]["headers"]["Authorization"] == "[redacted]"


@pytest.mark.asyncio
async def test_worker_enqueues_next_page_in_the_same_durable_commit(tmp_path) -> None:
    repository = BuyerSearchRepository(tmp_path / "buyer-pagination.sqlite3")
    await repository.create_run({"run_id": "run-1", "name": "Pagination", "mode": "manual"})
    await repository.create_queries(
        "run-1",
        [{"query_id": "query-1", "text": "telegram bot", "origin": "manual", "approved": True}],
    )
    await repository.enqueue_query_task(
        "run-1",
        {"task_id": "task-1", "query_id": "query-1", "source": "mobile_projects", "page": 1},
    )

    class _PaginatedSource:
        async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
            assert task["page"] == 1
            assert task["query_text"] == "telegram bot"
            return {
                "response": [{"id": "remote-1", "title": "First page"}],
                "paging": {"page": 1, "pages": 2, "total": 2},
            }

    worker = BuyerDiscoveryWorker(
        repository,
        _PaginatedSource(),
        run_id="run-1",
        identity=BuyerDiscoveryIdentity(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="transport-1",
            egress_ip="198.51.100.10",
        ),
    )

    result = await worker.run_once()

    assert result.outcome is BuyerDiscoveryOutcome.COMMITTED
    tasks = await repository.list_query_tasks("run-1", query_id="query-1")
    assert [(task["page"], task["state"]) for task in tasks] == [(1, "completed"), (2, "queued")]
    run = await repository.get_run("run-1")
    assert run["counters"]["queued_tasks"] == 1


@pytest.mark.asyncio
async def test_worker_persists_a_fenced_route_quarantine_before_retrying_a_429(tmp_path) -> None:
    repository = BuyerSearchRepository(tmp_path / "buyer-rate-limit.sqlite3")
    await repository.create_run({"run_id": "run-1", "name": "Rate limit", "mode": "manual"})
    await repository.create_queries(
        "run-1",
        [{"query_id": "query-1", "text": "telegram bot", "origin": "manual", "approved": True}],
    )
    await repository.enqueue_query_task(
        "run-1",
        {
            "task_id": "task-1",
            "query_id": "query-1",
            "source": "mobile_projects",
            "endpoint": "/projects",
            "page": 1,
        },
    )

    class _RateLimitedSource:
        async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
            assert task["endpoint"] == "/projects"
            raise BuyerDiscoverySourceError("slow down", status_code=429, retry_after_seconds=3)

    discovery_worker = BuyerDiscoveryWorker(
        repository,
        _RateLimitedSource(),
        run_id="run-1",
        identity=BuyerDiscoveryIdentity(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="transport-1",
            egress_ip="198.51.100.10",
        ),
    )

    result = await discovery_worker.run_once()

    assert result.outcome is BuyerDiscoveryOutcome.RETRY
    assert result.endpoint == "/projects"
    assert result.quarantine_until
    quarantine = (await repository.list_discovery_quarantines("run-1", active_only=True))[0]
    assert quarantine["failure_kind"] == "http_429"
    assert quarantine["last_task_id"] == "task-1"
    assert quarantine["last_lease_fence"] == 1
    events = await repository.replay_events("run-1")
    assert [event["event_type"] for event in events] == ["query.page.started", "identity.quarantined", "query.page.retry"]
