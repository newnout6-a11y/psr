from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.platforms.kwork_buyer.query_ai import BuyerAIQueryGenerationResult
from src.platforms.kwork_buyer.query_generation import BuyerQueryGenerationResult
from src.platforms.kwork_buyer.query_planner import BuyerQueryCandidate
from src.platforms.kwork_buyer.service import (
    BuyerSearchService,
    BuyerSearchSettings,
    BuyerSearchStateError,
    format_buyer_event_log,
    should_log_buyer_event,
)
from src.platforms.kwork_buyer.repository import BuyerSearchRepository


def test_runtime_defaults_to_single_primary_discovery_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUYER_SEARCH_DISCOVERY_SOURCES", raising=False)
    assert BuyerSearchSettings.from_env().discovery_sources == ("mobile_projects",)

    monkeypatch.setenv("BUYER_SEARCH_DISCOVERY_SOURCES", "mobile_projects,web_projects")
    assert BuyerSearchSettings.from_env().discovery_sources == ("mobile_projects", "web_projects")


def test_shared_log_suppresses_only_noisy_worker_events() -> None:
    assert should_log_buyer_event("worker.state") is False
    assert should_log_buyer_event("worker.idle") is False
    assert should_log_buyer_event("worker.heartbeat") is False
    assert should_log_buyer_event("query.page.completed") is True
    assert should_log_buyer_event("run.started") is True


def test_shared_log_describes_an_empty_kwork_card_in_russian() -> None:
    message = format_buyer_event_log(
        "run-12345678",
        "query.page.failed",
        {
            "error": "mobile project payload has no project ID; исчерпан лимит повторных попыток (3)",
            "source": "mobile_projects",
            "page": 1,
            "failure_kind": "transient",
        },
    )

    assert "пустая карточка в мобильной выдаче Kwork" in message
    assert "источник: мобильная выдача Kwork" in message
    assert "тип: временный сбой" in message
    assert "mobile_projects" not in message


