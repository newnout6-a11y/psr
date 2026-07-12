from __future__ import annotations

import pytest

from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.events import MarketEventHub
from src.platforms.kwork_supply.models import (
    JobPhase,
    JobState,
    MarketJobCreate,
    MarketScope,
    Operation,
    OperationKind,
    OperationState,
    ShardSpec,
    WorkerCommandKind,
)
from src.platforms.kwork_supply.repository import MarketJobRepository


@pytest.fixture
def coordinator(tmp_path) -> MarketScanCoordinator:
    return MarketScanCoordinator(MarketJobRepository(tmp_path / "market-jobs.sqlite3"), MarketEventHub())


def make_job() -> MarketJobCreate:
    return MarketJobCreate(
        scope=MarketScope(category_id=38, category_name="Website work", canonical_alias="website-repair"),
        target_unique_cards=60,
        desired_workers=2,
    )


@pytest.mark.asyncio
async def test_create_job_is_durable_fast_and_queues_one_mapping_operation(coordinator: MarketScanCoordinator):
    created = await coordinator.create_job(make_job(), job_id="job_coord")

    assert created["job"]["state"] == JobState.PREPARING.value
    assert created["status_url"].endswith("/job_coord")
    assert created["events_url"].endswith("/job_coord")
    assert created["initial_operation"]["kind"] == OperationKind.MAP_SCOPE.value
    assert created["initial_operation"]["state"] == "queued"

    operation, enqueued = await coordinator.ensure_mapping_operation("job_coord")
    assert enqueued is False
    assert operation["operation_id"] == created["initial_operation"]["operation_id"]

    events = await coordinator.replay_events("job_coord")
    assert [event["type"] for event in events] == ["job.snapshot", "operation.queued"]
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["schema_version"] == 1

    snapshot = await coordinator.snapshot("job_coord")
    assert snapshot["job_id"] == "job_coord"
    assert snapshot["operations"] == [created["initial_operation"]]


