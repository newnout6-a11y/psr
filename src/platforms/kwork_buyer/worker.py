"""Read-only, fenced Buyer Search discovery worker for one account identity."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import inspect
from time import perf_counter
from typing import Any, Protocol

from .mapper import (
    MOBILE_PROJECT_SOURCE,
    WEB_PROJECT_SOURCE,
    BuyerMappedProject,
    BuyerProjectMappingError,
    BuyerProjectObservationContext,
)
from .sources.capabilities import BuyerReadProvenance
from .sources.mobile_projects import normalize_mobile_projects
from .sources.web_projects import normalize_web_projects


class BuyerDiscoveryFailureKind(StrEnum):
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    FATAL = "fatal"


class BuyerDiscoveryOutcome(StrEnum):
    IDLE = "idle"
    COMMITTED = "committed"
    RETRY = "retry"
    FAILED = "failed"


class BuyerDiscoveryWorkerError(RuntimeError):
    """Raised when a leased task is unsafe to process or commit."""


class BuyerDiscoverySourceError(RuntimeError):
    """A source adapter error with optional HTTP semantics and retry hint."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class BuyerDiscoveryReadSource(Protocol):
    """Only read page fetching is exposed to a discovery worker."""

    def fetch_page(
        self,
        *,
        task: Mapping[str, Any],
        identity: Mapping[str, Any],
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Return one raw source response envelope for the leased query task."""


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryIdentity:
    """One account, one transport, and one currently verified egress identity."""

    worker_id: str
    account_registration_id: str
    transport_id: str
    egress_ip: str
    route_generation: int | None = None

    def __post_init__(self) -> None:
        for name in ("worker_id", "account_registration_id", "transport_id", "egress_ip"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be blank")
            object.__setattr__(self, name, value.strip())
        if self.route_generation is not None and (
            isinstance(self.route_generation, bool) or not isinstance(self.route_generation, int) or self.route_generation < 0
        ):
            raise ValueError("route_generation must be a non-negative integer or None")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def provenance(self, source: str) -> BuyerReadProvenance:
        return BuyerReadProvenance(
            worker_id=self.worker_id,
            account_registration_id=self.account_registration_id,
            transport_id=self.transport_id,
            egress_ip=self.egress_ip,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryRetryPolicy:
    """Retry delays are persisted as hints; this worker never sleeps while leased."""

    timeout_seconds: float = 10.0
    http_403_seconds: float = 60.0
    http_429_seconds: float = 30.0
    transient_seconds: float = 15.0
    max_transient_retries: int = 3

    def __post_init__(self) -> None:
        for name in ("timeout_seconds", "http_403_seconds", "http_429_seconds", "transient_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if isinstance(self.max_transient_retries, bool) or not isinstance(self.max_transient_retries, int) or self.max_transient_retries < 0:
            raise ValueError("max_transient_retries must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryRunResult:
    """The local result of one lease/process/commit attempt."""

    outcome: BuyerDiscoveryOutcome
    task_id: str | None = None
    project_count: int = 0
    failure_kind: BuyerDiscoveryFailureKind | None = None
    retry_after_seconds: float | None = None
    error: str | None = None
    endpoint: str | None = None
    quarantine_until: str | None = None


class BuyerDiscoveryWorker:
    """Lease and commit one read-only Buyer Search page at a time."""

    def __init__(
        self,
        repository: object,
        source: BuyerDiscoveryReadSource,
        *,
        run_id: str,
        identity: BuyerDiscoveryIdentity,
        lease_seconds: int = 60,
        retry_policy: BuyerDiscoveryRetryPolicy | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id cannot be blank")
        if not isinstance(identity, BuyerDiscoveryIdentity):
            raise TypeError("identity must be a BuyerDiscoveryIdentity")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        if not callable(getattr(source, "fetch_page", None)):
            raise TypeError("source must expose a read-only fetch_page method")
        self.repository = repository
        self.source = source
        self.run_id = run_id.strip()
        self.identity = identity
        self.lease_seconds = lease_seconds
        self.retry_policy = retry_policy or BuyerDiscoveryRetryPolicy()
        self._single_flight_lock = asyncio.Lock()
        self._current_task: dict[str, Any] | None = None

    @property
    def current_task(self) -> dict[str, Any] | None:
        """A small, non-secret execution snapshot for the fleet UI."""

        return dict(self._current_task) if self._current_task is not None else None

    async def run_once(self) -> BuyerDiscoveryRunResult:
        """Process at most one leased page; concurrent calls serialize per worker."""

        async with self._single_flight_lock:
            task = await self._lease_task()
            if task is None:
                return BuyerDiscoveryRunResult(outcome=BuyerDiscoveryOutcome.IDLE)
            task_id = _required_task_text(task, "task_id")
            self._current_task = _current_task_payload(task)
            try:
                await self._append_event(task_id, "query.page.started", {"source": task.get("source"), "page": task.get("page")})
                fetch_started = perf_counter()
                response = dict(await self._fetch_page(task))
                if not isinstance(response.get("latency_ms"), (int, float)) or isinstance(response.get("latency_ms"), bool):
                    response["latency_ms"] = round((perf_counter() - fetch_started) * 1_000)
                raw_artifact_id = await self._store_raw_artifact(task, response)
                projects = self._map_response(task, response, raw_artifact_id=raw_artifact_id)
                committed = await self._commit_page(task, projects, response, raw_artifact_id=raw_artifact_id)
                try:
                    await self._append_event(
                        task_id,
                        "query.page.completed",
                        {
                            "source": _task_source(task),
                            "page": _task_page(task),
                            "project_count": len(projects),
                            "continuation_task_id": committed.get("continuation_task_id") if isinstance(committed, Mapping) else None,
                        },
                    )
                except Exception:
                    # The fenced page commit already succeeded; telemetry failure
                    # must not cause a stale retry of the same remote response.
                    pass
                await self._update_run(last_error=None, last_task_id=task_id)
                return BuyerDiscoveryRunResult(
                    outcome=BuyerDiscoveryOutcome.COMMITTED,
                    task_id=task_id,
                    project_count=len(projects),
                )
            except Exception as exc:  # noqa: BLE001 - classify source and mapping failures into durable task outcomes.
                kind, retry_after_seconds = self._classify_failure(exc)
                retry_limit_reached = (
                    kind is BuyerDiscoveryFailureKind.TRANSIENT
                    and _task_retry_count(task) >= self.retry_policy.max_transient_retries
                )
                if kind is BuyerDiscoveryFailureKind.FATAL or retry_limit_reached:
                    error = str(exc)
                    if retry_limit_reached:
                        error = f"{error}; исчерпан лимит повторных попыток ({self.retry_policy.max_transient_retries})"
                    await self._mark_failed(task, kind=kind, error=error)
                    return BuyerDiscoveryRunResult(
                        outcome=BuyerDiscoveryOutcome.FAILED,
                        task_id=task_id,
                        failure_kind=kind,
                        error=error,
                    )
                quarantine = await self._schedule_retry(
                    task,
                    kind=kind,
                    retry_after_seconds=retry_after_seconds,
                    error=str(exc),
                )
                return BuyerDiscoveryRunResult(
                    outcome=BuyerDiscoveryOutcome.RETRY,
                    task_id=task_id,
                    failure_kind=kind,
                    retry_after_seconds=retry_after_seconds,
                    error=str(exc),
                    endpoint=_task_endpoint(task),
                    quarantine_until=_optional_mapping_text(quarantine, "expires_at"),
                )
            finally:
                self._current_task = None

    async def _lease_task(self) -> Mapping[str, Any] | None:
        leased = await _call_required(
            self.repository,
            "lease_query_task",
            self.run_id,
            self.identity.worker_id,
            lease_seconds=self.lease_seconds,
            identity=self.identity.to_payload(),
        )
        if leased is None:
            return None
        if not isinstance(leased, Mapping):
            raise BuyerDiscoveryWorkerError("lease_query_task must return a mapping or None")
        _required_task_text(leased, "task_id")
        _required_task_text(leased, "attempt_id")
        _lease_fence(leased)
        _task_source(leased)
        _task_page(leased)
        return leased

    async def _fetch_page(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self.source.fetch_page(task=task, identity=self.identity.to_payload())
        response = await value if inspect.isawaitable(value) else value
        if not isinstance(response, Mapping):
            raise BuyerDiscoveryWorkerError("read-only fetch_page must return a mapping")
        status_code = response.get("http_status") or response.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            raise BuyerDiscoverySourceError(f"source returned HTTP {status_code}", status_code=status_code)
        return response

    def _map_response(
        self,
        task: Mapping[str, Any],
        response: Mapping[str, Any],
        *,
        raw_artifact_id: str | None,
    ) -> tuple[BuyerMappedProject, ...]:
        source = _task_source(task)
        context = BuyerProjectObservationContext(
            run_id=self.run_id,
            query_id=_required_task_text(task, "query_id"),
            task_id=_required_task_text(task, "task_id"),
            attempt_id=_required_task_text(task, "attempt_id"),
            observed_at=_response_observed_at(response),
            page=_task_page(task),
            response_position=0,
            provenance=self.identity.provenance(source),
            raw_artifact_id=raw_artifact_id,
            route_generation=self.identity.route_generation,
        )
        if source == MOBILE_PROJECT_SOURCE:
            return normalize_mobile_projects(response, context=context)
        if source == WEB_PROJECT_SOURCE:
            return normalize_web_projects(response, context=context)
        raise BuyerDiscoveryWorkerError(f"unsupported Buyer discovery source {source!r}")

    async def _commit_page(
        self,
        task: Mapping[str, Any],
        projects: tuple[BuyerMappedProject, ...],
        response: Mapping[str, Any],
        *,
        raw_artifact_id: str | None,
    ) -> Any:
        task_id = _required_task_text(task, "task_id")
        attempt_id = _required_task_text(task, "attempt_id")
        lease_fence = _lease_fence(task)
        payloads = tuple(
            {"canonical": project.canonical.to_payload(), "observation": project.observation.to_payload()} for project in projects
        )
        kwargs: dict[str, Any] = {
            "task_id": task_id,
            "worker_id": self.identity.worker_id,
            "attempt_id": attempt_id,
            "lease_fence": lease_fence,
            "projects": payloads,
            "source": _task_source(task),
            "raw_artifact_id": raw_artifact_id,
            "latency_ms": _response_latency_ms(response),
            "total_hint": _response_total_hint(response),
            "observed_at": _response_observed_at(response),
        }
        next_tasks = _next_page_tasks(task, response)
        if next_tasks:
            kwargs["next_tasks"] = next_tasks
        return await _call_required(self.repository, "commit_observed_page", **kwargs)

    async def _store_raw_artifact(self, task: Mapping[str, Any], response: Mapping[str, Any]) -> str | None:
        """Persist the exact read envelope before its normalized cards are committed."""

        existing = _optional_task_text(task, "raw_artifact_id")
        if existing:
            return existing
        artifact = {
            "source": _task_source(task),
            "endpoint": _optional_task_text(task, "endpoint") or _task_source(task),
            "request_fingerprint": _optional_task_text(task, "request_fingerprint"),
            "account_registration_id": self.identity.account_registration_id,
            "transport_id": self.identity.transport_id,
            "egress_ip": self.identity.egress_ip,
            "route_generation": self.identity.route_generation,
            "status_code": response.get("http_status") or response.get("status_code"),
            "headers": response.get("headers") if isinstance(response.get("headers"), Mapping) else {},
            "body": dict(response),
            "content_type": response.get("content_type"),
            "parser_version": "buyer-discovery-v1",
            "observed_at": _response_observed_at(response),
        }
        stored = await _call_optional(self.repository, "store_raw_artifact", artifact)
        if not isinstance(stored, Mapping):
            return None
        value = stored.get("artifact_id")
        return str(value).strip() if value is not None and str(value).strip() else None

    async def _schedule_retry(
        self,
        task: Mapping[str, Any],
        *,
        kind: BuyerDiscoveryFailureKind,
        retry_after_seconds: float,
        error: str,
    ) -> Mapping[str, Any] | None:
        task_id = _required_task_text(task, "task_id")
        quarantine = await self._quarantine_protected_identity(
            task,
            kind=kind,
            retry_after_seconds=retry_after_seconds,
        )
        payload = {
            "task_id": task_id,
            "attempt_id": _required_task_text(task, "attempt_id"),
            "lease_fence": _lease_fence(task),
            "failure_kind": kind.value,
            "error": error,
            "retry_after_seconds": retry_after_seconds,
        }
        if quarantine is not None:
            payload["quarantine_id"] = quarantine.get("quarantine_id")
            payload["quarantine_until"] = quarantine.get("expires_at")
            payload["endpoint"] = _task_endpoint(task)
        await self._append_event(task_id, "query.page.retry", payload)
        await _call_optional(
            self.repository,
            "retry_query_task",
            task_id,
            self.identity.worker_id,
            _required_task_text(task, "attempt_id"),
            _lease_fence(task),
            retry_after_seconds=retry_after_seconds,
            failure_kind=kind.value,
            error=error,
        )
        await self._update_run(last_error=error, last_failure_kind=kind.value, last_task_id=task_id)
        return quarantine

    async def _quarantine_protected_identity(
        self,
        task: Mapping[str, Any],
        *,
        kind: BuyerDiscoveryFailureKind,
        retry_after_seconds: float,
    ) -> Mapping[str, Any] | None:
        """Fence 403/429 route protection before making the task retryable."""

        if kind not in {BuyerDiscoveryFailureKind.HTTP_403, BuyerDiscoveryFailureKind.HTTP_429}:
            return None
        quarantined = await _call_optional(
            self.repository,
            "quarantine_discovery_identity",
            self.run_id,
            task_id=_required_task_text(task, "task_id"),
            worker_id=self.identity.worker_id,
            attempt_id=_required_task_text(task, "attempt_id"),
            lease_fence=_lease_fence(task),
            identity=self.identity.to_payload(),
            endpoint=_task_endpoint(task),
            failure_kind=kind.value,
            retry_after_seconds=retry_after_seconds,
        )
        return dict(quarantined) if isinstance(quarantined, Mapping) else None

    async def _mark_failed(
        self,
        task: Mapping[str, Any],
        *,
        kind: BuyerDiscoveryFailureKind,
        error: str,
    ) -> None:
        task_id = _required_task_text(task, "task_id")
        await self._append_event(
            task_id,
            "query.page.failed",
            {"task_id": task_id, "failure_kind": kind.value, "error": error},
        )
        await _call_optional(
            self.repository,
            "fail_query_task",
            task_id,
            self.identity.worker_id,
            _required_task_text(task, "attempt_id"),
            _lease_fence(task),
            failure_kind=kind.value,
            error=error,
        )
        await self._update_run(last_error=error, last_failure_kind=kind.value, last_task_id=task_id)

    async def _append_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        await _call_required(
            self.repository,
            "append_event",
            self.run_id,
            event_type,
            {"task_id": task_id, "worker_id": self.identity.worker_id, **payload},
        )

    async def _update_run(self, **fields: Any) -> None:
        try:
            await _call_optional(self.repository, "update_run", self.run_id, dict(fields))
        except Exception:  # noqa: BLE001 - page commit is already durable; diagnostics must not turn it into a failed retry.
            return

    def _classify_failure(self, exc: Exception) -> tuple[BuyerDiscoveryFailureKind, float]:
        status_code = _status_code(exc)
        retry_after = _retry_after_seconds(exc)
        if status_code == 403:
            return BuyerDiscoveryFailureKind.HTTP_403, retry_after or float(self.retry_policy.http_403_seconds)
        if status_code == 429:
            return BuyerDiscoveryFailureKind.HTTP_429, retry_after or float(self.retry_policy.http_429_seconds)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(exc).__name__.casefold():
            return BuyerDiscoveryFailureKind.TIMEOUT, retry_after or float(self.retry_policy.timeout_seconds)
        if isinstance(exc, (BuyerDiscoverySourceError, BuyerProjectMappingError)) or hasattr(exc, "retry_after_seconds"):
            return BuyerDiscoveryFailureKind.TRANSIENT, retry_after or float(self.retry_policy.transient_seconds)
        return BuyerDiscoveryFailureKind.FATAL, 0.0


def _current_task_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    """Render only stable task metadata; leases and request filters stay private."""

    payload = {
        "task_id": _required_task_text(task, "task_id"),
        "query_id": _required_task_text(task, "query_id"),
        "source": _task_source(task),
        "page": _task_page(task),
    }
    query_text = _optional_task_text(task, "query_text") or _optional_task_text(task, "text")
    if query_text:
        payload["query_text"] = query_text[:1_000]
    return payload


def _required_task_text(task: Mapping[str, Any], key: str) -> str:
    value = task.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BuyerDiscoveryWorkerError(f"leased task requires non-empty {key}")
    return value.strip()


def _optional_task_text(task: Mapping[str, Any], key: str) -> str | None:
    value = task.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _optional_mapping_text(value: Mapping[str, Any] | None, key: str) -> str | None:
    if value is None:
        return None
    raw = value.get(key)
    return str(raw).strip() if raw is not None and str(raw).strip() else None


def _lease_fence(task: Mapping[str, Any]) -> int:
    value = task.get("lease_fence")
    if isinstance(value, bool):
        raise BuyerDiscoveryWorkerError("leased task lease_fence must be a positive integer")
    try:
        fence = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerDiscoveryWorkerError("leased task lease_fence must be a positive integer") from exc
    if fence <= 0:
        raise BuyerDiscoveryWorkerError("leased task lease_fence must be a positive integer")
    return fence


def _task_retry_count(task: Mapping[str, Any]) -> int:
    value = task.get("retry_count", 0)
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _task_page(task: Mapping[str, Any]) -> int:
    value = task.get("page", 1)
    if isinstance(value, bool):
        raise BuyerDiscoveryWorkerError("leased task page must be a positive integer")
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerDiscoveryWorkerError("leased task page must be a positive integer") from exc
    if page <= 0:
        raise BuyerDiscoveryWorkerError("leased task page must be a positive integer")
    return page


def _task_source(task: Mapping[str, Any]) -> str:
    raw_source = task.get("source")
    if not isinstance(raw_source, str):
        raise BuyerDiscoveryWorkerError("leased task requires source")
    aliases = {
        "mobile": MOBILE_PROJECT_SOURCE,
        MOBILE_PROJECT_SOURCE: MOBILE_PROJECT_SOURCE,
        "web": WEB_PROJECT_SOURCE,
        WEB_PROJECT_SOURCE: WEB_PROJECT_SOURCE,
    }
    try:
        return aliases[raw_source.strip().casefold()]
    except KeyError as exc:
        raise BuyerDiscoveryWorkerError(f"unsupported Buyer discovery source {raw_source!r}") from exc


def _task_endpoint(task: Mapping[str, Any]) -> str:
    """Match the repository's endpoint key without carrying query credentials."""

    endpoint = _optional_task_text(task, "endpoint") or _task_source(task)
    endpoint = endpoint.split("?", 1)[0].split("#", 1)[0].strip()
    if not endpoint:
        raise BuyerDiscoveryWorkerError("leased task requires an endpoint")
    return endpoint


def _response_observed_at(response: Mapping[str, Any]) -> str:
    value = response.get("observed_at")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _response_metadata(response: Mapping[str, Any]) -> dict[str, Any]:
    paging = response.get("paging") if isinstance(response.get("paging"), Mapping) else {}
    return {
        "reported_page": paging.get("page") or response.get("page"),
        "total": paging.get("total") or response.get("total"),
        "pages": paging.get("pages") or response.get("pages"),
    }


def _response_total_hint(response: Mapping[str, Any]) -> int | None:
    metadata = _response_metadata(response)
    value = metadata.get("total")
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _next_page_tasks(task: Mapping[str, Any], response: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Derive safe continuations only from explicit source paging evidence."""

    cursor = task.get("cursor")
    if isinstance(cursor, Mapping) and ("refresh_cycle" in cursor or "refresh_epoch" in cursor):
        return ()
    if isinstance(cursor, Mapping) and cursor.get("pagination_fanout") is True:
        return ()
    current_page = _task_page(task)
    paging = response.get("paging") if isinstance(response.get("paging"), Mapping) else {}
    if not paging and isinstance(response.get("pagination"), Mapping):
        paging = response["pagination"]
    next_cursor = (
        paging.get("next_cursor")
        or paging.get("cursor_next")
        or response.get("next_cursor")
        or response.get("cursor_next")
    )
    raw_next_page = paging.get("next_page") or response.get("next_page")
    nested_next = paging.get("next") if isinstance(paging.get("next"), Mapping) else response.get("next")
    if isinstance(nested_next, Mapping):
        raw_next_page = raw_next_page or nested_next.get("page")
        next_cursor = next_cursor or nested_next.get("cursor") or nested_next.get("next_cursor")
    total_pages = _positive_int_or_none(paging.get("pages") or paging.get("last_page") or response.get("pages"))
    has_more = paging.get("has_more") if "has_more" in paging else response.get("has_more")
    next_page = _positive_int_or_none(raw_next_page)
    if next_page is None and total_pages is not None and current_page < total_pages:
        next_page = current_page + 1
    if next_page is None and next_cursor not in (None, ""):
        next_page = current_page + 1
    if next_page is None and has_more is True:
        next_page = current_page + 1
    if next_page is None or next_page <= current_page:
        return ()
    if total_pages is not None and next_page > total_pages:
        return ()

    origin = str(task.get("query_origin") or "").strip().casefold()
    if origin == "category_browse" and current_page == 1 and total_pages is not None:
        return tuple(
            {
                "source": _task_source(task),
                "endpoint": _task_endpoint(task),
                "page": page,
                "priority": task.get("priority"),
                "cursor": {"pagination_fanout": True},
            }
            for page in range(next_page, total_pages + 1)
        )

    continuation: dict[str, Any] = {
        "source": _task_source(task),
        "endpoint": _task_endpoint(task),
        "page": next_page,
        "priority": task.get("priority"),
    }
    if next_cursor not in (None, ""):
        continuation["cursor"] = next_cursor
    return (continuation,)


def _next_page_task(task: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any] | None:
    """Compatibility helper for callers that only inspect one continuation."""

    tasks = _next_page_tasks(task, response)
    return tasks[0] if tasks else None


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _response_latency_ms(response: Mapping[str, Any]) -> int | None:
    value = response.get("latency_ms")
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    nested = getattr(response, "status_code", None)
    return nested if isinstance(nested, int) else None


def _retry_after_seconds(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


async def _call_required(target: object, name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        raise BuyerDiscoveryWorkerError(f"repository must implement {name}")
    value = method(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


async def _call_optional(target: object, name: str, *args: Any, **kwargs: Any) -> Any | None:
    method = getattr(target, name, None)
    if not callable(method):
        return None
    value = method(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "BuyerDiscoveryFailureKind",
    "BuyerDiscoveryIdentity",
    "BuyerDiscoveryOutcome",
    "BuyerDiscoveryReadSource",
    "BuyerDiscoveryRetryPolicy",
    "BuyerDiscoveryRunResult",
    "BuyerDiscoverySourceError",
    "BuyerDiscoveryWorker",
    "BuyerDiscoveryWorkerError",
]
