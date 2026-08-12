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


async def _project_in_run(repository: BuyerSearchRepository) -> tuple[str, str]:
    await repository.create_run({"run_id": "run_1", "mode": "manual", "name": "Buyer test"})
    await repository.create_queries(
        "run_1",
        [
            {
                "query_id": "query_1",
                "text": "Telegram bot",
                "normalized_text": "telegram bot",
                "origin": "manual",
            }
        ],
    )
    await repository.enqueue_query_task(
        "run_1",
        {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1},
    )
    task = await repository.lease_query_task("run_1", "worker_1")
    assert task is not None
    await repository.commit_observed_page(
        task_id="task_1",
        worker_id="worker_1",
        attempt_id=task["attempt_id"],
        lease_fence=task["lease_fence"],
        projects=[
            {
                "id": "remote_1",
                "title": "Build Telegram automation",
                "description": "A detailed project description for a buyer.",
                "price": 20_000,
                "offers": 2,
                "views": 14,
                "age_seconds": 300,
            }
        ],
    )
    project = (await repository.list_run_projects("run_1"))["items"][0]
    return "run_1", project["project_id"]


@pytest.mark.asyncio
async def test_attachment_score_and_shortlist_are_durable_in_projection_and_detail(repository: BuyerSearchRepository) -> None:
    run_id, project_id = await _project_in_run(repository)
    counters = (await repository.get_run(run_id))["counters"]
    assert counters["pages_completed"] == 1
    assert counters["projects_seen"] == 1
    assert counters["unique_projects"] == 1
    initial = await repository.get_run_project(run_id, project_id)
    observation_id = initial["observations"][0]["observation_id"]

    attachment = await repository.upsert_attachment(
        run_id,
        project_id,
        {
            "attachment_id": "attachment_1",
            "source_observation_id": observation_id,
            "remote_url": "https://kwork.example/brief.txt",
            "filename": "brief.txt",
            "content_type": "text/plain",
            "detected_type": "text",
            "size_bytes": 42,
            "sha256": "fixture",
            "state": "downloaded",
        },
    )
    assert attachment["attachment_id"] == "attachment_1"
    assert (await repository.list_run_projects(run_id))["items"][0]["attachment_parse_state"] == "pending"

    derivative = await repository.record_attachment_derivative(
        attachment["attachment_id"],
        {
            "kind": "text",
            "parser_name": "fixture-parser",
            "parser_version": "1",
            "content_hash": "fixture-text-v1",
            "extracted_text": "bounded extracted brief",
            "metadata": {"characters": 22},
            "token_count": 4,
            "state": "parsed",
        },
    )
    assert derivative["state"] == "parsed"

    preliminary = await repository.record_score(
        run_id,
        project_id,
        {
            "score_kind": "preliminary",
            "profile_id": "deterministic-v1",
            "profile_version": "1",
            "input_context_hash": "preliminary-context",
            "total": 71.5,
            "breakdown": {"budget": 31.5, "competition": 40},
            "rationale": "good budget and low competition",
        },
    )
    final = await repository.record_score(
        run_id,
        project_id,
        {
            "score_kind": "ai",
            "profile_id": "ai-fit-v2",
            "profile_version": "2",
            "provider": "openai",
            "model": "fixture",
            "prompt_version": "2026-07",
            "input_context_hash": "final-context",
            "total_score": 88,
            "breakdown": {"fit": 88},
        },
    )
    assert preliminary["score_kind"] == "preliminary"
    assert final["score_kind"] == "final"

    shortlist = await repository.set_shortlist(
        run_id,
        project_id,
        {
            "state": "shortlisted",
            "rank": 1,
            "selected_by": "operator",
            "tags": ["priority", "backend", "priority"],
            "note": "Need a proposal with integration milestones.",
        },
    )
    assert shortlist["tags"] == ["backend", "priority"]
    assert len(shortlist["notes"]) == 1

    filtered = await repository.list_run_projects(
        run_id,
        sort="final_score_desc",
        filters={
            "score_gte": 80,
            "attachment_parse_state": "parsed",
            "has_attachments": True,
            "tag": "priority",
            "max_offers": 2,
        },
    )
    assert [item["project_id"] for item in filtered["items"]] == [project_id]
    row = filtered["items"][0]
    assert row["attachment_count"] == 1
    assert row["attachment_parse_state"] == "parsed"
    assert row["preliminary_score"] == 71.5
    assert row["final_score"] == 88
    assert row["shortlist_state"] == "shortlisted"
    assert row["tags"] == ["backend", "priority"]

    detail = await repository.get_run_project(run_id, project_id)
    assert detail["attachments"][0]["derivatives"][0]["kind"] == "text"
    assert {score["score_kind"] for score in detail["scores"]} == {"preliminary", "final"}
    assert detail["shortlist"]["notes"][0]["body"].startswith("Need a proposal")
    facets = await repository.get_facets(run_id)
    assert facets["attachment_parse_states"] == [{"value": "parsed", "count": 1}]
    assert {entry["value"] for entry in facets["tags"]} == {"backend", "priority"}