class _Repository:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.queries: dict[str, list[dict[str, Any]]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.projects: dict[str, list[dict[str, Any]]] = {}
        self.exports: dict[tuple[str, str], dict[str, Any]] = {}
        self.scores: list[dict[str, Any]] = []
        self.completed_run_ids: list[str] = []

    async def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        self.runs[run["run_id"]] = dict(run)
        return dict(run)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        value = self.runs.get(run_id)
        return dict(value) if value else None

    async def list_runs(self, *, limit: int, cursor: str | None = None) -> list[dict[str, Any]]:
        return [dict(run) for run in list(self.runs.values())[:limit]]

    async def update_run(self, run_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.runs[run_id].update(changes)
        return dict(self.runs[run_id])

    async def complete_run_if_exhausted(self, run_id: str) -> dict[str, Any]:
        self.completed_run_ids.append(run_id)
        self.runs[run_id]["state"] = "completed"
        return dict(self.runs[run_id])

    async def create_queries(self, run_id: str, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.queries.setdefault(run_id, []).extend(dict(query) for query in queries)
        return [dict(query) for query in queries]

    async def list_queries(self, run_id: str, **_: Any) -> list[dict[str, Any]]:
        return [dict(query) for query in self.queries.get(run_id, [])]

    async def enqueue_query_task(self, run_id: str, task: dict[str, Any]) -> dict[str, Any]:
        assert task["run_id"] == run_id
        self.tasks.append(dict(task))
        return dict(task)

    async def list_query_tasks(self, run_id: str, *, query_id: str | None = None, limit: int = 5_000, **_: Any) -> list[dict[str, Any]]:
        return [
            dict(task)
            for task in self.tasks
            if task["run_id"] == run_id and (query_id is None or task["query_id"] == query_id)
        ][:limit]

    async def restart_query_tasks(self, run_id: str) -> dict[str, Any]:
        count = 0
        for task in self.tasks:
            if task["run_id"] != run_id:
                continue
            task.update({"state": "queued", "attempt_id": None, "lease_owner": None, "lease_deadline": None})
            count += 1
        return {"run_id": run_id, "requeued_task_count": count, "restart_count": 1}

    async def update_query(self, run_id: str, query_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        for query in self.queries.get(run_id, []):
            if query["query_id"] == query_id:
                query.update(changes)
                return dict(query)
        raise KeyError(query_id)

    async def list_query_coverage(self, run_id: str) -> list[dict[str, Any]]:
        coverage: dict[str, set[str]] = {}
        for project in self.projects.get(run_id, []):
            project_id = str(project.get("project_id") or project.get("id") or "")
            for query_id in project.get("query_ids", []):
                coverage.setdefault(str(query_id), set()).add(project_id)
        return [
            {"query_id": query_id, "project_ids": sorted(project_ids)}
            for query_id, project_ids in coverage.items()
        ]

    async def compact_query_plan(self, run_id: str, retain_query_ids: list[str]) -> dict[str, Any]:
        retained = set(retain_query_ids)
        for query in self.queries.get(run_id, []):
            if query["query_id"] in retained:
                query.update({"enabled": True, "approved": True})
            else:
                query.update({"enabled": False, "state": "disabled"})
        for task in self.tasks:
            if task["run_id"] == run_id and task["query_id"] not in retained and task.get("state") in {"queued", "retry_wait"}:
                task["state"] = "cancelled"
        counters = dict(self.runs[run_id].get("counters") or {})
        counters["planned_queries"] = len(retained)
        self.runs[run_id]["counters"] = counters
        return {
            "run_id": run_id,
            "retained_query_ids": list(retain_query_ids),
            "retained_query_count": len(retained),
        }

    async def list_run_projects(self, run_id: str, **_: Any) -> dict[str, Any]:
        return {"items": [dict(project) for project in self.projects.get(run_id, [])], "next_cursor": None}

    async def get_run_project(self, run_id: str, project_id: str) -> dict[str, Any] | None:
        return next((dict(project) for project in self.projects.get(run_id, []) if project["project_id"] == project_id), None)

    async def get_facets(self, run_id: str, **_: Any) -> dict[str, Any]:
        return {"facets": [], "run_id": run_id}

    async def record_score(self, run_id: str, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        stored = {"score_id": f"score-{len(self.scores) + 1}", "run_id": run_id, "project_id": project_id, "total_score": payload["total"], **payload}
        self.scores.append(stored)
        return dict(stored)

    async def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"run_id": run_id, "type": event_type, "payload": dict(payload), "seq": len(self.events) + 1}
        self.events.append(event)
        return event

    async def replay_events(self, run_id: str, *, after_seq: int, limit: int) -> list[dict[str, Any]]:
        return [event for event in self.events if event["run_id"] == run_id and event["seq"] > after_seq][:limit]

    async def store_raw_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        self.artifacts.append(dict(artifact))
        return {"artifact_id": artifact["artifact_id"]}

    async def create_export(self, run_id: str, export: dict[str, Any]) -> dict[str, Any]:
        key = (run_id, export["export_id"])
        self.exports.setdefault(key, dict(export))
        return dict(self.exports[key])

    async def update_export(self, run_id: str, export_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.exports[(run_id, export_id)].update(changes)
        return dict(self.exports[(run_id, export_id)])

    async def get_export(self, run_id: str, export_id: str) -> dict[str, Any]:
        return dict(self.exports[(run_id, export_id)])


@pytest.fixture
def repository() -> _Repository:
    return _Repository()


@pytest.fixture
def service(tmp_path: Path, repository: _Repository) -> BuyerSearchService:
    return BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )


@pytest.mark.asyncio
async def test_create_manual_run_persists_plan_tasks_and_events(service: BuyerSearchService, repository: _Repository) -> None:
    run = await service.create_run(
        {
            "name": "CRM buyers",
            "mode": "manual",
            "requested_workers": 2,
            "query_batch_size": 1,
            "queries": [
                {"text": "CRM integration", "rationale": "operator"},
                {"text": "sales dashboard", "rationale": "operator"},
            ],
        }
    )

    assert run["state"] == "ready"
    assert run["counters"]["planned_queries"] == 2
    assert len(repository.queries[run["run_id"]]) == 2
    assert len(repository.tasks) == 4
    assert {task["source"] for task in repository.tasks} == {"mobile_projects", "web_projects"}
    assert all(task["lease_fence"] if "lease_fence" in task else True for task in repository.tasks)
    assert {event["type"] for event in repository.events} >= {"run.created", "run.ready", "query.assigned"}


@pytest.mark.asyncio
async def test_category_run_creates_one_keyword_free_browse_anchor_without_ai(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    class _Gateway:
        calls = 0

        async def generate(self, *, prompt: str, task: str) -> dict[str, object]:
            self.calls += 1
            return {
                "queries": [
                    {"text": f"создание сайта вариант {index}", "rationale": "тестовый вариант"}
                    for index in range(30)
                ]
            }

    gateway = _Gateway()
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        query_generation_gateway=gateway,
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            initial_query_probe_count=3,
            auto_query_replenishment=False,
        ),
    )

    run = await planner.create_run(
        {
            "mode": "category",
            "requested_workers": 15,
            "query_batch_size": 2,
            "category_scope": {"category_id": 37, "category_path": [1, 37], "category_name": "Создание сайта"},
        }
    )

    assert gateway.calls == 0
    assert run["counters"]["planned_queries"] == 1
    assert len(repository.queries[run["run_id"]]) == 1
    assert repository.queries[run["run_id"]][0]["origin"] == "category_browse"
    assert repository.queries[run["run_id"]][0]["category_id"] == 37
    assert len(repository.tasks) == 2
    assert {task["query_origin"] for task in repository.tasks} == {"category_browse"}
    ready = next(event for event in repository.events if event["type"] == "run.ready")
    assert ready["payload"]["query_count"] == 1
    created = next(event for event in repository.events if event["type"] == "run.created")
    assert created["payload"]["query_generator"] == "category_browse"
    assert created["payload"]["ai_used"] is False


@pytest.mark.asyncio
async def test_category_replenishment_never_expands_keyword_phrases(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            auto_query_replenishment=True,
            auto_query_replenishment_minimum_count=3,
        ),
    )
    run_id = "run-overlap"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Создание сайта",
        "mode": "category",
        "category_scope": {"category_id": 37, "category_path": [1, 37], "category_name": "Создание сайта"},
        "filters": {},
        "requested_workers": 15,
        "query_batch_size": 2,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 2},
    }
    repository.queries[run_id] = [
        {
            "query_id": query_id,
            "run_id": run_id,
            "text": f"создание сайта {query_id}",
            "category_id": 37,
            "category_path": [1, 37],
            "filters": {},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        }
        for query_id in ("query-1", "query-2", "query-3")
    ]
    repository.projects[run_id] = [
        {"project_id": "project-1", "query_ids": ["query-1", "query-2", "query-3"]},
        {"project_id": "project-2", "query_ids": ["query-1", "query-2", "query-3"]},
    ]

    created = await planner._replenish_query_queue(repository.runs[run_id])

    assert created == 0
    assert len(repository.queries[run_id]) == 3
    assert not any(event["type"].startswith("query.replenish") for event in repository.events)


