"""Account-scoped runtime adapters for Buyer Search discovery.

The Buyer discovery supervisor deliberately knows nothing about the existing
Market worker runtime.  This module is the narrow bridge between the two:

* :class:`MarketIdentityPoolDiscoveryAdapter` maps durable account/transport
  leases into Buyer discovery identities and renews or releases only leases it
  allocated itself;
* :class:`AccountBoundBuyerReadSourceFactory` builds a page source from an
  injected, account-bound client factory.  It never exposes a generic Kwork
  client to the Buyer worker.

Neither adapter imports a global Kwork client or changes VPNTE.  The caller
must explicitly provide the Market job associated with every Buyer run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import inspect
from typing import Any, Protocol, TypeAlias

from src.platforms.kwork_supply.identity_pool import MarketAccountContext, MarketIdentityLease, MarketIdentityPool

from .mapper import MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE
from .sources.capabilities import BuyerReadCapabilities
from .sources.read_pages import BuyerMobileProjectsPageSource, BuyerReadPageSourceError, BuyerWebProjectsPageSource
from .worker import BuyerDiscoveryIdentity, BuyerDiscoveryReadSource


class BuyerRuntimeAdapterError(RuntimeError):
    """Raised when a Buyer-to-Market runtime bridge cannot be used safely."""


class BuyerAccountContextUnavailableError(BuyerRuntimeAdapterError):
    """Raised when an allocated worker no longer has its account session."""


class BuyerAccountClientFactory(Protocol):
    """Build one account-bound read client for an already-leased identity."""

    def __call__(
        self,
        context: MarketAccountContext,
        identity: BuyerDiscoveryIdentity,
    ) -> object | Awaitable[object]:
        """Return a client exposing the explicit Buyer read methods."""


BuyerJobIdForRun: TypeAlias = Callable[[str], str | Awaitable[str]]
BuyerWorkerIdFactory: TypeAlias = Callable[[str, int], str]


@dataclass(frozen=True, slots=True)
class _LeaseBinding:
    """In-process ownership evidence for a Market identity lease."""

    run_id: str
    job_id: str
    identity: BuyerDiscoveryIdentity


class MarketIdentityPoolDiscoveryAdapter:
    """Adapt :class:`MarketIdentityPool` to Buyer discovery supervisor protocols.

    ``job_id_for_run`` is intentionally required.  Buyer run IDs are not Market
    job IDs, and silently guessing that relationship would let a discovery
    worker claim an unrelated account/route lease.
    """

    def __init__(
        self,
        identity_pool: MarketIdentityPool,
        *,
        job_id_for_run: BuyerJobIdForRun,
        worker_id_factory: BuyerWorkerIdFactory | None = None,
        worker_id_prefix: str = "buyer-discovery",
    ) -> None:
        for method_name in ("acquire", "release", "renew", "snapshot"):
            if not callable(getattr(identity_pool, method_name, None)):
                raise TypeError(f"identity_pool must expose {method_name}")
        if not callable(job_id_for_run):
            raise TypeError("job_id_for_run must be callable")
        if worker_id_factory is not None and not callable(worker_id_factory):
            raise TypeError("worker_id_factory must be callable")
        self.worker_id_prefix = _required_text(worker_id_prefix, "worker_id_prefix")
        self.identity_pool = identity_pool
        self.job_id_for_run = job_id_for_run
        self.worker_id_factory = worker_id_factory
        self._next_worker_ordinal: dict[str, int] = {}
        self._leases: dict[str, _LeaseBinding] = {}

    async def allocate(
        self,
        *,
        run_id: str,
        requested_workers: int,
        active_identities: Sequence[BuyerDiscoveryIdentity],
    ) -> tuple[BuyerDiscoveryIdentity, ...]:
        """Reserve up to ``requested_workers`` account/transport/egress triples."""

        normalized_run_id = _required_text(run_id, "run_id")
        requested = _positive_int(requested_workers, "requested_workers")
        active_worker_ids = {_identity_worker_id(identity) for identity in active_identities}
        job_id = await self._job_id(normalized_run_id)
        await self._reclaim_unowned_job_bindings(job_id)
        allocated: list[BuyerDiscoveryIdentity] = []
        reserved_worker_ids = set(active_worker_ids)

        for _ in range(requested):
            worker_id = self._new_worker_id(normalized_run_id, reserved_worker_ids)
            lease = await _await_value(self.identity_pool.acquire(worker_id, job_id))
            if lease is None:
                break
            try:
                identity = _identity_from_market_lease(lease, worker_id=worker_id, job_id=job_id)
            except Exception:
                await _await_value(self.identity_pool.release(worker_id, reason="buyer_discovery_invalid_lease"))
                raise
            self._leases[worker_id] = _LeaseBinding(
                run_id=normalized_run_id,
                job_id=job_id,
                identity=identity,
            )
            allocated.append(identity)
            reserved_worker_ids.add(worker_id)

        return tuple(allocated)

    async def release(
        self,
        *,
        run_id: str,
        identity: BuyerDiscoveryIdentity,
        reason: str,
    ) -> None:
        """Release only a lease allocated by this adapter for this Buyer run."""

        binding = self._owned_binding(run_id, identity)
        await _await_value(self.identity_pool.release(binding.identity.worker_id, reason=_required_text(reason, "reason")))
        self._leases.pop(binding.identity.worker_id, None)

    async def inspect(
        self,
        *,
        run_id: str,
        requested_workers: int,
    ) -> tuple[BuyerDiscoveryIdentity, ...]:
        """Return a read-only view of candidate account/route capacity.

        The Market pool performs route verification when assembling its
        snapshot.  Inventory identities are synthetic and are never passed to
        ``acquire``; they exist only for the supervisor's preflight math.
        """

        normalized_run_id = _required_text(run_id, "run_id")
        requested = _positive_int(requested_workers, "requested_workers")
        job_id = await self._job_id(normalized_run_id)
        await self._reclaim_unowned_job_bindings(job_id)
        snapshot = await _await_value(self.identity_pool.snapshot(job_id=job_id, refresh_routes=True))
        if not isinstance(snapshot, Mapping):
            raise BuyerRuntimeAdapterError("Market identity snapshot must be a mapping")

        retained = tuple(
            binding.identity
            for binding in self._leases.values()
            if binding.run_id == normalized_run_id and binding.job_id == job_id
        )
        return _inventory_identities(
            snapshot,
            run_id=normalized_run_id,
            worker_id_prefix=self.worker_id_prefix,
            retained=retained,
            requested_workers=requested,
        )

    async def heartbeat(
        self,
        run_id: str,
        identity: BuyerDiscoveryIdentity,
        _payload: Mapping[str, Any],
    ) -> bool:
        """Renew a known Market lease; unknown or reassigned leases fail closed."""

        try:
            binding = self._owned_binding(run_id, identity)
            if binding.job_id != await self._job_id(binding.run_id):
                return False
            renewed = await _await_value(self.identity_pool.renew(binding.identity.worker_id))
            return renewed is True
        except Exception:  # noqa: BLE001 - a heartbeat must fail closed without killing supervisor cleanup.
            return False

    def _owned_binding(self, run_id: str, identity: BuyerDiscoveryIdentity) -> _LeaseBinding:
        normalized_run_id = _required_text(run_id, "run_id")
        if not isinstance(identity, BuyerDiscoveryIdentity):
            raise TypeError("identity must be BuyerDiscoveryIdentity")
        binding = self._leases.get(identity.worker_id)
        if binding is None:
            raise BuyerRuntimeAdapterError("Buyer discovery identity was not allocated by this adapter")
        if binding.run_id != normalized_run_id or binding.identity != identity:
            raise BuyerRuntimeAdapterError("Buyer discovery identity does not belong to this run")
        return binding

    async def _job_id(self, run_id: str) -> str:
        job_id = await _await_value(self.job_id_for_run(run_id))
        return _required_text(job_id, "job_id_for_run result")

    async def _reclaim_unowned_job_bindings(self, job_id: str) -> None:
        reclaim_buyer = getattr(self.identity_pool, "reclaim_unowned_buyer_bindings", None)
        if callable(reclaim_buyer):
            await _await_value(reclaim_buyer())
            return
        reclaim = getattr(self.identity_pool, "reclaim_unowned_job_bindings", None)
        if callable(reclaim):
            await _await_value(reclaim(job_id))

    def _new_worker_id(self, run_id: str, reserved_worker_ids: set[str]) -> str:
        next_ordinal = self._next_worker_ordinal.get(run_id, 0)
        for _ in range(10_000):
            next_ordinal += 1
            raw_candidate = (
                self.worker_id_factory(run_id, next_ordinal)
                if self.worker_id_factory is not None
                else f"{self.worker_id_prefix}:{run_id}:{next_ordinal}"
            )
            candidate = _required_text(raw_candidate, "worker_id_factory result")
            if candidate not in reserved_worker_ids and candidate not in self._leases:
                self._next_worker_ordinal[run_id] = next_ordinal
                return candidate
        raise BuyerRuntimeAdapterError("worker_id_factory did not produce a free Buyer worker ID")


class AccountBoundBuyerReadSource:
    """Dispatch mobile and web reads without exposing the wrapped account client."""

    def __init__(self, client: object, *, identity: BuyerDiscoveryIdentity) -> None:
        self._mobile_source = BuyerMobileProjectsPageSource(
            BuyerReadCapabilities(client, identity.provenance(MOBILE_PROJECT_SOURCE))
        )
        self._web_source = BuyerWebProjectsPageSource(
            BuyerReadCapabilities(client, identity.provenance(WEB_PROJECT_SOURCE))
        )

    async def fetch_page(self, *, task: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
        """Route a leased task to its source-specific, read-only capability."""

        source = _task_source(task)
        if source == MOBILE_PROJECT_SOURCE:
            return await self._mobile_source.fetch_page(task=task, identity=identity)
        if source == WEB_PROJECT_SOURCE:
            return await self._web_source.fetch_page(task=task, identity=identity)
        raise BuyerReadPageSourceError(f"unsupported Buyer discovery source {source!r}")


class AccountBoundBuyerReadSourceFactory:
    """Build read-only Buyer sources from the account context of a leased worker."""

    def __init__(
        self,
        identity_pool: MarketIdentityPool,
        *,
        account_client_factory: BuyerAccountClientFactory,
    ) -> None:
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory

    async def create(self, *, run_id: str, identity: BuyerDiscoveryIdentity) -> BuyerDiscoveryReadSource:
        """Create a source tied to exactly the account reserved for ``identity``."""

        _required_text(run_id, "run_id")
        if not isinstance(identity, BuyerDiscoveryIdentity):
            raise TypeError("identity must be BuyerDiscoveryIdentity")
        context = await _await_value(self.identity_pool.context_for_worker(identity.worker_id))
        if context is None:
            raise BuyerAccountContextUnavailableError(
                f"Buyer worker {identity.worker_id!r} has no active account context"
            )
        registration_id = _required_text(getattr(context, "registration_id", None), "account context registration_id")
        if registration_id != identity.account_registration_id:
            raise BuyerAccountContextUnavailableError(
                "Buyer worker account context does not match its reserved account registration"
            )
        client = await _await_value(self.account_client_factory(context, identity))
        if client is None:
            raise BuyerRuntimeAdapterError("account_client_factory returned no client")
        return AccountBoundBuyerReadSource(client, identity=identity)


def _identity_from_market_lease(
    lease: MarketIdentityLease,
    *,
    worker_id: str,
    job_id: str,
) -> BuyerDiscoveryIdentity:
    if not isinstance(lease, MarketIdentityLease):
        raise BuyerRuntimeAdapterError("Market identity pool must return MarketIdentityLease or None")
    if lease.worker_id != worker_id:
        raise BuyerRuntimeAdapterError("Market identity lease worker_id does not match the requested Buyer worker")
    if lease.job_id != job_id:
        raise BuyerRuntimeAdapterError("Market identity lease job_id does not match job_id_for_run")
    return BuyerDiscoveryIdentity(
        worker_id=lease.worker_id,
        account_registration_id=lease.registration_id,
        transport_id=lease.transport_id,
        egress_ip=lease.egress_ip,
    )


def _inventory_identities(
    snapshot: Mapping[str, Any],
    *,
    run_id: str,
    worker_id_prefix: str,
    retained: Sequence[BuyerDiscoveryIdentity],
    requested_workers: int,
) -> tuple[BuyerDiscoveryIdentity, ...]:
    """Build deterministic synthetic preflight identities from pool inventory."""

    candidates = list(retained[:requested_workers])
    used_accounts = {identity.account_registration_id for identity in candidates}
    used_transports = {identity.transport_id for identity in candidates}
    used_egress_ips = {identity.egress_ip for identity in candidates}

    accounts = _eligible_inventory_accounts(snapshot.get("accounts"), used_accounts)
    routes = _eligible_inventory_routes(snapshot.get("routes"), used_transports, used_egress_ips)
    for index, (account_registration_id, route) in enumerate(zip(accounts, routes, strict=False), start=1):
        if len(candidates) >= requested_workers:
            break
        transport_id, egress_ip = route
        candidates.append(
            BuyerDiscoveryIdentity(
                worker_id=f"{worker_id_prefix}:inventory:{run_id}:{index}",
                account_registration_id=account_registration_id,
                transport_id=transport_id,
                egress_ip=egress_ip,
            )
        )
    return tuple(candidates)


def _eligible_inventory_accounts(value: Any, used_accounts: set[str]) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    candidates: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        registration_id = _optional_text(item.get("registration_id"))
        if registration_id is None or registration_id in used_accounts:
            continue
        if item.get("market_enabled") is not True:
            continue
        if _optional_text(item.get("status")) != "activated":
            continue
        if "selected_for_job" in item and item.get("selected_for_job") is not True:
            continue
        if _nonnegative_int(item.get("session_cookie_count")) <= 0:
            continue
        candidates.append(registration_id)
    return tuple(sorted(set(candidates)))


def _eligible_inventory_routes(
    value: Any,
    used_transports: set[str],
    used_egress_ips: set[str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    ranked: list[tuple[int, str, str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        transport_id = _optional_text(item.get("transport_id"))
        egress_ip = _optional_text(item.get("egress_ip"))
        if transport_id is None or egress_ip is None:
            continue
        if transport_id in used_transports or egress_ip in used_egress_ips:
            continue
        if _optional_text(item.get("health")) != "healthy" or _optional_text(item.get("lease_owner")) is not None:
            continue
        ranked.append((_nonnegative_int(item.get("slot")), transport_id, egress_ip, transport_id))
    selected: list[tuple[str, str]] = []
    seen_egress_ips = set(used_egress_ips)
    for _slot, _sort_transport_id, egress_ip, transport_id in sorted(ranked):
        if egress_ip in seen_egress_ips:
            continue
        seen_egress_ips.add(egress_ip)
        selected.append((transport_id, egress_ip))
    return tuple(selected)


def _identity_worker_id(identity: BuyerDiscoveryIdentity) -> str:
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise TypeError("active_identities must contain BuyerDiscoveryIdentity values")
    return identity.worker_id


def _task_source(task: Mapping[str, Any]) -> str:
    if not isinstance(task, Mapping):
        raise BuyerReadPageSourceError("leased task must be a mapping")
    raw_source = _optional_text(task.get("source"))
    aliases = {
        "mobile": MOBILE_PROJECT_SOURCE,
        MOBILE_PROJECT_SOURCE: MOBILE_PROJECT_SOURCE,
        "web": WEB_PROJECT_SOURCE,
        WEB_PROJECT_SOURCE: WEB_PROJECT_SOURCE,
    }
    try:
        return aliases[raw_source.casefold()] if raw_source is not None else ""
    except KeyError as exc:
        raise BuyerReadPageSourceError(f"unsupported Buyer discovery source {raw_source!r}") from exc


async def _await_value(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be blank")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return result


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


__all__ = [
    "AccountBoundBuyerReadSource",
    "AccountBoundBuyerReadSourceFactory",
    "BuyerAccountClientFactory",
    "BuyerAccountContextUnavailableError",
    "BuyerJobIdForRun",
    "BuyerRuntimeAdapterError",
    "BuyerWorkerIdFactory",
    "MarketIdentityPoolDiscoveryAdapter",
]
