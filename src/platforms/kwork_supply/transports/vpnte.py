"""Slot-aware VPNTE transport management for market workers.

This module never derives a proxy from a port range.  The VPNTE control plane
is authoritative: a slot becomes usable only after it reports both ``running``
and an explicit ``proxyUrl``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from src.utils.vpnte_proxy import VpnteProxyClient

from ..models import NetworkPolicy, TransportHealth, TransportKind, TransportSnapshot
from .base import TransportLeaseError, TransportUnavailableError


MIN_VPNTE_SLOT = 1
# Slots are discovered from VPNTE /instances; no artificial upper bound.
MAX_VPNTE_SLOT: int | None = None
VPNTE_TRANSPORT_PREFIX = "vpnte-slot-"


@runtime_checkable
class VpnteTransportProvider(Protocol):
    """The slot-aware portion of :class:`VpnteProxyClient` used here."""

    def instances(self) -> list[dict[str, Any]]:
        """Return the current VPNTE instance records from ``/instances``."""

    def status(self, slot: int) -> dict[str, Any]:
        """Return the current state of one VPNTE slot."""

    def start(
        self,
        slot: int,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        """Start one VPNTE slot."""

    def rotate(
        self,
        slot: int,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        """Rotate one VPNTE slot."""

    def stop(self, slot: int) -> dict[str, Any]:
        """Stop one VPNTE slot."""


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _value(instance: Mapping[str, object], camel_name: str, snake_name: str) -> object:
    return instance.get(camel_name, instance.get(snake_name))


def _transport_id(slot: int) -> str:
    return f"{VPNTE_TRANSPORT_PREFIX}{slot}"


def _quarantine_is_active(until: str | None) -> bool:
    """Keep indefinite/invalid quarantines, but release valid expired ones."""

    if until is None:
        return True
    try:
        expires_at = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


class VpnteTransportManager:
    """Manage VPNTE slot snapshots and exclusive in-process worker leases."""

    def __init__(self, provider: VpnteTransportProvider | None = None) -> None:
        self._provider: VpnteTransportProvider = provider or VpnteProxyClient()
        self._snapshots: dict[str, TransportSnapshot] = {}

    def refresh(self) -> list[TransportSnapshot]:
        """Poll ``/instances`` and preserve local generation and lease state."""

        previous = self._snapshots
        refreshed: dict[str, TransportSnapshot] = {}
        for instance in self._provider.instances():
            if not isinstance(instance, Mapping):
                continue
            snapshot = self._snapshot_from_instance(instance, previous=previous)
            if snapshot is not None:
                refreshed[snapshot.transport_id] = snapshot

        # Keep disappeared slots as stopped records so durable state cannot
        # overcount a stale healthy route. Probe each transition once for
        # diagnostics; a later refresh of an already-stopped slot is cheap.
        for transport_id, snapshot in previous.items():
            if transport_id not in refreshed:
                if snapshot.health is TransportHealth.STOPPED:
                    refreshed[transport_id] = snapshot
                    continue
                try:
                    if snapshot.slot is not None:
                        self._provider.status(snapshot.slot)
                except Exception:
                    pass
                refreshed[transport_id] = replace(snapshot, health=TransportHealth.STOPPED, proxy_url=None)

        self._snapshots = refreshed
        return self._ordered_snapshots()

    def acquire(
        self,
        worker_id: str,
        *,
        policy: NetworkPolicy | str = NetworkPolicy.VPNTE_ONLY,
        preferred_slot: int | None = None,
    ) -> TransportSnapshot | None:
        """Lease one healthy VPNTE slot to ``worker_id``.

        A direct transport is intentionally not synthesized for a VPNTE
        failure.  ``None`` therefore means that the requested route is not
        currently available.
        """

        normalized_worker_id = _optional_text(worker_id)
        if normalized_worker_id is None:
            raise ValueError("worker_id is required")
        normalized_policy = self._normalize_policy(policy)
        if normalized_policy is NetworkPolicy.DIRECT_ONLY:
            return None

        self.refresh()
        candidate = self._available_slot(preferred_slot, health=TransportHealth.HEALTHY)
        if candidate is None:
            candidate = self._first_available(TransportHealth.HEALTHY)
        if candidate is None:
            stopped = self._available_slot(preferred_slot, health=TransportHealth.STOPPED)
            if stopped is None:
                stopped = self._first_available(TransportHealth.STOPPED)
            if stopped is not None:
                candidate = self._start_slot(stopped.slot)

        if candidate is None or candidate.health is not TransportHealth.HEALTHY:
            return None

        leased = replace(candidate, lease_owner=normalized_worker_id)
        self._snapshots[leased.transport_id] = leased
        return leased

    def acquire_slot(
        self,
        worker_id: str,
        slot: int,
        *,
        policy: NetworkPolicy | str = NetworkPolicy.VPNTE_ONLY,
        refresh: bool = True,
    ) -> TransportSnapshot | None:
        """Lease one exact VPNTE slot when it is healthy and currently free."""

        normalized_worker_id = _optional_text(worker_id)
        if normalized_worker_id is None:
            raise ValueError("worker_id is required")
        normalized_policy = self._normalize_policy(policy)
        if normalized_policy is NetworkPolicy.DIRECT_ONLY:
            return None
        normalized_slot = self._normalize_slot(slot)
        if refresh:
            self.refresh()
        snapshot = self.get(_transport_id(normalized_slot))
        if snapshot is None:
            return None
        if snapshot.health is TransportHealth.STOPPED:
            snapshot = self._start_slot(normalized_slot)
        if (
            snapshot.health is not TransportHealth.HEALTHY
            or (snapshot.lease_owner is not None and snapshot.lease_owner != normalized_worker_id)
        ):
            return None
        leased = replace(snapshot, lease_owner=normalized_worker_id)
        self._snapshots[leased.transport_id] = leased
        return leased

    def release(self, transport_id: str, worker_id: str) -> None:
        """Release an exclusive lease; another worker cannot release it."""

        snapshot = self._require_snapshot(transport_id)
        normalized_worker_id = _optional_text(worker_id)
        if normalized_worker_id is None:
            raise ValueError("worker_id is required")
        if snapshot.lease_owner != normalized_worker_id:
            raise TransportLeaseError(f"transport {transport_id!r} is not leased by {worker_id!r}")
        self._snapshots[snapshot.transport_id] = replace(snapshot, lease_owner=None)

    def release_all(self, worker_id: str) -> list[TransportSnapshot]:
        """Release every local slot owned by one worker, including stale duplicates."""

        normalized_worker_id = _optional_text(worker_id)
        if normalized_worker_id is None:
            raise ValueError("worker_id is required")
        released: list[TransportSnapshot] = []
        for transport_id, snapshot in tuple(self._snapshots.items()):
            if snapshot.lease_owner != normalized_worker_id:
                continue
            cleared = replace(snapshot, lease_owner=None)
            self._snapshots[transport_id] = cleared
            released.append(cleared)
        return released

    def rotate(
        self,
        transport_id: str,
        worker_id: str,
        *,
        country: str | None = None,
    ) -> TransportSnapshot:
        """Rotate precisely one leased VPNTE slot.

        A generation advances only after VPNTE returns a running proxy URL.
        An invalid rotate response remains visible as degraded and is never
        replaced by a direct or guessed proxy URL.
        """

        snapshot = self._require_snapshot(transport_id)
        normalized_worker_id = _optional_text(worker_id)
        if normalized_worker_id is None:
            raise ValueError("worker_id is required")
        if snapshot.lease_owner != normalized_worker_id:
            raise TransportLeaseError(f"transport {transport_id!r} is not leased by {worker_id!r}")
        if snapshot.slot is None:
            raise TransportUnavailableError(f"transport {transport_id!r} has no VPNTE slot")

        self._provider.rotate(snapshot.slot, country=country)
        instance = next(
            (
                item
                for item in self._provider.instances()
                if isinstance(item, Mapping) and _optional_int(item.get("slot")) == snapshot.slot
            ),
            None,
        )
        if instance is None:
            raise TransportUnavailableError(f"VPNTE slot {snapshot.slot} disappeared after rotate")
        rotated = self._snapshot_from_instance(
            instance,
            previous=self._snapshots,
            fallback_slot=snapshot.slot,
            preserve_quarantine=False,
        )
        if rotated is None or rotated.slot != snapshot.slot:
            raise TransportUnavailableError(f"VPNTE rotate response omitted slot {snapshot.slot}")

        generation = snapshot.generation + 1 if rotated.health is TransportHealth.HEALTHY else snapshot.generation
        reason = f"country:{country}" if _optional_text(country) is not None else "manual_rotate"
        rotated = replace(
            rotated,
            generation=generation,
            lease_owner=snapshot.lease_owner,
            quarantine_until=None if rotated.health is TransportHealth.HEALTHY else rotated.quarantine_until,
            last_rotate_reason=reason,
            egress_ip=None,
            egress_checked_at=None,
        )
        self._snapshots[rotated.transport_id] = rotated
        return rotated

    def get(self, transport_id: str) -> TransportSnapshot | None:
        """Return the latest local snapshot without polling VPNTE."""

        return self._snapshots.get(transport_id)

    def health(self, transport_id: str) -> TransportHealth:
        """Return ``UNKNOWN`` for a slot that has not been discovered yet."""

        snapshot = self.get(transport_id)
        return snapshot.health if snapshot is not None else TransportHealth.UNKNOWN

    def record_egress(self, transport_id: str, egress_ip: str | None, *, checked_at: str | None = None) -> TransportSnapshot:
        """Attach a verified external IP observation to a local slot snapshot."""

        snapshot = self._require_snapshot(transport_id)
        updated = replace(
            snapshot,
            egress_ip=_optional_text(egress_ip),
            egress_checked_at=_optional_text(checked_at),
        )
        self._snapshots[transport_id] = updated
        return updated

    def quarantine(
        self,
        transport_id: str,
        *,
        reason: str,
        until: str | None = None,
    ) -> TransportSnapshot:
        """Keep an unhealthy route visible but unavailable for new leases."""

        snapshot = self._require_snapshot(transport_id)
        quarantined = replace(
            snapshot,
            health=TransportHealth.QUARANTINED,
            quarantine_until=_optional_text(until),
            last_rotate_reason=_optional_text(reason),
        )
        self._snapshots[transport_id] = quarantined
        return quarantined

    def clear_quarantine(self, transport_id: str) -> TransportSnapshot:
        """Remove a local quarantine and poll VPNTE for the actual health."""

        snapshot = self._require_snapshot(transport_id)
        if snapshot.health is not TransportHealth.QUARANTINED:
            return snapshot
        self._snapshots[transport_id] = replace(snapshot, health=TransportHealth.UNKNOWN, quarantine_until=None)
        self.refresh()
        return self._require_snapshot(transport_id)

    def ensure_slot(self, slot: int) -> TransportSnapshot:
        """Start a discovered stopped slot and return its latest snapshot."""

        normalized_slot = self._normalize_slot(slot)
        self.refresh()
        snapshot = self.get(_transport_id(normalized_slot))
        if snapshot is None:
            raise TransportUnavailableError(f"VPNTE slot {normalized_slot} was not returned by /instances")
        if snapshot.health is TransportHealth.STOPPED:
            return self._start_slot(normalized_slot)
        return snapshot

    def close(self) -> None:
        """Close a provider only when it exposes an explicit close hook."""

        close = getattr(self._provider, "close", None)
        if callable(close):
            close()

    def _start_slot(self, slot: int | None) -> TransportSnapshot:
        if slot is None:
            raise TransportUnavailableError("VPNTE slot is required")
        self._provider.start(slot)
        instance = next(
            (
                item
                for item in self._provider.instances()
                if isinstance(item, Mapping) and _optional_int(item.get("slot")) == slot
            ),
            None,
        )
        started = self._snapshot_from_instance(instance, previous=self._snapshots) if instance is not None else None
        if started is None or started.slot != slot:
            raise TransportUnavailableError(f"VPNTE start response omitted slot {slot}")
        self._snapshots[started.transport_id] = started
        return started

    def _first_available(self, health: TransportHealth) -> TransportSnapshot | None:
        return next(
            (
                snapshot
                for snapshot in self._ordered_snapshots()
                if snapshot.health is health and snapshot.lease_owner is None
            ),
            None,
        )

    def _available_slot(self, slot: int | None, *, health: TransportHealth) -> TransportSnapshot | None:
        if slot is None:
            return None
        try:
            snapshot = self.get(_transport_id(self._normalize_slot(slot)))
        except ValueError:
            return None
        if snapshot is None or snapshot.health is not health or snapshot.lease_owner is not None:
            return None
        return snapshot

    def _snapshot_from_instance(
        self,
        instance: Mapping[str, object],
        *,
        previous: Mapping[str, TransportSnapshot],
        fallback_slot: int | None = None,
        preserve_quarantine: bool = True,
    ) -> TransportSnapshot | None:
        slot = _optional_int(instance.get("slot"))
        if slot is None:
            slot = fallback_slot
        if slot is None or slot < MIN_VPNTE_SLOT:
            return None

        transport_id = _transport_id(slot)
        prior = previous.get(transport_id)
        proxy_url = _optional_text(_value(instance, "proxyUrl", "proxy_url"))
        running = _optional_bool(instance.get("running"))
        health = self._health_for(proxy_url, running=running)
        quarantine_expired = False
        if preserve_quarantine and prior is not None and prior.health is TransportHealth.QUARANTINED:
            quarantine_expired = not _quarantine_is_active(prior.quarantine_until)
        if preserve_quarantine and prior is not None and prior.health is TransportHealth.QUARANTINED and not quarantine_expired:
            health = TransportHealth.QUARANTINED

        return TransportSnapshot(
            transport_id=transport_id,
            kind=TransportKind.VPNTE,
            health=health,
            slot=slot,
            proxy_url=proxy_url,
            profile_id=_optional_text(_value(instance, "profileId", "profile_id"))
            or (prior.profile_id if prior is not None else None),
            profile_name=_optional_text(_value(instance, "profileName", "profile_name"))
            or (prior.profile_name if prior is not None else None),
            country=_optional_text(instance.get("country")) or (prior.country if prior is not None else None),
            pid=_optional_int(instance.get("pid"))
            if instance.get("pid") is not None
            else (prior.pid if prior else None),
            generation=prior.generation if prior is not None else 1,
            lease_owner=prior.lease_owner if prior is not None else None,
            quarantine_until=prior.quarantine_until if prior is not None and not quarantine_expired else None,
            last_rotate_reason=prior.last_rotate_reason if prior is not None and not quarantine_expired else None,
            egress_ip=prior.egress_ip if prior is not None else None,
            egress_checked_at=prior.egress_checked_at if prior is not None else None,
        )

    @staticmethod
    def _health_for(proxy_url: str | None, *, running: bool | None) -> TransportHealth:
        if running is True:
            return TransportHealth.HEALTHY if proxy_url is not None else TransportHealth.DEGRADED
        if running is False:
            return TransportHealth.STOPPED
        return TransportHealth.UNKNOWN

    @staticmethod
    def _normalize_slot(slot: int) -> int:
        normalized_slot = _optional_int(slot)
        if normalized_slot is None or normalized_slot < MIN_VPNTE_SLOT:
            raise ValueError(f"VPNTE slot must be an integer >= {MIN_VPNTE_SLOT}")
        return normalized_slot

    @staticmethod
    def _normalize_policy(policy: NetworkPolicy | str) -> NetworkPolicy:
        try:
            return NetworkPolicy(policy)
        except ValueError as exc:
            raise ValueError(f"unknown network policy: {policy!r}") from exc

    def _ordered_snapshots(self) -> list[TransportSnapshot]:
        return sorted(
            self._snapshots.values(),
            key=lambda snapshot: (snapshot.slot is None, snapshot.slot or 0, snapshot.transport_id),
        )

    def _require_snapshot(self, transport_id: str) -> TransportSnapshot:
        snapshot = self.get(transport_id)
        if snapshot is None:
            raise TransportUnavailableError(f"unknown transport {transport_id!r}")
        return snapshot
