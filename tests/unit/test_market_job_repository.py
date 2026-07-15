from __future__ import annotations

import json
import sqlite3

import pytest

from src.platforms.kwork_supply.models import (
    CommandState,
    JobPhase,
    JobState,
    MarketJobCreate,
    MarketScope,
    Operation,
    OperationKind,
    OperationState,
    ShardSpec,
    TransportHealth,
    TransportKind,
    TransportSnapshot,
    WorkerCommandKind,
    WorkerDesiredState,
    WorkerRecord,
    WorkerState,
)
from src.platforms.kwork_supply.repository import (
    MarketJobRepository,
    MarketJobRevisionConflictError,
    MarketOperationLeaseError,
)


@pytest.fixture
def repository(tmp_path) -> MarketJobRepository:
    return MarketJobRepository(tmp_path / "market-jobs.sqlite3")


async def create_job(repository: MarketJobRepository, *, job_id: str = "job_test") -> dict[str, object]:
    return await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(
                category_id=38,
                category_name="Website work",
                classifier_id=1587,
                canonical_alias="website-repair",
                filters={"price_to": 5000},
            ),
            target_unique_cards=100,
            desired_workers=2,
        ),
        job_id=job_id,
    )


@pytest.mark.asyncio
async def test_create_get_list_and_transition_job_with_revision(repository: MarketJobRepository):
    created = await create_job(repository)

    assert created["job_id"] == "job_test"
    assert created["state"] == JobState.PREPARING.value
    assert created["phase"] == JobPhase.PREPARE.value
    assert created["revision"] == 1
    assert created["scope"] == {
        "category_id": 38,
        "category_name": "Website work",
        "classifier_id": 1587,
        "classifier_name": "",
        "canonical_alias": "website-repair",
        "filters": {"price_to": 5000},
    }

    updated = await repository.update_job_state(
        "job_test",
        JobState.MAPPING,
        phase=JobPhase.MAP,
        expected_revision=1,
        counters={"mapping_requests": 1},
    )

    assert updated["state"] == JobState.MAPPING.value
    assert updated["phase"] == JobPhase.MAP.value
    assert updated["revision"] == 2
    assert updated["counters"] == {"mapping_requests": 1}
    assert (await repository.get_job("job_test"))["revision"] == 2
    assert [job["job_id"] for job in await repository.list_jobs(states=[JobState.MAPPING])] == ["job_test"]

    with pytest.raises(MarketJobRevisionConflictError):
        await repository.update_job_state("job_test", JobState.RUNNING, expected_revision=1)


@pytest.mark.asyncio
async def test_job_persists_a_normalized_manual_account_team(repository: MarketJobRepository):
    created = await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, category_name="Website work"),
            account_registration_ids=(" account-a ", "account-b", "account-a"),
        ),
        job_id="job_account_team",
    )

    restored = await repository.get_job("job_account_team")

    assert created["account_registration_ids"] == ["account-a", "account-b"]
    assert restored is not None
    assert restored["account_registration_ids"] == ["account-a", "account-b"]


