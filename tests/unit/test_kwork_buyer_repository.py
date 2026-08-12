from __future__ import annotations

import sqlite3

import pytest

from src.platforms.kwork_buyer.repository import (
    BuyerQueryTaskLeaseLostError,
    BuyerRepositoryValidationError,
    BuyerSearchRepository,
)


@pytest.fixture
def repository(tmp_path) -> BuyerSearchRepository:
    return BuyerSearchRepository(tmp_path / "buyer-search.sqlite3")


async def _run_with_queries(repository: BuyerSearchRepository) -> None:
    await repository.create_run(
        {"run_id": "run_1", "mode": "manual", "name": "Buyer test", "counters": {"unique_projects": 0}}
    )
    await repository.create_queries(
        "run_1",
        [
            {
                "query_id": "query_1",
                "text": "Telegram bot",
                "normalized_text": "telegram bot",
                "origin": "manual",
                "category_id": "41",
                "filters": {"price_to": 5000},
            },
            {
                "query_id": "query_2",
                "text": "Automation Telegram",
                "normalized_text": "automation telegram",
                "origin": "manual",
                "category_id": "41",
                "filters": {"price_to": 5000},
            },
        ],
    )


@pytest.mark.asyncio
async def test_run_record_decodes_json_fields_for_typed_api(repository: BuyerSearchRepository):
    await _run_with_queries(repository)

    run = await repository.get_run("run_1")

    assert run["counters"] == {"unique_projects": 0}
    assert run["filters"] == {}
    assert run["category_scope"] == []


@pytest.mark.asyncio
async def test_workspace_snapshot_round_trip_and_stale_reference_cleanup(repository: BuyerSearchRepository):
    await repository.create_run({"run_id": "run_workspace", "mode": "manual", "name": "Workspace"})
    state = {
        "builder": {"name": "Draft", "brief": "React landing", "exact_queries": ["react landing"]},
        "view": {
            "active_section": "projects",
            "selected_run_id": "run_workspace",
            "selected_project_id": "missing_project",
            "cursor": "cursor-1",
            "cursor_history": ["cursor-0"],
            "selected_project_ids": [],
        },
    }

    written = await repository.put_workspace_state(state)
    restored = await repository.get_workspace_state()

    assert written["schema_version"] == 1
    assert restored["state"]["builder"]["name"] == "Draft"
    assert restored["state"]["view"]["selected_run_id"] == "run_workspace"
    assert restored["state"]["view"]["selected_project_id"] is None

    await repository.delete_run("run_workspace")
    cleaned = await repository.get_workspace_state()
    assert cleaned["state"]["view"]["selected_run_id"] is None
    assert cleaned["state"]["view"]["cursor"] is None
    assert cleaned["state"]["view"]["cursor_history"] == []


@pytest.mark.asyncio
async def test_workspace_snapshot_rejects_unknown_schema_version(repository: BuyerSearchRepository):
    with pytest.raises(BuyerRepositoryValidationError):
        await repository.put_workspace_state({}, schema_version=2)


@pytest.mark.asyncio
async def test_project_projection_uses_latest_buyer_observation_and_hired_filter(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_buyer_projection", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    lease = await repository.lease_query_task("run_1", "worker_1")
    assert lease is not None
    await repository.commit_observed_page(
        task_id="task_buyer_projection",
        worker_id="worker_1",
        attempt_id=lease["attempt_id"],
        lease_fence=lease["lease_fence"],
        projects=[{
            "id": "remote_buyer",
            "title": "Landing page",
            "price": 25_000,
            "buyer_username": "client42",
            "buyer_hired_percent": 73,
            "buyer_projects_count": 19,
            "buyer_active_projects_count": 3,
            "orders": 11,
            "expires_at": "2026-08-06T12:00:00Z",
        }],
    )

    page = await repository.list_run_projects("run_1", filters={"min_buyer_hired_percent": 70})
    assert page["total"] == 1
    assert page["run_total"] == 1
    assert len(page["items"]) == 1
    project = page["items"][0]
    assert project["buyer_username"] == "client42"
    assert project["buyer_projects_count"] == 19
    assert project["buyer_active_projects_count"] == 3
    assert project["orders"] == 11
    assert project["expires_at"] == "2026-08-06T12:00:00Z"

    excluded = await repository.list_run_projects("run_1", filters={"min_buyer_hired_percent": 80})
    assert excluded["total"] == 0
    assert excluded["run_total"] == 1
    assert excluded["items"] == []


@pytest.mark.asyncio
async def test_query_constraint_and_task_fingerprint_are_durable(repository: BuyerSearchRepository):
    await _run_with_queries(repository)

    with pytest.raises(BuyerRepositoryValidationError):
        await repository.create_queries(
            "run_1",
            [
                {
                    "query_id": "query_duplicate",
                    "text": "Telegram Bot Again",
                    "normalized_text": "telegram bot",
                    "origin": "manual",
                    "category_id": "41",
                    "filters": {"price_to": 5000},
                }
            ],
        )

    task = await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1},
    )
    assert task["state"] == "queued"

    with pytest.raises(BuyerRepositoryValidationError):
        await repository.enqueue_query_task(
            "run_1",
            {"task_id": "task_2", "query_id": "query_1", "source": "mobile_projects", "page": 1},
        )