@pytest.mark.asyncio
async def test_pause_resume_stop_and_worker_commands_are_durable(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_control")
    mapping = await coordinator.start_mapping("job_control")
    assert mapping["state"] == JobState.MAPPING.value
    assert mapping["phase"] == JobPhase.MAP.value

    pausing = await coordinator.pause_job("job_control")
    assert pausing["state"] == JobState.PAUSING.value
    commands = await coordinator.repository.list_worker_commands(job_id="job_control")
    assert commands[0]["command_type"] == WorkerCommandKind.DRAIN.value

    paused = await coordinator.mark_paused("job_control")
    assert paused["state"] == JobState.PAUSED.value
    resumed = await coordinator.resume_job("job_control")
    assert resumed["state"] == JobState.MAPPING.value

    updated = await coordinator.update_job("job_control", target_unique_cards=120, desired_workers=3)
    assert updated["target_unique_cards"] == 120
    assert updated["desired_workers"] == 3

    stopped = await coordinator.stop_job("job_control", force=True)
    assert stopped["state"] == JobState.STOPPING.value
    commands = await coordinator.repository.list_worker_commands(job_id="job_control")
    assert {command["command_type"] for command in commands} == {
        WorkerCommandKind.DRAIN.value,
        WorkerCommandKind.DISABLE.value,
    }
    assert (await coordinator.mark_stopped("job_control"))["state"] == JobState.STOPPED.value


@pytest.mark.asyncio
async def test_live_hub_receives_only_persisted_event_envelopes(coordinator: MarketScanCoordinator):
    queue = coordinator.event_hub.subscribe("job_events")
    await coordinator.create_job(make_job(), job_id="job_events")

    first = queue.get_nowait()
    second = queue.get_nowait()
    assert [first["type"], second["type"]] == ["job.snapshot", "operation.queued"]
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert (await coordinator.repository.replay_events("job_events"))[0]["event_type"] == first["type"]


@pytest.mark.asyncio
async def test_collection_drain_queues_one_durable_analysis_operation(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_analysis")
    mapping = await coordinator.repository.lease_operation("worker_analysis", job_id="job_analysis")
    assert mapping is not None
    await coordinator.repository.complete_operation(mapping["operation_id"], "worker_analysis")
    prepared = await coordinator.repository.get_job("job_analysis")
    assert prepared is not None
    await coordinator.repository.update_job_state(
        "job_analysis",
        JobState.RUNNING,
        phase=JobPhase.COLLECT,
        expected_revision=prepared["revision"],
    )

    analysis = await coordinator.ensure_analysis_operation("job_analysis")
    repeated = await coordinator.ensure_analysis_operation("job_analysis")

    assert analysis is not None
    assert analysis["kind"] == OperationKind.ANALYZE_SNAPSHOT.value
    assert repeated is not None and repeated["operation_id"] == analysis["operation_id"]
    job = await coordinator.repository.get_job("job_analysis")
    assert job is not None and job["state"] == JobState.ANALYZING.value


@pytest.mark.asyncio
async def test_target_unreached_collection_queues_a_partition_mapping_wave(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_partition_wave")
    mapping = await coordinator.repository.lease_operation("worker_partition", job_id="job_partition_wave")
    assert mapping is not None
    await coordinator.repository.complete_operation(mapping["operation_id"], "worker_partition")
    prepared = await coordinator.repository.get_job("job_partition_wave")
    assert prepared is not None
    await coordinator.repository.update_job_state(
        "job_partition_wave",
        JobState.RUNNING,
        phase=JobPhase.COLLECT,
        expected_revision=prepared["revision"],
        counters={"unique_cards": 2},
    )

    operation = await coordinator.ensure_partition_mapping("job_partition_wave")
    repeated = await coordinator.ensure_partition_mapping("job_partition_wave")

    assert operation is not None
    assert operation["kind"] == OperationKind.MAP_SCOPE.value
    assert operation["payload"] == {"partition_mapping": True}
    assert repeated is None
    job = await coordinator.repository.get_job("job_partition_wave")
    assert job is not None
    assert job["state"] == JobState.MAPPING.value
    assert job["phase"] == JobPhase.MAP.value


@pytest.mark.asyncio
async def test_time_budget_finishes_partial_collection_without_leasing_more_work(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_budget")
    prepared = await coordinator.repository.get_job("job_budget")
    assert prepared is not None
    await coordinator.repository.update_job_state(
        "job_budget",
        JobState.RUNNING,
        phase=JobPhase.COLLECT,
        expected_revision=prepared["revision"],
    )

    finished = await coordinator.finish_for_time_budget("job_budget")

    operations = await coordinator.repository.list_operations("job_budget")
    events = await coordinator.replay_events("job_budget")
    assert finished["state"] == JobState.ANALYZING.value
    assert finished["last_warning"] == "time_budget_exhausted"
    assert {operation["kind"]: operation["state"] for operation in operations} == {
        OperationKind.MAP_SCOPE.value: OperationState.CANCELLED.value,
        OperationKind.ANALYZE_SNAPSHOT.value: OperationState.QUEUED.value,
    }
    assert any(event["type"] == "warning" and event["payload"].get("code") == "time_budget_exhausted" for event in events)


@pytest.mark.asyncio
async def test_completed_job_with_larger_target_resumes_from_validated_web_cursor(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_deepen")
    await coordinator.repository.create_shard(
        ShardSpec(
            shard_id="shard_deepen",
            job_id="job_deepen",
            source="web_catalog",
            alias="website-repair",
            cursor={"kind": "exclude_ids", "exclude_ids": ["101", "102"], "page": None, "token": None},
        )
    )
    initial = await coordinator.repository.get_job("job_deepen")
    assert initial is not None
    completed = await coordinator.repository.update_job_state(
        "job_deepen",
        JobState.COMPLETED,
        phase=JobPhase.EXPORT,
        expected_revision=initial["revision"],
        counters={"unique_cards": 2},
    )
    reconfigured = await coordinator.repository.update_job_configuration(
        "job_deepen",
        target_unique_cards=4,
        expected_revision=completed["revision"],
    )

    resumed = await coordinator.resume_job("job_deepen", expected_revision=reconfigured["revision"])

    operations = await coordinator.repository.list_operations("job_deepen")
    resumed_fetches = [
        operation
        for operation in operations
        if operation["kind"] == OperationKind.FETCH_BATCH.value and operation["payload"].get("resumed") is True
    ]
    assert resumed["state"] == JobState.RUNNING.value
    assert resumed["phase"] == JobPhase.COLLECT.value
    assert len(resumed_fetches) == 1
    assert resumed_fetches[0]["payload"] == {
        "cursor": {"kind": "exclude_ids", "exclude_ids": ["101", "102"], "page": None, "token": None},
        "remaining_requests": 2,
        "resumed": True,
    }


@pytest.mark.asyncio
async def test_completed_job_without_a_cursor_requeues_mapping_for_new_partitions(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_remap")
    initial = await coordinator.repository.get_job("job_remap")
    assert initial is not None
    completed = await coordinator.repository.update_job_state(
        "job_remap",
        JobState.COMPLETED,
        phase=JobPhase.EXPORT,
        expected_revision=initial["revision"],
        counters={"unique_cards": 2},
    )
    reconfigured = await coordinator.repository.update_job_configuration(
        "job_remap",
        target_unique_cards=120,
        expected_revision=completed["revision"],
    )

    resumed = await coordinator.resume_job("job_remap", expected_revision=reconfigured["revision"])

    operations = await coordinator.repository.list_operations("job_remap")
    remaps = [
        operation
        for operation in operations
        if operation["kind"] == OperationKind.MAP_SCOPE.value
        and operation["payload"].get("resume_from_completed") is True
    ]
    assert resumed["state"] == JobState.MAPPING.value
    assert resumed["phase"] == JobPhase.MAP.value
    assert len(remaps) == 1
    assert remaps[0]["state"] == OperationState.QUEUED.value


@pytest.mark.asyncio
async def test_collection_after_resume_runs_enrichment_before_a_new_analysis(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_reanalyze")
    mapping = await coordinator.repository.lease_operation("worker_reanalyze", job_id="job_reanalyze")
    assert mapping is not None
    await coordinator.repository.complete_operation(mapping["operation_id"], "worker_reanalyze")
    initial = await coordinator.repository.get_job("job_reanalyze")
    assert initial is not None
    running = await coordinator.repository.update_job_state(
        "job_reanalyze",
        JobState.RUNNING,
        phase=JobPhase.COLLECT,
        expected_revision=initial["revision"],
    )
    historical = await coordinator.repository.enqueue_operation(
        Operation(
            operation_id="op_historical_analysis",
            job_id="job_reanalyze",
            shard_id=None,
            kind=OperationKind.ANALYZE_SNAPSHOT,
            state=OperationState.SUCCEEDED,
            idempotency_key="analyze:historical",
        )
    )

    queued = await coordinator.ensure_analysis_operation("job_reanalyze")

    assert queued is not None
    assert queued["operation_id"] != historical["operation_id"]
    assert queued["state"] == OperationState.QUEUED.value
    assert queued["idempotency_key"] == f"analyze:job_reanalyze:{running['revision'] + 2}"
    job = await coordinator.repository.get_job("job_reanalyze")
    assert job is not None
    assert job["state"] == JobState.ANALYZING.value
    assert job["phase"] == JobPhase.ANALYZE.value


@pytest.mark.asyncio
async def test_resume_after_pause_requeues_a_missing_collection_continuation(coordinator: MarketScanCoordinator):
    await coordinator.create_job(make_job(), job_id="job_pause_resume_continuation")
    mapping = await coordinator.repository.lease_operation("worker_resume", job_id="job_pause_resume_continuation")
    assert mapping is not None
    await coordinator.repository.complete_operation(mapping["operation_id"], "worker_resume")
    initial = await coordinator.repository.get_job("job_pause_resume_continuation")
    assert initial is not None
    running = await coordinator.repository.update_job_state(
        "job_pause_resume_continuation",
        JobState.RUNNING,
        phase=JobPhase.COLLECT,
        expected_revision=initial["revision"],
    )
    await coordinator.repository.create_shard(
        ShardSpec(
            shard_id="shard_pause_resume",
            job_id="job_pause_resume_continuation",
            source="web_catalog",
            alias="website-repair",
            cursor={"kind": "exclude_ids", "exclude_ids": []},
        )
    )
    await coordinator.repository.enqueue_operation(
        Operation(
            operation_id="op_pause_last_batch",
            job_id="job_pause_resume_continuation",
            shard_id="shard_pause_resume",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await coordinator.repository.lease_operation("worker_resume", job_id="job_pause_resume_continuation")
    assert leased is not None
    pausing = await coordinator.pause_job("job_pause_resume_continuation", expected_revision=running["revision"])
    await coordinator.repository.commit_accepted_batch(
        job_id="job_pause_resume_continuation",
        shard_id="shard_pause_resume",
        operation_id=leased["operation_id"],
        attempt_id=leased["attempt_id"],
        idempotency_key="pause-final-batch",
        source="web_catalog",
        listings=[{"id": 101, "title": "Saved during pause"}],
        next_cursor={"kind": "exclude_ids", "exclude_ids": ["101"]},
    )
    paused = await coordinator.mark_paused("job_pause_resume_continuation")

    resumed = await coordinator.resume_job(
        "job_pause_resume_continuation",
        expected_revision=paused["revision"],
    )

    operations = await coordinator.repository.list_operations("job_pause_resume_continuation")
    resumed_fetches = [
        operation
        for operation in operations
        if operation["kind"] == OperationKind.FETCH_BATCH.value and operation["payload"].get("resumed") is True
    ]
    assert pausing["state"] == JobState.PAUSING.value
    assert resumed["state"] == JobState.RUNNING.value
    assert resumed["phase"] == JobPhase.COLLECT.value
    assert len(resumed_fetches) == 1
    assert resumed_fetches[0]["payload"]["cursor"] == {"kind": "exclude_ids", "exclude_ids": ["101"]}