@pytest.mark.asyncio
async def test_old_category_run_self_heals_with_a_browse_anchor_before_refresh(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run_id = "run-old-category"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "category_scope": {
            "category_id": 24,
            "category_path": [1, 24],
            "category_name": "Web design",
        },
        "filters": {"max_budget": 10_000},
        "requested_workers": 15,
        "query_batch_size": 2,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 16, "planned_queries": 1},
    }
    repository.queries[run_id] = [
        {
            "query_id": "query-legacy-keyword",
            "run_id": run_id,
            "text": "professional web design",
            "origin": "category",
            "category_id": 24,
            "category_path": [1, 24],
            "filters": {"max_budget": 10_000},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        }
    ]
    repository.tasks = [
        {
            "task_id": "legacy-mobile",
            "run_id": run_id,
            "query_id": "query-legacy-keyword",
            "source": "mobile_projects",
            "page": 1,
            "state": "completed",
        },
        {
            "task_id": "legacy-web",
            "run_id": run_id,
            "query_id": "query-legacy-keyword",
            "source": "web_projects",
            "page": 1,
            "state": "completed",
        },
    ]

    completed = await planner._complete_exhausted_run(run_id)

    assert completed is None
    browse = next(query for query in repository.queries[run_id] if query["origin"] == "category_browse")
    assert browse["category_id"] == 24
    assert browse["filters"] == {"max_budget": 10_000}
    browse_tasks = [task for task in repository.tasks if task["query_id"] == browse["query_id"]]
    assert len(browse_tasks) == 2
    assert {task["source"] for task in browse_tasks} == {"mobile_projects", "web_projects"}
    assert {task["state"] for task in browse_tasks} == {"queued"}
    assert repository.runs[run_id]["counters"]["planned_queries"] == 2
    assert repository.runs[run_id]["counters"].get("auto_project_refresh_mode") is not True
    added = next(event for event in repository.events if event["type"] == "query.category_browse.added")
    assert added["payload"]["created_count"] == 1
    assert added["payload"]["task_count"] == 2


@pytest.mark.asyncio
async def test_unreached_target_replenishes_the_query_queue_before_completion(tmp_path: Path, repository: _Repository) -> None:
    class _Gateway:
        prompt = ""

        async def generate(self, *, prompt: str, task: str) -> dict[str, object]:
            self.prompt = prompt
            return {
                "queries": [
                    {"text": "дизайн лендинга", "rationale": "новая специализация"},
                    {"text": "дизайн мобильного приложения", "rationale": "новая специализация"},
                ]
            }

    gateway = _Gateway()
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            auto_query_replenishment=True,
            auto_query_replenishment_max_batches=3,
            auto_query_replenishment_minimum_count=2,
        ),
        query_generation_gateway=gateway,
    )
    run_id = "run-replenish"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "hybrid",
        "brief": "заказы на веб-дизайн",
        "category_scope": {"category_id": 24, "category_path": [1, 24], "category_name": "Веб-дизайн"},
        "filters": {},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 15, "completed_tasks": 2},
    }
    repository.queries[run_id] = [
        {
            "query_id": "query-browse",
            "run_id": run_id,
            "text": "all active web design projects",
            "origin": "category_browse",
            "category_id": 24,
            "category_path": [1, 24],
            "filters": {},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        },
        {
            "query_id": "query-old",
            "run_id": run_id,
            "origin": "hybrid_seed",
            "text": "старый веб дизайн",
            "category_id": 24,
            "category_path": [1, 24],
            "filters": {},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        }
    ]
    repository.tasks = [
        {"task_id": "task-browse-mobile", "run_id": run_id, "query_id": "query-browse", "source": "mobile_projects", "page": 1, "state": "completed"},
        {"task_id": "task-browse-web", "run_id": run_id, "query_id": "query-browse", "source": "web_projects", "page": 1, "state": "completed"},
        {"task_id": "task-old-mobile", "run_id": run_id, "query_id": "query-old", "source": "mobile_projects", "page": 1, "state": "completed"},
        {"task_id": "task-old-web", "run_id": run_id, "query_id": "query-old", "source": "web_projects", "page": 1, "state": "completed"},
    ]

    completed = await planner._complete_exhausted_run(run_id)

    assert completed is None
    assert len(repository.queries[run_id]) > 1
    assert any(task["state"] == "queued" for task in repository.tasks)
    assert repository.runs[run_id]["counters"]["auto_query_replenishment_batches"] == 1
    assert "старый веб дизайн" in gateway.prompt


