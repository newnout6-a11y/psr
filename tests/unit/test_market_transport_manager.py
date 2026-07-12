from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.platforms.kwork_supply.models import NetworkPolicy, TransportHealth
from src.platforms.kwork_supply.transports import TransportLeaseError, VpnteTransportManager


class FakeVpnteProvider:
    """Slot-aware VPNTE control fixture; it never reaches the real client."""

    def __init__(
        self,
        instances: list[dict[str, Any]],
        *,
        starts: dict[int, dict[str, Any]] | None = None,
        rotations: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self._instances = deepcopy(instances)
        self._starts = deepcopy(starts or {})
        self._rotations = deepcopy(rotations or {})
        self.instance_calls = 0
        self.start_calls: list[tuple[int, str | None]] = []
        self.rotate_calls: list[tuple[int, str | None]] = []

    def instances(self) -> list[dict[str, Any]]:
        self.instance_calls += 1
        return deepcopy(self._instances)

    def status(self, slot: int) -> dict[str, Any]:
        return deepcopy(next(instance for instance in self._instances if instance["slot"] == slot))

    def start(
        self,
        slot: int,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        del profile_id, port
        self.start_calls.append((slot, country))
        return deepcopy(self._starts[slot])

    def rotate(
        self,
        slot: int,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        del profile_id, port
        self.rotate_calls.append((slot, country))
        return deepcopy(self._rotations[slot])

    def stop(self, slot: int) -> dict[str, Any]:
        raise AssertionError(f"unexpected stop for slot {slot}")


def healthy_instance(slot: int, *, country: str = "Netherlands", pid: int = 1234) -> dict[str, Any]:
    return {
        "slot": slot,
        "running": True,
        "proxyUrl": f"http://127.0.0.1:{17989 + slot}",
        "profileId": f"profile-{slot}",
        "profileName": f"Profile {slot}",
        "country": country,
        "pid": pid,
    }


def test_refresh_maps_vpnte_instances_into_transport_snapshots():
    provider = FakeVpnteProvider([healthy_instance(3, country="Germany", pid=4321)])
    manager = VpnteTransportManager(provider)

    snapshots = manager.refresh()

    assert provider.instance_calls == 1
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.transport_id == "vpnte-slot-3"
    assert snapshot.health is TransportHealth.HEALTHY
    assert snapshot.slot == 3
    assert snapshot.proxy_url == "http://127.0.0.1:17992"
    assert snapshot.profile_id == "profile-3"
    assert snapshot.profile_name == "Profile 3"
    assert snapshot.country == "Germany"
    assert snapshot.pid == 4321
    assert manager.get("vpnte-slot-3") == snapshot


def test_acquire_starts_a_stopped_slot_and_keeps_lease_exclusive():
    provider = FakeVpnteProvider(
        [{"slot": 3, "running": False, "proxyUrl": None}],
        starts={3: healthy_instance(3)},
    )
    manager = VpnteTransportManager(provider)

    lease = manager.acquire("worker-01")

    assert provider.instance_calls == 1
    assert provider.start_calls == [(3, None)]
    assert lease is not None
    assert lease.transport_id == "vpnte-slot-3"
    assert lease.lease_owner == "worker-01"
    assert manager.acquire("worker-02") is None

    with pytest.raises(TransportLeaseError):
        manager.release("vpnte-slot-3", "worker-02")

    manager.release("vpnte-slot-3", "worker-01")
    assert manager.get("vpnte-slot-3").lease_owner is None


def test_running_slot_without_proxy_url_is_degraded_and_never_falls_back():
    provider = FakeVpnteProvider(
        [{"slot": 3, "running": False}],
        starts={3: {"slot": 3, "running": True, "profileId": "profile-3"}},
    )
    manager = VpnteTransportManager(provider)

    assert manager.acquire("worker-01", policy=NetworkPolicy.VPNTE_ONLY) is None

    snapshot = manager.get("vpnte-slot-3")
    assert snapshot is not None
    assert snapshot.health is TransportHealth.DEGRADED
    assert snapshot.proxy_url is None
    assert snapshot.lease_owner is None
    assert all(item.kind.value == "vpnte" for item in manager.refresh())

    direct_manager = VpnteTransportManager(provider)
    assert direct_manager.acquire("worker-02", policy=NetworkPolicy.DIRECT_ONLY) is None
    assert direct_manager.get("direct") is None


def test_rotate_changes_only_leased_slot_and_advances_generation():
    provider = FakeVpnteProvider(
        [healthy_instance(3), healthy_instance(4, country="France", pid=5678)],
        rotations={3: healthy_instance(3, country="Germany", pid=9999)},
    )
    manager = VpnteTransportManager(provider)
    lease = manager.acquire("worker-03")

    assert lease is not None
    rotated = manager.rotate(lease.transport_id, "worker-03", country="Germany")

    assert provider.rotate_calls == [(3, "Germany")]
    assert rotated.transport_id == "vpnte-slot-3"
    assert rotated.health is TransportHealth.HEALTHY
    assert rotated.generation == 2
    assert rotated.lease_owner == "worker-03"
    assert rotated.country == "Germany"
    assert rotated.pid == 9999
    assert rotated.last_rotate_reason == "country:Germany"
    untouched = manager.get("vpnte-slot-4")
    assert untouched is not None
    assert untouched.generation == 1
    assert untouched.country == "France"
    assert untouched.lease_owner is None


def test_refresh_releases_an_expired_quarantine():
    provider = FakeVpnteProvider([healthy_instance(3)])
    manager = VpnteTransportManager(provider)
    manager.refresh()
    manager.quarantine(
        "vpnte-slot-3",
        reason="http_403",
        until="2000-01-01T00:00:00Z",
    )

    [snapshot] = manager.refresh()

    assert snapshot.health is TransportHealth.HEALTHY
    assert snapshot.quarantine_until is None
    assert snapshot.last_rotate_reason is None