@pytest.mark.asyncio
async def test_schema_uses_separate_wal_database_and_alias_registry(repository: MarketJobRepository):
    await repository.initialize()
    alias = await repository.upsert_category_alias(
        38,
        "website-repair",
        validation_status="accepted",
        active_category_id=38,
        source_url="https://kwork.ru/categories/website-repair",
        protection_state="ok",
    )

    assert alias["validation_status"] == "accepted"
    assert alias["active_category_id"] == 38
    assert await repository.get_category_alias(38, "website-repair") == alias

    with sqlite3.connect(repository.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert {
        "market_jobs",
        "market_category_aliases",
        "market_shards",
        "market_operations",
        "market_operation_attempts",
        "market_workers",
        "market_transports",
        "market_listings",
        "market_listing_observations",
        "market_listing_features",
        "market_seller_features",
        "market_listing_reviews",
        "market_listing_embeddings",
        "market_semantic_clusters",
        "market_semantic_dossiers",
        "market_semantic_cluster_members",
        "market_recommendations",
        "market_draft_handoffs",
        "market_published_listings",
        "market_events",
        "market_checkpoints",
        "market_worker_commands",
    } <= tables


@pytest.mark.asyncio
async def test_worker_job_binding_migrates_an_existing_repository_database(tmp_path):
    db_path = tmp_path / "old-market-jobs.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE market_workers (
                worker_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                desired_state TEXT NOT NULL,
                actual_state TEXT NOT NULL,
                runtime_kind TEXT NOT NULL,
                transport_id TEXT,
                current_operation_id TEXT,
                heartbeat_at TEXT,
                counters_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    old_repository = MarketJobRepository(db_path)
    await old_repository.initialize()
    await create_job(old_repository)
    worker = await old_repository.upsert_worker(
        WorkerRecord(
            worker_id="worker_a",
            generation=1,
            desired_state=WorkerDesiredState.RUNNING,
            actual_state=WorkerState.IDLE,
        ),
        job_id="job_test",
    )

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(market_workers)")}

    assert "job_id" in columns
    assert worker["job_id"] == "job_test"
    assert [record["worker_id"] for record in await old_repository.list_workers(job_id="job_test")] == ["worker_a"]


@pytest.mark.asyncio
async def test_operation_lease_expires_is_recovered_and_released_again(repository: MarketJobRepository):
    await create_job(repository)
    await repository.create_shard(
        ShardSpec(shard_id="shard_1", job_id="job_test", source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_1",
            job_id="job_test",
            shard_id="shard_1",
            kind=OperationKind.FETCH_BATCH,
            state=OperationState.QUEUED,
        )
    )

    first = await repository.lease_operation(
        "worker_a",
        lease_seconds=10,
        now="2026-07-11T10:00:00Z",
    )

    assert first is not None
    assert first["state"] == OperationState.LEASED.value
    assert first["lease_owner"] == "worker_a"
    assert first["current_attempt"] == 1
    assert first["attempt_id"]
    assert await repository.recover_expired_operations(now="2026-07-11T10:00:11Z") == 1

    recovered = await repository.get_operation("op_1")
    assert recovered is not None
    assert recovered["state"] == OperationState.QUEUED.value
    assert recovered["lease_owner"] is None

    second = await repository.lease_operation(
        "worker_b",
        lease_seconds=10,
        now="2026-07-11T10:00:12Z",
    )
    assert second is not None
    assert second["current_attempt"] == 2
    assert second["attempt_id"] != first["attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_state",
    (
        JobState.PAUSING,
        JobState.PAUSED,
        JobState.STOPPING,
        JobState.STOPPED,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.COMPLETED,
    ),
)
async def test_non_leasable_job_states_keep_queued_operations_unclaimed(
    repository: MarketJobRepository,
    blocked_state: JobState,
):
    await create_job(repository)
    await repository.enqueue_operation(
        Operation(
            operation_id="op_state_gate",
            job_id="job_test",
            shard_id=None,
            kind=OperationKind.MAP_SCOPE,
        )
    )
    blocked = await repository.update_job_state(
        "job_test",
        blocked_state,
        expected_revision=1,
        now="2026-07-11T10:00:00Z",
    )

    assert await repository.lease_operation("worker_a", job_id="job_test", now="2026-07-11T10:00:01Z") is None
    operation = await repository.get_operation("op_state_gate")
    assert operation is not None
    assert operation["state"] == OperationState.QUEUED.value
    assert operation["lease_owner"] is None

    if blocked_state is JobState.PAUSED:
        resumed = await repository.update_job_state(
            "job_test",
            JobState.RUNNING,
            expected_revision=blocked["revision"],
            now="2026-07-11T10:00:02Z",
        )
        leased = await repository.lease_operation("worker_a", job_id="job_test", now="2026-07-11T10:00:03Z")
        assert resumed["state"] == JobState.RUNNING.value
        assert leased is not None
        assert leased["operation_id"] == "op_state_gate"


@pytest.mark.asyncio
async def test_events_replay_monotonically_and_accepted_commit_is_idempotent(repository: MarketJobRepository):
    await create_job(repository)
    await repository.create_shard(
        ShardSpec(shard_id="shard_1", job_id="job_test", source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_1",
            job_id="job_test",
            shard_id="shard_1",
            kind=OperationKind.FETCH_BATCH,
            state=OperationState.QUEUED,
        )
    )
    leased = await repository.lease_operation("worker_a", now="2026-07-11T10:00:00Z")
    assert leased is not None

    first_event = await repository.append_event("job_test", "job.created", {"source": "test"})
    second_event = await repository.append_event("job_test", "job.mapping.started", {"step": 1})
    assert (first_event["sequence"], second_event["sequence"]) == (1, 2)
    assert [event["sequence"] for event in await repository.replay_events("job_test", after_seq=1)] == [2]

    commit = await repository.commit_accepted_batch(
        job_id="job_test",
        shard_id="shard_1",
        operation_id="op_1",
        attempt_id=leased["attempt_id"],
        idempotency_key="batch:web-catalog:1",
        source="web_catalog",
        requested_cursor={"exclude_ids": []},
        reported_cursor={"exclude_ids": []},
        next_cursor={"exclude_ids": ["101", "102"]},
        fingerprint="fixture-fingerprint",
        listings=[
            {"id": 101, "title": "First", "seller_id": "seller_1", "price": 1000},
            {"id": 102, "title": "Second", "seller_id": "seller_2", "price": 2000},
        ],
        counter_deltas={"requests": 1},
        event_payloads={"event_type": "batch.accepted", "payload": {"fixture": True}},
        next_operation={"kind": OperationKind.FETCH_BATCH, "idempotency_key": "batch:web-catalog:2"},
        now="2026-07-11T10:00:05Z",
    )
    replay = await repository.commit_accepted_batch(
        job_id="job_test",
        shard_id="shard_1",
        operation_id="op_1",
        idempotency_key="batch:web-catalog:1",
        listings=[],
        now="2026-07-11T10:01:00Z",
    )

    assert commit["idempotent_replay"] is False
    assert commit["new_listings"] == 2
    assert commit["observations"] == 2
    assert replay["idempotent_replay"] is True
    assert replay["revision"] == commit["revision"]
    assert replay["counters"] == commit["counters"]

    job = await repository.get_job("job_test")
    assert job is not None
    assert job["counters"] == {
        "accepted_batches": 1,
        "card_occurrences": 2,
        "listing_observations": 2,
        "requests": 1,
        "unique_cards": 2,
        "unique_listings": 2,
    }
    events = await repository.replay_events("job_test")
    assert [event["sequence"] for event in events] == [1, 2, 3]
    attempts = await repository.list_operation_attempts("op_1")
    assert attempts[0]["requested_cursor"] == {"exclude_ids": []}
    assert attempts[0]["reported_cursor"] == {"exclude_ids": []}
    assert attempts[0]["page_fingerprint"] == "fixture-fingerprint"

    await repository.append_event("job_test", "request.started", {"peak_requests": 4})
    recent = await repository.list_recent_events("job_test", limit=2)
    assert [event["sequence"] for event in recent] == [3, 4]
    concurrency = await repository.get_request_concurrency_summary("job_test")
    assert concurrency["fetch_attempt_count"] == 1
    assert concurrency["peak_parallel_requests"] == 4
    assert concurrency["distinct_workers"] == 1

    with sqlite3.connect(repository.db_path) as connection:
        listing_count = connection.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
        observation_count = connection.execute("SELECT COUNT(*) FROM market_listing_observations").fetchone()[0]
        completed_operations = connection.execute(
            "SELECT COUNT(*) FROM market_operations WHERE state = 'succeeded'"
        ).fetchone()[0]

    assert (listing_count, observation_count, completed_operations) == (2, 2, 1)


@pytest.mark.asyncio
async def test_accepted_commit_does_not_enqueue_continuation_after_target_is_reached(repository: MarketJobRepository):
    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=1,
        ),
        job_id="job_target",
    )
    await repository.create_shard(
        ShardSpec(shard_id="shard_target", job_id="job_target", source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_target",
            job_id="job_target",
            shard_id="shard_target",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation("worker_target", job_id="job_target")
    assert leased is not None

    committed = await repository.commit_accepted_batch(
        job_id="job_target",
        shard_id="shard_target",
        operation_id="op_target",
        attempt_id=leased["attempt_id"],
        listings=[{"id": 1}, {"id": 2}],
        next_operation={"kind": OperationKind.FETCH_BATCH, "idempotency_key": "target-continuation"},
    )

    assert committed["target_reached"] is True
    assert committed["next_operation_id"] is None
    assert await repository.list_operations("job_target", state=OperationState.QUEUED) == []


@pytest.mark.asyncio
async def test_concurrency_signals_are_derived_from_a_bounded_attempt_window(repository: MarketJobRepository):
    await create_job(repository)
    await repository.enqueue_operation(
        Operation(
            operation_id="op_success",
            job_id="job_test",
            shard_id=None,
            kind=OperationKind.MAP_SCOPE,
        )
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_rate_limited",
            job_id="job_test",
            shard_id=None,
            kind=OperationKind.MAP_SCOPE,
        )
    )
    first = await repository.lease_operation("worker_success", job_id="job_test")
    assert first is not None
    await repository.complete_operation(first["operation_id"], "worker_success")
    second = await repository.lease_operation("worker_rate", job_id="job_test")
    assert second is not None
    await repository.fail_operation(
        second["operation_id"],
        "worker_rate",
        "HTTP 429",
        failure_kind="rate_limited",
    )

    signals = await repository.get_job_concurrency_signals("job_test", window=10)

    assert signals == {
        "window_attempt_count": 2,
        "success_count": 1,
        "http_403_count": 0,
        "http_429_count": 1,
        "timeout_count": 0,
        "received_count": 0,
        "new_unique_count": 0,
        "novelty_rate": None,
    }


@pytest.mark.asyncio
async def test_completing_job_does_not_enqueue_a_late_collection_continuation(repository: MarketJobRepository):
    await create_job(repository, job_id="job_time_budget")
    await repository.create_shard(
        ShardSpec(shard_id="shard_time_budget", job_id="job_time_budget", source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_time_budget",
            job_id="job_time_budget",
            shard_id="shard_time_budget",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation("worker_time_budget", job_id="job_time_budget")
    assert leased is not None
    job = await repository.get_job("job_time_budget")
    assert job is not None
    await repository.update_job_state(
        "job_time_budget",
        JobState.COMPLETING,
        phase=JobPhase.COLLECT,
        expected_revision=job["revision"],
    )

    committed = await repository.commit_accepted_batch(
        job_id="job_time_budget",
        shard_id="shard_time_budget",
        operation_id="op_time_budget",
        attempt_id=leased["attempt_id"],
        listings=[{"id": 1}],
        next_operation={"kind": OperationKind.FETCH_BATCH, "idempotency_key": "late-continuation"},
        shard_state="active",
    )

    shard = await repository.get_shard("shard_time_budget")
    assert committed["next_operation_id"] is None
    assert shard is not None and shard["state"] == "exhausted"


@pytest.mark.asyncio
async def test_operational_views_commands_and_generic_lease_completion(repository: MarketJobRepository):
    await create_job(repository)
    controls = await repository.update_job_configuration(
        "job_test",
        desired_workers=3,
        target_unique_cards=250,
        profile="deep",
        request_budget=500,
        time_budget_seconds=3600,
        expected_revision=1,
        now="2026-07-11T10:00:00Z",
    )
    cleared = await repository.update_job_settings(
        "job_test",
        request_budget=None,
        expected_revision=controls["revision"],
        now="2026-07-11T10:00:01Z",
    )
    assert controls["revision"] == 2
    assert cleared["revision"] == 3
    assert {
        "desired_workers": cleared["desired_workers"],
        "target_unique_cards": cleared["target_unique_cards"],
        "profile": cleared["profile"],
        "request_budget": cleared["request_budget"],
        "time_budget_seconds": cleared["time_budget_seconds"],
    } == {
        "desired_workers": 3,
        "target_unique_cards": 250,
        "profile": "deep",
        "request_budget": None,
        "time_budget_seconds": 3600,
    }

    await repository.upsert_transport(
        TransportSnapshot(
            transport_id="transport_a",
            kind=TransportKind.VPNTE,
            health=TransportHealth.HEALTHY,
            slot=1,
            proxy_url="http://127.0.0.1:18001",
        )
    )
    await repository.upsert_transport(
        TransportSnapshot(transport_id="transport_z", kind=TransportKind.DIRECT, health=TransportHealth.HEALTHY)
    )
    await repository.upsert_worker(
        WorkerRecord(
            worker_id="worker_a",
            generation=1,
            desired_state=WorkerDesiredState.RUNNING,
            actual_state=WorkerState.IDLE,
            transport_id="transport_a",
        ),
        job_id="job_test",
    )
    await repository.upsert_worker(
        WorkerRecord(
            worker_id="worker_z",
            generation=1,
            desired_state=WorkerDesiredState.RUNNING,
            actual_state=WorkerState.IDLE,
        )
    )
    for operation_id in ("op_a", "op_b", "op_c"):
        await repository.enqueue_operation(
            Operation(
                operation_id=operation_id,
                job_id="job_test",
                shard_id=None,
                kind=OperationKind.MAP_SCOPE,
                state=OperationState.QUEUED,
            )
        )

    assert [worker["worker_id"] for worker in await repository.list_workers(job_id="job_test")] == ["worker_a"]
    assert [transport["transport_id"] for transport in await repository.list_transports(job_id="job_test")] == [
        "transport_a", "transport_z"
    ]
    assert (await repository.get_worker("worker_a"))["job_id"] == "job_test"
    assert (await repository.get_transport("transport_a"))["transport_id"] == "transport_a"

    first_page = await repository.list_operations("job_test", state=OperationState.QUEUED, limit=2)
    second_page = await repository.list_operations(
        "job_test",
        state=[OperationState.QUEUED],
        cursor=first_page[-1]["operation_id"],
        limit=2,
    )
    assert [operation["operation_id"] for operation in first_page] == ["op_a", "op_b"]
    assert [operation["operation_id"] for operation in second_page] == ["op_c"]

    leased = await repository.lease_operation(
        "worker_a",
        job_id="job_test",
        transport_id="transport_a",
        now="2026-07-11T10:01:00Z",
    )
    assert leased is not None
    assert leased["operation_id"] == "op_a"
    assert [worker["worker_id"] for worker in await repository.list_workers(job_id="job_test")] == ["worker_a"]
    assert [transport["transport_id"] for transport in await repository.list_transports(job_id="job_test")] == [
        "transport_a", "transport_z"
    ]
    workers_page = await repository.list_workers(limit=1)
    assert [worker["worker_id"] for worker in await repository.list_workers(cursor=workers_page[0]["worker_id"])] == [
        "worker_z"
    ]
    transports_page = await repository.list_transports(limit=1)
    assert [transport["transport_id"] for transport in await repository.list_transports(cursor=transports_page[0]["transport_id"])] == [
        "transport_z"
    ]

    with pytest.raises(MarketOperationLeaseError):
        await repository.complete_operation("op_a", "worker_z", now="2026-07-11T10:01:01Z")
    completed = await repository.complete_operation("op_a", "worker_a", now="2026-07-11T10:01:01Z")
    assert completed["state"] == OperationState.SUCCEEDED.value
    assert completed["lease_owner"] is None
    assert (await repository.get_operation("op_a"))["state"] == OperationState.SUCCEEDED.value
    assert (await repository.list_operation_attempts("op_a"))[0]["transport_id"] == "transport_a"
    assert [worker["worker_id"] for worker in await repository.list_workers(job_id="job_test")] == ["worker_a"]
    assert [transport["transport_id"] for transport in await repository.list_transports(job_id="job_test")] == [
        "transport_a", "transport_z"
    ]

    retry = await repository.lease_operation("worker_a", job_id="job_test", now="2026-07-11T10:01:02Z")
    assert retry is not None
    failed = await repository.fail_operation(
        retry["operation_id"],
        "worker_a",
        "temporary network failure",
        failure_kind="timeout",
        retry_at="2026-07-11T10:05:00Z",
        now="2026-07-11T10:01:03Z",
    )
    assert failed["state"] == OperationState.RETRY_WAIT.value
    assert failed["not_before"] == "2026-07-11T10:05:00Z"
    assert [operation["operation_id"] for operation in await repository.list_operations(
        "job_test", state=OperationState.RETRY_WAIT
    )] == ["op_b"]
    terminal = await repository.lease_operation("worker_a", job_id="job_test", now="2026-07-11T10:01:04Z")
    assert terminal is not None
    assert terminal["operation_id"] == "op_c"
    terminal_failure = await repository.fail_operation(
        "op_c",
        "worker_a",
        "invalid map response",
        failure_kind="contract_violation",
        now="2026-07-11T10:01:05Z",
    )
    assert terminal_failure["state"] == OperationState.FAILED.value
    assert terminal_failure["completed_at"] == "2026-07-11T10:01:05Z"

    command = await repository.enqueue_worker_command(
        WorkerCommandKind.DRAIN,
        job_id="job_test",
        worker_id="worker_a",
        command_id="command_a",
        payload={"reason": "maintenance"},
        now="2026-07-11T10:02:00Z",
    )
    acknowledged = await repository.update_worker_command(
        command["command_id"],
        CommandState.ACKNOWLEDGED,
        now="2026-07-11T10:02:01Z",
    )
    command_b = await repository.enqueue_command(
        WorkerCommandKind.RESTART,
        job_id="job_test",
        worker_id="worker_a",
        command_id="command_b",
        now="2026-07-11T10:02:02Z",
    )
    completed_command = await repository.update_worker_command(
        command_b["command_id"],
        CommandState.COMPLETED,
        now="2026-07-11T10:02:03Z",
    )

    assert acknowledged["acknowledged_at"] == "2026-07-11T10:02:01Z"
    assert completed_command["acknowledged_at"] == "2026-07-11T10:02:03Z"
    assert completed_command["completed_at"] == "2026-07-11T10:02:03Z"
    command_page = await repository.list_worker_commands(job_id="job_test", limit=1)
    assert [command["command_id"] for command in await repository.list_worker_commands(
        job_id="job_test",
        cursor=command_page[-1]["command_id"],
    )] == ["command_b"]
    assert [command["command_id"] for command in await repository.list_worker_commands(
        state=CommandState.COMPLETED
    )] == ["command_b"]
    assert await repository.get_worker_command("command_a") == acknowledged


@pytest.mark.asyncio
async def test_listing_and_checkpoint_operational_views_are_paginated_and_serializable(repository: MarketJobRepository):
    await create_job(repository)
    await repository.create_shard(
        ShardSpec(shard_id="shard_1", job_id="job_test", source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_fetch",
            job_id="job_test",
            shard_id="shard_1",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation("worker_a", now="2026-07-11T10:00:00Z")
    assert leased is not None
    commit = await repository.commit_accepted_batch(
        job_id="job_test",
        shard_id="shard_1",
        operation_id="op_fetch",
        attempt_id=leased["attempt_id"],
        idempotency_key="listing-view-batch",
        source="web_catalog",
        listings=[{"id": 101, "title": "First"}, {"id": 102, "title": "Second"}],
        now="2026-07-11T10:00:01Z",
    )
    manual_checkpoint = await repository.create_checkpoint(
        "job_test",
        frontier={"manual": True},
        metrics={"requests": 1},
        now="2026-07-11T10:00:02Z",
    )

    listings_page = await repository.list_listings("job_test", limit=1)
    listings_next = await repository.list_listings(
        "job_test",
        cursor=listings_page[-1]["listing_id"],
        shard_id="shard_1",
        limit=1,
    )
    assert [listing["listing_key"] for listing in listings_page] == ["101"]
    assert [listing["listing_key"] for listing in listings_next] == ["102"]
    assert await repository.get_listing("job_test", listings_page[0]["listing_id"]) == listings_page[0]

    checkpoints_page = await repository.list_checkpoints("job_test", limit=1)
    checkpoints_next = await repository.list_checkpoints(
        "job_test",
        cursor=checkpoints_page[-1]["checkpoint_id"],
        limit=1,
    )
    assert checkpoints_page[0]["checkpoint_id"] == manual_checkpoint["checkpoint_id"]
    assert checkpoints_next[0]["checkpoint_id"] == commit["checkpoint_id"]
    assert await repository.get_checkpoint(commit["checkpoint_id"]) == checkpoints_next[0]
    serializable = [listings_page[0], manual_checkpoint, checkpoints_next[0]]
    assert json.loads(json.dumps(serializable)) == serializable


@pytest.mark.asyncio
async def test_nonlatest_checkpoint_preserves_the_previous_result_pointer(repository: MarketJobRepository):
    await create_job(repository)
    result_checkpoint = await repository.create_checkpoint(
        "job_test",
        frontier={"result": True},
        metrics={"analysis": {"selected_count": 2}},
        now="2026-07-11T10:00:00Z",
    )
    mapping_checkpoint = await repository.create_checkpoint(
        "job_test",
        frontier={"mapping": True},
        metrics={"mapping": {"attribute_count": 3}},
        set_latest=False,
        now="2026-07-11T10:00:01Z",
    )

    job = await repository.get_job("job_test")
    checkpoints = await repository.list_checkpoints("job_test", limit=2)
    assert job is not None and job["latest_checkpoint_id"] == result_checkpoint["checkpoint_id"]
    assert [checkpoint["checkpoint_id"] for checkpoint in checkpoints] == [
        mapping_checkpoint["checkpoint_id"],
        result_checkpoint["checkpoint_id"],
    ]