@pytest.mark.asyncio
async def test_unreached_target_waits_and_retries_when_a_query_wave_is_empty(
    tmp_path: Path,
    repository: _Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            auto_query_replenishment=True,
            auto_query_replenishment_max_batches=3,
            auto_query_replenishment_minimum_count=2,
            auto_query_replenishment_retry_seconds=3_600,
            auto_query_replenishment_retry_max_seconds=3_600,
        ),
    )
    run_id = "run-empty-wave"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "hybrid",
        "brief": "заказы на веб-дизайн",
        "category_scope": {"category_id": 24, "category_path": [1, 24], "category_name": "Веб-дизайн"},
        "filters": {},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 15, "completed_tasks": 2},
    }
    repository.queries[run_id] = [
        {
            "query_id": "query-browse",
            "run_id": run_id,
            "text": "all active web design projects",
            "origin": "category_browse",
            "category_id": 24,
            "category_path": [1, 24],
            "filters": {},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        },
        {
            "query_id": "query-old",
            "run_id": run_id,
            "origin": "hybrid_seed",
            "text": "старый веб дизайн",
            "category_id": 24,
            "category_path": [1, 24],
            "filters": {},
            "approved": True,
            "enabled": True,
            "state": "exhausted",
        }
    ]
    repository.tasks = [
        {"task_id": "task-browse-mobile", "run_id": run_id, "query_id": "query-browse", "source": "mobile_projects", "page": 1, "state": "completed"},
        {"task_id": "task-browse-web", "run_id": run_id, "query_id": "query-browse", "source": "web_projects", "page": 1, "state": "completed"},
        {"task_id": "task-old-mobile", "run_id": run_id, "query_id": "query-old", "source": "mobile_projects", "page": 1, "state": "completed"},
        {"task_id": "task-old-web", "run_id": run_id, "query_id": "query-old", "source": "web_projects", "page": 1, "state": "completed"},
    ]

    async def no_new_queries(*_: object, **__: object) -> dict[str, object]:
        return {"items": []}

    monkeypatch.setattr(planner, "generate_queries", no_new_queries)
    completed = await planner._complete_exhausted_run(run_id)

    assert completed is None
    assert repository.runs[run_id]["state"] == "running"
    assert repository.completed_run_ids == []
    assert repository.runs[run_id]["counters"]["auto_project_refresh_mode"] is True
    assert str(repository.runs[run_id]["counters"]["auto_project_refresh_next_at"]).endswith("Z")
    assert run_id in planner._replenishment_retry_tasks
    refresh_wait = next(event for event in repository.events if event["type"] == "query.refresh.waiting")
    assert refresh_wait["payload"]["next_check_at"] == repository.runs[run_id]["counters"]["auto_project_refresh_next_at"]
    await planner.close()


@pytest.mark.asyncio
async def test_pending_retry_prevents_duplicate_query_generation_attempts(
    tmp_path: Path,
    repository: _Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            auto_query_replenishment_retry_seconds=3_600,
            auto_query_replenishment_retry_max_seconds=3_600,
            auto_project_refresh_seconds=3_600,
        ),
    )
    run_id = "run-pending-retry"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "category_scope": {"category_id": 24, "category_name": "Веб-дизайн"},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 17},
    }
    repository.tasks = [
        {"task_id": "terminal", "run_id": run_id, "query_id": "query-old", "source": "web_projects", "page": 1, "state": "completed"}
    ]

    async def unexpected_replenishment(*_: object, **__: object) -> int:
        raise AssertionError("a pending retry must suppress another planning attempt")

    retry = asyncio.create_task(asyncio.sleep(3_600))
    planner._replenishment_retry_tasks[run_id] = retry
    monkeypatch.setattr(planner, "_replenish_query_queue", unexpected_replenishment)

    assert await planner._complete_exhausted_run(run_id) is None
    assert repository.completed_run_ids == []
    await planner.close()


