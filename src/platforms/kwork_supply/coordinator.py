"""Durable control-plane coordination for Kwork market jobs.

The coordinator is intentionally thin: SQLite remains the source of truth,
while :class:`MarketEventHub` only fans out events that have already been
committed.  Worker and API layers use this class rather than mutating the
repository directly so state changes are visible to reconnecting clients.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from .events import MarketEventHub
from .models import (
    JobPhase,
    JobState,
    MarketJobCreate,
    Operation,
    OperationKind,
    OperationState,
    SourcePolicy,
    WorkerCommandKind,
)
from .repository import MarketJobNotFoundError, MarketJobRepository


JsonDict = dict[str, Any]


_TERMINAL_STATES = {JobState.COMPLETED.value, JobState.STOPPED.value, JobState.FAILED.value}


class MarketScanCoordinator:
    """Create, control, and expose durable market-collection jobs."""

    def __init__(self, repository: MarketJobRepository, event_hub: MarketEventHub | None = None) -> None:
        self.repository = repository
        self.event_hub = event_hub or MarketEventHub()

    async def initialize(self) -> None:
        """Initialize durable storage before accepting market-job commands."""

        await self.repository.initialize()

    async def create_job(self, create: MarketJobCreate, *, job_id: str | None = None) -> JsonDict:
        """Persist a job and its idempotent scope-mapping operation.

        This does not perform network work.  It is therefore suitable for the
        API's fast ``202 Accepted`` response and crash recovery can safely call
        :meth:`ensure_mapping_operation` again later.
        """

        job = await self.repository.create_job(create, job_id=job_id)
        operation, enqueued = await self.ensure_mapping_operation(job["job_id"])
        snapshot = await self.snapshot(job["job_id"])
        await self.emit(
            job["job_id"],
            "job.snapshot",
            {"snapshot": snapshot},
            revision=job["revision"],
        )
        if enqueued:
            await self.emit(
                job["job_id"],
                "operation.queued",
                {
                    "operation_id": operation["operation_id"],
                    "kind": operation["kind"],
                    "phase": JobPhase.MAP.value,
                },
                revision=job["revision"],
                operation_id=operation["operation_id"],
            )
        return {
            "job": job,
            "initial_operation": operation,
            "status_url": f"/api/kwork/market/jobs/{job['job_id']}",
            "events_url": f"/ws/kwork-market/jobs/{job['job_id']}",
        }

    async def ensure_mapping_operation(self, job_id: str) -> tuple[JsonDict, bool]:
        """Ensure a new job always has one durable ``map_scope`` operation."""

        job = await self._require_job(job_id)
        existing = await self._list_all_operations(job_id)
        for operation in existing:
            if operation["kind"] == OperationKind.MAP_SCOPE.value:
                return operation, False

        operation = await self.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{uuid4().hex}",
                job_id=job_id,
                shard_id=None,
                kind=OperationKind.MAP_SCOPE,
                state=OperationState.QUEUED,
                priority=10_000,
                idempotency_key=f"map_scope:{job_id}",
                payload={"scope": job["scope"], "profile": job["profile"]},
            )
        )
        return operation, True

    async def ensure_partition_mapping(self, job_id: str) -> JsonDict | None:
        """Queue another mapping wave when an uncapped target outlives known shards."""

        job = await self._require_job(job_id)
        if (
            job["state"] != JobState.RUNNING.value
            or job["phase"] != JobPhase.COLLECT.value
            or job["source_policy"] != SourcePolicy.VALIDATED_ONLY.value
        ):
            return None
        active = await self.repository.list_operations(
            job_id,
            state=(OperationState.QUEUED, OperationState.LEASED, OperationState.RUNNING, OperationState.RETRY_WAIT),
            limit=1,
        )
        if active:
            return None
        remapping = await self.repository.update_job_state(
            job_id,
            JobState.MAPPING,
            phase=JobPhase.MAP,
            expected_revision=job["revision"],
        )
        await self.emit_state_changed(remapping, {"reason": "target_unreached_partition_mapping"})
        operation = await self.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{uuid4().hex}",
                job_id=job_id,
                shard_id=None,
                kind=OperationKind.MAP_SCOPE,
                state=OperationState.QUEUED,
                priority=1_000,
                idempotency_key=f"partition-map:{job_id}:{remapping['revision']}",
                payload={"partition_mapping": True},
            )
        )
        await self.emit(
            job_id,
            "operation.queued",
            {
                "operation_id": operation["operation_id"],
                "kind": operation["kind"],
                "partition_mapping": True,
            },
            revision=remapping["revision"],
            operation_id=operation["operation_id"],
        )
        return operation

    async def start_mapping(self, job_id: str) -> JsonDict:
        """Move a prepared job into mapping once a worker preflight begins."""

        job = await self._require_job(job_id)
        if job["state"] in _TERMINAL_STATES:
            raise ValueError(f"cannot start mapping for terminal job {job_id!r}")
        updated = await self.repository.update_job_state(
            job_id,
            JobState.MAPPING,
            phase=JobPhase.MAP,
            expected_revision=job["revision"],
        )
        await self.emit_state_changed(updated)
        return updated

    async def pause_job(self, job_id: str, *, expected_revision: int | None = None) -> JsonDict:
        """Request a graceful pause and ask active workers to drain."""

        job = await self._require_job(job_id)
        if job["state"] in _TERMINAL_STATES:
            raise ValueError(f"cannot pause terminal job {job_id!r}")
        updated = await self.repository.update_job_state(
            job_id,
            JobState.PAUSING,
            phase=job["phase"],
            expected_revision=expected_revision if expected_revision is not None else job["revision"],
        )
        await self.emit_state_changed(updated)
        command = await self.repository.enqueue_worker_command(WorkerCommandKind.DRAIN, job_id=job_id)
        await self._emit_command(command, updated["revision"])
        return updated

    async def mark_paused(self, job_id: str) -> JsonDict:
        """Finalize a graceful pause after the supervisor observes drained workers."""

        job = await self._require_job(job_id)
        if job["state"] != JobState.PAUSING.value:
            return job
        updated = await self.repository.update_job_state(
            job_id,
            JobState.PAUSED,
            phase=job["phase"],
            expected_revision=job["revision"],
        )
        await self.emit_state_changed(updated)
        return updated

    async def resume_job(self, job_id: str, *, expected_revision: int | None = None) -> JsonDict:
        """Resume the same job rather than starting collection from scratch."""

        job = await self._require_job(job_id)
        if job["state"] == JobState.COMPLETED.value and job["phase"] == JobPhase.EXPORT.value:
            return await self._resume_completed_collection(job, expected_revision=expected_revision)
        phase = str(job["phase"])
        if phase == JobPhase.COLLECT.value:
            return await self._resume_interrupted_collection(job, expected_revision=expected_revision)
        resumed_state = JobState.MAPPING if phase in {JobPhase.PREPARE.value, JobPhase.MAP.value, JobPhase.PLAN.value} else JobState.RUNNING
        updated = await self.repository.update_job_state(
            job_id,
            resumed_state,
            phase=phase,
            expected_revision=expected_revision if expected_revision is not None else job["revision"],
        )
        await self.emit_state_changed(updated)
        if phase == JobPhase.ENRICH.value:
            await self.ensure_enrichment_operations(job_id)
            refreshed = await self._require_job(job_id)
            return refreshed
        return updated

    async def _resume_interrupted_collection(
        self,
        job: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> JsonDict:
        """Restore work that completed while a graceful pause was draining."""

        job_id = str(job["job_id"])
        active = await self.repository.list_operations(
            job_id,
            state=(OperationState.QUEUED, OperationState.LEASED, OperationState.RUNNING, OperationState.RETRY_WAIT),
            limit=1,
        )
        if active:
            resumed = await self.repository.update_job_state(
                job_id,
                JobState.RUNNING,
                phase=JobPhase.COLLECT,
                expected_revision=expected_revision if expected_revision is not None else int(job["revision"]),
            )
            await self.emit_state_changed(resumed)
            return resumed

        counters = job.get("counters") or {}
        unique_cards = int(counters.get("unique_cards", counters.get("unique_listings", 0)) or 0)
        target_reached = unique_cards >= int(job["target_unique_cards"])
        if not target_reached and job.get("request_budget") is None:
            # Reuse durable shard cursors whenever possible; when none are
            # available the helper queues another validated mapping wave.
            return await self._resume_completed_collection(job, expected_revision=expected_revision)

        resumed = await self.repository.update_job_state(
            job_id,
            JobState.RUNNING,
            phase=JobPhase.COLLECT,
            expected_revision=expected_revision if expected_revision is not None else int(job["revision"]),
        )
        await self.emit_state_changed(resumed)
        await self.ensure_analysis_operation(job_id)
        return await self._require_job(job_id)

    async def _resume_completed_collection(
        self,
        job: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> JsonDict:
        job_id = str(job["job_id"])
        counters = job.get("counters") or {}
        unique_cards = int(counters.get("unique_cards", counters.get("unique_listings", 0)) or 0)
        if unique_cards >= int(job["target_unique_cards"]):
            return dict(job)
        shards = await self.repository.list_shards(job_id, limit=500)
        eligible = [
            shard
            for shard in shards
            if shard["source"] == "web_catalog" and isinstance(shard.get("cursor"), Mapping)
        ]
        if not eligible:
            return await self._resume_with_partition_mapping(job, expected_revision=expected_revision)
        resumed = await self.repository.update_job_state(
            job_id,
            JobState.RUNNING,
            phase=JobPhase.COLLECT,
            expected_revision=expected_revision if expected_revision is not None else int(job["revision"]),
        )
        await self.emit_state_changed(resumed)
        budget = int(resumed.get("request_budget") or max(int(resumed["target_unique_cards"]) - unique_cards, 1))
        budgets = self._split_resume_budget(budget, len(eligible))
        for shard, shard_budget in zip(eligible, budgets, strict=True):
            operation = await self.repository.enqueue_operation(
                Operation(
                    operation_id=f"op_{uuid4().hex}",
                    job_id=job_id,
                    shard_id=str(shard["shard_id"]),
                    kind=OperationKind.FETCH_BATCH,
                    state=OperationState.QUEUED,
                    priority=int(shard["priority"]),
                    idempotency_key=f"resume:{job_id}:{resumed['revision']}:{shard['shard_id']}",
                    payload={"cursor": dict(shard["cursor"]), "remaining_requests": shard_budget, "resumed": True},
                )
            )
            await self.emit(
                job_id,
                "operation.queued",
                {"operation_id": operation["operation_id"], "kind": operation["kind"], "resumed": True},
                revision=resumed["revision"],
                operation_id=operation["operation_id"],
            )
        return resumed

    async def _resume_with_partition_mapping(
        self,
        job: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> JsonDict:
        """Re-map a completed web job when no cursor can deepen it further."""

        job_id = str(job["job_id"])
        scope = job.get("scope") if isinstance(job.get("scope"), Mapping) else {}
        if (
            job.get("source_policy") != SourcePolicy.VALIDATED_ONLY.value
            or not isinstance(scope.get("canonical_alias"), str)
            or not scope["canonical_alias"].strip()
        ):
            completed = await self.repository.update_job_state(
                job_id,
                JobState.COMPLETED,
                phase=JobPhase.EXPORT,
                expected_revision=expected_revision if expected_revision is not None else int(job["revision"]),
                last_warning="resume_no_validated_continuation_cursor",
            )
            await self.emit_state_changed(completed, {"reason": "resume_no_validated_continuation_cursor"})
            return completed

        remapping = await self.repository.update_job_state(
            job_id,
            JobState.MAPPING,
            phase=JobPhase.MAP,
            expected_revision=expected_revision if expected_revision is not None else int(job["revision"]),
        )
        await self.emit_state_changed(remapping, {"reason": "resume_partition_mapping"})
        operation = await self.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{uuid4().hex}",
                job_id=job_id,
                shard_id=None,
                kind=OperationKind.MAP_SCOPE,
                state=OperationState.QUEUED,
                priority=1_000,
                idempotency_key=f"resume-map:{job_id}:{remapping['revision']}",
                payload={"resume_from_completed": True},
            )
        )
        await self.emit(
            job_id,
            "operation.queued",
            {
                "operation_id": operation["operation_id"],
                "kind": operation["kind"],
                "resumed": True,
            },
            revision=remapping["revision"],
            operation_id=operation["operation_id"],
        )
        return remapping

    @staticmethod
    def _split_resume_budget(total: int, shard_count: int) -> tuple[int, ...]:
        if shard_count <= 0:
            return ()
        bounded_total = min(max(total, shard_count), 10_000)
        base, remainder = divmod(bounded_total, shard_count)
        return tuple(base + (1 if index < remainder else 0) for index in range(shard_count))

    async def stop_job(
        self,
        job_id: str,
        *,
        force: bool = False,
        expected_revision: int | None = None,
    ) -> JsonDict:
        """Request a graceful or forceful stop without deleting durable evidence."""

        job = await self._require_job(job_id)
        if job["state"] in _TERMINAL_STATES:
            return job
        updated = await self.repository.update_job_state(
            job_id,
            JobState.STOPPING,
            phase=job["phase"],
            expected_revision=expected_revision if expected_revision is not None else job["revision"],
        )
        await self.emit_state_changed(updated, {"force": force})
        command_kind = WorkerCommandKind.DISABLE if force else WorkerCommandKind.DRAIN
        command = await self.repository.enqueue_worker_command(
            command_kind,
            job_id=job_id,
            payload={"force": force},
        )
        await self._emit_command(command, updated["revision"])
        return updated

    async def mark_stopped(self, job_id: str) -> JsonDict:
        """Finalize stop once no worker owns an operation for the job."""

        job = await self._require_job(job_id)
        if job["state"] != JobState.STOPPING.value:
            return job
        updated = await self.repository.update_job_state(
            job_id,
            JobState.STOPPED,
            phase=job["phase"],
            expected_revision=job["revision"],
        )
        await self.emit_state_changed(updated)
        return updated

    async def update_job(
        self,
        job_id: str,
        *,
        desired_workers: int | None = None,
        target_unique_cards: int | None = None,
        profile: str | None = None,
        request_budget: int | None | object = None,
        time_budget_seconds: int | None | object = None,
        clear_request_budget: bool = False,
        clear_time_budget: bool = False,
        expected_revision: int | None = None,
    ) -> JsonDict:
        """Change mutable controls and emit one revisioned configuration event."""

        job = await self._require_job(job_id)
        kwargs: JsonDict = {
            "desired_workers": desired_workers,
            "target_unique_cards": target_unique_cards,
            "profile": profile,
            "expected_revision": expected_revision if expected_revision is not None else job["revision"],
        }
        # Repository distinguishes an omitted budget from an explicit null.
        if request_budget is not None or clear_request_budget:
            kwargs["request_budget"] = None if clear_request_budget else request_budget
        if time_budget_seconds is not None or clear_time_budget:
            kwargs["time_budget_seconds"] = None if clear_time_budget else time_budget_seconds
        updated = await self.repository.update_job_configuration(job_id, **kwargs)
        await self.emit(
            job_id,
            "job.configuration_changed",
            {
                "desired_workers": updated["desired_workers"],
                "target_unique_cards": updated["target_unique_cards"],
                "profile": updated["profile"],
                "request_budget": updated["request_budget"],
                "time_budget_seconds": updated["time_budget_seconds"],
            },
            revision=updated["revision"],
        )
        return updated

    async def set_worker_pool(
        self,
        job_id: str,
        desired_workers: int,
        *,
        expected_revision: int | None = None,
    ) -> JsonDict:
        """Update desired actor count independently of execution profile."""

        if desired_workers < 0 or desired_workers > 10:
            raise ValueError("desired_workers must be between 0 and 10")
        return await self.update_job(
            job_id,
            desired_workers=desired_workers,
            expected_revision=expected_revision,
        )

    async def retry_operation(self, job_id: str, operation_id: str) -> JsonDict:
        """Create a fresh durable attempt of a failed or blocked operation."""

        operation = await self.repository.get_operation(operation_id)
        if operation is None or operation["job_id"] != job_id:
            raise MarketJobNotFoundError(f"operation {operation_id!r} was not found for job {job_id!r}")
        retry = await self.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{uuid4().hex}",
                job_id=job_id,
                shard_id=operation["shard_id"],
                kind=OperationKind(operation["kind"]),
                state=OperationState.QUEUED,
                priority=int(operation["priority"]),
                idempotency_key=f"retry:{operation_id}:{uuid4().hex}",
                payload={**operation["payload"], "retry_of": operation_id},
            )
        )
        job = await self._require_job(job_id)
        await self.emit(
            job_id,
            "operation.queued",
            {"operation_id": retry["operation_id"], "kind": retry["kind"], "retry_of": operation_id},
            revision=job["revision"],
            operation_id=retry["operation_id"],
        )
        return retry

    async def finish_for_time_budget(self, job_id: str) -> JsonDict:
        """Stop new collection leases and finalize the durable partial snapshot."""

        job = await self._require_job(job_id)
        if job["state"] in _TERMINAL_STATES | {JobState.BLOCKED.value, JobState.STOPPING.value, JobState.STOPPED.value}:
            return job
        if job["state"] not in {
            JobState.MAPPING.value,
            JobState.PLANNING.value,
            JobState.RUNNING.value,
            JobState.COMPLETING.value,
        }:
            return job
        if job["state"] != JobState.COMPLETING.value:
            job = await self.repository.update_job_state(
                job_id,
                JobState.COMPLETING,
                phase=JobPhase.COLLECT,
                expected_revision=job["revision"],
                last_warning="time_budget_exhausted",
            )
            await self.emit_state_changed(job, {"reason": "time_budget_exhausted"})
        cancelled = await self.repository.cancel_collection_operations(
            job_id,
            reason="time_budget_exhausted",
        )
        if cancelled:
            await self.emit(
                job_id,
                "warning",
                {"code": "time_budget_exhausted", "cancelled_operations": cancelled},
                revision=job["revision"],
            )
        await self.ensure_analysis_operation(job_id)
        return await self._require_job(job_id)

    async def ensure_enrichment_operations(self, job_id: str) -> JsonDict | None:
        """Price-gate and queue durable local enrichment as collection progresses.

        Re-entering this method is intentional: the repository keeps pending
        operations and cache generations stable, so collection workers can
        enqueue enrichment immediately after a committed batch without causing
        duplicate detail requests. The job switches to the enrichment phase
        only after collection operations have drained.
        """

        job = await self._require_job(job_id)
        phase = str(job["phase"])
        active_states = (
            OperationState.QUEUED,
            OperationState.LEASED,
            OperationState.RUNNING,
            OperationState.RETRY_WAIT,
        )
        if phase == JobPhase.COLLECT.value:
            if job["state"] not in {JobState.RUNNING.value, JobState.COMPLETING.value}:
                return None
        elif phase == JobPhase.ENRICH.value:
            if job["state"] not in {
                JobState.ENRICHING.value,
                JobState.RUNNING.value,
                JobState.COMPLETING.value,
            }:
                return None
        else:
            return None

        summary = await self.repository.prepare_listing_enrichment(job_id)
        active = await self.repository.list_operations(job_id, state=active_states, limit=1_000)
        collection_kinds = {
            OperationKind.MAP_SCOPE.value,
            OperationKind.RESOLVE_ALIAS.value,
            OperationKind.FETCH_BATCH.value,
        }
        if phase == JobPhase.COLLECT.value and not any(item["kind"] in collection_kinds for item in active):
            transitioned = await self.repository.update_job_state(
                job_id,
                JobState.ENRICHING,
                phase=JobPhase.ENRICH,
                expected_revision=job["revision"],
            )
            await self.emit_state_changed(transitioned)
            job = transitioned
        queued = await self.repository.list_operations(
            job_id,
            state=OperationState.QUEUED,
            limit=1_000,
        )
        enrichment_queued = next(
            (operation for operation in queued if operation["kind"] == OperationKind.ENRICH_LISTING.value),
            None,
        )
        if summary["queued"]:
            await self.emit(
                job_id,
                "enrichment.queued",
                summary,
                revision=job["revision"],
                operation_id=enrichment_queued["operation_id"] if enrichment_queued else None,
            )
        return enrichment_queued

    async def ensure_analysis_operation(self, job_id: str) -> JsonDict | None:
        """Queue deterministic analysis only after local enrichment is drained."""

        job = await self._require_job(job_id)
        if job["phase"] in {JobPhase.COLLECT.value, JobPhase.ENRICH.value}:
            enrichment = await self.ensure_enrichment_operations(job_id)
            if enrichment is not None:
                return enrichment
            job = await self._require_job(job_id)
            if job["phase"] != JobPhase.ENRICH.value:
                return None
        existing = await self.repository.list_operations(job_id, limit=500)
        for operation in existing:
            if operation["kind"] == OperationKind.ANALYZE_SNAPSHOT.value and operation["state"] in {
                OperationState.QUEUED.value,
                OperationState.LEASED.value,
                OperationState.RUNNING.value,
                OperationState.RETRY_WAIT.value,
            }:
                return operation
        retried_operation_ids = {
            str(payload["retry_of"])
            for item in existing
            if isinstance((payload := item.get("payload")), Mapping)
            and isinstance(payload.get("retry_of"), str)
            and payload["retry_of"].strip()
        }
        terminal_enrichment = [
            operation
            for operation in existing
            if operation["kind"] == OperationKind.ENRICH_LISTING.value
            and operation["operation_id"] not in retried_operation_ids
            and operation["state"]
            in {
                OperationState.FAILED.value,
                OperationState.CONTRACT_VIOLATION.value,
                OperationState.BLOCKED.value,
            }
        ]
        if terminal_enrichment:
            if job["state"] != JobState.BLOCKED.value:
                blocked = await self.repository.update_job_state(
                    job_id,
                    JobState.BLOCKED,
                    phase=JobPhase.ENRICH,
                    expected_revision=job["revision"],
                    last_warning="enrichment_incomplete",
                )
                await self.emit_state_changed(
                    blocked,
                    {
                        "reason": "enrichment_incomplete",
                        "failed_operation_ids": [item["operation_id"] for item in terminal_enrichment[:10]],
                    },
                )
            return None
        if job["state"] not in {
            JobState.ENRICHING.value,
            JobState.RUNNING.value,
            JobState.COMPLETING.value,
        } or job["phase"] != JobPhase.ENRICH.value:
            return None
        active = await self.repository.list_operations(
            job_id,
            state=(OperationState.QUEUED, OperationState.LEASED, OperationState.RUNNING, OperationState.RETRY_WAIT),
            limit=1,
        )
        if active:
            return None
        analyzing = await self.repository.update_job_state(
            job_id,
            JobState.ANALYZING,
            phase=JobPhase.ANALYZE,
            expected_revision=job["revision"],
        )
        await self.emit_state_changed(analyzing)
        operation = await self.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{uuid4().hex}",
                job_id=job_id,
                shard_id=None,
                kind=OperationKind.ANALYZE_SNAPSHOT,
                state=OperationState.QUEUED,
                priority=500,
                idempotency_key=f"analyze:{job_id}:{analyzing['revision']}",
                payload={"checkpoint_id": analyzing.get("latest_checkpoint_id")},
            )
        )
        await self.emit(
            job_id,
            "operation.queued",
            {"operation_id": operation["operation_id"], "kind": operation["kind"]},
            revision=analyzing["revision"],
            operation_id=operation["operation_id"],
        )
        return operation

    async def _list_all_operations(self, job_id: str) -> list[JsonDict]:
        """Read every operation when a completion decision must be global to the job."""

        operations: list[JsonDict] = []
        cursor: str | None = None
        while True:
            page = await self.repository.list_operations(job_id, cursor=cursor, limit=1_000)
            operations.extend(page)
            if len(page) < 1_000:
                return operations
            cursor = str(page[-1]["operation_id"])

    async def command_worker(
        self,
        job_id: str,
        worker_id: str,
        command: WorkerCommandKind | str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        """Persist a worker-control command for the supervisor to acknowledge."""

        await self._require_job(job_id)
        record = await self.repository.enqueue_worker_command(
            command,
            job_id=job_id,
            worker_id=worker_id,
            payload=payload,
        )
        job = await self._require_job(job_id)
        await self._emit_command(record, job["revision"])
        return record

    async def emit(
        self,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        revision: int | None = None,
        worker_id: str | None = None,
        operation_id: str | None = None,
    ) -> JsonDict:
        """Persist an event before publishing its versioned WebSocket envelope."""

        event = await self.repository.append_event(
            job_id,
            event_type,
            payload,
            revision=revision,
            worker_id=worker_id,
            operation_id=operation_id,
        )
        envelope = self.event_envelope(event)
        self.event_hub.publish(envelope)
        return envelope

    async def replay_events(self, job_id: str, *, after_seq: int = 0, limit: int = 500) -> list[JsonDict]:
        """Return durable event envelopes strictly after a client cursor."""

        await self._require_job(job_id)
        events = await self.repository.replay_events(job_id, after_seq=after_seq, limit=limit)
        return [self.event_envelope(event) for event in events]

    async def snapshot(self, job_id: str) -> JsonDict:
        """Build a read model from durable state; nothing lives only in memory."""

        job = await self._require_job(job_id)
        shards, operations, workers, transports, checkpoints, event_bounds = await asyncio.gather(
            self.repository.list_shards(job_id, limit=500),
            self.repository.list_operations(job_id, limit=500),
            self.repository.list_workers(job_id=job_id, limit=500),
            self.repository.list_transports(job_id=job_id, limit=500),
            self.repository.list_checkpoints(job_id, limit=1),
            self.repository.get_event_sequence_bounds(job_id),
        )
        return {
            "schema_version": 1,
            "job_id": job_id,
            "revision": job["revision"],
            "state": job["state"],
            "phase": job["phase"],
            "counters": job["counters"],
            "job": job,
            "shards": shards,
            "operations": operations,
            "workers": workers,
            "transports": transports,
            "latest_checkpoint": checkpoints[0] if checkpoints else None,
            "last_event_sequence": event_bounds["last_sequence"] or 0,
        }

    async def publish_committed_events(
        self,
        job_id: str,
        event_sequences: Sequence[int],
    ) -> list[JsonDict]:
        """Fan out events produced atomically by ``commit_accepted_batch``."""

        envelopes: list[JsonDict] = []
        for sequence in event_sequences:
            events = await self.repository.replay_events(job_id, after_seq=int(sequence) - 1, limit=1)
            if not events or int(events[0]["sequence"]) != int(sequence):
                continue
            envelope = self.event_envelope(events[0])
            self.event_hub.publish(envelope)
            envelopes.append(envelope)
        return envelopes

    @staticmethod
    def event_envelope(event: Mapping[str, Any]) -> JsonDict:
        """Translate a repository record into the stable stream schema."""

        return {
            "schema_version": 1,
            "seq": int(event["sequence"]),
            "job_id": event["job_id"],
            "revision": event.get("revision"),
            "type": event["event_type"],
            "emitted_at": event["emitted_at"],
            "worker_id": event.get("worker_id"),
            "operation_id": event.get("operation_id"),
            "payload": dict(event.get("payload") or {}),
        }

    async def emit_state_changed(self, job: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> JsonDict:
        payload: JsonDict = {"state": job["state"], "phase": job["phase"]}
        if extra:
            payload.update(extra)
        return await self.emit(job["job_id"], "job.state_changed", payload, revision=job["revision"])

    async def _emit_command(self, command: Mapping[str, Any], revision: int) -> JsonDict:
        return await self.emit(
            str(command["job_id"]),
            "worker.command.queued",
            {
                "command_id": command["command_id"],
                "command_type": command["command_type"],
                "worker_id": command.get("worker_id"),
            },
            revision=revision,
            worker_id=command.get("worker_id"),
        )

    async def _require_job(self, job_id: str) -> JsonDict:
        job = await self.repository.get_job(job_id)
        if job is None:
            raise MarketJobNotFoundError(f"market job {job_id!r} was not found")
        return job