@pytest.mark.asyncio
async def test_export_records_are_idempotent_and_update_with_manifest(repository: BuyerSearchRepository) -> None:
    run_id, _ = await _project_in_run(repository)

    created = await repository.create_export(
        run_id,
        {
            "export_id": "export_1",
            "format": "jsonl",
            "filters": {"score_gte": 70},
            "selected_project_ids": ["project-not-required-for-audit"],
            "include_attachments": True,
        },
    )
    duplicate = await repository.create_export(run_id, {"export_id": "export_1", "format": "csv"})
    assert duplicate == created
    assert created["state"] == "queued"
    assert created["selection"]["filters"] == {"score_gte": 70}

    updated = await repository.update_export(
        run_id,
        "export_1",
        {
            "state": "completed",
            "progress": {"rows": 1, "bytes": 12},
            "filename": "buyer-search.jsonl",
            "bytes": 12,
            "sha256": "sha256:fixture",
            "manifest": {"row_count": 1, "project_ids": ["project_1"]},
        },
    )
    assert updated["state"] == "completed"
    assert updated["manifest"]["row_count"] == 1
    assert updated["manifest_hash"].startswith("sha256:")
    assert (await repository.get_export(run_id, "export_1"))["filename"] == "buyer-search.jsonl"


@pytest.mark.asyncio
async def test_query_update_task_fencing_and_run_counters(repository: BuyerSearchRepository) -> None:
    await repository.create_run({"run_id": "run_1", "mode": "manual", "name": "Buyer test"})
    await repository.create_queries(
        "run_1",
        [
            {"query_id": "query_1", "text": "First", "normalized_text": "first", "origin": "manual"},
            {"query_id": "query_2", "text": "Second", "normalized_text": "second", "origin": "manual"},
        ],
    )
    changed = await repository.update_query(
        "run_1",
        "query_1",
        {"text": "Updated query", "filters": {"price_to": 5000}, "approved": True, "enabled": False},
    )
    assert changed["normalized_text"] == "updated query"
    assert changed["filters"] == {"price_to": 5000}
    with pytest.raises(BuyerRepositoryValidationError):
        await repository.update_query("run_1", "query_2", {"text": "Updated query", "filters": {"price_to": 5000}})

    await repository.update_query("run_1", "query_1", {"enabled": True})

    await repository.enqueue_query_task(
        "run_1", {"task_id": "task_1", "query_id": "query_1", "source": "mobile_projects", "page": 1}
    )
    first = await repository.lease_query_task("run_1", "worker_1")
    assert first is not None
    retried = await repository.retry_query_task(
        "task_1",
        "worker_1",
        first["attempt_id"],
        first["lease_fence"],
        retry_after_seconds=0,
        failure_kind="timeout",
        error="fixture timeout",
    )
    assert retried["state"] == "retry_wait"
    assert (await repository.list_query_tasks("run_1", query_id="query_1"))[0]["task_id"] == "task_1"

    second = await repository.lease_query_task("run_1", "worker_2")
    assert second is not None
    with pytest.raises(BuyerQueryTaskLeaseLostError):
        await repository.fail_query_task(
            "task_1", "worker_1", first["attempt_id"], first["lease_fence"], failure_kind="fatal", error="stale"
        )
    failed = await repository.fail_query_task(
        "task_1", "worker_2", second["attempt_id"], second["lease_fence"], failure_kind="fatal", error="fixture fatal"
    )
    assert failed["state"] == "failed"
    counters = (await repository.get_run("run_1"))["counters"]
    assert counters["retries"] == 1
    assert counters["failed_tasks"] == 1
    assert counters["errors"] == 1


@pytest.mark.asyncio
async def test_initialize_migrates_legacy_projection_columns_idempotently(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE buyer_runs (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE buyer_run_projects (
                run_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, project_id)
            );
            """
        )

    repository = BuyerSearchRepository(path)
    await repository.initialize()
    await repository.initialize()
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(buyer_run_projects)")}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(buyer_runs)")}

    assert {"attachment_count", "attachment_parse_state", "preliminary_score", "final_score", "tags_json"} <= columns
    assert {"counters_json", "last_error", "last_failure_kind", "last_task_id"} <= run_columns