@pytest.mark.asyncio
async def test_terminal_task_updates_query_state_and_completes_an_exhausted_running_run(repository: BuyerSearchRepository):
    await repository.create_run(
        {
            "run_id": "run_exhausted",
            "mode": "manual",
            "name": "Exhausted",
            "state": "running",
            "target_unique_projects": 100,
        }
    )
    await repository.create_queries(
        "run_exhausted",
        [{"query_id": "query_exhausted", "text": "CRM", "normalized_text": "crm", "origin": "manual"}],
    )
    await repository.enqueue_query_task(
        "run_exhausted",
        {"task_id": "task_exhausted", "query_id": "query_exhausted", "source": "mobile_projects", "page": 1},
    )
    leased = await repository.lease_query_task("run_exhausted", "worker_1")
    assert leased is not None
    await repository.commit_observed_page(
        task_id="task_exhausted",
        worker_id="worker_1",
        attempt_id=leased["attempt_id"],
        lease_fence=leased["lease_fence"],
        projects=[],
    )

    query = (await repository.list_queries("run_exhausted"))[0]
    completed = await repository.complete_run_if_exhausted("run_exhausted")

    assert query["state"] == "exhausted"
    assert completed is not None
    assert completed["state"] == "completed"
    assert (await repository.complete_run_if_exhausted("run_exhausted")) is None


@pytest.mark.asyncio
async def test_page_commit_enqueues_all_explicit_continuation_pages_atomically(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_page_1", "query_id": "query_1", "source": "mobile_projects", "page": 1},
    )
    leased = await repository.lease_query_task("run_1", "worker_1")
    assert leased is not None

    committed = await repository.commit_observed_page(
        task_id="task_page_1",
        worker_id="worker_1",
        attempt_id=leased["attempt_id"],
        lease_fence=leased["lease_fence"],
        projects=[],
        next_tasks=[
            {"source": "mobile_projects", "page": 2, "cursor": {"pagination_fanout": True}},
            {"source": "mobile_projects", "page": 3, "cursor": {"pagination_fanout": True}},
        ],
    )

    tasks = await repository.list_query_tasks("run_1")
    queued_pages = sorted(task["page"] for task in tasks if task["state"] == "queued")
    assert committed["continuation_count"] == 2
    assert len(committed["continuation_task_ids"]) == 2
    assert queued_pages == [2, 3]


@pytest.mark.asyncio
async def test_malformed_response_tasks_are_requeued_and_high_budget_ranges_are_excluded(repository: BuyerSearchRepository):
    await repository.create_run({"run_id": "run_1", "mode": "manual", "name": "Buyer test", "state": "running"})
    await repository.create_queries(
        "run_1",
        [{"query_id": "query_1", "text": "Telegram bot", "normalized_text": "telegram bot", "origin": "manual"}],
    )
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    leased = await repository.lease_query_task("run_1", "worker_1")
    assert leased is not None
    await repository.fail_query_task(
        "task_1",
        "worker_1",
        leased["attempt_id"],
        leased["lease_fence"],
        failure_kind="transient",
        error="mobile project payload has no project ID",
    )

    requeued = await repository.requeue_malformed_response_tasks("run_1")
    replay = await repository.lease_query_task("run_1", "worker_2")
    assert requeued["requeued_task_count"] == 1
    assert replay is not None

    await repository.commit_observed_page(
        task_id="task_1",
        worker_id="worker_2",
        attempt_id=replay["attempt_id"],
        lease_fence=replay["lease_fence"],
        projects=[{"id": "budget-range", "title": "Range", "price": 5_000, "possible_price_limit": 15_000}],
    )

    assert (await repository.list_run_projects("run_1", filters={"max_budget": 10_000}))["items"] == []