@pytest.mark.asyncio
async def test_saturated_query_waves_switch_to_live_project_refresh(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            auto_project_refresh_seconds=3_600,
        ),
    )
    run_id = "run-live-refresh"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "hybrid",
        "category_scope": {"category_id": 24, "category_name": "Веб-дизайн"},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {
            "unique_projects": 17,
            "auto_query_replenishment_batches": 2,
            "auto_query_replenishment_last_unique_projects": 17,
        },
    }
    repository.queries[run_id] = [
        {
            "query_id": "query-category",
            "run_id": run_id,
            "category_id": 24,
            "category_path": [24],
            "filters": {},
            "text": "Веб-дизайн",
            "origin": "category_browse",
            "approved": True,
            "enabled": True,
            "priority": 0,
        }
    ]
    repository.tasks = [
        {"task_id": "terminal", "run_id": run_id, "query_id": "query-category", "source": "web_projects", "page": 1, "state": "completed"}
    ]

    assert await planner._complete_exhausted_run(run_id) is None
    assert repository.runs[run_id]["counters"]["auto_project_refresh_mode"] is True
    assert repository.completed_run_ids == []
    assert run_id in planner._replenishment_retry_tasks
    assert {event["type"] for event in repository.events} >= {"query.refresh.enabled", "query.refresh.waiting"}
    await planner.close()


@pytest.mark.asyncio
async def test_compact_query_plan_keeps_a_minimal_non_overlapping_coverage_set(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run_id = "run-compact-plan"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "category_scope": {"category_id": 24, "category_name": "Web design"},
        "requested_workers": 3,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "paused",
        "counters": {"unique_projects": 4, "planned_queries": 3},
    }
    repository.queries[run_id] = [
        {"query_id": "broad", "run_id": run_id, "text": "web design", "enabled": True, "approved": True, "unique_projects": 3},
        {"query_id": "tail", "run_id": run_id, "text": "landing page", "enabled": True, "approved": True, "unique_projects": 1},
        {"query_id": "duplicate", "run_id": run_id, "text": "site design", "enabled": True, "approved": True, "unique_projects": 3},
    ]
    repository.projects[run_id] = [
        {"project_id": "one", "query_ids": ["broad", "duplicate"]},
        {"project_id": "two", "query_ids": ["broad", "duplicate"]},
        {"project_id": "three", "query_ids": ["broad", "duplicate"]},
        {"project_id": "four", "query_ids": ["tail"]},
    ]
    repository.tasks = [
        {"task_id": f"task-{query_id}", "run_id": run_id, "query_id": query_id, "source": "web_projects", "page": 1, "state": "queued"}
        for query_id in ("broad", "tail", "duplicate")
    ]

    compacted = await planner.compact_query_plan(run_id)

    retained = set(compacted["plan_compaction"]["retained_query_ids"])
    assert len(retained) == 2
    assert "tail" in retained
    assert retained.intersection({"broad", "duplicate"})
    assert {query["query_id"] for query in repository.queries[run_id] if query["enabled"]} == retained
    assert next(task for task in repository.tasks if task["query_id"] not in retained)["state"] == "cancelled"
    assert any(event["type"] == "query.plan.compacted" for event in repository.events)


@pytest.mark.asyncio
async def test_live_project_refresh_enqueues_fresh_anchor_tasks(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30, auto_project_refresh_query_limit=1),
    )
    run_id = "run-refresh-anchor"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "category_scope": {"category_id": 24, "category_name": "Веб-дизайн"},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {"unique_projects": 17},
    }
    repository.queries[run_id] = [
        {
            "query_id": "query-category",
            "run_id": run_id,
            "text": "Веб-дизайн",
            "origin": "category",
            "approved": True,
            "enabled": True,
            "priority": 0,
        }
    ]
    repository.tasks = [
        {"task_id": "terminal", "run_id": run_id, "query_id": "query-category", "source": "web_projects", "page": 1, "state": "completed"}
    ]

    await planner._enable_live_project_refresh(repository.runs[run_id])
    refreshed = await planner.get_run(run_id)
    enqueued = await planner._enqueue_live_project_refresh_tasks(refreshed)

    assert enqueued == 2
    refresh_tasks = [task for task in repository.tasks if task["state"] == "queued"]
    assert {task["source"] for task in refresh_tasks} == {"mobile_projects", "web_projects"}
    assert all(task["cursor"] == {"refresh_epoch": 0, "refresh_cycle": 1} for task in refresh_tasks)
    assert repository.runs[run_id]["counters"]["auto_project_refresh_cycles"] == 1


@pytest.mark.asyncio
async def test_continue_to_target_reuses_pending_tasks_without_creating_another_wave(
    tmp_path: Path,
    repository: _Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run_id = "run-resume-pending"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "category_scope": {"category_id": 24, "category_name": "Веб-дизайн"},
        "requested_workers": 1,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "stopped",
        "counters": {"unique_projects": 17},
    }
    repository.tasks = [
        {"task_id": "queued", "run_id": run_id, "query_id": "query-category", "source": "web_projects", "page": 1, "state": "queued"}
    ]

    async def unexpected_replenishment(*_: object, **__: object) -> int:
        raise AssertionError("pending tasks must be reused before planning another wave")

    monkeypatch.setattr(planner, "_replenish_query_queue", unexpected_replenishment)
    resumed = await planner.continue_to_target(run_id)

    assert resumed["state"] == "running"
    assert len(repository.tasks) == 1


