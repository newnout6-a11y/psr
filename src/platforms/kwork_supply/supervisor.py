"""Reconciliation loop for stable in-process market workers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from .coordinator import MarketScanCoordinator
from .identity_pool import MarketIdentityPool
from .models import JobKind, JobState, NetworkPolicy, OperationKind, TransportHealth
from .rate_control import (
    ConcurrencyPolicy,
    ConcurrencySignals,
    recommend_worker_concurrency,
)
from .transports.base import TransportManager
from .worker import MarketWorker, OperationHandler


_ACTIVE_STATES = {
    JobState.PREPARING.value,
    JobState.MAPPING.value,
    JobState.PLANNING.value,
    JobState.RUNNING.value,
    JobState.COMPLETING.value,
    JobState.ENRICHING.value,
    JobState.ANALYZING.value,
    JobState.FINALIZING.value,
    JobState.PAUSING.value,
    JobState.STOPPING.value,
}


class MarketWorkerSupervisor:
    """Reconcile desired worker pools and recover work after a process restart."""

    def __init__(
        self,
        coordinator: MarketScanCoordinator,
        *,
        handlers: Mapping[OperationKind | str, OperationHandler],
        transport_manager: TransportManager | None = None,
        identity_pool: MarketIdentityPool | None = None,
        reconcile_interval_seconds: float = 1.0,
        worker_poll_interval_seconds: float = 2.0,
        concurrency_policy: ConcurrencyPolicy | None = None,
        concurrency_window_attempts: int = 50,
    ) -> None:
        if reconcile_interval_seconds <= 0 or worker_poll_interval_seconds <= 0:
            raise ValueError("supervisor intervals must be positive")
        if concurrency_window_attempts < 1 or concurrency_window_attempts > 1_000:
            raise ValueError("concurrency_window_attempts must be between 1 and 1000")
        self.coordinator = coordinator
        self.handlers = handlers
        self.transport_manager = transport_manager
        self.identity_pool = identity_pool
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.worker_poll_interval_seconds = worker_poll_interval_seconds
        self.concurrency_policy = concurrency_policy or ConcurrencyPolicy()
        self.concurrency_window_attempts = concurrency_window_attempts
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._workers: dict[str, MarketWorker] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._last_concurrency_advice: dict[str, tuple[int, int, tuple[str, ...], int | None, int | None]] = {}
        self._closed = False

    @property
    def worker_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._workers))

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("supervisor is closed")
        if self._loop_task is not None:
            return
        await self.coordinator.initialize()
        await self.coordinator.repository.recover_expired_operations()
        await self.reconcile_once()
        self._loop_task = asyncio.create_task(self._run_loop(), name="market-worker-supervisor")

    async def close(self) -> None:
        self._closed = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for worker in self._workers.values():
            worker.request_stop()
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._workers.clear()
        if self.transport_manager is not None:
            self.transport_manager.close()

    async def reconcile_once(self) -> None:
        """Run one idempotent desired-state reconciliation pass."""

        await self.coordinator.repository.recover_expired_operations()
        await self._refresh_transports()
        active_jobs = await self.coordinator.repository.list_jobs(states=_ACTIVE_STATES, limit=500)
        # Buyer Search creates a Market job only as a durable account/VPNTE
        # lease container. Its read workers are owned by BuyerDiscoverySupervisor;
        # starting a supply worker for it steals the same routes from Buyer Search.
        jobs = [
            job
            for job in active_jobs
            if str(job.get("job_kind") or JobKind.SUPPLY.value) != JobKind.BUYER_SEARCH.value
        ]
        active_job_ids = {str(job["job_id"]) for job in active_jobs}
        for job in jobs:
            await self._reconcile_job(job)
        for worker_id, worker in tuple(self._workers.items()):
            task = self._tasks.get(worker_id)
            if worker.job_id not in active_job_ids:
                worker.request_terminal_stop()
                if (
                    worker.current_operation_id is None
                    and task is not None
                    and not task.done()
                    and task.cancelling() == 0
                ):
                    task.cancel()
            if task is not None and task.done():
                self._tasks.pop(worker_id, None)
                self._workers.pop(worker_id, None)
        if self.identity_pool is not None:
            await self.identity_pool.release_inactive_job_bindings(active_job_ids)

    async def _reconcile_job(self, job: Mapping[str, Any]) -> None:
        job_id = str(job["job_id"])
        state = str(job["state"])
        job_workers = [worker for worker in self._workers.values() if worker.job_id == job_id]
        if state in {JobState.PAUSING.value, JobState.STOPPING.value}:
            for worker in job_workers:
                worker.request_drain()
            if not job_workers or all(worker.current_operation_id is None for worker in job_workers):
                if state == JobState.PAUSING.value:
                    await self.coordinator.mark_paused(job_id)
                else:
                    await self.coordinator.mark_stopped(job_id)
            return

        desired = await self._effective_worker_count(job)
        live_workers = {worker.worker_id: worker for worker in job_workers if not self._task_done(worker.worker_id)}
        for index in range(1, desired + 1):
            worker_id = self._worker_id(job_id, index)
            if worker_id not in live_workers:
                await self._start_worker(job_id, worker_id)
        for worker_id, worker in live_workers.items():
            if self._worker_index(worker_id) > desired:
                worker.request_drain()

    async def _effective_worker_count(self, job: Mapping[str, Any]) -> int:
        """Apply only temporary safety caps; never overwrite user configuration."""

        job_id = str(job["job_id"])
        configured = max(int(job["desired_workers"]), 0)
        policy = NetworkPolicy(str(job["network_policy"]))
        healthy_transport_count: int | None = None
        if policy in {NetworkPolicy.VPNTE_ONLY, NetworkPolicy.EXPLICIT_POOL}:
            # Consume the durable pool page-by-page so the worker cap follows
            # the discovered VPNTE pool instead of a repository page size.
            healthy_transport_count = 0
            cursor: str | None = None
            while True:
                transports = await self.coordinator.repository.list_transports(cursor=cursor, limit=1_000)
                healthy_transport_count += sum(
                    transport["health"] == TransportHealth.HEALTHY.value for transport in transports
                )
                if len(transports) < 1_000:
                    break
                next_cursor = str(transports[-1]["transport_id"])
                if next_cursor == cursor:
                    break
                cursor = next_cursor
        identity_capacity: int | None = None
        if self.identity_pool is not None:
            identity_capacity = await self.identity_pool.capacity_for_job(job_id)
        signal_record = await self.coordinator.repository.get_job_concurrency_signals(
            job_id,
            window=self.concurrency_window_attempts,
        )
        signals = ConcurrencySignals(
            success_count=int(signal_record["success_count"]),
            http_403_count=int(signal_record["http_403_count"]),
            http_429_count=int(signal_record["http_429_count"]),
            timeout_count=int(signal_record["timeout_count"]),
            novelty_rate=signal_record["novelty_rate"],
            healthy_transport_count=healthy_transport_count,
        )
        has_protection = bool(signals.http_403_count or signals.http_429_count)
        has_window = signals.attempt_count >= self.concurrency_policy.min_sample_size
        if identity_capacity is None and healthy_transport_count is None and not has_protection and not has_window:
            return configured

        recommendation = recommend_worker_concurrency(
            self.concurrency_policy,
            current_workers=configured,
            signals=signals,
        )
        effective = configured
        if healthy_transport_count is not None:
            effective = min(effective, healthy_transport_count)
        if identity_capacity is not None:
            effective = min(effective, identity_capacity)
        recommended = min(effective, recommendation.recommended_workers)
        await self._emit_concurrency_advice(
            job,
            configured=configured,
            effective=effective,
            recommended=recommended,
            healthy_transport_count=healthy_transport_count,
            identity_capacity=identity_capacity,
            signal_record=signal_record,
            reasons=recommendation.reasons,
        )
        return effective

    async def _emit_concurrency_advice(
        self,
        job: Mapping[str, Any],
        *,
        configured: int,
        effective: int,
        recommended: int,
        healthy_transport_count: int | None,
        identity_capacity: int | None,
        signal_record: Mapping[str, Any],
        reasons: tuple[str, ...],
    ) -> None:
        if effective == configured and recommended == effective:
            return
        job_id = str(job["job_id"])
        normalized_reasons = reasons
        if identity_capacity is not None and effective < configured:
            normalized_reasons += ("account_identity_capacity",)
        if healthy_transport_count is not None and effective < configured:
            normalized_reasons += ("healthy_transport_cap",)
        signature = (effective, recommended, normalized_reasons, healthy_transport_count, identity_capacity)
        if self._last_concurrency_advice.get(job_id) == signature:
            return
        self._last_concurrency_advice[job_id] = signature
        await self.coordinator.emit(
            job_id,
            "worker.concurrency_recommended",
            {
                "configured_workers": configured,
                "effective_workers": effective,
                "recommended_workers": recommended,
                "healthy_transport_count": healthy_transport_count,
                "identity_capacity": identity_capacity,
                "window_attempt_count": signal_record["window_attempt_count"],
                "success_count": signal_record["success_count"],
                "http_403_count": signal_record["http_403_count"],
                "http_429_count": signal_record["http_429_count"],
                "timeout_count": signal_record["timeout_count"],
                "novelty_rate": signal_record["novelty_rate"],
                "reasons": list(normalized_reasons),
                "advisory": True,
            },
            revision=int(job["revision"]),
        )

    async def _start_worker(self, job_id: str, worker_id: str) -> None:
        previous = await self.coordinator.repository.get_worker(worker_id)
        generation = int(previous["generation"]) + 1 if previous is not None else 1
        worker = MarketWorker(
            self.coordinator,
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            handlers=self.handlers,
            transport_manager=self.transport_manager,
            identity_pool=self.identity_pool,
            poll_interval_seconds=self.worker_poll_interval_seconds,
        )
        task = asyncio.create_task(worker.run(), name=f"market-worker:{worker_id}")
        self._workers[worker_id] = worker
        self._tasks[worker_id] = task

    async def _refresh_transports(self) -> None:
        if self.transport_manager is None:
            return
        try:
            snapshots = await asyncio.to_thread(self.transport_manager.refresh)
        except Exception:
            return
        for snapshot in snapshots:
            await self.coordinator.repository.upsert_transport(snapshot)

    async def _run_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.reconcile_interval_seconds)
                await self.reconcile_once()
        except asyncio.CancelledError:
            raise

    def _task_done(self, worker_id: str) -> bool:
        task = self._tasks.get(worker_id)
        return task is None or task.done()

    @staticmethod
    def _worker_id(job_id: str, index: int) -> str:
        return f"worker-{job_id}-{index:02d}"

    @staticmethod
    def _worker_index(worker_id: str) -> int:
        try:
            return int(worker_id.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0
