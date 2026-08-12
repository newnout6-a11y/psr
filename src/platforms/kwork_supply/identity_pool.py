"""Durable account, VPNTE route and request-persona leases for Market workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import ipaddress
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

import httpx

from src.platforms.kwork_account_store import KworkAccountStore, StoredKworkAccount

from .models import JobKind, TransportHealth, TransportKind, TransportSnapshot, utc_now
from .repository import MarketJobRepository, MarketJobRepositoryError
from .transports.vpnte import VpnteTransportManager


ExitIpProbe = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class MarketAccountContext:
    """Private runtime data used to issue requests as one registered account."""

    registration_id: str
    username: str
    email: str
    password: str
    cookies: Mapping[str, str]
    headers: Mapping[str, str]
    persona_id: str
    signup_ip: str | None
    preferred_slot: int | None


@dataclass(frozen=True, slots=True)
class MarketIdentityLease:
    """Safe binding details that can be persisted and exposed to Market UI."""

    worker_id: str
    job_id: str
    registration_id: str
    username: str
    persona_id: str
    transport_id: str
    slot: int | None
    proxy_url: str | None
    egress_ip: str
    signup_ip: str | None
    preferred_slot: int | None
    binding_mode: str
    lease_token: str
    lease_deadline: str

    def public_data(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "job_id": self.job_id,
            "registration_id": self.registration_id,
            "username": self.username,
            "persona_id": self.persona_id,
            "transport_id": self.transport_id,
            "slot": self.slot,
            "proxy_url": self.proxy_url,
            "egress_ip": self.egress_ip,
            "signup_ip": self.signup_ip,
            "preferred_slot": self.preferred_slot,
            "binding_mode": self.binding_mode,
            "lease_deadline": self.lease_deadline,
        }


class MarketIdentityPool:
    """Assign one activated account and one distinct verified egress IP per worker."""

    def __init__(
        self,
        repository: MarketJobRepository,
        transport_manager: VpnteTransportManager,
        *,
        account_store: KworkAccountStore | None = None,
        exit_ip_probe: ExitIpProbe | None = None,
        lease_seconds: int = 90,
        probe_cache_seconds: float = 180.0,
        probe_concurrency: int = 24,
        route_refresh_cache_seconds: float = 3.0,
    ) -> None:
        if lease_seconds < 15:
            raise ValueError("lease_seconds must be at least 15")
        if probe_cache_seconds <= 0:
            raise ValueError("probe_cache_seconds must be positive")
        if probe_concurrency < 1:
            raise ValueError("probe_concurrency must be positive")
        if route_refresh_cache_seconds <= 0:
            raise ValueError("route_refresh_cache_seconds must be positive")
        self.repository = repository
        self.transport_manager = transport_manager
        self.account_store = account_store or KworkAccountStore()
        self.exit_ip_probe = exit_ip_probe or self._probe_exit_ip
        self.lease_seconds = lease_seconds
        self.probe_cache_seconds = probe_cache_seconds
        self.probe_concurrency = probe_concurrency
        self.route_refresh_cache_seconds = route_refresh_cache_seconds
        self._lock = asyncio.Lock()
        self._route_refresh_lock = asyncio.Lock()
        self._contexts: dict[str, MarketAccountContext] = {}
        self._leases: dict[str, MarketIdentityLease] = {}
        self._route_ip_cache: dict[str, tuple[str | None, float]] = {}
        self._route_snapshot_cache: dict[str, TransportSnapshot] = {}
        self._route_snapshot_cached_at = 0.0
        self._route_cursor = 0

    async def sync_inventory(self) -> list[dict[str, Any]]:
        """Mirror local registration metadata into the Market database without secrets."""

        records = self.account_store.list()
        inventory = [self._inventory_item(record) for record in records]
        return await self.repository.sync_market_account_inventory(inventory)

    async def capacity_for_job(self, job_id: str) -> int:
        """Return strict account-and-unique-IP capacity for one active Market job."""

        async with self._lock:
            inventory = await self.sync_inventory()
            routes = await self._refresh_routes()
            selected_account_ids = await self._selected_account_ids(job_id)
            bindings = await self.repository.list_market_identity_bindings(active_only=True)
        occupied_elsewhere_accounts = {
            str(item["registration_id"])
            for item in bindings
            if item["job_id"] != job_id and item["state"] == "active"
        }
        occupied_elsewhere_ips = {
            str(item["current_egress_ip"])
            for item in bindings
            if item["job_id"] != job_id and item.get("current_egress_ip")
        }
        eligible_accounts = {
            str(item["registration_id"])
            for item in inventory
            if item["market_enabled"]
            and item["status"] == "activated"
            and int(item["session_cookie_count"]) > 0
            and (not selected_account_ids or str(item["registration_id"]) in selected_account_ids)
        }
        available_accounts = len(eligible_accounts - occupied_elsewhere_accounts)
        available_ips = {
            str(route.egress_ip)
            for route in routes.values()
            if (
                route.health is TransportHealth.HEALTHY
                and route.egress_ip
                and route.egress_ip not in occupied_elsewhere_ips
            )
        }
        return min(available_accounts, len(available_ips))

    async def acquire(self, worker_id: str, job_id: str) -> MarketIdentityLease | None:
        """Lease an account and route, preferring the account's registration IP and slot."""

        async with self._lock:
            await self._ensure_worker_record(worker_id, job_id)
            inventory = await self.sync_inventory()
            routes = await self._refresh_routes()
            selected_account_ids = await self._selected_account_ids(job_id)
            bindings = await self.repository.list_market_identity_bindings(active_only=True)
            records = {record.registration_id: record for record in self.account_store.list()}
            existing = await self.repository.get_market_identity_binding(worker_id)
            enabled_inventory = [
                item
                for item in inventory
                if item["market_enabled"]
                and item["status"] == "activated"
                and int(item["session_cookie_count"]) > 0
                and (not selected_account_ids or str(item["registration_id"]) in selected_account_ids)
            ]
            enabled_ids = {
                str(item["registration_id"])
                for item in enabled_inventory
            }
            account_order = [str(item["registration_id"]) for item in enabled_inventory]
            candidates = self._account_candidates(worker_id, existing, bindings, records, enabled_ids, account_order)
            occupied_transport_ids = {
                str(binding["transport_id"])
                for binding in bindings
                if binding["worker_id"] != worker_id and binding.get("transport_id")
            }
            occupied_ips = {
                str(binding["current_egress_ip"])
                for binding in bindings
                if binding["worker_id"] != worker_id and binding.get("current_egress_ip")
            }
            for account in candidates:
                context = self._context_for(account, routes)
                for route, mode in self._route_candidates(account, routes, occupied_transport_ids, occupied_ips, existing):
                    snapshot = self.transport_manager.acquire_slot(worker_id, route.slot or 0, refresh=False)
                    if snapshot is None:
                        continue
                    verified = routes.get(snapshot.transport_id)
                    if verified is None or not verified.egress_ip:
                        await self._release_transport(snapshot.transport_id, worker_id)
                        continue
                    deadline = self._deadline()
                    token = uuid4().hex
                    try:
                        binding = await self.repository.claim_market_identity_binding(
                            worker_id=worker_id,
                            job_id=job_id,
                            registration_id=account.registration_id,
                            transport_id=snapshot.transport_id,
                            current_egress_ip=verified.egress_ip,
                            binding_mode=mode,
                            lease_token=token,
                            lease_deadline=deadline,
                        )
                    except MarketJobRepositoryError:
                        await self._release_transport(snapshot.transport_id, worker_id)
                        continue
                    lease = MarketIdentityLease(
                        worker_id=worker_id,
                        job_id=job_id,
                        registration_id=account.registration_id,
                        username=account.username,
                        persona_id=context.persona_id,
                        transport_id=snapshot.transport_id,
                        slot=snapshot.slot,
                        proxy_url=snapshot.proxy_url,
                        egress_ip=verified.egress_ip,
                        signup_ip=account.signup_ip,
                        preferred_slot=context.preferred_slot,
                        binding_mode=str(binding["binding_mode"]),
                        lease_token=token,
                        lease_deadline=deadline,
                    )
                    self._contexts[worker_id] = context
                    self._leases[worker_id] = lease
                    self._route_cursor = (snapshot.slot or 0) + 1
                    return lease
            return None

    async def release(self, worker_id: str, *, reason: str | None = None) -> None:
        """Release both the durable account binding and its managed VPNTE route."""

        async with self._lock:
            await self.repository.release_market_identity_binding(worker_id, reason=reason)
            await self._release_worker_transports(worker_id)
            self._contexts.pop(worker_id, None)
            self._leases.pop(worker_id, None)

    async def reclaim_unowned_job_bindings(self, job_id: str) -> int:
        """Release durable bindings left by an older process for one Buyer job.

        Buyer discovery owns its own in-process workers.  On an API restart,
        those worker objects no longer exist, while their short-lived durable
        identity leases may still be marked active.  Keeping them would make
        the next Buyer fleet look as if every account and VPNTE route were
        already occupied.
        """

        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            raise ValueError("job_id is required")

        async with self._lock:
            bindings = await self.repository.list_market_identity_bindings(
                job_id=normalized_job_id,
                active_only=True,
            )
            return await self._release_unowned_bindings(bindings)

    async def reclaim_unowned_buyer_bindings(self) -> int:
        """Release all orphaned Buyer Search bindings after an API restart.

        Buyer backing jobs intentionally stay active as lease containers, so a
        generic active-job cleanup cannot identify their stale workers.  Limit
        this recovery to the Buyer job kind and preserve every local lease that
        was already re-created by the current process.
        """

        async with self._lock:
            active_bindings = await self.repository.list_market_identity_bindings(active_only=True)
            job_kinds: dict[str, str] = {}
            buyer_bindings: list[Mapping[str, Any]] = []
            for binding in active_bindings:
                job_id = str(binding.get("job_id") or "").strip()
                if not job_id:
                    continue
                if job_id not in job_kinds:
                    job = await self.repository.get_job(job_id)
                    job_kinds[job_id] = str(job.get("job_kind") or JobKind.SUPPLY.value) if job else JobKind.SUPPLY.value
                if job_kinds[job_id] == JobKind.BUYER_SEARCH.value:
                    buyer_bindings.append(binding)
            return await self._release_unowned_bindings(buyer_bindings)

    async def release_inactive_job_bindings(self, active_job_ids: set[str]) -> int:
        """Release durable leases left behind by paused, terminal, or deleted jobs."""

        bindings = await self.repository.list_market_identity_bindings(active_only=True)
        stale_worker_ids = sorted(
            str(binding["worker_id"])
            for binding in bindings
            if str(binding["job_id"]) not in active_job_ids
        )
        for worker_id in stale_worker_ids:
            await self.release(worker_id, reason="inactive_job_reconcile")
        return len(stale_worker_ids)

    async def rebind(self, worker_id: str, job_id: str, *, reason: str | None = None) -> MarketIdentityLease | None:
        """Keep the account exclusive while returning the old route and choosing a free one."""

        async with self._lock:
            await self._release_worker_transports(worker_id)
            await self.repository.clear_market_identity_route(worker_id, reason=reason)
            self._leases.pop(worker_id, None)
        return await self.acquire(worker_id, job_id)

    async def rotate(
        self,
        worker_id: str,
        job_id: str,
        *,
        country: str | None = None,
    ) -> MarketIdentityLease | None:
        """Rotate one worker route without allowing the resulting IP to be shared.

        VPNTE can return an already-used external IP after a rotation.  The
        old durable binding therefore cannot simply keep its stale egress
        value: the new IP is probed and atomically claimed, or the account is
        immediately rebound to another currently free verified route.
        """

        async with self._lock:
            binding = await self.repository.get_market_identity_binding(worker_id)
            transport_id = str(binding.get("transport_id") or "") if binding else ""
            if not transport_id:
                return None
            snapshot = self.transport_manager.get(transport_id)
            if snapshot is None or snapshot.lease_owner != worker_id:
                await self.repository.clear_market_identity_route(worker_id, reason="rotate_route_unavailable")
                self._leases.pop(worker_id, None)
            else:
                old_proxy_url = snapshot.proxy_url
                rotated = self.transport_manager.rotate(transport_id, worker_id, country=country)
                if old_proxy_url:
                    self._route_ip_cache.pop(old_proxy_url, None)
                if rotated.proxy_url:
                    self._route_ip_cache.pop(rotated.proxy_url, None)
                self._route_snapshot_cached_at = 0.0
                await self.repository.upsert_transport(rotated)
                routes = await self._refresh_routes(force=True)
                refreshed = routes.get(rotated.transport_id)
                other_bindings = await self.repository.list_market_identity_bindings(active_only=True)
                occupied_ips = {
                    str(item["current_egress_ip"])
                    for item in other_bindings
                    if item["worker_id"] != worker_id and item.get("current_egress_ip")
                }
                account = self.account_store.get(str(binding.get("registration_id") or ""))
                if (
                    refreshed is not None
                    and refreshed.health is TransportHealth.HEALTHY
                    and refreshed.egress_ip
                    and refreshed.egress_ip not in occupied_ips
                    and account is not None
                    and self._eligible(account)
                ):
                    context = self._context_for(account, routes)
                    deadline = self._deadline()
                    token = uuid4().hex
                    try:
                        claimed = await self.repository.claim_market_identity_binding(
                            worker_id=worker_id,
                            job_id=job_id,
                            registration_id=account.registration_id,
                            transport_id=refreshed.transport_id,
                            current_egress_ip=refreshed.egress_ip,
                            binding_mode="rotated",
                            lease_token=token,
                            lease_deadline=deadline,
                        )
                    except MarketJobRepositoryError:
                        claimed = None
                    if claimed is not None:
                        lease = MarketIdentityLease(
                            worker_id=worker_id,
                            job_id=job_id,
                            registration_id=account.registration_id,
                            username=account.username,
                            persona_id=context.persona_id,
                            transport_id=refreshed.transport_id,
                            slot=refreshed.slot,
                            proxy_url=refreshed.proxy_url,
                            egress_ip=refreshed.egress_ip,
                            signup_ip=account.signup_ip,
                            preferred_slot=context.preferred_slot,
                            binding_mode=str(claimed["binding_mode"]),
                            lease_token=token,
                            lease_deadline=deadline,
                        )
                        self._contexts[worker_id] = context
                        self._leases[worker_id] = lease
                        return lease
                await self._release_transport(rotated.transport_id, worker_id)
                await self.repository.clear_market_identity_route(worker_id, reason="rotate_ip_not_unique")
                self._leases.pop(worker_id, None)
        return await self.acquire(worker_id, job_id)

    async def renew(self, worker_id: str) -> bool:
        """Refresh the lease deadline while a worker emits its heartbeat."""

        lease = self._leases.get(worker_id)
        if lease is None:
            return False
        deadline = self._deadline()
        renewed = await self.repository.renew_market_identity_binding(
            worker_id,
            lease_token=lease.lease_token,
            lease_deadline=deadline,
        )
        if renewed is None or renewed.get("state") != "active":
            self._leases.pop(worker_id, None)
            self._contexts.pop(worker_id, None)
            return False
        self._leases[worker_id] = replace(lease, lease_deadline=deadline)
        return True

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        """Return private account session data only to the local operation executor."""

        context = self._contexts.get(worker_id)
        if context is not None:
            return context
        binding = await self.repository.get_market_identity_binding(worker_id)
        if binding is None or binding.get("state") != "active":
            return None
        record = self.account_store.get(str(binding["registration_id"]))
        if record is None or not self._eligible(record):
            return None
        async with self._lock:
            routes = await self._refresh_routes()
        context = self._context_for(record, routes)
        self._contexts[worker_id] = context
        return context

    async def snapshot(
        self,
        *,
        job_id: str | None = None,
        refresh_routes: bool = True,
    ) -> dict[str, Any]:
        """Build a UI-safe inventory, route and worker-binding snapshot.

        Interactive views can use the durable route cache to show account
        records immediately. A full VPNTE/IP probe remains an explicit action.
        """

        async with self._lock:
            inventory = await self.sync_inventory()
            routes = await self._refresh_routes(force=True) if refresh_routes else await self._cached_routes()
            provider_profiles_total = await asyncio.to_thread(
                self.transport_manager.profile_count,
                refresh=refresh_routes,
            )
            selected_account_ids = await self._selected_account_ids(job_id) if job_id else frozenset()
        selection_is_manual = bool(selected_account_ids)
        rendered_accounts = [
            {
                **account,
                "selected_for_job": (
                    str(account["registration_id"]) in selected_account_ids if selection_is_manual else True
                ),
            }
            for account in inventory
        ]
        bindings = await self.repository.list_market_identity_bindings(job_id=job_id, active_only=False)
        active = [binding for binding in bindings if binding["state"] == "active"]
        accounts_by_id = {str(item["registration_id"]): item for item in rendered_accounts}
        routes_by_id = {route.transport_id: route for route in routes.values()}
        rendered_bindings: list[dict[str, Any]] = []
        for binding in bindings:
            account = accounts_by_id.get(str(binding["registration_id"]), {})
            route = routes_by_id.get(str(binding.get("transport_id") or ""))
            rendered_bindings.append(
                {
                    **binding,
                    "username": account.get("username"),
                    "signup_ip": account.get("signup_ip"),
                    "preferred_slot": account.get("preferred_slot"),
                    "persona_id": account.get("persona_id"),
                    "slot": route.slot if route else None,
                    "proxy_url": route.proxy_url if route else None,
                    "route_health": route.health.value if route else None,
                }
            )
        route_items = [
            {
                "transport_id": route.transport_id,
                "slot": route.slot,
                "proxy_url": route.proxy_url,
                "egress_ip": route.egress_ip,
                "egress_checked_at": route.egress_checked_at,
                "health": route.health.value,
                "profile_id": route.profile_id,
                "profile_name": route.profile_name,
                "country": route.country,
                "lease_owner": route.lease_owner,
            }
            for route in sorted(routes.values(), key=lambda item: (item.slot or 0, item.transport_id))
        ]
        eligible_accounts = [
            item
            for item in rendered_accounts
            if (
                item["market_enabled"]
                and item["status"] == "activated"
                and item["session_cookie_count"] > 0
                and item["selected_for_job"]
            )
        ]
        active_account_ids = {str(item["registration_id"]) for item in active}
        active_ips = {str(item["current_egress_ip"]) for item in active if item.get("current_egress_ip")}
        verified_ips = {
            str(route.egress_ip)
            for route in routes.values()
            if route.health is TransportHealth.HEALTHY and route.egress_ip
        }
        healthy_ips = {
            str(route.egress_ip)
            for route in routes.values()
            if route.health is TransportHealth.HEALTHY and route.egress_ip
        }
        return {
            "job_id": job_id,
            "summary": {
                "accounts_total": len(inventory),
                "accounts_eligible": len(eligible_accounts),
                "accounts_selected": len(selected_account_ids) if selection_is_manual else len(eligible_accounts),
                "account_selection_mode": "manual" if selection_is_manual else "automatic",
                "accounts_active": len(active_account_ids),
                "routes_healthy": sum(route.health is TransportHealth.HEALTHY for route in routes.values()),
                "provider_profiles_total": provider_profiles_total,
                "routes_verified": sum(
                    route.health is TransportHealth.HEALTHY and bool(route.egress_ip)
                    for route in routes.values()
                ),
                "egress_ips_distinct": len(verified_ips),
                "egress_ips_active": len(active_ips),
                "effective_capacity": min(len(eligible_accounts), len(healthy_ips)),
            },
            "accounts": rendered_accounts,
            "bindings": rendered_bindings,
            "routes": route_items,
        }

    async def _selected_account_ids(self, job_id: str) -> frozenset[str]:
        job = await self.repository.get_job(job_id)
        if job is None:
            return frozenset()
        raw_ids = job.get("account_registration_ids")
        if not isinstance(raw_ids, list):
            return frozenset()
        return frozenset(
            str(value).strip()
            for value in raw_ids
            if isinstance(value, str) and value.strip()
        )

    def _account_candidates(
        self,
        worker_id: str,
        existing: Mapping[str, Any] | None,
        bindings: list[Mapping[str, Any]],
        records: Mapping[str, StoredKworkAccount],
        enabled_ids: set[str],
        account_order: list[str],
    ) -> list[StoredKworkAccount]:
        occupied = {
            str(binding["registration_id"])
            for binding in bindings
            if binding["worker_id"] != worker_id and binding["state"] == "active"
        }
        candidates: list[StoredKworkAccount] = []
        if existing and existing.get("state") == "active":
            prior = records.get(str(existing.get("registration_id") or ""))
            if prior is not None and prior.registration_id in enabled_ids and self._eligible(prior):
                candidates.append(prior)
                occupied.discard(prior.registration_id)
        rank = {registration_id: index for index, registration_id in enumerate(account_order)}
        for record in sorted(records.values(), key=lambda item: (rank.get(item.registration_id, len(rank)), item.registration_id)):
            if record.registration_id not in enabled_ids or record.registration_id in occupied or not self._eligible(record):
                continue
            if all(existing_record.registration_id != record.registration_id for existing_record in candidates):
                candidates.append(record)
        return candidates

    def _route_candidates(
        self,
        account: StoredKworkAccount,
        routes: Mapping[str, TransportSnapshot],
        occupied_transport_ids: set[str],
        occupied_ips: set[str],
        existing: Mapping[str, Any] | None,
    ) -> list[tuple[TransportSnapshot, str]]:
        preferred_slot = account.registration_slot
        existing_transport = str(existing.get("transport_id") or "") if existing else ""
        options: list[tuple[tuple[int, int, int, int, str], TransportSnapshot, str]] = []
        for route in routes.values():
            if (
                route.health is not TransportHealth.HEALTHY
                or route.slot is None
                or not route.egress_ip
                or route.transport_id in occupied_transport_ids
                or route.egress_ip in occupied_ips
            ):
                continue
            if route.transport_id == existing_transport:
                mode = "retained"
                rank = 0
            elif account.signup_ip and route.egress_ip == account.signup_ip:
                mode = "preferred_ip"
                rank = 1
            elif preferred_slot is not None and route.slot == preferred_slot:
                mode = "preferred_slot"
                rank = 2
            else:
                mode = "fallback"
                rank = 3
            rotation_rank = (route.slot - self._route_cursor) % 10_000
            options.append(((rank, rotation_rank, route.slot, 0, route.transport_id), route, mode))
        options.sort(key=lambda item: item[0])
        return [(route, mode) for _key, route, mode in options]

    def _context_for(
        self,
        account: StoredKworkAccount,
        routes: Mapping[str, TransportSnapshot],
    ) -> MarketAccountContext:
        persona = dict(account.persona)
        headers = {
            "User-Agent": str(persona.get("user_agent") or account.session.get("user_agent") or "Mozilla/5.0"),
            "Accept-Language": str(persona.get("accept_language") or "ru-RU,ru;q=0.9,en;q=0.8"),
        }
        return MarketAccountContext(
            registration_id=account.registration_id,
            username=account.username,
            email=account.email,
            password=account.password,
            cookies=self._cookies_for(account.session),
            headers=headers,
            persona_id=str(persona.get("persona_id") or account.registration_id),
            signup_ip=account.signup_ip,
            preferred_slot=self._preferred_slot(account, routes),
        )

    @staticmethod
    def _cookies_for(session: Mapping[str, Any]) -> dict[str, str]:
        raw = session.get("cookies", []) if isinstance(session, Mapping) else []
        cookies: dict[str, str] = {}
        if not isinstance(raw, list):
            return cookies
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if name and value:
                cookies[name] = value
        return cookies

    @staticmethod
    def _eligible(record: StoredKworkAccount) -> bool:
        return (
            record.market_enabled
            and record.status == "activated"
            and record.cookie_count > 0
            and bool(MarketIdentityPool._cookies_for(record.session))
        )

    @staticmethod
    def _inventory_item(record: StoredKworkAccount) -> dict[str, Any]:
        return {
            "registration_id": record.registration_id,
            "username": record.username,
            "email": record.email,
            "status": record.status,
            "market_enabled": record.market_enabled,
            "session_cookie_count": record.cookie_count,
            "signup_ip": record.signup_ip,
            "registration_slot": record.registration_slot,
            "registration_transport_id": (
                f"vpnte-slot-{record.registration_slot}" if record.registration_slot is not None else None
            ),
            "persona_id": record.persona.get("persona_id"),
        }

    @staticmethod
    def _preferred_slot(account: StoredKworkAccount, routes: Mapping[str, TransportSnapshot]) -> int | None:
        if account.registration_slot is not None:
            return account.registration_slot
        proxy_url = account.registration_proxy_url
        if proxy_url:
            for route in routes.values():
                if route.proxy_url == proxy_url:
                    return route.slot
        return None

    async def _cached_routes(self) -> dict[str, TransportSnapshot]:
        """Read prior route observations without touching VPNTE or the network."""

        rows: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self.repository.list_transports(cursor=cursor, limit=1_000)
            if not page:
                break
            rows.extend(page)
            if len(page) < 1_000:
                break
            next_cursor = str(page[-1].get("transport_id") or "").strip()
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        routes: dict[str, TransportSnapshot] = {}
        for row in rows:
            route = self._cached_route_snapshot(row)
            if route is not None:
                routes[route.transport_id] = route
        return routes

    @staticmethod
    def _cached_route_snapshot(record: Mapping[str, Any]) -> TransportSnapshot | None:
        transport_id = str(record.get("transport_id") or "").strip()
        if not transport_id:
            return None
        try:
            kind = TransportKind(str(record.get("kind") or TransportKind.VPNTE))
            health = TransportHealth(str(record.get("health") or TransportHealth.UNKNOWN))
        except ValueError:
            return None

        def text(name: str) -> str | None:
            value = record.get(name)
            if value is None:
                return None
            return str(value).strip() or None

        def integer(name: str) -> int | None:
            value = record.get(name)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return TransportSnapshot(
            transport_id=transport_id,
            kind=kind,
            health=health,
            slot=integer("slot"),
            proxy_url=text("proxy_url"),
            profile_id=text("profile_id"),
            profile_name=text("profile_name"),
            country=text("country"),
            pid=integer("pid"),
            generation=integer("generation") or 1,
            lease_owner=text("lease_owner"),
            quarantine_until=text("quarantine_until"),
            last_rotate_reason=text("last_rotate_reason"),
            egress_ip=text("egress_ip"),
            egress_checked_at=text("egress_checked_at"),
        )

    async def _refresh_routes(self, *, force: bool = False) -> dict[str, TransportSnapshot]:
        now_monotonic = time.monotonic()
        if (
            not force
            and self._route_snapshot_cache
            and now_monotonic - self._route_snapshot_cached_at < self.route_refresh_cache_seconds
        ):
            return dict(self._route_snapshot_cache)

        async with self._route_refresh_lock:
            now_monotonic = time.monotonic()
            if (
                not force
                and self._route_snapshot_cache
                and now_monotonic - self._route_snapshot_cached_at < self.route_refresh_cache_seconds
            ):
                return dict(self._route_snapshot_cache)
            durable_routes = await self._cached_routes()
            try:
                snapshots = await asyncio.to_thread(self.transport_manager.refresh)
            except Exception:
                return dict(self._route_snapshot_cache)
            identity_bindings = await self.repository.list_market_identity_bindings(active_only=False)
            known_binding_workers = {str(binding["worker_id"]) for binding in identity_bindings}
            active_pairs = {
                (str(binding["worker_id"]), str(binding.get("transport_id") or ""))
                for binding in identity_bindings
                if binding.get("state") == "active" and binding.get("transport_id")
            }
            for snapshot in tuple(snapshots):
                owner = snapshot.lease_owner
                if (
                    owner is None
                    or owner not in known_binding_workers
                    or (owner, snapshot.transport_id) in active_pairs
                ):
                    continue
                try:
                    self.transport_manager.release(snapshot.transport_id, owner)
                except Exception:
                    continue
            snapshots = [
                self.transport_manager.get(snapshot.transport_id) or snapshot
                for snapshot in snapshots
            ]
            semaphore = asyncio.Semaphore(self.probe_concurrency)

            async def enrich(snapshot: TransportSnapshot) -> TransportSnapshot:
                if snapshot.health is not TransportHealth.HEALTHY or not snapshot.proxy_url:
                    await self.repository.upsert_transport(snapshot)
                    return snapshot
                cached = self._route_ip_cache.get(snapshot.proxy_url)
                checked_at = time.monotonic()
                cache_ttl = (
                    self.probe_cache_seconds
                    if cached is not None and cached[0]
                    else min(8.0, self.probe_cache_seconds)
                )
                if cached is not None and checked_at - cached[1] < cache_ttl:
                    egress_ip = cached[0]
                elif snapshot.egress_ip:
                    egress_ip = snapshot.egress_ip
                    self._route_ip_cache[snapshot.proxy_url] = (egress_ip, checked_at)
                else:
                    async with semaphore:
                        egress_ip = await self.exit_ip_probe(snapshot.proxy_url)
                    self._route_ip_cache[snapshot.proxy_url] = (egress_ip, checked_at)
                enriched_snapshot = replace(
                    snapshot,
                    egress_ip=egress_ip,
                    egress_checked_at=utc_now() if egress_ip else snapshot.egress_checked_at,
                )
                record_egress = getattr(self.transport_manager, "record_egress", None)
                if callable(record_egress):
                    enriched_snapshot = record_egress(
                        enriched_snapshot.transport_id,
                        enriched_snapshot.egress_ip,
                        checked_at=enriched_snapshot.egress_checked_at,
                    )
                await self.repository.upsert_transport(enriched_snapshot)
                return enriched_snapshot

            enriched = await asyncio.gather(*(enrich(snapshot) for snapshot in snapshots))
            result = {snapshot.transport_id: snapshot for snapshot in enriched}
            for transport_id, durable in durable_routes.items():
                if transport_id in result or durable.health is TransportHealth.STOPPED:
                    continue
                await self.repository.upsert_transport(
                    replace(
                        durable,
                        health=TransportHealth.STOPPED,
                        proxy_url=None,
                        lease_owner=None,
                    )
                )
            self._route_snapshot_cache = result
            self._route_snapshot_cached_at = time.monotonic()
            return dict(result)

    @staticmethod
    async def _probe_exit_ip(proxy_url: str) -> str | None:
        endpoints = (
            ("https://api.ipify.org", {"format": "json"}, True),
            ("https://checkip.amazonaws.com", None, False),
        )
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=6.0,
            trust_env=False,
            follow_redirects=True,
        ) as client:
            for url, params, is_json in endpoints:
                try:
                    response = await client.get(url, params=params)
                    if not response.is_success:
                        continue
                    if is_json:
                        payload = response.json()
                        candidate = str(payload.get("ip") or "") if isinstance(payload, Mapping) else ""
                    else:
                        candidate = response.text.strip()
                    if candidate:
                        return str(ipaddress.ip_address(candidate))
                except Exception:
                    continue
        return None

    def _deadline(self) -> str:
        return (
            datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=self.lease_seconds)
        ).isoformat().replace("+00:00", "Z")

    async def _release_transport(self, transport_id: str, worker_id: str) -> None:
        try:
            self.transport_manager.release(transport_id, worker_id)
        except Exception:
            return
        snapshot = self.transport_manager.get(transport_id)
        if snapshot is not None:
            await self.repository.upsert_transport(snapshot)

    async def _release_worker_transports(self, worker_id: str) -> None:
        release_all = getattr(self.transport_manager, "release_all", None)
        if callable(release_all):
            snapshots = release_all(worker_id)
            for snapshot in snapshots:
                await self.repository.upsert_transport(snapshot)
            return
        lease = self._leases.get(worker_id)
        if lease is not None:
            await self._release_transport(lease.transport_id, worker_id)

    async def _ensure_worker_record(self, worker_id: str, job_id: str) -> None:
        """Create the durable worker row required by an identity binding.

        Supply workers persist themselves before obtaining an identity. Buyer
        discovery workers intentionally bypass the supply supervisor, so they
        need this minimal row before SQLite can accept their foreign-key-bound
        account/route lease.
        """

        existing = await self.repository.get_worker(worker_id)
        if existing is not None:
            return
        await self.repository.upsert_worker(
            {
                "worker_id": worker_id,
                "generation": 1,
                "desired_state": "running",
                "actual_state": "starting",
                "runtime_kind": "identity_lease",
            },
            job_id=job_id,
        )

    async def _release_unowned_bindings(self, bindings: Sequence[Mapping[str, Any]]) -> int:
        """Release binding rows that are not owned by this process anymore."""

        released_count = 0
        for binding in bindings:
            worker_id = str(binding.get("worker_id") or "").strip()
            if not worker_id or worker_id in self._leases:
                continue
            released = await self.repository.release_market_identity_binding(
                worker_id,
                reason="buyer_runtime_recovery",
            )
            if released is None or released.get("state") != "released":
                continue
            await self._release_worker_transports(worker_id)
            self._contexts.pop(worker_id, None)
            self._leases.pop(worker_id, None)
            released_count += 1
        return released_count