@pytest.mark.asyncio
async def test_automatic_replenishment_uses_a_smaller_fresh_batch_instead_of_discarding_it(
    tmp_path: Path,
    repository: _Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run_id = "run-partial-wave"
    repository.runs[run_id] = {
        "run_id": run_id,
        "name": "Web design",
        "mode": "category",
        "brief": "заказы на веб-дизайн",
        "category_scope": {"category_id": 24, "category_path": [1, 24], "category_name": "Веб-дизайн"},
        "filters": {},
        "requested_workers": 15,
        "query_batch_size": 1,
        "target_unique_projects": 100,
        "state": "running",
        "counters": {},
    }

    async def ten_fresh_candidates(**_: object) -> BuyerAIQueryGenerationResult:
        return BuyerAIQueryGenerationResult(
            generation=BuyerQueryGenerationResult(
                candidates=tuple(
                    BuyerQueryCandidate(text=f"fresh Kwork design phrase {index}") for index in range(10)
                ),
                generator="test",
                used_fallback=False,
            ),
            prompt="",
            used_model=False,
        )

    monkeypatch.setattr(planner, "_generate_query_candidates_for_scopes", ten_fresh_candidates)
    generated = await planner.generate_queries(
        run_id,
        {
            "mode": "category",
            "requested_workers": 15,
            "query_batch_size": 1,
            "minimum_count": 30,
            "auto_approve": True,
        },
        created_by="automatic_replenishment",
    )

    assert len(generated["items"]) == 10
    assert len(repository.tasks) == 20


@pytest.mark.asyncio
async def test_hybrid_query_generation_combines_category_browse_with_task_routed_ai(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    class _QueryGateway:
        calls: list[dict[str, str]] = []

        async def generate(self, *, prompt: str, task: str) -> dict[str, object]:
            self.calls.append({"prompt": prompt, "task": task})
            return {"queries": [{"text": "crm integration", "rationale": "taxonomy-aligned"}]}

    gateway = _QueryGateway()
    planner = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(
            enabled=True,
            live_discovery=False,
            max_workers=30,
            initial_query_probe_count=1,
        ),
        query_generation_gateway=gateway,  # type: ignore[arg-type]
        taxonomy_context_provider=lambda scope: {
            "snapshot_id": scope["taxonomy_snapshot_id"],
            "vocabulary": ["CRM", "integration"],
        },
    )

    run = await planner.create_run(
        {
            "mode": "hybrid",
            "brief": "CRM integration projects",
            "requested_workers": 1,
            "category_scope": {
                "category_id": 42,
                "category_path": [10, 42],
                "taxonomy_snapshot_id": "snapshot-1",
            },
        }
    )

    assert gateway.calls[0]["task"] == "query_generation"
    assert '"snapshot_id":"snapshot-1"' in gateway.calls[0]["prompt"]
    assert any(event["type"] == "run.created" and event["payload"]["ai_used"] is True for event in repository.events)
    assert {query["origin"] for query in repository.queries[run["run_id"]]} >= {"category_browse"}
    assert len(repository.queries[run["run_id"]]) == 2

    await planner.create_run({"mode": "manual", "queries": [{"text": "operator query"}]})
    assert len(gateway.calls) == 1
    assert run["state"] == "ready"


@pytest.mark.asyncio
async def test_hybrid_query_generation_runs_every_selected_scope_concurrently(
    tmp_path: Path,
    repository: _Repository,
) -> None:
    class _ConcurrentGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.peak = 0

        async def generate(self, *, prompt: str, task: str) -> dict[str, object]:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {"queries": [{"text": f"scope query {self.calls}", "rationale": "test"}]}

    gateway = _ConcurrentGateway()
    service = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30, initial_query_probe_count=1),
        query_generation_gateway=gateway,  # type: ignore[arg-type]
    )
    scopes = [
        {"category_id": category_id, "category_path": [category_id], "category_name": f"Scope {category_id}"}
        for category_id in range(1, 6)
    ]

    await service.create_run(
        {
            "mode": "hybrid",
            "brief": "find relevant projects",
            "requested_workers": 1,
            "category_scope": {"category_id": 1, "category_path": [1], "category_scopes": scopes},
        }
    )

    assert gateway.calls == 5
    assert gateway.peak == 5


