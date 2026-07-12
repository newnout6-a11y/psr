"""Common contracts for managed Kwork market transports.

The transport manager deliberately owns the local lease state.  A worker may
use a proxy only after the manager has assigned it an exclusive snapshot.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import NetworkPolicy, TransportHealth, TransportSnapshot


class TransportError(RuntimeError):
    """Base error for transport-management failures."""


class TransportLeaseError(TransportError):
    """Raised when a worker attempts to use another worker's transport."""


class TransportUnavailableError(TransportError):
    """Raised when a requested managed transport does not exist."""


@runtime_checkable
class TransportManager(Protocol):
    """Minimal transport contract consumed by the worker supervisor."""

    def refresh(self) -> list[TransportSnapshot]:
        """Poll the provider and return the current managed snapshots."""

    def acquire(
        self,
        worker_id: str,
        *,
        policy: NetworkPolicy = NetworkPolicy.VPNTE_ONLY,
    ) -> TransportSnapshot | None:
        """Return an exclusively leased healthy transport, when one exists."""

    def release(self, transport_id: str, worker_id: str) -> None:
        """Release a transport lease held by ``worker_id``."""

    def rotate(
        self,
        transport_id: str,
        worker_id: str,
        *,
        country: str | None = None,
    ) -> TransportSnapshot:
        """Rotate one worker-owned transport and return its new snapshot."""

    def get(self, transport_id: str) -> TransportSnapshot | None:
        """Return one locally known snapshot."""

    def health(self, transport_id: str) -> TransportHealth:
        """Return the latest health state for one transport."""

    def close(self) -> None:
        """Release optional provider resources."""