@pytest.mark.asyncio
async def test_page_commit_canonicalizes_project_and_preserves_multi_query_provenance(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_2", "query_id": "query_2", "source": "mobile_projects", "page": 1}
    )

    first = await repository.lease_query_task("run_1", "worker_1")
    assert first is not None
    first_result = await repository.commit_observed_page(
        task_id=first["task_id"],
        worker_id="worker_1",
        attempt_id=first["attempt_id"],
        lease_fence=first["lease_fence"],
        projects=[{"id": "remote_42", "title": "Build a bot", "description": "Need Telegram automation", "price": 1000}],
    )
    assert first_result["new_project_count"] == 1

    second = await repository.lease_query_task("run_1", "worker_2")
    assert second is not None
    second_result = await repository.commit_observed_page(
        task_id=second["task_id"],
        worker_id="worker_2",
        attempt_id=second["attempt_id"],
        lease_fence=second["lease_fence"],
        projects=[{"id": "remote_42", "title": "Build a bot", "description": "Need Telegram automation", "price": 1200}],
    )
    assert second_result["new_project_count"] == 0

    page = await repository.list_run_projects("run_1")
    assert len(page["items"]) == 1
    detail = await repository.get_run_project("run_1", page["items"][0]["project_id"])
    assert detail["project"]["remote_project_id"] == "remote_42"
    assert {match["query_id"] for match in detail["matches"]} == {"query_1", "query_2"}
    assert len(detail["observations"]) == 2


@pytest.mark.asyncio
async def test_stale_task_fence_cannot_commit_new_observations(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    first = await repository.lease_query_task("run_1", "worker_a")
    assert first is not None

    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE buyer_query_tasks SET lease_deadline = '2000-01-01T00:00:00.000Z' WHERE task_id = ?", ("task_1",))

    second = await repository.lease_query_task("run_1", "worker_b")
    assert second is not None
    assert second["lease_fence"] > first["lease_fence"]

    with pytest.raises(BuyerQueryTaskLeaseLostError):
        await repository.commit_observed_page(
            task_id="task_1",
            worker_id="worker_a",
            attempt_id=first["attempt_id"],
            lease_fence=first["lease_fence"],
            projects=[{"id": "should_not_exist", "title": "Stale"}],
        )

    assert (await repository.list_run_projects("run_1"))["items"] == []


@pytest.mark.asyncio
async def test_restart_requeues_tasks_and_invalidates_an_inflight_lease(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    first = await repository.lease_query_task("run_1", "worker_a")
    assert first is not None

    restarted = await repository.restart_query_tasks("run_1")
    replay = await repository.lease_query_task("run_1", "worker_b")

    assert restarted["requeued_task_count"] == 1
    assert restarted["restart_count"] == 1
    assert replay is not None
    assert replay["lease_fence"] > first["lease_fence"]
    with pytest.raises(BuyerQueryTaskLeaseLostError):
        await repository.commit_observed_page(
            task_id="task_1",
            worker_id="worker_a",
            attempt_id=first["attempt_id"],
            lease_fence=first["lease_fence"],
            projects=[{"id": "stale-after-restart", "title": "Stale"}],
        )


@pytest.mark.asyncio
async def test_compact_query_plan_cancels_redundant_tasks_and_preserves_enabled_coverage(
    repository: BuyerSearchRepository,
) -> None:
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_keep", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_archive", "query_id": "query_2", "source": "web_projects", "page": 1}
    )

    compacted = await repository.compact_query_plan("run_1", ["query_1"])
    queries = await repository.list_queries("run_1")
    tasks = await repository.list_query_tasks("run_1")
    run = await repository.get_run("run_1")

    assert compacted["retained_query_ids"] == ["query_1"]
    assert {query["query_id"] for query in queries if query["enabled"]} == {"query_1"}
    assert next(task for task in tasks if task["task_id"] == "task_keep")["state"] == "cancelled"
    assert next(task for task in tasks if task["task_id"] == "task_archive")["state"] == "cancelled"
    assert run["counters"]["planned_queries"] == 1
    assert run["counters"]["queued_tasks"] == 2
    leased = await repository.lease_query_task("run_1", "worker_1")
    assert leased is not None
    assert leased["query_id"] == "query_1"


