"""In-process durable worker actor for Kwork market operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .coordinator import MarketScanCoordinator
from .models import (
    CommandState,
    JobState,
    NetworkPolicy,
    OperationKind,
    OperationState,
    TransportHealth,
    WorkerCommandKind,
    WorkerDesiredState,
    WorkerState,
    utc_now,
)
from .repository import MarketJobRevisionConflictError
from .transports.base import TransportManager


JsonDict = dict[str, Any]
OperationHandler = Callable[["MarketWorker", Mapping[str, Any]], Awaitable[None]]


class RetryableOperationError(RuntimeError):
    """Signal a retryable handler failure without claiming operation success."""

    def __init__(self, message: str, *, retry_at: str | None = None, failure_kind: str = "retryable") -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.failure_kind = failure_kind


class MarketWorker:
    """A stable actor that leases and executes work for exactly one job."""

    def __init__(
        self,
        coordinator: MarketScanCoordinator,
        *,
        job_id: str,
        worker_id: str,
        generation: int,
        handlers: Mapping[OperationKind | str, OperationHandler],
        transport_manager: TransportManager | None = None,
        lease_seconds: int = 60,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if not job_id.strip() or not worker_id.strip():
            raise ValueError("job_id and worker_id are required")
        if generation < 1:
            raise ValueError("generation must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.coordinator = coordinator
        self.job_id = job_id
        self.worker_id = worker_id
        self.generation = generation
        self.handlers = {self._kind_key(kind): handler for kind, handler in handlers.items()}
        self.transport_manager = transport_manager
        self.lease_seconds = lease_seconds
        self.poll_interval_seconds = poll_interval_seconds

        self._actual_state = WorkerState.STARTING
        self._desired_state = WorkerDesiredState.RUNNING
        self._current_operation_id: str | None = None
        self._transport_id: str | None = None
        self._stop_requested = False
        self._drain_requested = False
        self._direct_fallback_reported = False
        self._last_heartbeat_at: str | None = None
        self._last_emitted_state_signature: tuple[str, str, str | None, str | None] | None = None

    @property
    def actual_state(self) -> WorkerState:
        return self._actual_state

    @property
    def desired_state(self) -> WorkerDesiredState:
        return self._desired_state

    @property
    def current_operation_id(self) -> str | None:
        return self._current_operation_id

    @property
    def transport_proxy_url(self) -> str | None:
        """Return the worker-owned proxy URL without exposing provider secrets."""

        if self.transport_manager is None or self._transport_id is None:
            return None
        snapshot = self.transport_manager.get(self._transport_id)
        return snapshot.proxy_url if snapshot is not None else None

    @property
    def transport_id(self) -> str | None:
        """Return the current durable route identifier, never the proxy secret."""

        return self._transport_id

    async def quarantine_current_transport(self, *, reason: str, until: str | None = None) -> bool:
        """Quarantine the currently leased route after an explicit protection signal."""

        if self.transport_manager is None or self._transport_id is None:
            return False
        quarantine = getattr(self.transport_manager, "quarantine", None)
        if not callable(quarantine):
            return False
        snapshot = quarantine(self._transport_id, reason=reason, until=until)
        await self.coordinator.repository.upsert_transport(snapshot)
        await self.coordinator.emit(
            self.job_id,
            "transport.state_changed",
            {
                "transport_id": snapshot.transport_id,
                "health": snapshot.health,
                "generation": snapshot.generation,
                "reason": reason,
            },
            worker_id=self.worker_id,
        )
        await self._release_transport()
        return True

    def request_drain(self) -> None:
        self._drain_requested = True
        if self._desired_state is WorkerDesiredState.RUNNING:
            self._desired_state = WorkerDesiredState.DRAINING

    def request_stop(self) -> None:
        self._stop_requested = True
        self._drain_requested = True

    async def run(self) -> None:
        """Persist lifecycle and process operations until drained or cancelled."""

        await self._persist_state(WorkerState.STARTING)
        try:
            while not self._stop_requested:
                await self._process_commands()
                if self._stop_requested:
                    break
                if self._drain_requested and self._current_operation_id is None:
                    break

                job = await self.coordinator.repository.get_job(self.job_id)
                if job is None:
                    self._desired_state = WorkerDesiredState.STOPPED
                    break
                state = str(job["state"])
                if state in {JobState.PAUSING.value, JobState.PAUSED.value, JobState.STOPPING.value}:
                    self.request_drain()
                    continue
                if state in {JobState.STOPPED.value, JobState.COMPLETED.value, JobState.FAILED.value, JobState.BLOCKED.value}:
                    self._desired_state = WorkerDesiredState.STOPPED
                    break
                if self._time_budget_exhausted(job):
                    await self.coordinator.finish_for_time_budget(self.job_id)
                    continue
                if state == JobState.PREPARING.value:
                    try:
                        await self.coordinator.start_mapping(self.job_id)
                    except MarketJobRevisionConflictError:
                        # Another worker won the transition; re-read state next loop.
                        continue

                if not await self._ensure_transport(job):
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue

                await self._persist_state(WorkerState.LEASING)
                operation = await self.coordinator.repository.lease_operation(
                    self.worker_id,
                    job_id=self.job_id,
                    transport_id=self._transport_id,
                    lease_seconds=self.lease_seconds,
                )
                if operation is None:
                    await self._persist_state(WorkerState.IDLE)
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue
                if self._drain_requested:
                    # The repository's job-state gate prevents normal pause races;
                    # this branch protects explicit per-worker drain commands too.
                    await self.coordinator.repository.fail_operation(
                        operation["operation_id"],
                        self.worker_id,
                        "worker drained before execution",
                        retry_at=utc_now(),
                        failure_kind="worker_drained",
                    )
                    continue
                await self._execute(operation)
        except asyncio.CancelledError:
            raise
        finally:
            await self._release_transport()
            await self._persist_state(WorkerState.STOPPED)

    async def _execute(self, operation: Mapping[str, Any]) -> None:
        operation_id = str(operation["operation_id"])
        self._current_operation_id = operation_id
        await self._persist_state(WorkerState.BUSY)
        await self.coordinator.emit(
            self.job_id,
            "operation.started",
            {"kind": operation["kind"], "attempt": operation.get("current_attempt")},
            worker_id=self.worker_id,
            operation_id=operation_id,
        )
        try:
            handler = self.handlers.get(str(operation["kind"]))
            if handler is None:
                raise RuntimeError(f"no handler registered for {operation['kind']!r}")
            await handler(self, operation)
            current = await self.coordinator.repository.get_operation(operation_id)
            if current is not None and current["state"] in {OperationState.LEASED.value, OperationState.RUNNING.value}:
                completed = await self.coordinator.repository.complete_operation(operation_id, self.worker_id)
                await self.coordinator.emit(
                    self.job_id,
                    "operation.completed",
                    {"kind": completed["kind"]},
                    worker_id=self.worker_id,
                    operation_id=operation_id,
                )
                await self.coordinator.ensure_analysis_operation(self.job_id)
        except RetryableOperationError as exc:
            await self._fail_operation(operation_id, str(exc), retry_at=exc.retry_at, failure_kind=exc.failure_kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - handlers are isolated durable operations.
            await self._fail_operation(operation_id, str(exc), failure_kind="handler_error")
        finally:
            self._current_operation_id = None
            if not self._drain_requested:
                await self._persist_state(WorkerState.IDLE)

    async def _fail_operation(
        self,
        operation_id: str,
        error: str,
        *,
        retry_at: str | None = None,
        failure_kind: str,
    ) -> None:
        try:
            failed = await self.coordinator.repository.fail_operation(
                operation_id,
                self.worker_id,
                error,
                retry_at=retry_at,
                failure_kind=failure_kind,
            )
        except Exception:  # The lease may have been atomically completed by a handler.
            return
        event_type = "operation.contract_violation" if failure_kind == "contract_violation" else "operation.failed"
        await self.coordinator.emit(
            self.job_id,
            event_type,
            {"kind": failed["kind"], "error": error, "failure_kind": failure_kind},
            worker_id=self.worker_id,
            operation_id=operation_id,
        )

    async def _process_commands(self) -> None:
        queued = await self.coordinator.repository.list_worker_commands(
            job_id=self.job_id,
            state=CommandState.QUEUED,
            limit=100,
        )
        for command in queued:
            target = command.get("worker_id")
            if target is not None and target != self.worker_id:
                continue
            command_id = str(command["command_id"])
            await self.coordinator.repository.update_worker_command(command_id, CommandState.ACKNOWLEDGED)
            try:
                await self._apply_command(WorkerCommandKind(command["command_type"]), command.get("payload") or {})
            except Exception as exc:  # noqa: BLE001 - command failures must be persisted.
                await self.coordinator.repository.update_worker_command(command_id, CommandState.FAILED, error=str(exc))
                await self.coordinator.emit(
                    self.job_id,
                    "warning",
                    {"command_id": command_id, "error": str(exc)},
                    worker_id=self.worker_id,
                )
            else:
                await self.coordinator.repository.update_worker_command(command_id, CommandState.COMPLETED)

    async def _apply_command(self, command: WorkerCommandKind, payload: Mapping[str, Any]) -> None:
        if command is WorkerCommandKind.DRAIN:
            self.request_drain()
            return
        if command is WorkerCommandKind.DISABLE:
            self._desired_state = WorkerDesiredState.DISABLED
            self.request_stop()
            return
        if command is WorkerCommandKind.RESTART:
            self._desired_state = WorkerDesiredState.RUNNING
            self.request_stop()
            return
        if command is WorkerCommandKind.ROTATE:
            if self.transport_manager is None or self._transport_id is None:
                raise RuntimeError("worker has no managed transport to rotate")
            snapshot = self.transport_manager.rotate(
                self._transport_id,
                self.worker_id,
                country=payload.get("country") if isinstance(payload.get("country"), str) else None,
            )
            await self.coordinator.repository.upsert_transport(snapshot)
            await self.coordinator.emit(
                self.job_id,
                "transport.state_changed",
                {"transport_id": snapshot.transport_id, "health": snapshot.health, "generation": snapshot.generation},
                worker_id=self.worker_id,
            )
            return
        if command is WorkerCommandKind.RECONNECT:
            await self._release_transport()
            return
        raise RuntimeError(f"unsupported worker command {command.value!r}")

    async def _ensure_transport(self, job: Mapping[str, Any]) -> bool:
        policy = NetworkPolicy(str(job["network_policy"]))
        if policy is NetworkPolicy.DIRECT_ONLY:
            return True
        if self.transport_manager is not None and self._transport_id is not None:
            snapshot = self.transport_manager.get(self._transport_id)
            if (
                snapshot is not None
                and snapshot.lease_owner == self.worker_id
                and snapshot.health is TransportHealth.HEALTHY
            ):
                return True
            await self._release_transport()
        if self.transport_manager is not None:
            snapshot = self.transport_manager.acquire(self.worker_id, policy=policy)
            if snapshot is not None:
                self._transport_id = snapshot.transport_id
                await self.coordinator.repository.upsert_transport(snapshot)
                await self.coordinator.emit(
                    self.job_id,
                    "transport.state_changed",
                    {"transport_id": snapshot.transport_id, "health": snapshot.health, "generation": snapshot.generation},
                    worker_id=self.worker_id,
                )
                return True
        if policy is NetworkPolicy.PREFER_VPNTE:
            if not self._direct_fallback_reported:
                self._direct_fallback_reported = True
                await self.coordinator.emit(
                    self.job_id,
                    "warning",
                    {"code": "transport_fallback_direct", "requested_policy": policy.value},
                    worker_id=self.worker_id,
                )
            return True
        await self._persist_state(WorkerState.BACKOFF, last_error="no healthy VPNTE transport")
        return False

    async def _release_transport(self) -> None:
        if self.transport_manager is None or self._transport_id is None:
            self._transport_id = None
            return
        transport_id, self._transport_id = self._transport_id, None
        try:
            self.transport_manager.release(transport_id, self.worker_id)
        except Exception:
            return
        snapshot = self.transport_manager.get(transport_id)
        if snapshot is not None:
            await self.coordinator.repository.upsert_transport(snapshot)

    async def _persist_state(self, actual: WorkerState, *, last_error: str | None = None) -> None:
        self._actual_state = actual
        heartbeat = utc_now()
        self._last_heartbeat_at = heartbeat
        record = await self.coordinator.repository.upsert_worker(
            {
                "worker_id": self.worker_id,
                "generation": self.generation,
                "desired_state": self._desired_state,
                "actual_state": actual,
                "runtime_kind": "in_process",
                "transport_id": self._transport_id,
                "current_operation_id": self._current_operation_id,
                "heartbeat_at": heartbeat,
                "last_error": last_error,
            },
            job_id=self.job_id,
        )
        state_signature = (
            str(record["actual_state"]),
            str(record["desired_state"]),
            record["transport_id"],
            record["current_operation_id"],
        )
        # Leasing and idle heartbeats happen several times a second. Persist
        # them for recovery, but reserve the user-facing stream for changes
        # a user can act on.
        if actual in {WorkerState.LEASING, WorkerState.IDLE} or state_signature == self._last_emitted_state_signature:
            return
        self._last_emitted_state_signature = state_signature
        await self.coordinator.emit(
            self.job_id,
            "worker.state_changed",
            {
                "actual_state": record["actual_state"],
                "desired_state": record["desired_state"],
                "transport_id": record["transport_id"],
                "current_operation_id": record["current_operation_id"],
            },
            worker_id=self.worker_id,
        )

    @staticmethod
    def _kind_key(kind: OperationKind | str) -> str:
        return kind.value if isinstance(kind, OperationKind) else str(kind)

    @staticmethod
    def _time_budget_exhausted(job: Mapping[str, Any]) -> bool:
        budget = job.get("time_budget_seconds")
        started_at = job.get("started_at")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            return False
        if not isinstance(started_at, str) or not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return datetime.now(UTC) >= started.astimezone(UTC) + timedelta(seconds=budget)
