from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_market_jobs import router
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.events import MarketEventHub
from src.platforms.kwork_supply.models import (
    MarketJobCreate,
    MarketScope,
    WorkerDesiredState,
    WorkerRecord,
    WorkerState,
)
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_market_supply import MarketAssistant, MarketAssistantStore


@pytest.fixture
def market_app(tmp_path) -> tuple[FastAPI, MarketScanCoordinator]:
    coordinator = MarketScanCoordinator(MarketJobRepository(tmp_path / "market-jobs.sqlite3"), MarketEventHub())
    asyncio.run(coordinator.initialize())
    app = FastAPI()
    app.state.market_jobs = coordinator
    app.include_router(router)
    return app, coordinator


def _create_payload() -> dict[str, object]:
    return {
        "scope": {
            "category_id": 38,
            "category_name": "Website work",
            "canonical_alias": "website-repair",
            "filters": {"price_to": 5000},
        },
        "profile": "working",
        "target_unique_cards": 60,
        "desired_workers": 2,
        "network_policy": "prefer_vpnte",
        "source_policy": "validated_only",
        "include_ai": True,
    }


def _create_job(coordinator: MarketScanCoordinator, job_id: str) -> dict[str, object]:
    return asyncio.run(
        coordinator.create_job(
            MarketJobCreate(
                scope=MarketScope(category_id=38, category_name="Website work", canonical_alias="website-repair"),
                target_unique_cards=60,
                desired_workers=2,
            ),
            job_id=job_id,
        )
    )


def test_create_list_snapshot_and_websocket_replay(market_app: tuple[FastAPI, MarketScanCoordinator]):
    app, _ = market_app

    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.post("/api/kwork/market/jobs", json=_create_payload())

        assert response.status_code == 202
        assert time.perf_counter() - started < 1
        accepted = response.json()
        job_id = accepted["job_id"]
        assert accepted["state"] == "preparing"
        assert accepted["status_url"] == f"/api/kwork/market/jobs/{job_id}"
        assert accepted["events_url"] == f"/ws/kwork-market/jobs/{job_id}"
        assert accepted["initial_operation"]["kind"] == "map_scope"

        jobs = client.get("/api/kwork/market/jobs")
        assert jobs.status_code == 200
        assert [item["job_id"] for item in jobs.json()["items"]] == [job_id]

        snapshot = client.get(accepted["status_url"])
        assert snapshot.status_code == 200
        assert snapshot.json()["job"]["scope"]["canonical_alias"] == "website-repair"
        assert snapshot.json()["last_event_sequence"] == 2

        replay = client.get(f"/api/kwork/market/jobs/{job_id}/events?after_seq=0")
        assert replay.status_code == 200
        assert [item["type"] for item in replay.json()["items"]] == ["job.snapshot", "operation.queued"]

        with client.websocket_connect(accepted["events_url"] + "?after_seq=0") as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()
            assert [first["seq"], second["seq"]] == [1, 2]
            assert [first["type"], second["type"]] == ["job.snapshot", "operation.queued"]

            transition = client.post(f"/api/kwork/market/jobs/{job_id}/pause")
            assert transition.status_code == 200
            third = websocket.receive_json()
            fourth = websocket.receive_json()
            assert [third["seq"], fourth["seq"]] == [3, 4]
            assert [third["type"], fourth["type"]] == ["job.state_changed", "worker.command.queued"]


def test_websocket_requests_snapshot_resync_when_replay_window_is_exceeded(
    market_app: tuple[FastAPI, MarketScanCoordinator],
):
    app, coordinator = market_app
    _create_job(coordinator, "job_routes_resync")

    async def append_backlog() -> None:
        for index in range(1_001):
            await coordinator.emit(
                "job_routes_resync",
                "warning",
                {"code": "test_backlog", "index": index},
                revision=1,
            )

    asyncio.run(append_backlog())

    with TestClient(app) as client:
        with client.websocket_connect("/ws/kwork-market/jobs/job_routes_resync?after_seq=0") as websocket:
            message = websocket.receive_json()

    assert message["type"] == "resync_required"
    assert message["job_id"] == "job_routes_resync"
    assert message["payload"] == {
        "reason": "event_replay_window_exceeded",
        "after_seq": 0,
        "replay_limit": 1_000,
    }


def test_websocket_requests_snapshot_resync_when_event_retention_has_a_gap(
    market_app: tuple[FastAPI, MarketScanCoordinator],
):
    app, coordinator = market_app
    _create_job(coordinator, "job_routes_retention")
    with sqlite3.connect(coordinator.repository.db_path) as connection:
        connection.execute(
            "DELETE FROM market_events WHERE job_id = ? AND sequence = 1",
            ("job_routes_retention",),
        )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/kwork-market/jobs/job_routes_retention?after_seq=0") as websocket:
            message = websocket.receive_json()

    assert message["type"] == "resync_required"
    assert message["payload"] == {
        "reason": "event_retention_gap",
        "after_seq": 0,
        "first_available_seq": 2,
    }