@pytest.mark.asyncio
async def test_category_run_plans_queries_for_every_selected_taxonomy_scope(
    service: BuyerSearchService,
    repository: _Repository,
) -> None:
    run = await service.create_run(
        {
            "mode": "category",
            "requested_workers": 2,
            "query_batch_size": 1,
            "category_scope": {
                "category_id": 5,
                "category_path": [5],
                "taxonomy_snapshot_id": "snapshot-1",
                "category_scopes": [
                    {
                        "category_id": 5,
                        "category_path": [5],
                        "category_name": "Тексты и переводы",
                        "taxonomy_snapshot_id": "snapshot-1",
                    },
                    {
                        "category_id": 7,
                        "category_path": [7],
                        "category_name": "Аудио, видео, съемка",
                        "taxonomy_snapshot_id": "snapshot-1",
                    },
                ],
            },
        }
    )

    queries = repository.queries[run["run_id"]]
    assert {query["category_id"] for query in queries} == {5, 7}
    assert run["category_scope"]["category_ids"] == [5, 7]
    assert [scope["category_id"] for scope in run["category_scope"]["category_scopes"]] == [5, 7]


@pytest.mark.asyncio
async def test_legacy_category_queries_are_repaired_from_the_saved_rubric_name(
    service: BuyerSearchService,
    repository: _Repository,
) -> None:
    run = await service.create_run(
        {
            "mode": "category",
            "requested_workers": 1,
            "category_scope": {
                "category_id": 80,
                "category_name": "Десктоп программирование",
                "category_path": [11, 80],
            },
        }
    )
    repository.queries[run["run_id"]][0]["origin"] = "category"
    repository.queries[run["run_id"]][0]["text"] = "category 80"
    repository.queries[run["run_id"]][0]["rationale"] = "Deterministic fallback candidate pending operator or AI refinement"

    page = await service.list_queries(run["run_id"])

    assert page["items"][0]["text"] == "Десктоп программирование"
    assert any(event["type"] == "query.legacy_text.repaired" for event in repository.events)


@pytest.mark.asyncio
async def test_lifecycle_transition_is_guarded(service: BuyerSearchService) -> None:
    run = await service.create_run(
        {
            "mode": "manual",
            "requested_workers": 1,
            "queries": [{"text": "landing page"}],
        }
    )

    running = await service.update_run(run["run_id"], {"state": "running"})
    assert running["state"] == "running"
    paused = await service.update_run(run["run_id"], {"state": "paused"})
    assert paused["state"] == "paused"
    with pytest.raises(BuyerSearchStateError):
        await service.update_run(run["run_id"], {"state": "completed"})


@pytest.mark.asyncio
async def test_restart_requeues_existing_tasks_without_deleting_durable_run_data(
    service: BuyerSearchService,
    repository: _Repository,
) -> None:
    run = await service.create_run({"mode": "manual", "queries": [{"text": "landing page"}]})
    stopped = await service.update_run(run["run_id"], {"state": "stopped"})

    restarted = await service.restart_run(run["run_id"])

    assert stopped["state"] == "stopped"
    assert restarted["state"] == "ready"
    assert restarted["restart"]["requeued_task_count"] == 2
    assert all(task["state"] == "queued" for task in repository.tasks)
    assert repository.events[-1]["type"] == "run.restarted"


@pytest.mark.asyncio
async def test_stopping_run_finalizes_its_backing_market_job(tmp_path: Path, repository: _Repository) -> None:
    finalized: list[tuple[str, str]] = []

    async def finalize(run_id: str, reason: str) -> None:
        finalized.append((run_id, reason))

    service = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
        runtime_finalizer=finalize,
    )
    run = await service.create_run({"mode": "manual", "queries": [{"text": "landing page"}]})

    stopped = await service.update_run(run["run_id"], {"state": "stopped"})

    assert stopped["state"] == "stopped"
    assert finalized == [(run["run_id"], "buyer_run_stopped")]


@pytest.mark.asyncio
async def test_live_run_is_blocked_until_the_durable_shadow_gate_approves(tmp_path: Path, repository: _Repository) -> None:
    class _Supervisor:
        run_ids: set[str] = set()

    gated = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=True, require_shadow_gate=True, max_workers=30),
        supervisor=_Supervisor(),  # type: ignore[arg-type]
        rollout_gate_checker=lambda _run_id, _workers: {"allowed": False, "blockers": ["accepted shadow comparison is required"]},
    )
    run = await gated.create_run(
        {
            "mode": "manual",
            "requested_workers": 2,
            "queries": [{"text": "landing page"}, {"text": "crm integration"}],
        }
    )

    blocked = await gated.update_run(run["run_id"], {"state": "running"})

    assert blocked["state"] == "blocked"
    assert "accepted shadow comparison" in blocked["last_error"]


@pytest.mark.asyncio
async def test_export_is_reproducible_and_audited(
    service: BuyerSearchService,
    repository: _Repository,
) -> None:
    run = await service.create_run(
        {
            "mode": "manual",
            "requested_workers": 1,
            "queries": [{"text": "landing page"}],
        }
    )
    repository.projects[run["run_id"]] = [
        {
            "project_id": "project-1",
            "title": "Landing page",
            "description": "Full project description",
            "budget_min": 1000,
            "budget_max": 5000,
            "offers": 2,
            "views": 14,
            "canonical_url": "https://kwork.ru/projects/1",
        }
    ]

    exported = await service.create_export(run["run_id"], format="jsonl")

    assert exported["state"] == "completed"
    assert exported["manifest"]["row_count"] == 1
    assert service.export_path(exported["export_id"]) is not None
    assert repository.artifacts[-1]["body"]["manifest"]["row_count"] == 1