@pytest.mark.asyncio
async def test_account_route_endpoint_quarantine_blocks_only_matching_leases_until_expiry(
    repository: BuyerSearchRepository,
) -> None:
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_mobile", "query_id": "query_1", "source": "mobile_projects", "page": 1},
    )
    await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_web", "query_id": "query_2", "source": "web_projects", "page": 1},
    )
    identity = {
        "account_registration_id": "account-1",
        "transport_id": "transport-1",
        "egress_ip": "198.51.100.10",
        "route_generation": 4,
    }

    mobile = await repository.lease_query_task("run_1", "worker-1", identity=identity)
    assert mobile is not None
    assert mobile["endpoint"] == "mobile_projects"
    quarantine = await repository.quarantine_discovery_identity(
        "run_1",
        task_id=mobile["task_id"],
        worker_id="worker-1",
        attempt_id=mobile["attempt_id"],
        lease_fence=mobile["lease_fence"],
        identity=identity,
        endpoint="mobile_projects?access_token=never-stored",
        failure_kind="http_429",
        retry_after_seconds=60,
    )
    await repository.retry_query_task(
        mobile["task_id"],
        "worker-1",
        mobile["attempt_id"],
        mobile["lease_fence"],
        retry_after_seconds=0,
        retry_at="2000-01-01T00:00:00.000Z",
        failure_kind="http_429",
    )

    await repository.create_run({"run_id": "run_2", "mode": "manual", "name": "Second Buyer test"})
    await repository.create_queries(
        "run_2",
        [{"query_id": "query_3", "text": "Telegram bot copy", "origin": "manual", "approved": True}],
    )
    await repository.enqueue_query_task(
        "run_2",
        {"task_id": "task_other_run", "query_id": "query_3", "source": "mobile_projects", "page": 1},
    )
    assert await repository.lease_query_task("run_2", "worker-2", identity=identity) is None

    web = await repository.lease_query_task("run_1", "worker-1", identity=identity)
    assert web is not None
    assert web["task_id"] == "task_web"
    await repository.fail_query_task(
        web["task_id"],
        "worker-1",
        web["attempt_id"],
        web["lease_fence"],
        failure_kind="fatal",
    )

    assert await repository.lease_query_task("run_1", "worker-1", identity=identity) is None
    active = await repository.list_discovery_quarantines("run_1", active_only=True)
    assert active == [quarantine]
    assert active[0]["endpoint"] == "mobile_projects"
    assert active[0]["last_egress_ip"] == "198.51.100.10"

    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE buyer_discovery_quarantines SET expires_at = '2000-01-01T00:00:00.000Z' WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        )

    replay = await repository.lease_query_task("run_1", "worker-1", identity=identity)
    assert replay is not None
    assert replay["task_id"] == "task_mobile"
    quarantines = await repository.list_discovery_quarantines("run_1")
    assert quarantines[0]["state"] == "expired"
    audit_events = await repository.replay_events("run_1")
    audit = next(event for event in audit_events if event["event_type"] == "identity.quarantined")
    assert audit["payload"]["policy"] == "account_transport_endpoint"
    assert audit["payload"]["endpoint"] == "mobile_projects"


@pytest.mark.asyncio
async def test_quarantine_requires_the_current_task_fence(repository: BuyerSearchRepository) -> None:
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1},
    )
    identity = {"account_registration_id": "account-1", "transport_id": "transport-1"}
    lease = await repository.lease_query_task("run_1", "worker-1", identity=identity)
    assert lease is not None

    with pytest.raises(BuyerQueryTaskLeaseLostError):
        await repository.quarantine_discovery_identity(
            "run_1",
            task_id=lease["task_id"],
            worker_id="worker-1",
            attempt_id=lease["attempt_id"],
            lease_fence=lease["lease_fence"] + 1,
            identity=identity,
            endpoint="mobile_projects",
            failure_kind="http_403",
            retry_after_seconds=60,
        )

    assert await repository.list_discovery_quarantines("run_1") == []


@pytest.mark.asyncio
async def test_cursor_artifacts_and_events(repository: BuyerSearchRepository):
    await _run_with_queries(repository)
    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    lease = await repository.lease_query_task("run_1", "worker_1")
    assert lease is not None
    await repository.commit_observed_page(
        task_id="task_1",
        worker_id="worker_1",
        attempt_id=lease["attempt_id"],
        lease_fence=lease["lease_fence"],
        projects=[{"id": "one", "title": "One"}, {"id": "two", "title": "Two"}],
    )

    first_page = await repository.list_run_projects("run_1", limit=1)
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"]
    second_page = await repository.list_run_projects("run_1", limit=1, cursor=first_page["next_cursor"])
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["project_id"] != first_page["items"][0]["project_id"]

    artifact = await repository.store_raw_artifact(
        {
            "source": "mobile_projects",
            "endpoint": "/projects",
            "headers": {"Authorization": "Bearer secret", "Content-Type": "application/json"},
            "body": {"token": "secret", "response": [{"id": "one"}]},
        }
    )
    assert artifact["headers"] == {"Content-Type": "application/json"}
    assert artifact["body"]["token"] == "[redacted]"

    event = await repository.append_event("run_1", "project.observed", {"project_id": "one"})
    replay = await repository.replay_events("run_1", after_seq=0)
    assert replay == [event]
