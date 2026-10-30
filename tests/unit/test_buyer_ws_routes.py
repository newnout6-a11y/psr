"""Regression tests for the real Buyer Search HTTP/WebSocket routes, with no network I/O."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.routes.kwork_buyer_search import router, stream_run_events, ws_router
from src.platforms.kwork_buyer.service import BuyerSearchService, BuyerSearchSettings
from test_kwork_buyer_search_routes import _Repository


@pytest.fixture
def buyer_app(tmp_path: Path):
    repo = _Repository()
    service = BuyerSearchService(repo, export_root=tmp_path / "exports", settings=BuyerSearchSettings(enabled=True))
    app = FastAPI()
    app.state.buyer_search = service
    app.include_router(router)
    app.include_router(ws_router)
    return app, repo, service


def _create(client: TestClient) -> str:
    response = client.post("/api/kwork/buyer-search/runs", json={"mode": "manual", "queries": [{"text": "landing page"}]})
    assert response.status_code == 201
    return response.json()["run_id"]


def test_websocket_snapshot_replay_cursor_and_explicit_disconnect(buyer_app):
    app, repo, service = buyer_app
    with TestClient(app) as client:
        run_id = _create(client)
        backlog = asyncio.run(service.replay_events(run_id, after_seq=0, limit=1000))
        assert len(backlog) >= 2
        cursor = backlog[0]["seq"]
        service.replay_events = AsyncMock(wraps=service.replay_events)
        with client.websocket_connect(f"/ws/kwork-buyer-search/runs/{run_id}?after_seq={cursor}") as ws:
            snapshot = ws.receive_json()
            assert snapshot == {"type": "snapshot", "run": repo.runs[run_id], "after_seq": cursor}
            assert [ws.receive_json()["event"]["seq"] for _ in backlog[1:]] == [item["seq"] for item in backlog[1:]]
            ws.send_text("disconnect")
        assert service.replay_events.await_count >= 1


def test_websocket_delivers_live_event_and_rest_replay_cursor(buyer_app):
    app, _, _ = buyer_app
    with TestClient(app) as client:
        run_id = _create(client)
        initial = client.get(f"/api/kwork/buyer-search/runs/{run_id}/events").json()
        cursor = initial["next_after_seq"]
        with client.websocket_connect(f"/ws/kwork-buyer-search/runs/{run_id}?after_seq={cursor}") as ws:
            assert ws.receive_json()["after_seq"] == cursor
            response = client.post(f"/api/kwork/buyer-search/runs/{run_id}/stop")
            assert response.status_code == 200
            event = ws.receive_json()
            assert event["type"] == "event"
            assert event["event"]["type"] == "run.stopped"
            assert event["event"]["seq"] > cursor
            ws.send_text("close")
        replay = client.get(f"/api/kwork/buyer-search/runs/{run_id}/events?after_seq={cursor}")
        assert replay.status_code == 200
        assert replay.json()["items"][-1]["type"] == "run.stopped"


def test_stop_restart_resume_preserves_durable_websocket_sequence(buyer_app):
    app, repo, _ = buyer_app
    with TestClient(app) as client:
        run_id = _create(client)
        assert client.post(f"/api/kwork/buyer-search/runs/{run_id}/stop").json()["state"] == "stopped"
        assert client.post(f"/api/kwork/buyer-search/runs/{run_id}/restart").json()["state"] == "ready"
        assert client.post(f"/api/kwork/buyer-search/runs/{run_id}/resume").json()["state"] == "running"
        with client.websocket_connect(f"/ws/kwork-buyer-search/runs/{run_id}?after_seq=0") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            received = [ws.receive_json()["event"] for _ in repo.events]
            assert [event["seq"] for event in received] == list(range(1, len(repo.events) + 1))
            assert [event["type"] for event in received][-3:] == ["run.stopped", "run.restarted", "run.started"]
            ws.send_text("close")


def test_websocket_backlog_over_page_limit_is_not_delayed_by_poll_sleep(buyer_app):
    app, repo, _ = buyer_app
    with TestClient(app) as client:
        run_id = _create(client)
        existing = len(repo.events)
        for number in range(1001):
            repo.events.append({"run_id": run_id, "seq": existing + number + 1, "type": "test.appended", "payload": {"number": number}})

    class ProbeSocket:
        def __init__(self):
            self.app = app
            self.messages = []
            self.idle_reads = 0

        async def accept(self):
            return None

        async def send_json(self, message):
            self.messages.append(message)

        async def receive_text(self):
            # No idle wait may be attempted before *all* full replay pages
            # have been sent. This detects the 750 ms delay without a brittle
            # wall-clock assertion on a busy CI machine.
            self.idle_reads += 1
            assert len(self.messages) == existing + 1002
            raise WebSocketDisconnect()

        async def close(self, code):
            raise AssertionError(f"unexpected WebSocket close: {code}")

    socket = ProbeSocket()
    asyncio.run(stream_run_events(socket, run_id, after_seq=0))
    assert socket.idle_reads == 1
    assert socket.messages[0]["type"] == "snapshot"
    assert socket.messages[-1]["event"]["seq"] == existing + 1001
