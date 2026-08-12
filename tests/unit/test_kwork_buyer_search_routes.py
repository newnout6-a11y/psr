from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_search import router
from src.platforms.kwork_buyer.service import BuyerSearchService, BuyerSearchSettings


class _Repository:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.queries: dict[str, list[dict[str, Any]]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.workspace: dict[str, Any] = {}

    async def get_workspace_state(self) -> dict[str, Any]:
        return {"workspace_id": "default", "schema_version": 1, "state": dict(self.workspace), "updated_at": None}

    async def put_workspace_state(self, state: dict[str, Any], *, schema_version: int = 1) -> dict[str, Any]:
        self.workspace = dict(state)
        return {"workspace_id": "default", "schema_version": schema_version, "state": dict(self.workspace), "updated_at": "now"}

    async def create_run(self, value: dict[str, Any]) -> dict[str, Any]:
        self.runs[value["run_id"]] = dict(value)
        return dict(value)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        value = self.runs.get(run_id)
        return dict(value) if value else None

    async def list_runs(self, **_: Any) -> list[dict[str, Any]]:
        return [dict(value) for value in self.runs.values()]

    async def update_run(self, run_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.runs[run_id].update(changes)
        return dict(self.runs[run_id])

    async def delete_run(self, run_id: str) -> dict[str, Any]:
        self.runs.pop(run_id)
        self.queries.pop(run_id, None)
        self.tasks = [task for task in self.tasks if task["run_id"] != run_id]
        return {"run_id": run_id, "deleted": True}

    async def create_queries(self, run_id: str, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.queries.setdefault(run_id, []).extend(dict(value) for value in values)
        return [dict(value) for value in values]

    async def list_queries(self, run_id: str, **_: Any) -> list[dict[str, Any]]:
        return [dict(value) for value in self.queries.get(run_id, [])]

    async def enqueue_query_task(self, _: str, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks.append(dict(task))
        return dict(task)

    async def list_query_tasks(self, run_id: str, *, query_id: str | None = None, limit: int = 5_000, **_: Any) -> list[dict[str, Any]]:
        return [
            dict(task)
            for task in self.tasks
            if task["run_id"] == run_id and (query_id is None or task["query_id"] == query_id)
        ][:limit]

    async def restart_query_tasks(self, run_id: str) -> dict[str, Any]:
        count = sum(1 for task in self.tasks if task["run_id"] == run_id)
        for task in self.tasks:
            if task["run_id"] == run_id:
                task["state"] = "queued"
        return {"run_id": run_id, "requeued_task_count": count, "restart_count": 1}

    async def update_query(self, run_id: str, query_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        for query in self.queries.get(run_id, []):
            if query["query_id"] == query_id:
                query.update(changes)
                return dict(query)
        raise KeyError(query_id)

    async def list_run_projects(self, _: str, **__: Any) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    async def get_run_project(self, *_: Any) -> None:
        return None

    async def get_facets(self, run_id: str, **_: Any) -> dict[str, Any]:
        return {"run_id": run_id, "facets": []}

    async def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"run_id": run_id, "seq": len(self.events) + 1, "type": event_type, "payload": payload}
        self.events.append(event)
        return event

    async def replay_events(self, run_id: str, *, after_seq: int, limit: int) -> list[dict[str, Any]]:
        return [item for item in self.events if item["run_id"] == run_id and item["seq"] > after_seq][:limit]

    async def store_raw_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return {"artifact_id": artifact["artifact_id"]}


def _client(tmp_path: Path, *, semantic_scorer=None) -> TestClient:
    app = FastAPI()
    app.state.buyer_search = BuyerSearchService(
        _Repository(),
        export_root=tmp_path / "exports",
        semantic_scorer=semantic_scorer,
        settings=BuyerSearchSettings(enabled=True, max_workers=30),
    )
    app.include_router(router)
    return TestClient(app)


def test_create_list_and_control_buyer_run(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/kwork/buyer-search/runs",
            json={
                "mode": "manual",
                "requested_workers": 1,
                "queries": [{"text": "landing page", "rationale": "operator"}],
            },
        )
        assert created.status_code == 201
        run = created.json()
        assert run["state"] == "ready"

        listed = client.get("/api/kwork/buyer-search/runs")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["run_id"] == run["run_id"]

        started = client.patch(f"/api/kwork/buyer-search/runs/{run['run_id']}", json={"state": "running"})
        assert started.status_code == 200
        assert started.json()["state"] == "running"

        events = client.get(f"/api/kwork/buyer-search/runs/{run['run_id']}/events")
        assert events.status_code == 200
        assert events.json()["items"]


def test_workspace_api_round_trip_and_forbids_unknown_fields(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "state": {
            "builder": {"name": "Draft", "brief": "React", "exact_queries": ["react project"]},
            "view": {"active_section": "setup", "project_filters": {"maxBudget": "10000"}},
        },
    }
    with _client(tmp_path) as client:
        saved = client.put("/api/kwork/buyer-search/workspace", json=payload)
        assert saved.status_code == 200
        assert saved.json()["state"]["builder"]["name"] == "Draft"

        restored = client.get("/api/kwork/buyer-search/workspace")
        assert restored.status_code == 200
        assert restored.json()["state"]["view"]["project_filters"]["maxBudget"] == "10000"

        invalid = client.put(
            "/api/kwork/buyer-search/workspace",
            json={"schema_version": 1, "state": {"builder": {"unknown": True}, "view": {}}},
        )
        assert invalid.status_code == 422


def test_unknown_run_maps_to_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/kwork/buyer-search/runs/missing")

    assert response.status_code == 404


def test_delete_terminal_buyer_run(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/kwork/buyer-search/runs",
            json={"mode": "manual", "queries": [{"text": "landing page"}]},
        )
        run_id = created.json()["run_id"]

        deleted = client.delete(f"/api/kwork/buyer-search/runs/{run_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"run_id": run_id, "deleted": True}


def test_restart_route_returns_a_ready_replay(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/kwork/buyer-search/runs",
            json={"mode": "manual", "queries": [{"text": "landing page"}]},
        )
        run_id = created.json()["run_id"]
        assert client.post(f"/api/kwork/buyer-search/runs/{run_id}/stop").status_code == 200

        restarted = client.post(f"/api/kwork/buyer-search/runs/{run_id}/restart")

    assert restarted.status_code == 200
    assert restarted.json()["state"] == "ready"
    assert restarted.json()["restart"]["requeued_task_count"] == 2


def test_query_collision_and_bundle_routes_are_durable_read_models(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/kwork/buyer-search/runs",
            json={
                "mode": "manual",
                "requested_workers": 1,
                "queries": [{"text": "landing page"}, {"text": "Landing   Page"}],
            },
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]

        collisions = client.get(f"/api/kwork/buyer-search/runs/{run_id}/queries/collisions")
        assert collisions.status_code == 200
        assert collisions.json()["items"][0]["kind"] == "exact"

        bundles = client.get(f"/api/kwork/buyer-search/runs/{run_id}/query-bundles")
        assert bundles.status_code == 200
        assert bundles.json()["bundles"][0]["task_count"] == 2

        regenerated = client.post(
            f"/api/kwork/buyer-search/runs/{run_id}/queries/regenerate-collisions",
            json={
                "queries": [{"text": "landing page audit"}],
                "minimum_count": 1,
                "auto_approve": True,
            },
        )
        assert regenerated.status_code == 201
        assert regenerated.json()["generation"]["items"][0]["text"] == "landing page audit"