@pytest.mark.asyncio
async def test_final_score_is_versioned_and_audited(tmp_path: Path, repository: _Repository) -> None:
    class _FinalScoreGateway:
        async def generate(self, *, prompt: str, task: str) -> dict[str, object]:
            assert task == "scoring"
            assert "response_schema" in prompt
            return {
                "total": 81,
                "breakdown": {"fit": 88, "risk": 71, "value": 84},
                "rationale": "Strong scope fit.",
                "provider": "test",
                "model": "gpt-5.6-sol",
            }

    scored_service = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
        final_scoring_gateway=_FinalScoreGateway(),  # type: ignore[arg-type]
    )
    run = await scored_service.create_run({"mode": "manual", "queries": [{"text": "landing page"}]})
    repository.projects[run["run_id"]] = [{"project_id": "project-1", "title": "Landing page", "description": "Build a site"}]

    result = await scored_service.final_score_project(run["run_id"], "project-1", profile_id="buyer-ai", profile_version="2")

    assert result["score_kind"] == "final"
    assert result["profile_id"] == "buyer-ai"
    assert result["profile_version"] == "2"
    assert result["model"] == "gpt-5.6-sol"
    assert repository.events[-1]["type"] == "project.final_scored"


@pytest.mark.asyncio
async def test_distribute_queries_schedules_each_source_once_per_approved_query(
    service: BuyerSearchService,
    repository: _Repository,
) -> None:
    run = await service.create_run(
        {
            "mode": "manual",
            "requested_workers": 1,
            "queries": [{"text": "landing page"}],
        }
    )

    repeated = await service.distribute_queries(run["run_id"])

    assert repeated["queued_count"] == 0
    assert repeated["queued_task_count"] == 0
    tasks = await repository.list_query_tasks(run["run_id"])
    assert len(tasks) == 2
    assert {(task["query_id"], task["source"], task["page"]) for task in tasks} == {
        (tasks[0]["query_id"], "mobile_projects", 1),
        (tasks[0]["query_id"], "web_projects", 1),
    }


@pytest.mark.asyncio
async def test_real_repository_keeps_two_source_initial_tasks_idempotent(tmp_path: Path) -> None:
    durable_repository = BuyerSearchRepository(tmp_path / "buyer-search.sqlite3")
    service = BuyerSearchService(
        durable_repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run = await service.create_run(
        {
            "mode": "manual",
            "requested_workers": 1,
            "queries": [{"text": "landing page"}],
        }
    )

    before = await durable_repository.list_query_tasks(run["run_id"], limit=10)
    repeated = await service.distribute_queries(run["run_id"])
    after = await durable_repository.list_query_tasks(run["run_id"], limit=10)
    bundles = await service.get_query_bundles(run["run_id"])

    assert {(task["source"], task["page"]) for task in before} == {
        ("mobile_projects", 1),
        ("web_projects", 1),
    }
    assert repeated["queued_task_count"] == 0
    assert [task["task_id"] for task in after] == [task["task_id"] for task in before]
    assert bundles["bundles"][0]["sources"] == ["mobile_projects", "web_projects"]
    assert bundles["bundles"][0]["task_count"] == 2
    stored_run = await durable_repository.get_run(run["run_id"])
    assert stored_run["counters"]["scheduled_tasks"] == 2


@pytest.mark.asyncio
async def test_semantic_collision_report_bundles_and_additive_regeneration(tmp_path: Path, repository: _Repository) -> None:
    service = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        semantic_scorer=lambda _left, _right: 0.99,
        settings=BuyerSearchSettings(enabled=True, live_discovery=False, max_workers=30),
    )
    run = await service.create_run(
        {
            "mode": "manual",
            "requested_workers": 2,
            "queries": [{"text": "CRM integration"}, {"text": "CRM automation"}],
        }
    )

    report = await service.get_query_collisions(run["run_id"])
    assert report["items"][0]["kind"] == "semantic"
    assert report["items"][0]["first_text"] == "CRM integration"
    bundles = await service.get_query_bundles(run["run_id"])
    assert bundles["eligible_query_count"] == 2
    assert [bundle["task_count"] for bundle in bundles["bundles"]] == [2, 2]

    regenerated = await service.regenerate_query_collisions(
        run["run_id"],
        {
            "collision_query_ids": [report["items"][0]["first_query_id"]],
            "queries": [{"text": "CRM migration"}, {"text": "CRM reporting"}],
            "minimum_count": 2,
            "auto_approve": True,
        },
    )

    assert regenerated["generation"]["items"][0]["text"] == "CRM migration"
    assert any(event["type"] == "query.collisions.regenerated" for event in repository.events)
