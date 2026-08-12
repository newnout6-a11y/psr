"""Read-only worker fleet supervision for Buyer Search discovery.

The module deliberately has no dependency on a Kwork client, VPNTEN, FastAPI,
or the Buyer service.  Integrations provide three small adapters instead:

* an identity allocator that reserves account/transport/egress triples;
* an identity releaser that returns those reservations; and
* a source factory that builds the read-only page source for one identity.

This keeps the execution policy deterministic and easy to exercise with fakes
while preserving the important runtime rule: one active worker owns one unique
account, transport and verified egress address.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import inspect
from typing import Any, Protocol, TypeAlias
from uuid import uuid4

from .worker import (
    BuyerDiscoveryFailureKind,
    BuyerDiscoveryIdentity,
    BuyerDiscoveryOutcome,
    BuyerDiscoveryReadSource,
    BuyerDiscoveryRunResult,
    BuyerDiscoveryWorker,
)


CANARY_WORKER_CAPS: tuple[int, ...] = (2, 5, 10, 20, 30)


class BuyerDiscoverySupervisorError(RuntimeError):
    """Raised for an invalid Buyer discovery fleet control operation."""


class BuyerDiscoveryCapacityMode(StrEnum):
    """Whether a run waits for its requested capacity or starts elastically."""

    ELASTIC = "elastic"
    STRICT = "strict"


class BuyerDiscoveryFleetState(StrEnum):
    """In-process lifecycle state for one Buyer discovery worker fleet."""

    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    BLOCKED = "blocked"


class BuyerDiscoveryWorkerState(StrEnum):
    """Observable state of an individual read-only discovery worker."""

    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


class BuyerDiscoveryIdentityAllocator(Protocol):
    """Reserve currently usable identity triples for a run.

    Returned identities are treated as reserved until the supervisor calls the
    paired releaser.  Implementations must not return an identity already in
    ``active_identities``.
    """

    def allocate(
        self,
        *,
        run_id: str,
        requested_workers: int,
        active_identities: Sequence[BuyerDiscoveryIdentity],
    ) -> Sequence[BuyerDiscoveryIdentity] | Awaitable[Sequence[BuyerDiscoveryIdentity]]:
        """Return at most ``requested_workers`` reserved identities."""


class BuyerDiscoveryIdentityReleaser(Protocol):
    """Release a reservation made by :class:`BuyerDiscoveryIdentityAllocator`."""

    def release(
        self,
        *,
        run_id: str,
        identity: BuyerDiscoveryIdentity,
        reason: str,
    ) -> None | Awaitable[None]:
        """Return the identity to the external allocator."""


class BuyerDiscoveryCapacityInventory(Protocol):
    """Inspect candidate capacity without acquiring an identity lease."""

    def inspect(
        self,
        *,
        run_id: str,
        requested_workers: int,
    ) -> Sequence[BuyerDiscoveryIdentity] | Awaitable[Sequence[BuyerDiscoveryIdentity]]:
        """Return eligible identities with fresh verified egress addresses."""


class BuyerDiscoverySourceFactory(Protocol):
    """Build a strictly read-only source adapter for a single identity."""

    def create(
        self,
        *,
        run_id: str,
        identity: BuyerDiscoveryIdentity,
    ) -> BuyerDiscoveryReadSource | Awaitable[BuyerDiscoveryReadSource]:
        """Return a source exposing only ``fetch_page``."""


BuyerDiscoveryEventHook: TypeAlias = Callable[[str, str, Mapping[str, Any]], Any]
BuyerDiscoveryHeartbeatHook: TypeAlias = Callable[[str, BuyerDiscoveryIdentity, Mapping[str, Any]], Any]
BuyerDiscoveryResultHook: TypeAlias = Callable[[str, BuyerDiscoveryIdentity, BuyerDiscoveryRunResult], Any]
BuyerDiscoveryWorkerFactory: TypeAlias = Callable[..., BuyerDiscoveryWorker]


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryCapacityPreflight:
    """A conservative capacity calculation based on unique identity fields."""

    run_id: str
    requested_workers: int
    canary_cap: int
    candidate_count: int
    eligible_accounts: int
    healthy_transports: int
    transports_with_fresh_verified_egress: int
    unique_egress_ips: int
    collision_count: int
    effective_capacity: int
    selected_identities: tuple[BuyerDiscoveryIdentity, ...]
    reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "requested_workers": self.requested_workers,
            "canary_cap": self.canary_cap,
            "candidate_count": self.candidate_count,
            "eligible_accounts": self.eligible_accounts,
            "healthy_transports": self.healthy_transports,
            "transports_with_fresh_verified_egress": self.transports_with_fresh_verified_egress,
            "unique_egress_ips": self.unique_egress_ips,
            "collision_count": self.collision_count,
            "effective_capacity": self.effective_capacity,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryWorkerSnapshot:
    """A JSON-friendly worker record for the execution grid."""

    worker_id: str
    account_registration_id: str
    transport_id: str
    egress_ip: str
    state: BuyerDiscoveryWorkerState
    started_at: str | None
    last_heartbeat_at: str | None
    last_outcome: BuyerDiscoveryOutcome | None
    last_error: str | None
    current_task: Mapping[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "account_registration_id": self.account_registration_id,
            "transport_id": self.transport_id,
            "egress_ip": self.egress_ip,
            "state": self.state.value,
            "started_at": self.started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_outcome": self.last_outcome.value if self.last_outcome is not None else None,
            "last_error": self.last_error,
            "current_task": dict(self.current_task) if self.current_task is not None else None,
        }


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryAttachmentReservation:
    """One supervisor-owned exclusive attachment-read slot for a worker identity."""

    run_id: str
    worker_id: str
    identity: BuyerDiscoveryIdentity
    token: str


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryFleetSnapshot:
    """A durable-API-shaped snapshot without owning persistence itself."""

    run_id: str
    state: BuyerDiscoveryFleetState
    capacity_mode: BuyerDiscoveryCapacityMode
    requested_workers: int
    canary_cap: int
    effective_workers: int
    preflight: BuyerDiscoveryCapacityPreflight
    workers: tuple[BuyerDiscoveryWorkerSnapshot, ...]
    reason: str | None = None
    adaptive_worker_cap: int | None = None
    throttle_until: str | None = None
    throttle_failures: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "capacity_mode": self.capacity_mode.value,
            "requested_workers": self.requested_workers,
            "canary_cap": self.canary_cap,
            "effective_workers": self.effective_workers,
            "capacity": self.preflight.to_payload(),
            "workers": [worker.to_payload() for worker in self.workers],
            "reason": self.reason,
            "adaptive_worker_cap": self.adaptive_worker_cap,
            "throttle_until": self.throttle_until,
            "throttle_failures": self.throttle_failures,
        }


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(slots=True)
class _WorkerBinding:
    identity: BuyerDiscoveryIdentity
    worker: BuyerDiscoveryWorker
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    state: BuyerDiscoveryWorkerState = BuyerDiscoveryWorkerState.STARTING
    started_at: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_ticks: int = 0
    last_outcome: BuyerDiscoveryOutcome | None = None
    last_error: str | None = None
    busy: bool = False
    released: bool = False
    lease_lost: bool = False
    rebind_reason: str | None = None
    attachment_reservation_token: str | None = None
    attachment_release_event: asyncio.Event = field(default_factory=_set_event)
    stop_after_attachment: bool = False


@dataclass(slots=True)
class _Fleet:
    run_id: str
    requested_workers: int
    canary_cap: int
    capacity_mode: BuyerDiscoveryCapacityMode
    state: BuyerDiscoveryFleetState
    preflight: BuyerDiscoveryCapacityPreflight
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    workers: dict[str, _WorkerBinding] = field(default_factory=dict)
    reason: str | None = None
    adaptive_worker_cap: int | None = None
    throttle_until: str | None = None
    throttle_failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    provision_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BuyerDiscoverySupervisor:
    """Manage asynchronous, single-flight read-only Buyer discovery workers.

    The supervisor does not persist a run state or perform any network mutation.
    The caller can mirror the emitted events to a repository and call
    :meth:`wake` after a queue change, resume, or retry deadline.  A modest idle
    poll is retained only as recovery fallback when no wake notification arrives.
    """

    def __init__(
        self,
        repository: object,
        *,
        identity_allocator: BuyerDiscoveryIdentityAllocator,
        identity_releaser: BuyerDiscoveryIdentityReleaser,
        source_factory: BuyerDiscoverySourceFactory,
        capacity_inventory: BuyerDiscoveryCapacityInventory | None = None,
        event_hook: BuyerDiscoveryEventHook | None = None,
        heartbeat_hook: BuyerDiscoveryHeartbeatHook | None = None,
        result_hook: BuyerDiscoveryResultHook | None = None,
        worker_factory: BuyerDiscoveryWorkerFactory | None = None,
        lease_seconds: int = 60,
        idle_poll_interval_seconds: float = 2.0,
        reconcile_interval_seconds: float = 2.0,
        heartbeat_interval_seconds: float = 10.0,
        max_workers: int = 30,
    ) -> None:
        if not callable(getattr(identity_allocator, "allocate", None)):
            raise TypeError("identity_allocator must expose allocate")
        if not callable(getattr(identity_releaser, "release", None)):
            raise TypeError("identity_releaser must expose release")
        if not callable(getattr(source_factory, "create", None)):
            raise TypeError("source_factory must expose create")
        if capacity_inventory is not None and not callable(getattr(capacity_inventory, "inspect", None)):
            raise TypeError("capacity_inventory must expose inspect")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        for name, value in {
            "idle_poll_interval_seconds": idle_poll_interval_seconds,
            "reconcile_interval_seconds": reconcile_interval_seconds,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
        }.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 30:
            raise ValueError("max_workers must be an integer between 1 and 30")

        self.repository = repository
        self.identity_allocator = identity_allocator
        self.identity_releaser = identity_releaser
        self.source_factory = source_factory
        self.capacity_inventory = capacity_inventory
        self.event_hook = event_hook
        self.heartbeat_hook = heartbeat_hook
        self.result_hook = result_hook
        self.worker_factory = worker_factory or BuyerDiscoveryWorker
        self.lease_seconds = lease_seconds
        self.idle_poll_interval_seconds = float(idle_poll_interval_seconds)
        self.reconcile_interval_seconds = float(reconcile_interval_seconds)
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self.max_workers = max_workers
        self._fleets: dict[str, _Fleet] = {}
        self._lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_wake = asyncio.Event()
        self._closed = False

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._fleets))

    async def preflight(
        self,
        run_id: str,
        *,
        requested_workers: int,
        canary_cap: int | None = None,
    ) -> BuyerDiscoveryCapacityPreflight:
        """Calculate safe capacity without making a remote Kwork request."""

        run_id = _required_text(run_id, "run_id")
        requested = self._validate_requested_workers(requested_workers)
        cap = self._validate_canary_cap(canary_cap or self.max_workers)
        candidates = await self._inspect_candidates(run_id, requested)
        return _build_preflight(run_id, requested, cap, candidates)

    async def start(
        self,
        run_id: str,
        *,
        requested_workers: int,
        canary_cap: int | None = None,
        capacity_mode: BuyerDiscoveryCapacityMode | str = BuyerDiscoveryCapacityMode.ELASTIC,
    ) -> BuyerDiscoveryFleetSnapshot:
        """Start an elastic or strict fleet for a single Buyer Search run."""

        if self._closed:
            raise BuyerDiscoverySupervisorError("Buyer discovery supervisor is closed")
        run_id = _required_text(run_id, "run_id")
        requested = self._validate_requested_workers(requested_workers)
        cap = self._validate_canary_cap(canary_cap or self.max_workers)
        try:
            mode = BuyerDiscoveryCapacityMode(capacity_mode)
        except ValueError as exc:
            raise ValueError("capacity_mode must be elastic or strict") from exc

        inspected = await self.preflight(run_id, requested_workers=requested, canary_cap=cap)
        async with self._lock:
            existing = self._fleets.get(run_id)
            if existing is not None and existing.state is not BuyerDiscoveryFleetState.STOPPED:
                fleet = existing
                fleet.requested_workers = requested
                fleet.canary_cap = cap
                fleet.capacity_mode = mode
                fleet.preflight = inspected
                fleet.reason = None
                if fleet.state in {BuyerDiscoveryFleetState.PAUSED, BuyerDiscoveryFleetState.PAUSING, BuyerDiscoveryFleetState.BLOCKED}:
                    fleet.state = BuyerDiscoveryFleetState.RUNNING
                    fleet.resume_event.set()
                    for binding in fleet.workers.values():
                        binding.wake_event.set()
            else:
                fleet = _Fleet(
                    run_id=run_id,
                    requested_workers=requested,
                    canary_cap=cap,
                    capacity_mode=mode,
                    state=BuyerDiscoveryFleetState.RUNNING,
                    preflight=inspected,
                )
                fleet.resume_event.set()
                self._fleets[run_id] = fleet

        desired = min(requested, cap)
        if (
            mode is BuyerDiscoveryCapacityMode.STRICT
            and self.capacity_inventory is not None
            and inspected.effective_capacity < desired
        ):
            fleet.state = BuyerDiscoveryFleetState.BLOCKED
            fleet.reason = "strict capacity preflight did not satisfy requested workers"
            await self._emit(fleet, "capacity.changed", {**inspected.to_payload(), "blocked": True})
            await self._emit(fleet, "run.blocked", {"reason": fleet.reason})
            return self.snapshot(run_id)

        await self._provision_workers(fleet)
        if fleet.state is BuyerDiscoveryFleetState.BLOCKED:
            return self.snapshot(run_id)
        self._ensure_reconciler()
        await self._emit(fleet, "capacity.changed", fleet.preflight.to_payload())
        await self._emit(fleet, "run.started", self.snapshot(run_id).to_payload())
        return self.snapshot(run_id)

    async def pause(self, run_id: str) -> BuyerDiscoveryFleetSnapshot:
        """Drain current read pages and stop leasing new ones."""

        fleet = self._get_fleet(run_id)
        async with fleet.lock:
            if fleet.state is BuyerDiscoveryFleetState.STOPPED:
                raise BuyerDiscoverySupervisorError("cannot pause a stopped Buyer discovery fleet")
            if fleet.state is BuyerDiscoveryFleetState.PAUSED:
                return self.snapshot(fleet.run_id)
            fleet.state = BuyerDiscoveryFleetState.PAUSING
            fleet.resume_event.clear()
            for binding in fleet.workers.values():
                binding.wake_event.set()
        await self._emit(fleet, "run.pausing", {"requested_workers": fleet.requested_workers})
        await self._refresh_pause_state(fleet)
        return self.snapshot(fleet.run_id)

    async def resume(self, run_id: str) -> BuyerDiscoveryFleetSnapshot:
        """Resume a paused or capacity-blocked elastic fleet."""

        fleet = self._get_fleet(run_id)
        async with fleet.lock:
            if fleet.state is BuyerDiscoveryFleetState.STOPPED:
                raise BuyerDiscoverySupervisorError("cannot resume a stopped Buyer discovery fleet")
            if fleet.state is BuyerDiscoveryFleetState.RUNNING:
                return self.snapshot(fleet.run_id)
            if fleet.state is BuyerDiscoveryFleetState.BLOCKED and fleet.capacity_mode is BuyerDiscoveryCapacityMode.STRICT:
                raise BuyerDiscoverySupervisorError("strict fleet remains blocked until start is called with sufficient capacity")
            fleet.state = BuyerDiscoveryFleetState.RUNNING
            fleet.reason = None
            fleet.resume_event.set()
            for binding in fleet.workers.values():
                binding.wake_event.set()
        await self._provision_workers(fleet)
        self._ensure_reconciler()
        await self._emit(fleet, "run.resumed", self.snapshot(fleet.run_id).to_payload())
        return self.snapshot(fleet.run_id)

    async def stop(self, run_id: str, *, reason: str = "operator") -> BuyerDiscoveryFleetSnapshot:
        """Stop all workers, wait for release, and retain a stopped snapshot."""

        fleet = self._get_fleet(run_id)
        async with fleet.lock:
            if fleet.state is BuyerDiscoveryFleetState.STOPPED:
                return self.snapshot(fleet.run_id)
            fleet.state = BuyerDiscoveryFleetState.STOPPING
            fleet.reason = _optional_text(reason) or "operator"
            fleet.resume_event.set()
            bindings = tuple(fleet.workers.values())
            for binding in bindings:
                binding.state = BuyerDiscoveryWorkerState.STOPPING
                if binding.attachment_reservation_token is not None:
                    # Keep the account/route lease alive until the short-lived
                    # attachment client closes. Releasing it earlier could let
                    # another worker reuse the same identity concurrently.
                    binding.stop_after_attachment = True
                else:
                    binding.stop_event.set()
                binding.wake_event.set()
            tasks = tuple(binding.task for binding in bindings if binding.task is not None)
        await self._emit(fleet, "run.stopping", {"reason": fleet.reason})
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._mark_stopped_if_drained(fleet)
        return self.snapshot(fleet.run_id)

    async def close(self) -> None:
        """Stop every fleet and prevent further start calls."""

        self._closed = True
        run_ids = tuple(self.run_ids)
        for run_id in run_ids:
            with suppress(BuyerDiscoverySupervisorError):
                await self.stop(run_id, reason="supervisor_closed")
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

    async def wake(self, run_id: str) -> None:
        """Wake idle workers after enqueue, resume, or a retry deadline."""

        fleet = self._get_fleet(run_id)
        for binding in fleet.workers.values():
            binding.wake_event.set()
        self._reconcile_wake.set()

    async def reserve_attachment_identity(
        self,
        run_id: str,
        account_registration_id: str,
    ) -> BuyerDiscoveryAttachmentReservation:
        """Atomically reserve an idle account/route for one attachment read.

        The reservation shares the same supervisor lock used by the discovery
        loop. A worker cannot lease another page until the holder calls
        :meth:`release_attachment_identity`.
        """

        fleet = self._get_fleet(run_id)
        account_id = _required_text(account_registration_id, "account_registration_id")
        async with fleet.lock:
            if fleet.state not in {BuyerDiscoveryFleetState.RUNNING, BuyerDiscoveryFleetState.PAUSED}:
                raise BuyerDiscoverySupervisorError(
                    "Buyer discovery fleet must be running or paused before borrowing an attachment identity"
                )
            for binding in fleet.workers.values():
                if binding.identity.account_registration_id != account_id:
                    continue
                if binding.state not in {BuyerDiscoveryWorkerState.IDLE, BuyerDiscoveryWorkerState.PAUSED}:
                    continue
                if (
                    binding.busy
                    or binding.released
                    or binding.stop_event.is_set()
                    or binding.attachment_reservation_token is not None
                ):
                    continue
                token = uuid4().hex
                binding.attachment_reservation_token = token
                binding.attachment_release_event.clear()
                return BuyerDiscoveryAttachmentReservation(
                    run_id=fleet.run_id,
                    worker_id=binding.identity.worker_id,
                    identity=binding.identity,
                    token=token,
                )
        raise BuyerDiscoverySupervisorError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        )

    async def reserve_account_identity(
        self,
        account_registration_id: str,
    ) -> BuyerDiscoveryAttachmentReservation:
        """Borrow one idle leased identity for an account across active fleets.

        Conversation reads are scoped by sender account rather than a Buyer
        Search run.  Reuse the same exclusive reservation as attachments so a
        sync cannot overlap discovery on the same account/route pair.
        """

        account_id = _required_text(account_registration_id, "account_registration_id")
        for run_id in self.run_ids:
            try:
                return await self.reserve_attachment_identity(run_id, account_id)
            except BuyerDiscoverySupervisorError:
                continue
        raise BuyerDiscoverySupervisorError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        )

    async def release_attachment_identity(self, reservation: BuyerDiscoveryAttachmentReservation) -> None:
        """Release a previously reserved attachment identity exactly once."""

        if not isinstance(reservation, BuyerDiscoveryAttachmentReservation):
            raise TypeError("reservation must be BuyerDiscoveryAttachmentReservation")
        fleet = self._get_fleet(reservation.run_id)
        async with fleet.lock:
            binding = fleet.workers.get(reservation.worker_id)
            if binding is None or binding.attachment_reservation_token != reservation.token:
                return
            binding.attachment_reservation_token = None
            if binding.stop_after_attachment:
                binding.stop_event.set()
            binding.attachment_release_event.set()
            binding.wake_event.set()

    async def reconcile(self, run_id: str) -> BuyerDiscoveryFleetSnapshot:
        """Attempt elastic scale-up after new identities become available."""

        fleet = self._get_fleet(run_id)
        if fleet.state is BuyerDiscoveryFleetState.RUNNING:
            refreshed = await self.preflight(
                fleet.run_id,
                requested_workers=fleet.requested_workers,
                canary_cap=fleet.canary_cap,
            )
            fleet.preflight = refreshed
            await self._provision_workers(fleet)
        return self.snapshot(fleet.run_id)

    async def set_canary_cap(self, run_id: str, canary_cap: int) -> BuyerDiscoveryFleetSnapshot:
        """Advance or lower a fleet only through the approved canary stages."""

        fleet = self._get_fleet(run_id)
        cap = self._validate_canary_cap(canary_cap)
        fleet.canary_cap = cap
        if fleet.state is BuyerDiscoveryFleetState.RUNNING:
            await self.reconcile(fleet.run_id)
        return self.snapshot(fleet.run_id)

    def snapshot(self, run_id: str) -> BuyerDiscoveryFleetSnapshot:
        """Return a synchronous copy suitable for HTTP/WS snapshot responses."""

        fleet = self._get_fleet(run_id)
        worker_snapshots = tuple(
            BuyerDiscoveryWorkerSnapshot(
                worker_id=binding.identity.worker_id,
                account_registration_id=binding.identity.account_registration_id,
                transport_id=binding.identity.transport_id,
                egress_ip=binding.identity.egress_ip,
                state=binding.state,
                started_at=binding.started_at,
                last_heartbeat_at=binding.last_heartbeat_at,
                last_outcome=binding.last_outcome,
                last_error=binding.last_error,
                current_task=binding.worker.current_task,
            )
            for _, binding in sorted(fleet.workers.items())
        )
        effective_workers = sum(
            worker.state
            not in {
                BuyerDiscoveryWorkerState.STOPPED,
                BuyerDiscoveryWorkerState.FAILED,
                BuyerDiscoveryWorkerState.LEASE_LOST,
            }
            for worker in worker_snapshots
        )
        return BuyerDiscoveryFleetSnapshot(
            run_id=fleet.run_id,
            state=fleet.state,
            capacity_mode=fleet.capacity_mode,
            requested_workers=fleet.requested_workers,
            canary_cap=fleet.canary_cap,
            effective_workers=effective_workers,
            preflight=fleet.preflight,
            workers=worker_snapshots,
            reason=fleet.reason,
            adaptive_worker_cap=fleet.adaptive_worker_cap,
            throttle_until=fleet.throttle_until,
            throttle_failures=fleet.throttle_failures,
        )

    @staticmethod
    def _baseline_worker_cap(fleet: _Fleet) -> int:
        return min(fleet.requested_workers, fleet.canary_cap)

    @classmethod
    def _desired_worker_cap(cls, fleet: _Fleet) -> int:
        baseline = cls._baseline_worker_cap(fleet)
        if fleet.adaptive_worker_cap is None:
            return baseline
        return min(baseline, max(1, fleet.adaptive_worker_cap))

    async def _throttle_blocks_provisioning(self, fleet: _Fleet) -> bool:
        """Return whether a persisted-in-memory rate response still cools this fleet."""

        recovered_payload: dict[str, Any] | None = None
        async with fleet.lock:
            if fleet.throttle_until is None:
                return False
            if _timestamp_is_in_future(fleet.throttle_until):
                return True
            recovered_payload = {
                "previous_adaptive_worker_cap": fleet.adaptive_worker_cap,
                "previous_throttle_until": fleet.throttle_until,
                "previous_throttle_failures": fleet.throttle_failures,
                "requested_workers": fleet.requested_workers,
                "canary_cap": fleet.canary_cap,
                "effective_worker_cap": self._baseline_worker_cap(fleet),
            }
            fleet.adaptive_worker_cap = None
            fleet.throttle_until = None
            fleet.throttle_failures = 0
        await self._emit(fleet, "capacity.throttle_recovered", recovered_payload)
        return False

    async def _apply_adaptive_throttle(
        self,
        fleet: _Fleet,
        binding: _WorkerBinding,
        result: BuyerDiscoveryRunResult,
    ) -> None:
        """Lower the next allocation ceiling after a rate-limit response.

        The failed worker drains and releases its route normally.  The temporary
        cap prevents the reconciler from immediately filling that vacancy, so
        an identity change cannot bypass a server-directed backoff.
        """

        retry_after = result.retry_after_seconds or 1.0
        retry_after = min(300.0, max(1.0, float(retry_after)))
        async with fleet.lock:
            baseline = self._baseline_worker_cap(fleet)
            current_cap = fleet.adaptive_worker_cap if fleet.adaptive_worker_cap is not None else baseline
            fleet.throttle_failures += 1
            fleet.adaptive_worker_cap = max(1, min(baseline, current_cap - 1))
            multiplier = 2 ** min(fleet.throttle_failures - 1, 4)
            delay_seconds = min(900.0, retry_after * multiplier)
            computed_until = _utc_after_seconds(delay_seconds)
            # A repository quarantine is independently durable.  Do not cut a
            # longer worker-visible deadline short when multiple workers fail.
            quarantine_until = result.quarantine_until
            until_candidates = [item for item in (fleet.throttle_until, computed_until, quarantine_until) if item]
            fleet.throttle_until = max(until_candidates, key=_timestamp_sort_key)
            payload = {
                **_worker_event_payload(binding),
                "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
                "task_id": result.task_id,
                "endpoint": result.endpoint,
                "retry_after_seconds": result.retry_after_seconds,
                "delay_seconds": delay_seconds,
                "adaptive_worker_cap": fleet.adaptive_worker_cap,
                "throttle_until": fleet.throttle_until,
                "throttle_failures": fleet.throttle_failures,
            }
        await self._emit(fleet, "capacity.throttle_applied", payload)

    async def _provision_workers(self, fleet: _Fleet) -> None:
        """Acquire only the deficit, then reject every non-unique reservation."""

        async with fleet.provision_lock:
            await self._provision_workers_locked(fleet)

    async def _provision_workers_locked(self, fleet: _Fleet) -> None:
        """Perform one serialized allocation pass for ``fleet``."""

        if await self._throttle_blocks_provisioning(fleet):
            return
        async with fleet.lock:
            if fleet.state is not BuyerDiscoveryFleetState.RUNNING:
                return
            desired = self._desired_worker_cap(fleet)
            active = tuple(binding.identity for binding in fleet.workers.values() if not binding.released)
            deficit = max(0, desired - len(active))
        if deficit == 0:
            return

        allocated = await _await_value(
            self.identity_allocator.allocate(
                run_id=fleet.run_id,
                requested_workers=deficit,
                active_identities=active,
            )
        )
        candidates = _identity_sequence(allocated, "identity_allocator.allocate")
        selected, rejected = _select_new_identities(active, candidates, limit=deficit)
        if fleet.capacity_mode is BuyerDiscoveryCapacityMode.STRICT and len(selected) < deficit:
            for identity in candidates:
                if _identity_key(identity) not in {_identity_key(item) for item in active}:
                    await self._release_identity(fleet, identity, "strict_capacity_shortfall")
            fleet.state = BuyerDiscoveryFleetState.BLOCKED
            fleet.reason = "allocator could not reserve the requested unique identity capacity"
            await self._emit(fleet, "capacity.changed", {**fleet.preflight.to_payload(), "blocked": True})
            await self._emit(fleet, "run.blocked", {"reason": fleet.reason})
            return
        for identity in rejected:
            if _identity_key(identity) not in {_identity_key(item) for item in active}:
                await self._release_identity(fleet, identity, "capacity_collision_or_excess")

        started: list[BuyerDiscoveryIdentity] = []
        for identity in selected:
            try:
                await self._start_worker(fleet, identity)
                started.append(identity)
            except Exception as exc:  # noqa: BLE001 - failed factories must release their reservation.
                await self._release_identity(fleet, identity, "source_factory_failed")
                await self._emit(
                    fleet,
                    "worker.failed",
                    {"worker_id": identity.worker_id, "error": str(exc), "stage": "source_factory"},
                )

        actual_candidates = (*active, *started)
        if self.capacity_inventory is None:
            fleet.preflight = _build_preflight(
                fleet.run_id,
                fleet.requested_workers,
                fleet.canary_cap,
                actual_candidates,
            )
        if not started and not active and fleet.capacity_mode is BuyerDiscoveryCapacityMode.STRICT:
            fleet.state = BuyerDiscoveryFleetState.BLOCKED
            fleet.reason = "allocator returned no unique account/transport/egress identity"

    async def _start_worker(self, fleet: _Fleet, identity: BuyerDiscoveryIdentity) -> None:
        async with fleet.lock:
            if fleet.state is not BuyerDiscoveryFleetState.RUNNING:
                raise BuyerDiscoverySupervisorError("fleet is no longer running")
            if identity.worker_id in fleet.workers:
                raise BuyerDiscoverySupervisorError(f"duplicate Buyer discovery worker_id {identity.worker_id!r}")
            if _identity_key(identity) in {_identity_key(binding.identity) for binding in fleet.workers.values()}:
                raise BuyerDiscoverySupervisorError("duplicate Buyer discovery account/transport/egress identity")

        source = await _await_value(self.source_factory.create(run_id=fleet.run_id, identity=identity))
        if not callable(getattr(source, "fetch_page", None)):
            raise TypeError("source_factory must return a read-only source exposing fetch_page")
        worker = self.worker_factory(
            self.repository,
            source,
            run_id=fleet.run_id,
            identity=identity,
            lease_seconds=self.lease_seconds,
        )
        if not isinstance(worker, BuyerDiscoveryWorker):
            raise TypeError("worker_factory must return BuyerDiscoveryWorker")
        binding = _WorkerBinding(identity=identity, worker=worker)
        async with fleet.lock:
            if fleet.state is not BuyerDiscoveryFleetState.RUNNING:
                raise BuyerDiscoverySupervisorError("fleet stopped while creating Buyer discovery worker")
            if identity.worker_id in fleet.workers:
                raise BuyerDiscoverySupervisorError(f"duplicate Buyer discovery worker_id {identity.worker_id!r}")
            fleet.workers[identity.worker_id] = binding
            binding.task = asyncio.create_task(
                self._worker_loop(fleet, binding),
                name=f"buyer-discovery-worker:{fleet.run_id}:{identity.worker_id}",
            )

    async def _worker_loop(self, fleet: _Fleet, binding: _WorkerBinding) -> None:
        binding.started_at = _utc_now()
        binding.heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(fleet, binding),
            name=f"buyer-discovery-heartbeat:{fleet.run_id}:{binding.identity.worker_id}",
        )
        await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.RUNNING)
        await self._emit(fleet, "worker.started", _worker_event_payload(binding))
        try:
            while not binding.stop_event.is_set():
                if not fleet.resume_event.is_set():
                    await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.PAUSED)
                    await self._refresh_pause_state(fleet)
                    await fleet.resume_event.wait()
                    continue

                if not await self._claim_discovery_slot(fleet, binding):
                    await self._wait_attachment_release(binding)
                    continue
                was_idle = binding.state is BuyerDiscoveryWorkerState.IDLE
                try:
                    result = await binding.worker.run_once()
                finally:
                    await self._release_discovery_slot(fleet, binding)
                binding.last_outcome = result.outcome
                if result.outcome is BuyerDiscoveryOutcome.IDLE:
                    if not was_idle:
                        await self._emit_worker_result(fleet, binding, result)
                        await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.IDLE)
                else:
                    if was_idle:
                        await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.RUNNING)
                    await self._emit_worker_result(fleet, binding, result)
                if (
                    result.outcome is BuyerDiscoveryOutcome.RETRY
                    and result.failure_kind in {BuyerDiscoveryFailureKind.HTTP_403, BuyerDiscoveryFailureKind.HTTP_429}
                ):
                    if result.failure_kind is BuyerDiscoveryFailureKind.HTTP_429:
                        await self._apply_adaptive_throttle(fleet, binding, result)
                    await self._emit(
                        fleet,
                        "identity.quarantine_requested",
                        {
                            **_worker_event_payload(binding),
                            "task_id": result.task_id,
                            "endpoint": result.endpoint,
                            "failure_kind": result.failure_kind.value,
                            "quarantine_until": result.quarantine_until,
                        },
                    )
                    binding.rebind_reason = result.failure_kind.value
                    binding.last_error = result.error
                    binding.stop_event.set()
                    binding.wake_event.set()
                    await self._emit(
                        fleet,
                        "identity.rebind_requested",
                        {**_worker_event_payload(binding), "reason": binding.rebind_reason, "task_id": result.task_id},
                    )
                    continue
                if result.outcome is BuyerDiscoveryOutcome.IDLE:
                    await self._wait_idle(binding)
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - stop just this identity and let reconcile replace it.
            binding.last_error = str(exc)
            await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.FAILED)
            await self._emit(fleet, "worker.failed", {**_worker_event_payload(binding), "error": str(exc)})
        finally:
            if binding.heartbeat_task is not None:
                binding.heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await binding.heartbeat_task
            if binding.lease_lost:
                await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.LEASE_LOST)
            else:
                await self._set_worker_state(fleet, binding, BuyerDiscoveryWorkerState.STOPPED)
            await self._release_binding(fleet, binding)
            await self._refresh_pause_state(fleet)
            await self._mark_stopped_if_drained(fleet)
            self._reconcile_wake.set()

    async def _heartbeat_loop(self, fleet: _Fleet, binding: _WorkerBinding) -> None:
        while not binding.stop_event.is_set():
            try:
                await asyncio.wait_for(binding.stop_event.wait(), timeout=self.heartbeat_interval_seconds)
                return
            except TimeoutError:
                pass
            if binding.stop_event.is_set():
                return
            binding.last_heartbeat_at = _utc_now()
            binding.heartbeat_ticks += 1
            payload = _worker_event_payload(binding)
            hook_result: Any = None
            if self.heartbeat_hook is not None:
                hook_result = await _await_value(self.heartbeat_hook(fleet.run_id, binding.identity, payload))
            if binding.heartbeat_ticks == 1 or binding.heartbeat_ticks % 6 == 0:
                await self._emit(fleet, "worker.heartbeat", payload)
            if hook_result is False:
                binding.lease_lost = True
                binding.stop_event.set()
                binding.wake_event.set()
                await self._emit(fleet, "worker.lease_lost", _worker_event_payload(binding))
                task = binding.task
                if task is not None and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                return

    async def _wait_idle(self, binding: _WorkerBinding) -> None:
        if binding.stop_event.is_set():
            return
        wake_task = asyncio.create_task(binding.wake_event.wait())
        stop_task = asyncio.create_task(binding.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {wake_task, stop_task},
                timeout=self.idle_poll_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wake_task in done:
                binding.wake_event.clear()
        finally:
            for task in (wake_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, stop_task, return_exceptions=True)

    async def _claim_discovery_slot(self, fleet: _Fleet, binding: _WorkerBinding) -> bool:
        """Claim the worker's account/route unless attachment enrichment owns it."""

        async with fleet.lock:
            if binding.stop_event.is_set() or binding.attachment_reservation_token is not None:
                return False
            binding.busy = True
            return True

    async def _release_discovery_slot(self, fleet: _Fleet, binding: _WorkerBinding) -> None:
        async with fleet.lock:
            binding.busy = False

    async def _wait_attachment_release(self, binding: _WorkerBinding) -> None:
        """Wait for an attachment reservation without polling or leasing work."""

        if binding.stop_event.is_set():
            return
        await binding.attachment_release_event.wait()

    async def _release_binding(self, fleet: _Fleet, binding: _WorkerBinding) -> None:
        if binding.released:
            return
        binding.released = True
        reason = "heartbeat_lost" if binding.lease_lost else (binding.rebind_reason or "worker_stopped")
        await self._release_identity(fleet, binding.identity, reason)
        async with fleet.lock:
            fleet.workers.pop(binding.identity.worker_id, None)
        await self._emit(fleet, "identity.released", {**_worker_event_payload(binding), "reason": reason})

    async def _release_identity(self, fleet: _Fleet, identity: BuyerDiscoveryIdentity, reason: str) -> None:
        await _await_value(self.identity_releaser.release(run_id=fleet.run_id, identity=identity, reason=reason))

    async def _set_worker_state(
        self,
        fleet: _Fleet,
        binding: _WorkerBinding,
        state: BuyerDiscoveryWorkerState,
    ) -> None:
        if binding.state is state:
            return
        binding.state = state
        await self._emit(fleet, "worker.state", _worker_event_payload(binding))

    async def _emit_worker_result(
        self,
        fleet: _Fleet,
        binding: _WorkerBinding,
        result: BuyerDiscoveryRunResult,
    ) -> None:
        # Non-idle outcomes are already persisted by BuyerDiscoveryWorker with
        # the same task/worker identity.  Emitting them here doubled every
        # durable page event and needlessly churned SQLite and WebSocket state.
        if result.outcome is not BuyerDiscoveryOutcome.IDLE:
            if self.result_hook is not None:
                try:
                    await _await_value(self.result_hook(fleet.run_id, binding.identity, result))
                except Exception:  # noqa: BLE001 - result notification cannot kill a read-only worker.
                    pass
            return
        event_type = {
            BuyerDiscoveryOutcome.IDLE: "worker.idle",
        }[result.outcome]
        payload = {
            **_worker_event_payload(binding),
            "task_id": result.task_id,
            "project_count": result.project_count,
            "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
            "retry_after_seconds": result.retry_after_seconds,
            "error": result.error,
            "endpoint": result.endpoint,
            "quarantine_until": result.quarantine_until,
        }
        await self._emit(fleet, event_type, payload)

    async def _refresh_pause_state(self, fleet: _Fleet) -> None:
        if fleet.state is not BuyerDiscoveryFleetState.PAUSING:
            return
        if any(binding.busy for binding in fleet.workers.values()):
            return
        if any(binding.state is not BuyerDiscoveryWorkerState.PAUSED for binding in fleet.workers.values()):
            return
        fleet.state = BuyerDiscoveryFleetState.PAUSED
        await self._emit(fleet, "run.paused", self.snapshot(fleet.run_id).to_payload())

    async def _mark_stopped_if_drained(self, fleet: _Fleet) -> None:
        if fleet.state is not BuyerDiscoveryFleetState.STOPPING:
            return
        if fleet.workers:
            return
        fleet.state = BuyerDiscoveryFleetState.STOPPED
        await self._emit(fleet, "run.stopped", self.snapshot(fleet.run_id).to_payload())

    async def _inspect_candidates(self, run_id: str, requested_workers: int) -> tuple[BuyerDiscoveryIdentity, ...]:
        if self.capacity_inventory is None:
            fleet = self._fleets.get(run_id)
            if fleet is None:
                return ()
            return tuple(binding.identity for binding in fleet.workers.values() if not binding.released)
        inspected = await _await_value(
            self.capacity_inventory.inspect(run_id=run_id, requested_workers=requested_workers)
        )
        return _identity_sequence(inspected, "capacity_inventory.inspect")

    async def _emit(self, fleet: _Fleet, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.event_hook is None:
            return
        try:
            await _await_value(self.event_hook(fleet.run_id, event_type, dict(payload)))
        except Exception:  # noqa: BLE001 - observability cannot kill a read-only fleet.
            return

    def _get_fleet(self, run_id: str) -> _Fleet:
        normalized = _required_text(run_id, "run_id")
        try:
            return self._fleets[normalized]
        except KeyError as exc:
            raise BuyerDiscoverySupervisorError(f"Buyer discovery fleet {normalized!r} does not exist") from exc

    def _validate_requested_workers(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.max_workers:
            raise ValueError(f"requested_workers must be an integer between 1 and {self.max_workers}")
        return value

    def _validate_canary_cap(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.max_workers:
            raise ValueError(f"canary_cap must be between 1 and configured max_workers={self.max_workers}")
        if value != self.max_workers and value not in CANARY_WORKER_CAPS:
            raise ValueError(f"canary_cap must be one of {CANARY_WORKER_CAPS} or configured max_workers")
        return value

    def _ensure_reconciler(self) -> None:
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(),
                name="buyer-discovery-supervisor",
            )

    async def _reconcile_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    await asyncio.wait_for(self._reconcile_wake.wait(), timeout=self.reconcile_interval_seconds)
                except TimeoutError:
                    pass
                self._reconcile_wake.clear()
                for run_id in tuple(self.run_ids):
                    fleet = self._fleets.get(run_id)
                    if fleet is None or fleet.state is not BuyerDiscoveryFleetState.RUNNING:
                        continue
                    with suppress(BuyerDiscoverySupervisorError):
                        await self.reconcile(run_id)
        except asyncio.CancelledError:
            raise


def _build_preflight(
    run_id: str,
    requested_workers: int,
    canary_cap: int,
    candidates: Sequence[BuyerDiscoveryIdentity],
) -> BuyerDiscoveryCapacityPreflight:
    identities = _identity_sequence(candidates, "capacity candidates")
    selected, _ = _select_new_identities((), identities, limit=min(requested_workers, canary_cap))
    eligible_accounts = len({identity.account_registration_id.casefold() for identity in identities})
    healthy_transports = len({identity.transport_id.casefold() for identity in identities})
    transports_with_fresh_verified_egress = len(
        {identity.transport_id.casefold() for identity in identities if identity.egress_ip.strip()}
    )
    unique_egress_ips = len({identity.egress_ip.casefold() for identity in identities if identity.egress_ip.strip()})
    limit = min(requested_workers, canary_cap)
    reasons: list[str] = []
    if requested_workers > canary_cap:
        reasons.append("canary_cap")
    if len(selected) < limit:
        if eligible_accounts < limit:
            reasons.append("unique_account_capacity")
        if healthy_transports < limit:
            reasons.append("unique_transport_capacity")
        if unique_egress_ips < limit:
            reasons.append("unique_egress_capacity")
        if len(identities) > len(selected):
            reasons.append("identity_collisions")
    return BuyerDiscoveryCapacityPreflight(
        run_id=run_id,
        requested_workers=requested_workers,
        canary_cap=canary_cap,
        candidate_count=len(identities),
        eligible_accounts=eligible_accounts,
        healthy_transports=healthy_transports,
        transports_with_fresh_verified_egress=transports_with_fresh_verified_egress,
        unique_egress_ips=unique_egress_ips,
        collision_count=max(0, len(identities) - len(selected)),
        effective_capacity=len(selected),
        selected_identities=tuple(selected),
        reasons=tuple(reasons),
    )


def _select_new_identities(
    active: Sequence[BuyerDiscoveryIdentity],
    candidates: Sequence[BuyerDiscoveryIdentity],
    *,
    limit: int,
) -> tuple[tuple[BuyerDiscoveryIdentity, ...], tuple[BuyerDiscoveryIdentity, ...]]:
    """Select a deterministic safe subset and return every rejected candidate.

    The capacity is deliberately conservative: no active or selected identity
    may share an account, transport, egress address, or worker id.  Candidate
    order comes from the allocator, making an adapter's priority policy visible
    and reproducible instead of being hidden in the supervisor.
    """

    account_ids = {identity.account_registration_id.casefold() for identity in active}
    transport_ids = {identity.transport_id.casefold() for identity in active}
    egress_ips = {identity.egress_ip.casefold() for identity in active}
    worker_ids = {identity.worker_id.casefold() for identity in active}
    accepted: list[BuyerDiscoveryIdentity] = []
    rejected: list[BuyerDiscoveryIdentity] = []
    for identity in candidates:
        duplicate = (
            identity.account_registration_id.casefold() in account_ids
            or identity.transport_id.casefold() in transport_ids
            or identity.egress_ip.casefold() in egress_ips
            or identity.worker_id.casefold() in worker_ids
        )
        if duplicate or len(accepted) >= limit:
            rejected.append(identity)
            continue
        accepted.append(identity)
        account_ids.add(identity.account_registration_id.casefold())
        transport_ids.add(identity.transport_id.casefold())
        egress_ips.add(identity.egress_ip.casefold())
        worker_ids.add(identity.worker_id.casefold())
    return tuple(accepted), tuple(rejected)


def _identity_key(identity: BuyerDiscoveryIdentity) -> tuple[str, str, str]:
    return (
        identity.account_registration_id.casefold(),
        identity.transport_id.casefold(),
        identity.egress_ip.casefold(),
    )


def _identity_sequence(value: Sequence[BuyerDiscoveryIdentity], source: str) -> tuple[BuyerDiscoveryIdentity, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{source} must return a sequence of BuyerDiscoveryIdentity")
    identities = tuple(value)
    if not all(isinstance(identity, BuyerDiscoveryIdentity) for identity in identities):
        raise TypeError(f"{source} must return BuyerDiscoveryIdentity values")
    return identities


async def _await_value(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _worker_event_payload(binding: _WorkerBinding) -> dict[str, Any]:
    return {
        "worker_id": binding.identity.worker_id,
        "account_registration_id": binding.identity.account_registration_id,
        "transport_id": binding.identity.transport_id,
        "egress_ip": binding.identity.egress_ip,
        "route_generation": binding.identity.route_generation,
        "state": binding.state.value,
        "last_outcome": binding.last_outcome.value if binding.last_outcome is not None else None,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_after_seconds(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_sort_key(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # All production timestamps are UTC ISO-8601.  An unexpected value
        # remains conservatively far in the future instead of bypassing a
        # cooldown due to malformed adapter data.
        return datetime.max.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_is_in_future(value: str) -> bool:
    return _timestamp_sort_key(value) > datetime.now(UTC)


__all__ = [
    "CANARY_WORKER_CAPS",
    "BuyerDiscoveryAttachmentReservation",
    "BuyerDiscoveryCapacityInventory",
    "BuyerDiscoveryCapacityMode",
    "BuyerDiscoveryCapacityPreflight",
    "BuyerDiscoveryEventHook",
    "BuyerDiscoveryFleetSnapshot",
    "BuyerDiscoveryFleetState",
    "BuyerDiscoveryHeartbeatHook",
    "BuyerDiscoveryIdentityAllocator",
    "BuyerDiscoveryIdentityReleaser",
    "BuyerDiscoverySourceFactory",
    "BuyerDiscoverySupervisor",
    "BuyerDiscoverySupervisorError",
    "BuyerDiscoveryWorkerSnapshot",
    "BuyerDiscoveryWorkerState",
]