def test_operational_views_and_worker_commands(market_app: tuple[FastAPI, MarketScanCoordinator]):
    app, coordinator = market_app
    created = _create_job(coordinator, "job_routes_control")
    operation_id = created["initial_operation"]["operation_id"]
    asyncio.run(
        coordinator.repository.upsert_worker(
            WorkerRecord(
                worker_id="worker_routes",
                generation=1,
                desired_state=WorkerDesiredState.RUNNING,
                actual_state=WorkerState.IDLE,
            ),
            job_id="job_routes_control",
        )
    )

    with TestClient(app) as client:
        workers = client.get("/api/kwork/market/jobs/job_routes_control/workers")
        assert workers.status_code == 200
        assert workers.json()["items"][0]["worker_id"] == "worker_routes"

        for action, command in (
            ("drain", "drain"),
            ("restart", "restart"),
            ("disable", "disable"),
            ("rotate", "rotate"),
            ("reconnect", "reconnect"),
        ):
            response = client.post(
                f"/api/kwork/market/jobs/job_routes_control/workers/worker_routes/{action}",
                json={"payload": {"reason": "route test"}},
            )
            assert response.status_code == 202
            assert response.json()["command_type"] == command

        retried = client.post(
            f"/api/kwork/market/jobs/job_routes_control/operations/{operation_id}/retry"
        )
        assert retried.status_code == 202
        assert retried.json()["payload"]["retry_of"] == operation_id

        operations = client.get("/api/kwork/market/jobs/job_routes_control/operations?limit=10")
        assert operations.status_code == 200
        assert len(operations.json()["items"]) == 2

        attempts = client.get(
            f"/api/kwork/market/jobs/job_routes_control/operations/{operation_id}/attempts"
        )
        assert attempts.status_code == 200
        assert attempts.json()["items"] == []

        transports = client.get("/api/kwork/market/jobs/job_routes_control/transports")
        assert transports.status_code == 200
        assert transports.json()["items"] == []

        shards = client.get("/api/kwork/market/jobs/job_routes_control/shards")
        assert shards.status_code == 200
        assert shards.json()["items"] == []

        listings = client.get("/api/kwork/market/jobs/job_routes_control/listings")
        assert listings.status_code == 200
        assert listings.json()["items"] == []

        results = client.get("/api/kwork/market/jobs/job_routes_control/results")
        assert results.status_code == 200
        assert results.json()["target_unique_cards"] == 60
        assert results.json()["metrics"]["observed_cards"]["observed_listing_count"] == 0
        assert results.json()["metrics"]["price_distribution"]["p50"] is None
        assert results.json()["analysis"] == {}


def test_control_updates_and_domain_error_mapping(market_app: tuple[FastAPI, MarketScanCoordinator]):
    app, coordinator = market_app
    _create_job(coordinator, "job_routes_update")

    with TestClient(app) as client:
        empty_patch = client.patch("/api/kwork/market/jobs/job_routes_update", json={})
        assert empty_patch.status_code == 422

        updated = client.patch(
            "/api/kwork/market/jobs/job_routes_update",
            json={"target_unique_cards": 120, "request_budget": 20, "expected_revision": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["target_unique_cards"] == 120
        assert updated.json()["request_budget"] == 20
        assert updated.json()["revision"] == 2

        cleared = client.patch(
            "/api/kwork/market/jobs/job_routes_update",
            json={"request_budget": None, "expected_revision": 2},
        )
        assert cleared.status_code == 200
        assert cleared.json()["request_budget"] is None

        pool = client.patch(
            "/api/kwork/market/jobs/job_routes_update/worker-pool",
            json={"desired_workers": 3, "expected_revision": 3},
        )
        assert pool.status_code == 200
        assert pool.json()["desired_workers"] == 3

        conflict = client.post(
            "/api/kwork/market/jobs/job_routes_update/pause",
            json={"expected_revision": 999},
        )
        assert conflict.status_code == 409

        missing = client.get("/api/kwork/market/jobs/not-a-real-job")
        assert missing.status_code == 404

        invalid = client.post("/api/kwork/market/jobs", json={"scope": {"category_id": 0}})
        assert invalid.status_code == 422


def test_routes_report_unavailable_runtime():
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/kwork/market/jobs")

    assert response.status_code == 503


def test_job_scoped_assistant_uses_the_current_durable_snapshot(
    market_app: tuple[FastAPI, MarketScanCoordinator],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app, coordinator = market_app
    _create_job(coordinator, "job_routes_assistant")

    async def fake_generate(*, system_prompt: str, prompt: str, tool_handler) -> dict[str, object]:
        overview = await tool_handler("market_overview", {})
        assert overview["scope"]["canonical_alias"] == "website-repair"
        return {"answer": "Срез привязан к текущему запуску.", "refresh": None}

    monkeypatch.setattr(MarketAssistant, "_generate_with_tools", staticmethod(fake_generate))
    monkeypatch.setattr(
        MarketAssistantStore,
        "path",
        staticmethod(lambda context_id: tmp_path / f"{context_id}.json"),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/kwork/market/jobs/job_routes_assistant/assistant",
            json={"message": "Что есть в этом запуске?"},
        )

    assert response.status_code == 200
    assert response.json()["context_id"] == "market_job_job_routes_assistant"
    assert response.json()["answer"] == "Срез привязан к текущему запуску."
