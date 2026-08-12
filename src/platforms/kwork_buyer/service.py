"""Buyer Search application service.

This is the orchestration boundary between HTTP/UI controls and the durable
Buyer repository.  It owns lifecycle transitions, deterministic planning, task
creation and file-backed exports; workers own remote reads and commits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from .export import BuyerExportFormat, BuyerExportSnapshot, build_buyer_export
from .ai_scoring import BuyerFinalScoringError, BuyerFinalScoringGateway, score_buyer_project_final
from .mapper import MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE
from .models import BuyerQueryOrigin, BuyerRunMode, BuyerRunState
from .query_ai import BuyerAIQueryGenerationResult, BuyerQueryGenerationGateway, generate_buyer_query_candidates_with_ai
from .query_generation import BuyerQueryGenerationInput, BuyerQueryGenerationResult, replace_legacy_category_fallback_text
from .query_normalizer import normalize_query_text
from .query_planner import (
    BuyerQueryCandidate,
    BuyerQueryPlan,
    BuyerQueryPlanRequest,
    BuyerQueryPlanner,
    InsufficientBuyerQueriesError,
    SemanticScorer,
)
from .repository import BuyerRepositoryValidationError
from .scoring import score_buyer_project
from .supervisor import BuyerDiscoveryFleetState, BuyerDiscoverySupervisor, BuyerDiscoverySupervisorError


BUYER_DISCOVERY_SOURCES = (MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE)
"""All supported read sources. Existing durable tasks may reference either source."""

DEFAULT_BUYER_DISCOVERY_SOURCES = (MOBILE_PROJECT_SOURCE,)
"""Primary discovery avoids fetching the same Kwork page through two equivalent APIs."""

BuyerRolloutGateChecker = Callable[[str, int], bool | Mapping[str, Any] | Awaitable[bool | Mapping[str, Any]]]
BuyerTaxonomyContextProvider = Callable[[Mapping[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class BuyerSearchServiceError(RuntimeError):
    """Base error for predictable Buyer Search API failures."""


class BuyerSearchRunNotFoundError(BuyerSearchServiceError):
    """Raised when an operation references an absent durable run."""


class BuyerSearchStateError(BuyerSearchServiceError):
    """Raised when an operator requests an invalid run transition."""


@dataclass(frozen=True, slots=True)
class BuyerSearchSettings:
    """Feature flags and bounded runtime limits for the new product surface."""

    enabled: bool = True
    live_discovery: bool = True
    require_shadow_gate: bool = False
    max_workers: int = 30
    discovery_sources: tuple[str, ...] = BUYER_DISCOVERY_SOURCES
    initial_query_probe_count: int = 3
    auto_query_replenishment: bool = True
    auto_query_replenishment_max_batches: int = 3
    auto_query_replenishment_minimum_count: int = 3
    auto_query_replenishment_retry_seconds: int = 15
    auto_query_replenishment_retry_max_seconds: int = 300
    max_query_plan_size: int = 30
    auto_project_refresh_seconds: int = 120
    auto_project_refresh_query_limit: int = 3
    attachment_download: bool = False
    attachment_vision: bool = False
    proposal_send: bool = False
    proposal_attachments: bool = False
    conversation_sync: bool = False
    conversation_send: bool = False
    taxonomy_refresh: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_workers, bool) or not isinstance(self.max_workers, int) or not 1 <= self.max_workers <= 30:
            raise ValueError("max_workers must be an integer between 1 and 30")
        if not self.discovery_sources or any(source not in BUYER_DISCOVERY_SOURCES for source in self.discovery_sources):
            raise ValueError(f"discovery_sources must contain values from {BUYER_DISCOVERY_SOURCES}")
        if len(set(self.discovery_sources)) != len(self.discovery_sources):
            raise ValueError("discovery_sources cannot contain duplicates")
        if (
            isinstance(self.initial_query_probe_count, bool)
            or not isinstance(self.initial_query_probe_count, int)
            or not 1 <= self.initial_query_probe_count <= 10
        ):
            raise ValueError("initial_query_probe_count must be an integer between 1 and 10")
        if (
            isinstance(self.auto_query_replenishment_max_batches, bool)
            or not isinstance(self.auto_query_replenishment_max_batches, int)
            or not 1 <= self.auto_query_replenishment_max_batches <= 100
        ):
            raise ValueError("auto_query_replenishment_max_batches must be an integer between 1 and 100")
        if (
            isinstance(self.auto_query_replenishment_minimum_count, bool)
            or not isinstance(self.auto_query_replenishment_minimum_count, int)
            or not 1 <= self.auto_query_replenishment_minimum_count <= 120
        ):
            raise ValueError("auto_query_replenishment_minimum_count must be an integer between 1 and 120")
        if (
            isinstance(self.auto_query_replenishment_retry_seconds, bool)
            or not isinstance(self.auto_query_replenishment_retry_seconds, int)
            or not 1 <= self.auto_query_replenishment_retry_seconds <= 3_600
        ):
            raise ValueError("auto_query_replenishment_retry_seconds must be an integer between 1 and 3600")
        if (
            isinstance(self.auto_query_replenishment_retry_max_seconds, bool)
            or not isinstance(self.auto_query_replenishment_retry_max_seconds, int)
            or not self.auto_query_replenishment_retry_seconds
            <= self.auto_query_replenishment_retry_max_seconds
            <= 3_600
        ):
            raise ValueError(
                "auto_query_replenishment_retry_max_seconds must be between retry_seconds and 3600"
            )
        if (
            isinstance(self.max_query_plan_size, bool)
            or not isinstance(self.max_query_plan_size, int)
            or not 1 <= self.max_query_plan_size <= 300
        ):
            raise ValueError("max_query_plan_size must be an integer between 1 and 300")
        if (
            isinstance(self.auto_project_refresh_seconds, bool)
            or not isinstance(self.auto_project_refresh_seconds, int)
            or not 30 <= self.auto_project_refresh_seconds <= 3_600
        ):
            raise ValueError("auto_project_refresh_seconds must be an integer between 30 and 3600")
        if (
            isinstance(self.auto_project_refresh_query_limit, bool)
            or not isinstance(self.auto_project_refresh_query_limit, int)
            or not 1 <= self.auto_project_refresh_query_limit <= 10
        ):
            raise ValueError("auto_project_refresh_query_limit must be an integer between 1 and 10")

    @classmethod
    def from_env(cls) -> "BuyerSearchSettings":
        retry_seconds = _env_int(
            "BUYER_SEARCH_AUTO_QUERY_REPLENISHMENT_RETRY_SECONDS", 15, minimum=1, maximum=3_600
        )
        retry_max_seconds = _env_int(
            "BUYER_SEARCH_AUTO_QUERY_REPLENISHMENT_RETRY_MAX_SECONDS", 300, minimum=1, maximum=3_600
        )
        return cls(
            enabled=_env_bool("BUYER_SEARCH_ENABLED", True),
            live_discovery=_env_bool("BUYER_SEARCH_LIVE_DISCOVERY", True),
            require_shadow_gate=_env_bool("BUYER_SEARCH_REQUIRE_SHADOW_GATE", False),
            max_workers=_env_int("BUYER_SEARCH_MAX_WORKERS", 30, minimum=1, maximum=30),
            discovery_sources=_env_discovery_sources(),
            initial_query_probe_count=_env_int(
                "BUYER_SEARCH_INITIAL_QUERY_PROBE_COUNT", 3, minimum=1, maximum=10
            ),
            auto_query_replenishment=_env_bool("BUYER_SEARCH_AUTO_QUERY_REPLENISHMENT", True),
            auto_query_replenishment_max_batches=_env_int(
                "BUYER_SEARCH_AUTO_QUERY_REPLENISHMENT_MAX_BATCHES", 3, minimum=1, maximum=100
            ),
            auto_query_replenishment_minimum_count=_env_int(
                "BUYER_SEARCH_AUTO_QUERY_REPLENISHMENT_MINIMUM_COUNT", 3, minimum=1, maximum=120
            ),
            auto_query_replenishment_retry_seconds=retry_seconds,
            auto_query_replenishment_retry_max_seconds=max(retry_seconds, retry_max_seconds),
            max_query_plan_size=_env_int(
                "BUYER_SEARCH_MAX_QUERY_PLAN_SIZE", 30, minimum=1, maximum=300
            ),
            auto_project_refresh_seconds=_env_int(
                "BUYER_SEARCH_AUTO_PROJECT_REFRESH_SECONDS", 120, minimum=30, maximum=3_600
            ),
            auto_project_refresh_query_limit=_env_int(
                "BUYER_SEARCH_AUTO_PROJECT_REFRESH_QUERY_LIMIT", 3, minimum=1, maximum=10
            ),
            attachment_download=_env_bool("BUYER_ATTACHMENT_DOWNLOAD", False),
            attachment_vision=_env_bool("BUYER_ATTACHMENT_VISION", False),
            proposal_send=_env_bool("BUYER_PROPOSAL_SEND", False),
            proposal_attachments=_env_bool("BUYER_PROPOSAL_ATTACHMENTS", False),
            conversation_sync=_env_bool("BUYER_CONVERSATION_SYNC", False),
            conversation_send=_env_bool("BUYER_CONVERSATION_SEND", False),
            taxonomy_refresh=_env_bool("BUYER_TAXONOMY_REFRESH", False),
        )


class BuyerSearchService:
    """Create, control and inspect durable Buyer Search runs."""

    def __init__(
        self,
        repository: object,
        *,
        export_root: Path,
        planner: BuyerQueryPlanner | None = None,
        semantic_scorer: SemanticScorer | None = None,
        semantic_threshold: float = 0.9,
        settings: BuyerSearchSettings | None = None,
        supervisor: BuyerDiscoverySupervisor | None = None,
        runtime_initializer: Callable[[Mapping[str, Any], Sequence[str]], Awaitable[str]] | None = None,
        runtime_finalizer: Callable[[str, str], Awaitable[object]] | None = None,
        query_generation_gateway: BuyerQueryGenerationGateway | None = None,
        taxonomy_context_provider: BuyerTaxonomyContextProvider | None = None,
        final_scoring_gateway: BuyerFinalScoringGateway | None = None,
        rollout_gate_checker: BuyerRolloutGateChecker | None = None,
    ) -> None:
        if not callable(getattr(repository, "create_run", None)):
            raise TypeError("repository must implement the Buyer Search run API")
        if planner is not None and semantic_scorer is not None:
            raise ValueError("provide either planner or semantic_scorer, not both")
        self.repository = repository
        self.export_root = Path(export_root)
        self.planner = planner or BuyerQueryPlanner(
            semantic_scorer=semantic_scorer,
            semantic_threshold=semantic_threshold,
        )
        self.settings = settings or BuyerSearchSettings.from_env()
        self.supervisor = supervisor
        self.runtime_initializer = runtime_initializer
        self.runtime_finalizer = runtime_finalizer
        if query_generation_gateway is not None and not callable(getattr(query_generation_gateway, "generate", None)):
            raise TypeError("query_generation_gateway must expose generate")
        if taxonomy_context_provider is not None and not callable(taxonomy_context_provider):
            raise TypeError("taxonomy_context_provider must be callable")
        self.query_generation_gateway = query_generation_gateway
        self.taxonomy_context_provider = taxonomy_context_provider
        self.final_scoring_gateway = final_scoring_gateway
        if rollout_gate_checker is not None and not callable(rollout_gate_checker):
            raise TypeError("rollout_gate_checker must be callable")
        self.rollout_gate_checker = rollout_gate_checker
        self._completion_tasks: dict[str, asyncio.Task[None]] = {}
        self._replenishment_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._completion_locks: dict[str, asyncio.Lock] = {}

    async def create_run(self, payload: Mapping[str, Any], *, created_by: str = "api") -> dict[str, Any]:
        """Create a run, persist its query plan, and queue one page per query."""

        self._require_enabled()
        request = _mapping(payload, "payload")
        run_id = str(uuid4())
        mode = _run_mode(request.get("mode"))
        requested_workers = _bounded_int(request.get("requested_workers", 1), "requested_workers", minimum=1, maximum=self.settings.max_workers)
        query_batch_size = _bounded_int(request.get("query_batch_size", 1), "query_batch_size", minimum=1, maximum=10)
        target_unique_projects = _bounded_int(
            request.get("target_unique_projects", 100), "target_unique_projects", minimum=1, maximum=1_000_000
        )
        category_scope = _mapping_or_empty(request.get("category_scope"))
        filters = _mapping_or_empty(request.get("filters"))
        category_id = _positive_int_or_none(category_scope.get("category_id") or request.get("category_id"))
        category_path = _category_path(category_scope.get("category_path") or request.get("category_path") or ())
        if category_id is not None and category_path and category_path[-1] != category_id:
            category_path = (*category_path, category_id)
        category_scopes = _category_scopes(
            category_scope,
            fallback_category_id=category_id,
            fallback_category_path=category_path,
        )
        primary_scope = category_scopes[0]
        category_id = _positive_int_or_none(primary_scope.get("category_id"))
        category_path = _category_path(primary_scope.get("category_path") or ())
        brief = _optional_text(request.get("brief"))
        supplied_queries = _query_inputs(request.get("queries"))
        account_registration_ids = _account_registration_ids(request.get("account_registration_ids"))
        if mode is BuyerRunMode.CATEGORY:
            planned_worker_count = 1
            planned_query_batch_size = max(1, len(category_scopes))
            generation_result = _category_browse_generation(category_scopes, filters=filters)
        elif mode is BuyerRunMode.MANUAL:
            planned_worker_count, planned_query_batch_size = _bounded_plan_shape(
                requested_workers,
                query_batch_size,
                maximum_queries=self.settings.max_query_plan_size,
            )
        else:
            # Kwork returns one category listing for many nearby phrases.  A
            # tiny first wave measures actual coverage before we spend the
            # whole worker pool on what may be the very same cards.
            planned_worker_count = 1
            planned_query_batch_size = _probe_query_count(
                scope_count=len(category_scopes),
                queries_per_scope=self.settings.initial_query_probe_count,
                maximum_queries=self.settings.max_query_plan_size,
            )
        if mode is not BuyerRunMode.CATEGORY:
            generation_result = await self._generate_query_candidates_for_scopes(
                mode=mode,
                brief=brief or "",
                category_scopes=category_scopes,
                filters=filters,
                supplied_queries=supplied_queries,
                minimum_count=planned_worker_count * planned_query_batch_size,
            )
            if mode is BuyerRunMode.HYBRID:
                generation_result = _merge_query_generation_results(
                    (_category_browse_generation(category_scopes, filters=filters), generation_result)
                )
        generation = generation_result.generation
        plan = self.planner.plan(
            BuyerQueryPlanRequest(
                run_id=run_id,
                mode=mode,
                worker_count=planned_worker_count,
                queries_per_worker=planned_query_batch_size,
                candidates=generation.candidates,
                category_id=category_id,
                category_path=category_path,
                filters=filters,
                auto_approve=True,
            )
        )
        now = _utc_now()
        record = {
            "run_id": run_id,
            "name": _run_name(request.get("name"), run_id),
            "mode": mode.value,
            "brief": brief,
            "category_scope": _stored_category_scope(
                category_scope,
                category_id=category_id,
                category_path=category_path,
                category_scopes=category_scopes,
            ),
            "filters": filters,
            "requested_workers": requested_workers,
            "query_batch_size": query_batch_size,
            "target_unique_projects": target_unique_projects,
            "enrichment_policy": _mapping_or_empty(request.get("enrichment_policy")),
            "scoring_profile_id": _optional_text(request.get("scoring_profile_id")) or "deterministic-v1",
            "state": BuyerRunState.PLANNING.value,
            "created_by": _optional_text(created_by) or "api",
            "config_version": 1,
            "config": {"account_registration_ids": list(account_registration_ids)},
            "counters": _initial_counters(plan),
            "created_at": now,
            "updated_at": now,
        }
        created = await self.repository.create_run(record)
        if self.runtime_initializer is not None:
            try:
                job_id = await self.runtime_initializer(
                    {**dict(created), "account_registration_ids": list(account_registration_ids)},
                    account_registration_ids,
                )
                created = await self.repository.update_run(run_id, {"job_id": _required_text(job_id, "Buyer backing job_id")})
            except Exception as exc:
                detail = f"Buyer discovery runtime setup failed: {type(exc).__name__}: {exc}"
                await self.repository.update_run(
                    run_id,
                    {"state": BuyerRunState.FAILED.value, "last_error": detail, "updated_at": _utc_now()},
                )
                await self._append_event(run_id, "run.failed", {"reason": detail})
                raise BuyerSearchServiceError(detail) from exc
        await self._append_event(
            run_id,
            "run.created",
            {
                "mode": mode.value,
                "requested_workers": requested_workers,
                "query_batch_size": query_batch_size,
                "initial_probe_query_count": len(plan.queries),
                "query_generator": generation.generator,
                "used_fallback": generation.used_fallback,
                "ai_used": generation_result.used_model,
                "ai_error": generation_result.error,
            },
        )
        distribution = await self._persist_plan(run_id, plan)
        updated = await self.repository.update_run(
            run_id,
            {
                "state": BuyerRunState.READY.value,
                "updated_at": _utc_now(),
            },
        )
        await self._append_event(
            run_id,
            "run.ready",
            {
                "query_count": len(plan.queries),
                "collision_count": len(plan.collisions),
                "task_count": distribution["queued_task_count"],
            },
        )
        return dict(updated)

    async def list_runs(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        self._require_enabled()
        records = await self.repository.list_runs(limit=_bounded_int(limit, "limit", minimum=1, maximum=500), cursor=cursor)
        if isinstance(records, Mapping):
            items = [dict(record) for record in records.get("items") or ()]
            for index, record in enumerate(items):
                if _run_state(record.get("state")) is BuyerRunState.RUNNING:
                    completed = await self._complete_exhausted_run(_required_text(record.get("run_id"), "run_id"))
                    if completed is not None:
                        items[index] = completed
            return {**dict(records), "items": items}
        items = [dict(record) for record in records]
        for index, record in enumerate(items):
            if _run_state(record.get("state")) is BuyerRunState.RUNNING:
                completed = await self._complete_exhausted_run(_required_text(record.get("run_id"), "run_id"))
                if completed is not None:
                    items[index] = completed
        return {"items": items, "next_cursor": None}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        self._require_enabled()
        run = await self.repository.get_run(_required_text(run_id, "run_id"))
        if run is None:
            raise BuyerSearchRunNotFoundError(run_id)
        return dict(run)

    async def delete_run(self, run_id: str) -> dict[str, Any]:
        """Delete a non-active run after releasing its in-memory worker fleet."""

        run = await self.get_run(run_id)
        current = _run_state(run.get("state"))
        active = {
            BuyerRunState.PLANNING,
            BuyerRunState.RUNNING,
            BuyerRunState.PAUSING,
            BuyerRunState.STOPPING,
            BuyerRunState.COMPLETING,
        }
        if current in active:
            raise BuyerSearchStateError("остановите активный запуск перед удалением")
        if not callable(getattr(self.repository, "delete_run", None)):
            raise BuyerSearchServiceError("repository does not support Buyer Search run deletion")
        if self.supervisor is not None and run_id in self.supervisor.run_ids:
            try:
                await self.supervisor.stop(run_id, reason="deleted")
            except BuyerDiscoverySupervisorError as exc:
                raise BuyerSearchServiceError(str(exc)) from exc
        deleted = await self.repository.delete_run(_required_text(run_id, "run_id"))
        logger.info(f"Поиск заказов [{run_id[:8]}]: запуск удалён")
        return dict(deleted)

    async def update_run(self, run_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a narrow operator edit or a guarded lifecycle transition."""

        self._require_enabled()
        run = await self.get_run(run_id)
        request = _mapping(changes, "changes")
        normalized: dict[str, Any] = {}
        if "name" in request:
            normalized["name"] = _run_name(request["name"], run_id)
        if "filters" in request:
            normalized["filters"] = _mapping_or_empty(request["filters"])
        if "requested_workers" in request:
            normalized["requested_workers"] = _bounded_int(
                request["requested_workers"], "requested_workers", minimum=1, maximum=self.settings.max_workers
            )
        state = request.get("state")
        event_type: str | None = None
        target: BuyerRunState | None = None
        if state is not None:
            target = _run_state(state)
            _validate_transition(_run_state(run.get("state")), target)
            if target is BuyerRunState.RUNNING and self.supervisor is not None and not self.settings.live_discovery:
                target = BuyerRunState.BLOCKED
                normalized["last_error"] = "live discovery disabled by feature flag"
            if (
                target is BuyerRunState.RUNNING
                and self.supervisor is not None
                and self.settings.require_shadow_gate
                and self.rollout_gate_checker is not None
            ):
                requested_workers = int(normalized.get("requested_workers") or run.get("requested_workers") or 1)
                gate_value = self.rollout_gate_checker(run_id, requested_workers)
                gate = await gate_value if inspect.isawaitable(gate_value) else gate_value
                allowed, reason = _rollout_gate_result(gate)
                if not allowed:
                    target = BuyerRunState.BLOCKED
                    normalized["last_error"] = reason or "shadow rollout gate has not approved this canary"
            if target is BuyerRunState.RUNNING and self.runtime_initializer is not None and not run.get("job_id"):
                account_registration_ids = _account_registration_ids(
                    _mapping_or_empty(run.get("config")).get("account_registration_ids")
                )
                try:
                    job_id = await self.runtime_initializer(
                        {**run, "account_registration_ids": list(account_registration_ids)},
                        account_registration_ids,
                    )
                except Exception as exc:
                    raise BuyerSearchServiceError(
                        f"Buyer discovery runtime setup failed: {type(exc).__name__}: {exc}"
                    ) from exc
                normalized["job_id"] = _required_text(job_id, "Buyer backing job_id")
            if target is BuyerRunState.RUNNING and self.supervisor is not None:
                try:
                    fleet = await self.supervisor.start(
                        run_id,
                        requested_workers=int(request.get("requested_workers") or run.get("requested_workers") or 1),
                    )
                except BuyerDiscoverySupervisorError as exc:
                    raise BuyerSearchServiceError(str(exc)) from exc
                if fleet.state is BuyerDiscoveryFleetState.BLOCKED:
                    target = BuyerRunState.BLOCKED
                    normalized["last_error"] = fleet.reason or "не удалось выделить воркеры для поиска"
            if target is BuyerRunState.RUNNING:
                normalized["last_error"] = None
            normalized["state"] = target.value
            now = _utc_now()
            if target is BuyerRunState.RUNNING:
                normalized["started_at"] = run.get("started_at") or now
                event_type = "run.started" if _run_state(run.get("state")) is not BuyerRunState.PAUSED else "run.resumed"
            elif target is BuyerRunState.PAUSED:
                event_type = "run.paused"
            elif target is BuyerRunState.STOPPED:
                normalized["stopped_at"] = now
                event_type = "run.stopped"
            elif target is BuyerRunState.BLOCKED:
                event_type = "run.blocked"
        if not normalized:
            raise ValueError("at least one editable Buyer Search field is required")
        normalized["updated_at"] = _utc_now()
        updated = await self.repository.update_run(_required_text(run_id, "run_id"), normalized)
        if self.supervisor is not None:
            try:
                if target is BuyerRunState.PAUSED and run_id in self.supervisor.run_ids:
                    await self.supervisor.pause(run_id)
                elif target is BuyerRunState.STOPPED and run_id in self.supervisor.run_ids:
                    await self.supervisor.stop(run_id)
            except BuyerDiscoverySupervisorError as exc:
                await self._append_event(run_id, "runtime.warning", {"detail": str(exc)})
        if target is BuyerRunState.STOPPED and self.runtime_finalizer is not None:
            try:
                await self.runtime_finalizer(run_id, "buyer_run_stopped")
            except Exception as exc:  # noqa: BLE001 - the Buyer run is already durably stopped.
                await self._append_event(run_id, "runtime.warning", {"detail": f"backing job finalization failed: {exc}"})
        if event_type:
            event_payload: dict[str, Any] = {"state": normalized["state"]}
            if target is BuyerRunState.BLOCKED and normalized.get("last_error"):
                event_payload["reason"] = normalized["last_error"]
            await self._append_event(run_id, event_type, event_payload)
        return dict(updated)

    async def restart_run(self, run_id: str) -> dict[str, Any]:
        """Fence and replay an explicitly stopped, failed, or completed run.

        A restart preserves projects, observations, artifacts, and query-plan
        history.  It only requeues the existing pages, so operators can review
        the new durable evidence before pressing the separate start command.
        """

        run = await self.get_run(run_id)
        current = _run_state(run.get("state"))
        restartable = {BuyerRunState.STOPPED, BuyerRunState.COMPLETED, BuyerRunState.FAILED, BuyerRunState.BLOCKED}
        if current not in restartable:
            raise BuyerSearchStateError(f"cannot restart Buyer Search run from {current.value}")
        if not callable(getattr(self.repository, "restart_query_tasks", None)):
            raise BuyerSearchServiceError("repository does not support Buyer Search task restart")

        if self.supervisor is not None and run_id in self.supervisor.run_ids:
            try:
                await self.supervisor.stop(run_id)
            except BuyerDiscoverySupervisorError as exc:
                raise BuyerSearchServiceError(str(exc)) from exc
        restarted = await self.repository.restart_query_tasks(_required_text(run_id, "run_id"))
        updated = await self.repository.update_run(
            _required_text(run_id, "run_id"),
            {
                "state": BuyerRunState.READY.value,
                "last_error": None,
                "last_failure_kind": None,
                "last_task_id": None,
                "completed_at": None,
                "stopped_at": None,
                "updated_at": _utc_now(),
            },
        )
        await self._append_event(
            run_id,
            "run.restarted",
            {
                "from_state": current.value,
                "state": BuyerRunState.READY.value,
                **(dict(restarted) if isinstance(restarted, Mapping) else {}),
            },
        )
        return {**dict(updated), "restart": dict(restarted) if isinstance(restarted, Mapping) else {}}

    async def continue_to_target(self, run_id: str) -> dict[str, Any]:
        """Resume a terminal run with fresh queries without replaying old pages."""

        normalized_run_id = _required_text(run_id, "run_id")
        lock = self._completion_locks.setdefault(normalized_run_id, asyncio.Lock())
        async with lock:
            run = await self.get_run(normalized_run_id)
            counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
            unique_projects = _integer(counters.get("unique_projects"), default=0)
            target_unique_projects = _integer(run.get("target_unique_projects"), default=0)
            if target_unique_projects and unique_projects >= target_unique_projects:
                return run
            current = _run_state(run.get("state"))
            if current is BuyerRunState.RUNNING:
                self.schedule_completion_check(normalized_run_id)
                return run
            if current not in {BuyerRunState.COMPLETED, BuyerRunState.STOPPED, BuyerRunState.FAILED, BuyerRunState.BLOCKED}:
                raise BuyerSearchStateError("Запуск пока нельзя продолжить до цели")

            pending_tasks = await self._has_pending_query_tasks(normalized_run_id)
            created_count = 0
            if not pending_tasks:
                if (
                    _live_project_refresh_enabled(counters)
                    or not self.settings.auto_query_replenishment
                    or _should_switch_to_live_project_refresh(counters)
                ):
                    await self._enable_live_project_refresh(run)
                else:
                    created_count = await self._replenish_query_queue(run, force=True)
            updated = await self.repository.update_run(
                normalized_run_id,
                {
                    "state": BuyerRunState.RUNNING.value,
                    "completed_at": None,
                    "stopped_at": None,
                    "last_error": None,
                    "last_failure_kind": None,
                    "last_task_id": None,
                },
            )
            if self.supervisor is not None:
                try:
                    fleet = await self.supervisor.start(
                        normalized_run_id,
                        requested_workers=int(updated.get("requested_workers") or 1),
                    )
                except BuyerDiscoverySupervisorError as exc:
                    updated = await self.repository.update_run(
                        normalized_run_id,
                        {"state": BuyerRunState.BLOCKED.value, "last_error": str(exc)},
                    )
                else:
                    if fleet.state is BuyerDiscoveryFleetState.BLOCKED:
                        updated = await self.repository.update_run(
                            normalized_run_id,
                            {
                                "state": BuyerRunState.BLOCKED.value,
                                "last_error": fleet.reason or "Не удалось выделить воркеры для поиска",
                            },
                        )
            final_state = _run_state(updated.get("state"))
            updated_counters = updated.get("counters") if isinstance(updated.get("counters"), Mapping) else {}
            if final_state is BuyerRunState.RUNNING and _live_project_refresh_enabled(updated_counters) and not pending_tasks:
                await self._schedule_query_replenishment_retry(updated, reason="manual_recovery_live_refresh")
            elif not created_count and final_state is BuyerRunState.RUNNING and not pending_tasks:
                await self._schedule_query_replenishment_retry(
                    updated,
                    reason="manual_recovery_no_new_queries",
                )
            await self._append_event(
                normalized_run_id,
                "run.continued_to_target" if final_state is BuyerRunState.RUNNING else "run.blocked",
                {
                    "state": final_state.value,
                    "created_count": created_count,
                    "unique_projects": unique_projects,
                    "target_unique_projects": target_unique_projects or None,
                    "reason": updated.get("last_error") if final_state is BuyerRunState.BLOCKED else None,
                },
            )
            return dict(updated)

    async def compact_query_plan(self, run_id: str, *, maximum_queries: int | None = None) -> dict[str, Any]:
        """Keep only representative queries after a saturated historical plan."""

        normalized_run_id = _required_text(run_id, "run_id")
        run = await self.get_run(normalized_run_id)
        state = _run_state(run.get("state"))
        if state not in {
            BuyerRunState.DRAFT,
            BuyerRunState.READY,
            BuyerRunState.PAUSED,
            BuyerRunState.STOPPED,
            BuyerRunState.COMPLETED,
            BuyerRunState.FAILED,
            BuyerRunState.BLOCKED,
        }:
            raise BuyerSearchStateError("pause the run before compacting its query plan")
        if not callable(getattr(self.repository, "compact_query_plan", None)):
            raise BuyerSearchServiceError("repository does not support Buyer Search query-plan compaction")

        limit = _bounded_int(
            maximum_queries if maximum_queries is not None else self.settings.max_query_plan_size,
            "maximum_queries",
            minimum=1,
            maximum=self.settings.max_query_plan_size,
        )
        queries = await self.list_queries(normalized_run_id, limit=1_000)
        items = [dict(item) for item in queries.get("items") or [] if isinstance(item, Mapping)]
        enabled_items = [item for item in items if item.get("enabled") is not False]
        if not enabled_items:
            raise BuyerSearchStateError("the run has no enabled queries to retain")

        coverage: dict[str, set[str]] = {}
        coverage_reader = getattr(self.repository, "list_query_coverage", None)
        if callable(coverage_reader):
            raw_coverage = coverage_reader(normalized_run_id)
            coverage_items = await raw_coverage if inspect.isawaitable(raw_coverage) else raw_coverage
            if isinstance(coverage_items, Sequence) and not isinstance(coverage_items, (str, bytes, bytearray)):
                for item in coverage_items:
                    if not isinstance(item, Mapping):
                        continue
                    query_id = _optional_text(item.get("query_id"))
                    project_ids = item.get("project_ids")
                    if not query_id or not isinstance(project_ids, Sequence) or isinstance(project_ids, (str, bytes, bytearray)):
                        continue
                    coverage[query_id] = {
                        str(project_id)
                        for project_id in project_ids
                        if _optional_text(project_id) is not None
                    }

        retained = _select_query_plan_representatives(enabled_items, coverage=coverage, limit=limit)
        result = await self.repository.compact_query_plan(normalized_run_id, retained)
        updated = await self.get_run(normalized_run_id)
        await self._append_event(
            normalized_run_id,
            "query.plan.compacted",
            {
                "retained_query_count": len(retained),
                "archived_query_count": max(0, len(enabled_items) - len(retained)),
                "query_plan_limit": limit,
            },
        )
        return {**dict(updated), "plan_compaction": dict(result) if isinstance(result, Mapping) else {}}

    async def list_queries(
        self,
        run_id: str,
        *,
        state: str | None = None,
        include_disabled: bool = False,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        run = await self.get_run(run_id)
        records = await self.repository.list_queries(
            _required_text(run_id, "run_id"), state=state, limit=_bounded_int(limit, "limit", minimum=1, maximum=1000), cursor=cursor
        )
        items = [dict(record) for record in records.get("items") or ()] if isinstance(records, Mapping) else [dict(record) for record in records]
        items = await self._repair_legacy_category_queries(run_id, run, items)
        if not include_disabled:
            items = [item for item in items if item.get("enabled") is not False]
        if isinstance(records, Mapping):
            return {**dict(records), "items": items}
        return {"items": items, "next_cursor": None}

    async def generate_queries(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        created_by: str = "api",
    ) -> dict[str, Any]:
        """Append a deduplicated, reviewable query batch to an existing run.

        The endpoint deliberately does not mutate the original plan in place:
        generated candidates remain individually auditable and only approved,
        enabled entries receive durable page tasks.
        """

        run = await self.get_run(run_id)
        request = _mapping(payload, "payload")
        requested_workers = _bounded_int(
            request.get("requested_workers", run.get("requested_workers") or 1),
            "requested_workers",
            minimum=1,
            maximum=self.settings.max_workers,
        )
        query_batch_size = _bounded_int(
            request.get("query_batch_size", run.get("query_batch_size") or 1),
            "query_batch_size",
            minimum=1,
            maximum=10,
        )
        category_scope = _mapping_or_empty(run.get("category_scope"))
        supplied_scope = _mapping_or_empty(request.get("category_scope"))
        category_scope = {**category_scope, **supplied_scope}
        filters = _mapping_or_empty(request.get("filters", run.get("filters") or {}))
        category_id = _positive_int_or_none(category_scope.get("category_id"))
        category_path = _category_path(category_scope.get("category_path") or ())
        if category_id is not None and category_path and category_path[-1] != category_id:
            category_path = (*category_path, category_id)
        category_scopes = _category_scopes(
            category_scope,
            fallback_category_id=category_id,
            fallback_category_path=category_path,
        )
        primary_scope = category_scopes[0]
        category_id = _positive_int_or_none(primary_scope.get("category_id"))
        category_path = _category_path(primary_scope.get("category_path") or ())
        mode = _run_mode(request.get("mode", run.get("mode")))
        supplied_queries = _query_inputs(request.get("queries"))
        existing = await self.list_queries(run_id, include_disabled=True, limit=1_000)
        active_query_count = sum(
            1
            for query in existing.get("items") or []
            if isinstance(query, Mapping) and query.get("enabled") is not False
        )
        remaining_query_capacity = self.settings.max_query_plan_size - active_query_count
        if remaining_query_capacity <= 0:
            raise BuyerSearchStateError(
                f"query plan limit reached ({self.settings.max_query_plan_size}); compact or disable existing queries first"
            )
        excluded_queries = tuple(
            _required_text(query.get("text"), "existing query text")
            for query in existing.get("items") or []
            if isinstance(query, Mapping) and _optional_text(query.get("text"))
        )
        requested_minimum = _bounded_int(
            request.get("minimum_count", requested_workers * query_batch_size),
            "minimum_count",
            minimum=1,
            maximum=300,
        )
        planned_worker_count, planned_query_batch_size = _bounded_plan_shape(
            requested_workers,
            query_batch_size,
            maximum_queries=remaining_query_capacity,
        )
        minimum_count = min(
            requested_minimum,
            planned_worker_count * planned_query_batch_size,
            remaining_query_capacity,
        )
        generation_result = await self._generate_query_candidates_for_scopes(
            mode=mode,
            brief=_optional_text(request.get("brief")) or _optional_text(run.get("brief")) or "",
            category_scopes=category_scopes,
            filters=filters,
            supplied_queries=supplied_queries,
            excluded_queries=excluded_queries,
            minimum_count=minimum_count,
        )
        generation = generation_result.generation
        plan_request = BuyerQueryPlanRequest(
            run_id=_required_text(run_id, "run_id"),
            mode=mode,
            worker_count=planned_worker_count,
            queries_per_worker=planned_query_batch_size,
            candidates=generation.candidates,
            category_id=category_id,
            category_path=category_path,
            filters=filters,
            auto_approve=bool(request.get("auto_approve", False)),
        )
        try:
            plan = self.planner.plan(plan_request)
        except InsufficientBuyerQueriesError as exc:
            # An automatic continuation should consume every fresh phrase it
            # did find.  Requiring a full worker-sized wave here would discard
            # a smaller useful batch and make the run appear stalled.
            if created_by != "automatic_replenishment" or exc.available < 1:
                raise
            plan = self.planner.plan(
                BuyerQueryPlanRequest(
                    run_id=plan_request.run_id,
                    mode=plan_request.mode,
                    worker_count=1,
                    queries_per_worker=exc.available,
                    candidates=plan_request.candidates,
                    category_id=plan_request.category_id,
                    category_path=plan_request.category_path,
                    filters=plan_request.filters,
                    auto_approve=plan_request.auto_approve,
                )
            )
        existing_keys = {
            _query_dedupe_key(query)
            for query in existing.get("items") or []
            if isinstance(query, Mapping)
        }
        payloads = [
            query_payload
            for query_payload in plan.query_payloads()
            if _query_dedupe_key(query_payload) not in existing_keys
        ][:remaining_query_capacity]
        if payloads:
            await self.repository.create_queries(_required_text(run_id, "run_id"), payloads)
        await self._record_plan_collisions(run_id, plan)
        distribution = await self.distribute_queries(run_id, query_ids=[payload["query_id"] for payload in payloads])
        await self._append_event(
            run_id,
            "query.generated",
            {
                "created_by": _optional_text(created_by) or "api",
                "generator": generation.generator,
                "used_fallback": generation.used_fallback,
                "ai_used": generation_result.used_model,
                "ai_error": generation_result.error,
                "candidate_count": len(plan.queries),
                "created_count": len(payloads),
                "duplicate_count": len(plan.queries) - len(payloads),
            },
        )
        return {
            "items": payloads,
            "collision_count": len(plan.collisions),
            "generator": generation.generator,
            "used_fallback": generation.used_fallback,
            "ai_used": generation_result.used_model,
            "ai_error": generation_result.error,
            "distribution": distribution,
        }

    async def _generate_query_candidates(
        self,
        request: BuyerQueryGenerationInput,
        *,
        category_scope: Mapping[str, Any],
    ) -> BuyerAIQueryGenerationResult:
        """Generate reviewable candidates with a taxonomy-aware model pass when eligible.

        Operator-authored manual plans deliberately stay deterministic.  The
        model is only an optional planner aid for brief, category, and hybrid
        inputs; the central durable planner remains responsible for all
        uniqueness and collision decisions.
        """

        if request.mode is BuyerRunMode.MANUAL:
            return await generate_buyer_query_candidates_with_ai(request, gateway=None)
        if self.query_generation_gateway is None:
            raise BuyerSearchServiceError("AI gateway is not configured for this search mode")
        taxonomy_context = await self._taxonomy_context(category_scope)
        generated = await generate_buyer_query_candidates_with_ai(
            request,
            gateway=self.query_generation_gateway,
            taxonomy_context=taxonomy_context,
        )
        if generated.error and not generated.generation.candidates:
            raise BuyerSearchServiceError(f"AI не сформировал план запросов: {generated.error}")
        return generated

    async def _generate_query_candidates_for_scopes(
        self,
        *,
        mode: BuyerRunMode,
        brief: str,
        category_scopes: Sequence[Mapping[str, Any]],
        filters: Mapping[str, Any],
        supplied_queries: Sequence[str | Mapping[str, Any]],
        minimum_count: int,
        excluded_queries: Sequence[str] = (),
    ) -> BuyerAIQueryGenerationResult:
        """Generate candidates per selected rubric so every query keeps its scope."""

        scope_count = max(1, len(category_scopes))
        minimum_per_scope = max(1, (minimum_count + scope_count - 1) // scope_count)
        scopes = tuple(category_scopes)
        if not scopes:
            raise BuyerSearchServiceError("query generation requires at least one category scope")

        async def generate_for_scope(scope: Mapping[str, Any]) -> BuyerAIQueryGenerationResult:
            category_id = _positive_int_or_none(scope.get("category_id"))
            category_path = _category_path(scope.get("category_path") or ())
            if category_id is not None and category_path and category_path[-1] != category_id:
                category_path = (*category_path, category_id)
            generated = await self._generate_query_candidates(
                BuyerQueryGenerationInput(
                    mode=mode,
                    brief=brief,
                    category_id=category_id,
                    category_name=_optional_text(scope.get("category_name") or scope.get("name")),
                    category_path=category_path,
                    filters=filters,
                    supplied_queries=supplied_queries,
                    excluded_queries=excluded_queries,
                    minimum_count=minimum_per_scope,
                ),
                category_scope=scope,
            )
            if mode is not BuyerRunMode.MANUAL:
                bounded_candidates = _bounded_generated_candidates(
                    generated.generation.candidates,
                    maximum=minimum_per_scope,
                )
                generated = BuyerAIQueryGenerationResult(
                    generation=BuyerQueryGenerationResult(
                        candidates=bounded_candidates,
                        generator=generated.generation.generator,
                        used_fallback=generated.generation.used_fallback,
                    ),
                    prompt=generated.prompt,
                    used_model=generated.used_model,
                    error=generated.error,
                )
            return generated

        results = await asyncio.gather(*(generate_for_scope(scope) for scope in scopes))
        return _merge_query_generation_results(results)

    async def _taxonomy_context(self, category_scope: Mapping[str, Any]) -> Mapping[str, Any]:
        base = {
            "category_id": category_scope.get("category_id"),
            "category_path": category_scope.get("category_path"),
            "taxonomy_snapshot_id": category_scope.get("taxonomy_snapshot_id"),
            "taxonomy_source": category_scope.get("taxonomy_source"),
        }
        if self.taxonomy_context_provider is None:
            return base
        value = self.taxonomy_context_provider(dict(category_scope))
        context = await value if inspect.isawaitable(value) else value
        if not isinstance(context, Mapping):
            raise BuyerSearchServiceError("taxonomy_context_provider must return an object")
        return {**base, "taxonomy": dict(context)}

    async def get_query_collisions(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return the durable collision audit trail for one query plan.

        Collisions are append-only planner evidence.  Keeping them in the
        event stream preserves both exact duplicates (which are deliberately
        not inserted into ``buyer_queries``) and semantic near-matches.
        """

        normalized_after = max(0, int(after_seq))
        normalized_limit = _bounded_int(limit, "limit", minimum=1, maximum=2_000)
        events = await self.replay_events(run_id, after_seq=normalized_after, limit=normalized_limit)
        collisions: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "query.collision":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                payload = {}
            collisions.append(
                {
                    "seq": int(event.get("seq") or 0),
                    "created_at": event.get("created_at"),
                    **dict(payload),
                }
            )
        return {
            "run_id": _required_text(run_id, "run_id"),
            "items": collisions,
            "scanned_event_count": len(events),
            "next_after_seq": int(events[-1].get("seq") or normalized_after) if events else normalized_after,
        }

    async def regenerate_query_collisions(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        created_by: str = "api",
    ) -> dict[str, Any]:
        """Append replacement candidates for recorded collisions.

        This is intentionally additive: a collision can involve an already
        running query, so silently deleting it would invalidate durable task
        provenance.  Operators may review and disable the old query after the
        replacement candidates have been planned and distributed.
        """

        request = _mapping(payload, "payload")
        requested_ids = tuple(
            dict.fromkeys(
                _required_text(query_id, "collision_query_id")
                for query_id in _sequence_of_text(request.get("collision_query_ids", ()), "collision_query_ids")
            )
        )
        report = await self.get_query_collisions(run_id, limit=2_000)
        collision_ids = {
            query_id
            for item in report["items"]
            if isinstance(item, Mapping)
            for query_id in (item.get("first_query_id"), item.get("second_query_id"))
            if isinstance(query_id, str) and query_id.strip()
        }
        if not collision_ids:
            raise BuyerSearchStateError("the run has no recorded query collisions to regenerate")
        unknown_ids = set(requested_ids).difference(collision_ids)
        if unknown_ids:
            raise ValueError(f"collision_query_ids are not present in the collision report: {', '.join(sorted(unknown_ids))}")

        generation_payload = {
            key: value
            for key, value in request.items()
            if key != "collision_query_ids"
        }
        result = await self.generate_queries(run_id, generation_payload, created_by=created_by)
        generated_ids = [
            _required_text(item.get("query_id"), "generated query_id")
            for item in result.get("items") or []
            if isinstance(item, Mapping)
        ]
        await self._append_event(
            run_id,
            "query.collisions.regenerated",
            {
                "collision_query_ids": list(requested_ids),
                "generated_query_ids": generated_ids,
                "created_by": _optional_text(created_by) or "api",
            },
        )
        return {
            "collision_query_ids": list(requested_ids),
            "generation": result,
            "collision_report": await self.get_query_collisions(run_id, limit=2_000),
        }

    async def get_query_bundles(
        self,
        run_id: str,
        *,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        """Build stable worker-index query bundles from durable query/task rows."""

        run = await self.get_run(run_id)
        queries = await self.list_queries(run_id, include_disabled=include_disabled, limit=1_000)
        task_rows = await self.repository.list_query_tasks(_required_text(run_id, "run_id"), limit=5_000)
        tasks_by_query: dict[str, list[dict[str, Any]]] = {}
        for task in task_rows:
            if not isinstance(task, Mapping):
                continue
            query_id = _optional_text(task.get("query_id"))
            if query_id is None:
                continue
            tasks_by_query.setdefault(query_id, []).append(dict(task))

        worker_count = _bounded_int(
            run.get("requested_workers", 1),
            "requested_workers",
            minimum=1,
            maximum=self.settings.max_workers,
        )
        bundles = [
            {
                "worker_index": worker_index,
                "queries": [],
                "task_count": 0,
                "sources": [],
            }
            for worker_index in range(1, worker_count + 1)
        ]
        excluded_query_ids: list[str] = []
        eligible_index = 0
        for query in queries.get("items") or []:
            if not isinstance(query, Mapping):
                continue
            query_id = _required_text(query.get("query_id"), "query_id")
            approved = bool(query.get("approved"))
            enabled = bool(query.get("enabled"))
            if not include_disabled and (not approved or not enabled):
                excluded_query_ids.append(query_id)
                continue
            bundle = bundles[eligible_index % worker_count]
            query_tasks = tasks_by_query.get(query_id, [])
            source_rows = sorted(
                {
                    _optional_text(task.get("source"))
                    for task in query_tasks
                    if _optional_text(task.get("source")) is not None
                }
            )
            bundle["queries"].append(
                {
                    "query_id": query_id,
                    "text": query.get("text"),
                    "priority": query.get("priority"),
                    "state": query.get("state"),
                    "approved": approved,
                    "enabled": enabled,
                    "assignment_order": len(bundle["queries"]) + 1,
                    "sources": source_rows,
                    "task_count": len(query_tasks),
                }
            )
            bundle["task_count"] += len(query_tasks)
            bundle["sources"] = sorted(set(bundle["sources"]) | set(source_rows))
            eligible_index += 1

        return {
            "run_id": _required_text(run_id, "run_id"),
            "requested_workers": worker_count,
            "bundles": bundles,
            "eligible_query_count": eligible_index,
            "excluded_query_ids": excluded_query_ids,
        }

    async def update_query(self, run_id: str, query_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one explicit query edit, approval, or disable command."""

        await self.get_run(run_id)
        request = _mapping(changes, "changes")
        allowed = {
            "text",
            "filters",
            "category_id",
            "category_path",
            "rationale",
            "origin",
            "parent_query_id",
            "priority",
            "predicted_total",
            "approved",
            "enabled",
            "state",
        }
        unknown = set(request).difference(allowed)
        if unknown:
            raise ValueError(f"unsupported query fields: {', '.join(sorted(unknown))}")
        normalized = dict(request)
        if "text" in normalized:
            normalized["text"] = _required_text(normalized["text"], "query text")
        if "filters" in normalized:
            normalized["filters"] = _mapping_or_empty(normalized["filters"])
        if "category_id" in normalized:
            normalized["category_id"] = _positive_int_or_none(normalized["category_id"])
        if "category_path" in normalized:
            normalized["category_path"] = list(_category_path(normalized["category_path"]))
        for flag in ("approved", "enabled"):
            if flag in normalized and not isinstance(normalized[flag], bool):
                raise ValueError(f"{flag} must be a boolean")
        updated = await self.repository.update_query(
            _required_text(run_id, "run_id"),
            _required_text(query_id, "query_id"),
            normalized,
        )
        await self._append_event(
            run_id,
            "query.updated",
            {"query_id": query_id, "fields": sorted(normalized), "approved": updated.get("approved"), "enabled": updated.get("enabled")},
        )
        distribution: dict[str, Any] | None = None
        if bool(updated.get("approved")) and bool(updated.get("enabled")):
            distribution = await self.distribute_queries(run_id, query_ids=[query_id])
        return {"query": dict(updated), "distribution": distribution}

    async def distribute_queries(
        self,
        run_id: str,
        *,
        query_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Queue each source's first page exactly once for approved queries."""

        await self.get_run(run_id)
        wanted = {
            _required_text(query_id, "query_id")
            for query_id in query_ids or ()
        }
        queries = await self.list_queries(run_id, limit=1_000)
        queued: list[str] = []
        queued_tasks: list[dict[str, str]] = []
        skipped: list[str] = []
        for query in queries.get("items") or []:
            if not isinstance(query, Mapping):
                continue
            query_id = _required_text(query.get("query_id"), "query_id")
            if wanted and query_id not in wanted:
                continue
            if not bool(query.get("approved")) or not bool(query.get("enabled")):
                skipped.append(query_id)
                continue
            tasks = await self.repository.list_query_tasks(run_id, query_id=query_id, limit=5_000)
            existing_initial_sources = {
                _optional_text(task.get("source"))
                for task in tasks
                if isinstance(task, Mapping) and _task_page(task) == 1
            }
            newly_queued_sources: list[str] = []
            for source in self.settings.discovery_sources:
                if source in existing_initial_sources:
                    continue
                task = _initial_task(query, source=source)
                try:
                    created = await self.repository.enqueue_query_task(run_id, task)
                except BuyerRepositoryValidationError:
                    raced_tasks = await self.repository.list_query_tasks(run_id, query_id=query_id, limit=5_000)
                    if not any(
                        isinstance(item, Mapping)
                        and _optional_text(item.get("source")) == source
                        and _task_page(item) == 1
                        for item in raced_tasks
                    ):
                        raise
                    continue
                created_task_id = _optional_text(created.get("task_id")) if isinstance(created, Mapping) else None
                queued_tasks.append({"query_id": query_id, "task_id": created_task_id or task["task_id"], "source": source})
                newly_queued_sources.append(source)
            if not newly_queued_sources:
                skipped.append(query_id)
                continue
            await self.repository.update_query(run_id, query_id, {"state": "assigned"})
            queued.append(query_id)
            await self._append_event(
                run_id,
                "query.assigned",
                {
                    "query_id": query_id,
                    "state": "assigned",
                    "sources": newly_queued_sources,
                    "task_count": len(newly_queued_sources),
                },
            )
        if queued_tasks:
            await self._wake_discovery(run_id)
        return {
            "queued_query_ids": queued,
            "skipped_query_ids": skipped,
            "queued_count": len(queued),
            "queued_tasks": queued_tasks,
            "queued_task_count": len(queued_tasks),
        }

    async def list_projects(
        self,
        run_id: str,
        *,
        filters: Mapping[str, Any] | None = None,
        sort: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        await self.get_run(run_id)
        return dict(
            await self.repository.list_run_projects(
                _required_text(run_id, "run_id"),
                filters=_mapping_or_empty(filters),
                sort=_optional_text(sort),
                cursor=_optional_text(cursor),
                limit=_bounded_int(limit, "limit", minimum=1, maximum=500),
            )
        )

    async def get_project(self, run_id: str, project_id: str) -> dict[str, Any]:
        await self.get_run(run_id)
        project = await self.repository.get_run_project(_required_text(run_id, "run_id"), _required_text(project_id, "project_id"))
        if project is None:
            raise BuyerSearchRunNotFoundError(f"{run_id}/{project_id}")
        detail = dict(project)
        run_project = detail.get("run_project")
        canonical = detail.get("project")
        if not isinstance(run_project, Mapping):
            return detail
        result = dict(run_project)
        if isinstance(canonical, Mapping):
            result.setdefault("remote_project_id", canonical.get("remote_project_id"))
            result.setdefault("canonical_url", canonical.get("canonical_url"))
            result.setdefault("description", canonical.get("latest_description"))
            result.setdefault("title", canonical.get("latest_title"))
            result.setdefault("remote_status", canonical.get("latest_status"))
            result.setdefault("buyer_remote_user_id", canonical.get("buyer_remote_user_id"))
            result.setdefault("buyer_username", canonical.get("buyer_username"))
            result.setdefault("category_id", canonical.get("latest_category_id"))
        result["observations"] = detail.get("observations") or []
        result["matches"] = detail.get("matches") or []
        result["enrichments"] = detail.get("enrichments") or []
        result["attachments"] = detail.get("attachments") or []
        result["scores"] = detail.get("scores") or []
        result["shortlist"] = detail.get("shortlist")
        return result

    async def get_facets(self, run_id: str, *, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        await self.get_run(run_id)
        result = await self.repository.get_facets(_required_text(run_id, "run_id"), filters=_mapping_or_empty(filters))
        return dict(result)

    async def get_workspace_state(self) -> dict[str, Any]:
        return dict(await self.repository.get_workspace_state())

    async def put_workspace_state(self, state: Mapping[str, Any], *, schema_version: int = 1) -> dict[str, Any]:
        return dict(
            await self.repository.put_workspace_state(
                _mapping_or_empty(state),
                schema_version=schema_version,
            )
        )

    async def set_shortlist(
        self,
        run_id: str,
        project_id: str,
        *,
        state: str = "shortlisted",
        tags: Sequence[str] | None = None,
        note: str | None = None,
        selected_by: str = "api",
        rank: int | None = None,
    ) -> dict[str, Any]:
        """Persist a shortlist decision, tags, and an optional operator note."""

        await self.get_project(run_id, project_id)
        normalized_state = _required_text(state, "shortlist state").casefold()
        if normalized_state not in {"shortlisted", "review", "contacted", "removed", "unshortlist", "unshortlisted", "none"}:
            raise ValueError("unsupported shortlist state")
        payload: dict[str, Any] = {
            "state": normalized_state,
            "selected_by": _required_text(selected_by, "selected_by"),
        }
        if tags is not None:
            if isinstance(tags, (str, bytes, bytearray)):
                raise TypeError("tags must be an array")
            payload["tags"] = [_required_text(tag, "tag") for tag in tags]
        if note is not None:
            payload["note"] = _required_text(note, "note")
        if rank is not None:
            payload["rank"] = _bounded_int(rank, "rank", minimum=0, maximum=1_000_000)
        result = await self.repository.set_shortlist(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            payload,
        )
        await self._append_event(
            run_id,
            "project.shortlist.changed",
            {
                "project_id": project_id,
                "state": result.get("state"),
                "tags": result.get("tags") or [],
                "selected_by": payload["selected_by"],
            },
        )
        return dict(result)

    async def batch_project_action(
        self,
        run_id: str,
        *,
        action: str,
        project_ids: Sequence[str],
        tags: Sequence[str] | None = None,
        note: str | None = None,
        selected_by: str = "api",
    ) -> dict[str, Any]:
        """Apply an explicit durable shortlist command across a stable ID set."""

        normalized_action = _required_text(action, "action").casefold()
        if normalized_action not in {"shortlist", "unshortlist"}:
            raise ValueError("action must be shortlist or unshortlist")
        if isinstance(project_ids, (str, bytes, bytearray)):
            raise TypeError("project_ids must be an array")
        ids = list(dict.fromkeys(_required_text(project_id, "project_id") for project_id in project_ids))
        if not ids:
            raise ValueError("project_ids cannot be empty")
        if len(ids) > 10_000:
            raise ValueError("project_ids cannot exceed 10000 items")
        applied: list[dict[str, Any]] = []
        for project_id in ids:
            applied.append(
                await self.set_shortlist(
                    run_id,
                    project_id,
                    state="shortlisted" if normalized_action == "shortlist" else "removed",
                    tags=tags if normalized_action == "shortlist" else None,
                    note=note if normalized_action == "shortlist" else None,
                    selected_by=selected_by,
                )
            )
        return {
            "action": normalized_action,
            "applied_count": len(applied),
            "items": applied,
        }

    async def rescore_project(self, run_id: str, project_id: str) -> dict[str, Any]:
        """Store the deterministic preliminary score and complete evidence."""

        project = await self.get_project(run_id, project_id)
        score = score_buyer_project(project)
        persisted = await self.repository.record_score(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            {
                "score_kind": "preliminary",
                "profile_id": score.profile_id,
                "profile_version": score.profile_version,
                "input_context_hash": score.input_hash,
                "total": score.total,
                "breakdown": dict(score.breakdown),
                "rationale": score.rationale,
            },
        )
        await self._append_event(
            run_id,
            "project.scored",
            {"project_id": project_id, "score_id": persisted.get("score_id"), "score": persisted.get("total_score")},
        )
        return dict(persisted)

    async def final_score_project(
        self,
        run_id: str,
        project_id: str,
        *,
        profile_id: str | None = None,
        profile_version: str = "1",
    ) -> dict[str, Any]:
        """Persist a versioned model score while retaining the deterministic pass."""

        if self.final_scoring_gateway is None:
            raise BuyerSearchStateError("final AI scoring is not configured")
        run = await self.get_run(run_id)
        project = await self.get_project(run_id, project_id)
        resolved_profile_id = _optional_text(profile_id) or _optional_text(run.get("scoring_profile_id")) or "buyer-ai-final"
        try:
            result = await score_buyer_project_final(
                project,
                gateway=self.final_scoring_gateway,
                profile_id=resolved_profile_id,
                profile_version=_required_text(profile_version, "profile_version"),
            )
        except BuyerFinalScoringError as exc:
            raise BuyerSearchServiceError(str(exc)) from exc
        persisted = await self.repository.record_score(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            result.to_payload(),
        )
        await self._append_event(
            run_id,
            "project.final_scored",
            {
                "project_id": project_id,
                "score_id": persisted.get("score_id"),
                "score": persisted.get("total_score"),
                "provider": result.score.provider,
                "model": result.score.model,
                "input_context_hash": result.input_context_hash,
            },
        )
        return dict(persisted)

    async def record_attachment(
        self,
        run_id: str,
        project_id: str,
        attachment: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist attachment metadata captured by a separately authorised reader."""

        await self.get_project(run_id, project_id)
        stored = await self.repository.upsert_attachment(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            _mapping(attachment, "attachment"),
        )
        await self._append_event(
            run_id,
            "attachment.discovered",
            {"project_id": project_id, "attachment_id": stored.get("attachment_id"), "state": stored.get("state")},
        )
        return dict(stored)

    async def record_project_enrichment(
        self,
        run_id: str,
        project_id: str,
        enrichment: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist raw and normalized evidence from one account-bound detail read.

        The caller supplies a compact, whitelisted ``normalized`` view for the
        project detail.  The original response is retained separately in the
        artifact store, where existing header/body redaction rules apply.
        """

        await self.get_project(run_id, project_id)
        payload = dict(_mapping(enrichment, "enrichment"))
        kind = _required_text(payload.get("kind"), "enrichment kind")
        source = _required_text(payload.get("source"), "enrichment source")
        state = _optional_text(payload.get("state")) or "completed"
        normalized = _mapping_or_empty(payload.get("normalized"))
        provenance = _mapping_or_empty(payload.get("provenance"))
        raw_artifact_id = _optional_text(payload.get("raw_artifact_id"))
        if "raw" in payload and payload.get("raw") is not None:
            raw_artifact = await self.repository.store_raw_artifact(
                {
                    "source": source,
                    "endpoint": _optional_text(payload.get("endpoint")),
                    "request_fingerprint": _optional_text(payload.get("request_fingerprint"))
                    or _stable_hash({"run_id": run_id, "project_id": project_id, "kind": kind, "source": source}),
                    "account_registration_id": _optional_text(provenance.get("account_registration_id")),
                    "transport_id": _optional_text(provenance.get("transport_id")),
                    "egress_ip": _optional_text(provenance.get("egress_ip")),
                    "route_generation": provenance.get("route_generation"),
                    "status_code": payload.get("status_code"),
                    "headers": _mapping_or_empty(payload.get("headers")),
                    "body": payload.get("raw"),
                    "content_type": _optional_text(payload.get("content_type")) or "application/json",
                    "parser_version": _optional_text(payload.get("parser_version")) or "buyer-enrichment-v1",
                    "observed_at": _optional_text(payload.get("observed_at")) or _utc_now(),
                }
            )
            raw_artifact_id = _optional_text(raw_artifact.get("artifact_id"))
        stored = await self.repository.record_project_enrichment(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            {
                "kind": kind,
                "source": source,
                "endpoint": _optional_text(payload.get("endpoint")),
                "raw_artifact_id": raw_artifact_id,
                "normalized": normalized,
                "content_hash": _optional_text(payload.get("content_hash")),
                "state": state,
                "error": _optional_text(payload.get("error")),
                "account_registration_id": _optional_text(provenance.get("account_registration_id")),
                "transport_id": _optional_text(provenance.get("transport_id")),
                "egress_ip": _optional_text(provenance.get("egress_ip")),
                "route_generation": provenance.get("route_generation"),
                "observed_at": _optional_text(payload.get("observed_at")) or _utc_now(),
            },
        )
        event_type = "detail.completed" if state.casefold() == "completed" else "detail.failed"
        if state.casefold() == "skipped":
            event_type = "detail.skipped"
        await self._append_event(
            run_id,
            event_type,
            {
                "project_id": project_id,
                "kind": kind,
                "state": stored.get("state"),
                "enrichment_id": stored.get("enrichment_id"),
                "raw_artifact_id": stored.get("raw_artifact_id"),
            },
        )
        return dict(stored)

    async def record_attachment_derivative(
        self,
        run_id: str,
        project_id: str,
        attachment_id: str,
        derivative: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record bounded parsing/OCR evidence without granting a remote capability."""

        await self.get_project(run_id, project_id)
        stored = await self.repository.record_attachment_derivative(
            _required_text(attachment_id, "attachment_id"),
            _mapping(derivative, "derivative"),
        )
        await self._append_event(
            run_id,
            "attachment.parsed",
            {
                "project_id": project_id,
                "attachment_id": attachment_id,
                "derivative_id": stored.get("derivative_id"),
                "state": stored.get("state"),
            },
        )
        return dict(stored)

    async def replay_events(self, run_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        await self.get_run(run_id)
        records = await self.repository.replay_events(
            _required_text(run_id, "run_id"), after_seq=max(0, int(after_seq)), limit=_bounded_int(limit, "limit", minimum=1, maximum=2000)
        )
        events: list[dict[str, Any]] = []
        for record in records:
            event = dict(record)
            event["seq"] = int(event.get("seq") or event.get("sequence") or 0)
            event["type"] = str(event.get("type") or event.get("event_type") or "")
            events.append(event)
        return events

    async def get_fleet(self, run_id: str) -> dict[str, Any]:
        run = await self.get_run(run_id)
        if _run_state(run.get("state")) is BuyerRunState.RUNNING:
            completed = await self._complete_exhausted_run(run_id)
            if completed is not None:
                run = completed
        if not self.settings.live_discovery:
            payload = {
                "run_id": run_id,
                "requested_workers": int(run.get("requested_workers") or 0),
                "configured_max_workers": self.settings.max_workers,
                "effective_workers": 0,
                "live_discovery_enabled": False,
                "state": run.get("state"),
                "capacity_reason": "live discovery disabled by feature flag",
            }
        elif self.supervisor is not None:
            if run_id in self.supervisor.run_ids:
                payload = self.supervisor.snapshot(run_id).to_payload()
            else:
                try:
                    preflight = await self.supervisor.preflight(
                        run_id,
                        requested_workers=int(run.get("requested_workers") or 1),
                    )
                except BuyerDiscoverySupervisorError as exc:
                    payload = {
                        "run_id": run_id,
                        "requested_workers": int(run.get("requested_workers") or 0),
                        "effective_workers": 0,
                        "state": run.get("state"),
                        "capacity_reason": str(exc),
                    }
                else:
                    payload = {
                        "run_id": run_id,
                        "requested_workers": int(run.get("requested_workers") or 0),
                        "configured_max_workers": self.settings.max_workers,
                        "effective_workers": preflight.effective_capacity,
                        "live_discovery_enabled": self.settings.live_discovery,
                        "state": run.get("state"),
                        "capacity": preflight.to_payload(),
                        "capacity_reason": None,
                    }
        else:
            requested = int(run.get("requested_workers") or 0)
            payload = {
                    "run_id": run_id,
                    "requested_workers": requested,
                    "configured_max_workers": self.settings.max_workers,
                    "effective_workers": 0,
                    "live_discovery_enabled": self.settings.live_discovery,
                    "state": run.get("state"),
                    "capacity_reason": "worker supervisor has not attached identities",
            }
        payload["metrics"] = await self._execution_metrics(run)
        return payload

    async def _execution_metrics(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Build durable speed and uniqueness metrics from completed page tasks."""

        run_id = _required_text(run.get("run_id"), "run_id")
        list_tasks = getattr(self.repository, "list_query_tasks", None)
        task_rows = await list_tasks(run_id, limit=5_000) if callable(list_tasks) else ()
        tasks = tuple(dict(task) for task in task_rows if isinstance(task, Mapping))
        completed = tuple(task for task in tasks if _optional_text(task.get("state")) == "completed")
        latencies = [
            latency
            for task in completed
            if (latency := _non_negative_int_or_none(task.get("latency_ms"))) is not None
        ]
        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        projects_seen = max(0, _integer(counters.get("projects_seen"), default=0))
        unique_projects = max(0, _integer(counters.get("unique_projects"), default=0))
        duplicate_observations = max(0, projects_seen - unique_projects)
        duration_seconds = _elapsed_seconds(run.get("started_at"), run.get("completed_at") or run.get("updated_at"))
        source_rows: dict[str, dict[str, int]] = {}
        worker_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for task in completed:
            source = _optional_text(task.get("source")) or "unknown"
            source_metric = source_rows.setdefault(source, {"pages": 0, "projects_seen": 0})
            source_metric["pages"] += 1
            source_metric["projects_seen"] += max(0, _integer(task.get("result_count"), default=0))
            account_id = _optional_text(task.get("account_registration_id"))
            transport_id = _optional_text(task.get("transport_id"))
            egress_ip = _optional_text(task.get("egress_ip"))
            if not account_id or not transport_id or not egress_ip:
                continue
            key = (account_id, transport_id, egress_ip)
            worker_metric = worker_rows.setdefault(
                key,
                {
                    "worker_id": _optional_text(task.get("lease_owner")),
                    "account_registration_id": account_id,
                    "transport_id": transport_id,
                    "egress_ip": egress_ip,
                    "pages": 0,
                    "projects_seen": 0,
                },
            )
            worker_metric["pages"] += 1
            worker_metric["projects_seen"] += max(0, _integer(task.get("result_count"), default=0))
        return {
            "duration_seconds": duration_seconds,
            "completed_requests": len(completed),
            "projects_seen": projects_seen,
            "unique_projects": unique_projects,
            "duplicate_observations": duplicate_observations,
            "uniqueness_percent": round(unique_projects * 100 / projects_seen, 1) if projects_seen else 0.0,
            "requests_per_second": round(len(completed) / duration_seconds, 2) if duration_seconds else 0.0,
            "projects_per_second": round(projects_seen / duration_seconds, 2) if duration_seconds else 0.0,
            "unique_projects_per_second": round(unique_projects / duration_seconds, 2) if duration_seconds else 0.0,
            "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "measured_latency_count": len(latencies),
            "sources": [{"source": source, **values} for source, values in sorted(source_rows.items())],
            "worker_history": list(worker_rows.values()),
        }

    async def close(self) -> None:
        """Release in-process worker leases when the API lifespan shuts down."""

        pending_tasks = tuple(
            dict.fromkeys((*self._completion_tasks.values(), *self._replenishment_retry_tasks.values()))
        )
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._completion_tasks.clear()
        self._replenishment_retry_tasks.clear()
        if self.supervisor is not None:
            await self.supervisor.close()

    def schedule_completion_check(self, run_id: str) -> None:
        """Coalesce terminal-task checks without blocking a worker event hook."""

        normalized_run_id = _required_text(run_id, "run_id")
        current = self._completion_tasks.get(normalized_run_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_completion_check(normalized_run_id),
            name=f"buyer-search-completion:{normalized_run_id}",
        )
        self._completion_tasks[normalized_run_id] = task

        def discard(completed_task: asyncio.Task[None]) -> None:
            if self._completion_tasks.get(normalized_run_id) is completed_task:
                self._completion_tasks.pop(normalized_run_id, None)

        task.add_done_callback(discard)

    async def _schedule_query_replenishment_retry(
        self,
        run: Mapping[str, Any],
        *,
        reason: str,
    ) -> None:
        """Keep an unmet automatic run alive while a fresh query wave is retried."""

        run_id = _required_text(run.get("run_id"), "run_id")
        current = self._replenishment_retry_tasks.get(run_id)
        if current is not None and not current.done():
            return

        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        attempt_number = _integer(counters.get("auto_query_replenishment_attempts"), default=0)
        live_refresh = _live_project_refresh_enabled(counters)
        if live_refresh:
            delay_seconds = self.settings.auto_project_refresh_seconds
        else:
            empty_attempts = max(1, _integer(counters.get("auto_query_replenishment_empty_attempts"), default=0))
            delay_seconds = min(
                self.settings.auto_query_replenishment_retry_max_seconds,
                self.settings.auto_query_replenishment_retry_seconds * (2 ** min(empty_attempts - 1, 8)),
            )
        next_check_at: str | None = None
        if live_refresh:
            next_check_at = _utc_after_seconds(delay_seconds)
            next_counters = dict(counters)
            next_counters["auto_project_refresh_next_at"] = next_check_at
            next_counters["auto_project_refresh_wait_reason"] = reason
            updated = await self.repository.update_run(
                run_id,
                {"counters": next_counters, "last_error": None},
            )
            run = dict(updated)
            counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else next_counters
        await self._append_event(
            run_id,
            "query.refresh.waiting" if live_refresh else "query.replenishment.waiting",
            {
                "attempt_number": attempt_number,
                "retry_after_seconds": delay_seconds,
                "next_check_at": next_check_at,
                "reason": reason,
                "unique_projects": _integer(counters.get("unique_projects"), default=0),
                "target_unique_projects": _integer(run.get("target_unique_projects"), default=0),
            },
        )
        task = asyncio.create_task(
            self._retry_query_replenishment(run_id, delay_seconds, live_refresh=live_refresh),
            name=f"buyer-search-{'refresh' if live_refresh else 'query-replenishment'}:{run_id}",
        )
        self._replenishment_retry_tasks[run_id] = task

        def discard(completed_task: asyncio.Task[None]) -> None:
            if self._replenishment_retry_tasks.get(run_id) is completed_task:
                self._replenishment_retry_tasks.pop(run_id, None)

        task.add_done_callback(discard)

    async def _retry_query_replenishment(self, run_id: str, delay_seconds: int, *, live_refresh: bool) -> None:
        """Wait before either retrying planning or checking the source for new projects."""

        try:
            await asyncio.sleep(delay_seconds)
            current_task = asyncio.current_task()
            if self._replenishment_retry_tasks.get(run_id) is current_task:
                self._replenishment_retry_tasks.pop(run_id, None)
            if live_refresh:
                run = await self.get_run(run_id)
                if _run_state(run.get("state")) is not BuyerRunState.RUNNING:
                    return
                run = await self._mark_live_project_refresh_check_started(run)
                enqueued = await self._enqueue_live_project_refresh_tasks(run)
                if enqueued:
                    await self._wake_discovery(run_id)
                else:
                    await self._schedule_query_replenishment_retry(run, reason="no_refresh_anchor")
                return
            await self._complete_exhausted_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a later worker completion can trigger another retry.
            logger.warning(f"Buyer Search query replenishment retry failed for {run_id[:8]}: {type(exc).__name__}: {exc}")

    async def _mark_live_project_refresh_check_started(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Clear the persisted wait marker when its scheduled source check begins."""

        run_id = _required_text(run.get("run_id"), "run_id")
        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        if "auto_project_refresh_next_at" not in counters:
            return dict(run)
        next_counters = dict(counters)
        next_counters.pop("auto_project_refresh_next_at", None)
        next_counters["auto_project_refresh_last_checked_at"] = _utc_now()
        updated = await self.repository.update_run(run_id, {"counters": next_counters, "last_error": None})
        return dict(updated)

    async def replay_failed_task_diagnostics(self, run_id: str, *, limit: int = 100) -> int:
        """Restore durable task failures into the shared desktop log after restart."""

        if not callable(getattr(self.repository, "list_query_tasks", None)):
            return 0
        tasks = await self.repository.list_query_tasks(
            _required_text(run_id, "run_id"), state="failed", limit=_bounded_int(limit, "limit", minimum=1, maximum=500)
        )
        emitted = 0
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            error = _optional_text(task.get("error_text"))
            if not error:
                continue
            logger.error(
                format_buyer_event_log(
                    run_id,
                    "query.page.failed",
                    {
                        "task_id": task.get("task_id"),
                        "source": task.get("source"),
                        "page": task.get("page"),
                        "failure_kind": task.get("error_kind"),
                        "error": error,
                    },
                )
            )
            emitted += 1
        return emitted

    async def recover_malformed_page_tasks(self, run_id: str, *, limit: int = 100) -> int:
        """Retry old malformed Kwork envelopes after a server restart."""

        recover = getattr(self.repository, "requeue_malformed_response_tasks", None)
        if not callable(recover):
            return 0
        result = await recover(
            _required_text(run_id, "run_id"),
            limit=_bounded_int(limit, "limit", minimum=1, maximum=500),
        )
        raw_count = result.get("requeued_task_count") if isinstance(result, Mapping) else 0
        try:
            count = max(0, int(raw_count))
        except (TypeError, ValueError):
            count = 0
        if count:
            logger.warning(
                format_buyer_event_log(
                    run_id,
                    "query.page.requeued",
                    {"task_count": count, "reason": "malformed_project_response"},
                )
            )
            await self._wake_discovery(run_id)
        return max(0, count)

    async def reconcile_task_counters(self, run_id: str) -> dict[str, Any] | None:
        reconcile = getattr(self.repository, "reconcile_task_counters", None)
        if not callable(reconcile):
            return None
        result = await reconcile(_required_text(run_id, "run_id"))
        return dict(result) if isinstance(result, Mapping) else None

    async def create_export(
        self,
        run_id: str,
        *,
        format: BuyerExportFormat | str,
        filters: Mapping[str, Any] | None = None,
        selected_project_ids: Sequence[str] = (),
        include_attachments: bool = False,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        """Build a reproducible export and retain an audit artifact for it."""

        await self.get_run(run_id)
        export_format = BuyerExportFormat(format)
        snapshot = BuyerExportSnapshot(
            run_id=_required_text(run_id, "run_id"),
            format=export_format,
            filters=_mapping_or_empty(filters),
            selected_project_ids=tuple(_required_text(value, "selected_project_id") for value in selected_project_ids),
            include_attachments=bool(include_attachments),
            include_raw=bool(include_raw),
        )
        export_id = str(uuid4())
        selection = {
            "filters": dict(snapshot.filters),
            "selected_project_ids": list(snapshot.selected_project_ids),
        }
        await self.repository.create_export(
            run_id,
            {
                "export_id": export_id,
                "format": export_format.value,
                "selection": selection,
                "include_attachments": snapshot.include_attachments,
                "include_raw": snapshot.include_raw,
                "state": "running",
                "progress": {"stage": "collecting"},
            },
        )
        try:
            projects = await self._all_projects(run_id, filters=snapshot.filters)
            artifact = build_buyer_export(snapshot, projects)
            path = await asyncio.to_thread(self._write_export, export_id, artifact.filename, artifact.content, artifact.manifest)
            audit = await self.repository.store_raw_artifact(
                {
                    "artifact_id": f"buyer_export_artifact_{export_id}",
                    "source": "buyer_search",
                    "endpoint": "export",
                    "request_fingerprint": _stable_hash({"run_id": run_id, "manifest": artifact.manifest}),
                    "account_registration_id": "system",
                    "transport_id": "local",
                    "egress_ip": "local",
                    "route_generation": 0,
                    "status_code": 200,
                    "headers": {"content_type": artifact.content_type},
                    "body": {"manifest": dict(artifact.manifest), "path": str(path), "sha256": artifact.sha256},
                    "content_type": artifact.content_type,
                    "parser_version": "buyer-export-v1",
                }
            )
            durable = await self.repository.update_export(
                run_id,
                export_id,
                {
                    "state": "completed",
                    "progress": {"stage": "completed", "rows": artifact.manifest["row_count"], "bytes": len(artifact.content)},
                    "object_ref": str(path),
                    "filename": artifact.filename,
                    "content_type": artifact.content_type,
                    "bytes": len(artifact.content),
                    "manifest": dict(artifact.manifest),
                    "sha256": artifact.sha256,
                },
            )
        except Exception as exc:
            await self.repository.update_export(
                run_id,
                export_id,
                {"state": "failed", "progress": {"stage": "failed"}, "error": str(exc)[:2_000]},
            )
            await self._append_event(run_id, "export.failed", {"export_id": export_id, "error": str(exc)[:500]})
            raise
        await self._append_event(
            run_id,
            "export.completed",
            {
                "export_id": export_id,
                "raw_artifact_id": audit.get("artifact_id"),
                "format": export_format.value,
                "row_count": artifact.manifest["row_count"],
                "sha256": artifact.sha256,
            },
        )
        return dict(durable)

    async def get_export(self, run_id: str, export_id: str) -> dict[str, Any]:
        """Return one durable export record scoped to its Buyer run."""

        await self.get_run(run_id)
        return dict(await self.repository.get_export(_required_text(run_id, "run_id"), _required_text(export_id, "export_id")))

    def export_path(self, export_id: str) -> Path | None:
        candidate = _required_text(export_id, "export_id")
        matches = tuple(self.export_root.glob(f"{candidate}.*"))
        root = self.export_root.resolve()
        for path in matches:
            if path.name.endswith(".manifest.json"):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.parent == root and resolved.is_file():
                return resolved
        return None

    async def _persist_plan(self, run_id: str, plan: BuyerQueryPlan) -> dict[str, Any]:
        await self.repository.create_queries(run_id, list(plan.query_payloads()))
        await self._record_plan_collisions(run_id, plan)
        return await self.distribute_queries(run_id, query_ids=[query.query_id for query in plan.queries])

    async def _record_plan_collisions(self, run_id: str, plan: BuyerQueryPlan) -> None:
        """Persist full planner collision evidence for later operator review."""

        for collision in plan.collisions:
            await self._append_event(
                run_id,
                "query.collision",
                {
                    "kind": collision.kind.value,
                    "first_candidate_index": collision.first_candidate_index,
                    "second_candidate_index": collision.second_candidate_index,
                    "first_query_id": collision.first_query_id,
                    "second_query_id": collision.second_query_id,
                    "first_text": collision.first_text,
                    "second_text": collision.second_text,
                    "key": list(collision.key) if collision.key is not None else None,
                    "score": collision.score,
                },
            )

    async def _all_projects(self, run_id: str, *, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        cursor: str | None = None
        rows: list[dict[str, Any]] = []
        while True:
            page = await self.list_projects(run_id, filters=filters, sort="project_id_asc", cursor=cursor, limit=500)
            items = page.get("items") or []
            project_ids = [
                _required_text(item.get("project_id"), "project_id")
                for item in items
                if isinstance(item, Mapping) and item.get("project_id")
            ]
            details = await asyncio.gather(*(self.get_project(run_id, project_id) for project_id in project_ids))
            rows.extend(_export_project_record(detail) for detail in details)
            cursor = page.get("next_cursor") if isinstance(page.get("next_cursor"), str) else None
            if not cursor:
                return rows

    def _write_export(self, export_id: str, filename: str, content: bytes, manifest: Mapping[str, Any]) -> Path:
        self.export_root.mkdir(parents=True, exist_ok=True)
        extension = Path(filename).suffix or ".bin"
        target = self.export_root / f"{export_id}{extension}"
        temporary = target.with_suffix(f"{extension}.tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        manifest_path = self.export_root / f"{export_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return target

    async def _repair_legacy_category_queries(
        self,
        run_id: str,
        run: Mapping[str, Any],
        queries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Replace old technical fallback phrases with the saved rubric names.

        The repair is deliberately limited to non-running runs and exact
        historical templates. It keeps operator edits and active work intact.
        """

        if str(run.get("state") or "") == BuyerRunState.RUNNING.value:
            return [dict(query) for query in queries]

        raw_category_scope = run.get("category_scope")
        names_by_id = _category_names_by_id(raw_category_scope) if isinstance(raw_category_scope, Mapping) else {}
        if not names_by_id:
            return [dict(query) for query in queries]

        repaired: list[dict[str, Any]] = []
        repaired_query_ids: list[str] = []
        for raw_query in queries:
            query = dict(raw_query)
            category_id = _positive_int_or_none(query.get("category_id"))
            category_name = names_by_id.get(category_id) if category_id is not None else None
            replacement = (
                replace_legacy_category_fallback_text(
                    str(query.get("text") or ""),
                    category_id=category_id,
                    category_name=category_name,
                )
                if category_id is not None and category_name and str(query.get("origin") or "") == BuyerRunMode.CATEGORY.value
                else None
            )
            if not replacement:
                repaired.append(query)
                continue

            updated = await self.repository.update_query(
                run_id,
                _required_text(query.get("query_id"), "query_id"),
                {
                    "text": replacement,
                    "rationale": "Запрос обновлён по сохранённому названию выбранной рубрики.",
                },
            )
            repaired.append(dict(updated))
            repaired_query_ids.append(_required_text(query.get("query_id"), "query_id"))

        if repaired_query_ids:
            await self._append_event(
                run_id,
                "query.legacy_text.repaired",
                {
                    "repaired_count": len(repaired_query_ids),
                    "query_ids": repaired_query_ids,
                },
            )
        return repaired

    async def _append_event(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        await self.repository.append_event(run_id, event_type, dict(payload))
        if should_log_buyer_event(event_type):
            logger.info(format_buyer_event_log(run_id, event_type, payload))

    async def _run_completion_check(self, run_id: str) -> None:
        try:
            await self._complete_exhausted_run(run_id)
        except BuyerSearchServiceError as exc:
            logger.warning(f"Buyer Search completion check skipped for {run_id[:8]}: {exc}")
        except Exception as exc:  # noqa: BLE001 - completion observability must not kill worker execution.
            logger.warning(f"Buyer Search completion check failed for {run_id[:8]}: {type(exc).__name__}: {exc}")

    async def _ensure_category_browse_queries(self, run: Mapping[str, Any]) -> int:
        """Add exhaustive category anchors to runs created by the old keyword-only logic."""

        mode = _run_mode(run.get("mode"))
        if mode not in {BuyerRunMode.CATEGORY, BuyerRunMode.HYBRID}:
            return 0
        run_id = _required_text(run.get("run_id"), "run_id")
        existing_page = await self.list_queries(run_id, include_disabled=True, limit=1_000)
        existing_items = [
            dict(item)
            for item in existing_page.get("items") or ()
            if isinstance(item, Mapping)
        ]
        covered_category_ids = {
            _positive_int_or_none(item.get("category_id"))
            for item in existing_items
            if str(item.get("origin") or "") == BuyerQueryOrigin.CATEGORY_BROWSE.value
            and item.get("enabled") is not False
        }
        category_scope = _mapping_or_empty(run.get("category_scope"))
        scopes = _category_scopes(
            category_scope,
            fallback_category_id=_positive_int_or_none(category_scope.get("category_id")),
            fallback_category_path=_category_path(category_scope.get("category_path") or ()),
        )
        missing_scopes = [
            scope
            for scope in scopes
            if _positive_int_or_none(scope.get("category_id")) not in covered_category_ids
        ]
        if not missing_scopes:
            return 0

        filters = _mapping_or_empty(run.get("filters"))
        generation = _category_browse_generation(missing_scopes, filters=filters)
        primary_scope = missing_scopes[0]
        primary_category_id = _positive_int_or_none(primary_scope.get("category_id"))
        primary_category_path = _category_path(primary_scope.get("category_path") or ())
        plan = self.planner.plan(
            BuyerQueryPlanRequest(
                run_id=run_id,
                mode=mode,
                worker_count=1,
                queries_per_worker=len(generation.generation.candidates),
                candidates=generation.generation.candidates,
                category_id=primary_category_id,
                category_path=primary_category_path,
                filters=filters,
                auto_approve=True,
            )
        )
        existing_keys = {_query_dedupe_key(item) for item in existing_items}
        payloads = [payload for payload in plan.query_payloads() if _query_dedupe_key(payload) not in existing_keys]
        if not payloads:
            return 0
        await self.repository.create_queries(run_id, payloads)
        await self._record_plan_collisions(run_id, plan)
        distribution = await self.distribute_queries(run_id, query_ids=[payload["query_id"] for payload in payloads])
        refreshed = await self.get_run(run_id)
        counters = refreshed.get("counters") if isinstance(refreshed.get("counters"), Mapping) else {}
        query_page = await self.list_queries(run_id, include_disabled=True, limit=1_000)
        next_counters = dict(counters)
        next_counters["planned_queries"] = len(query_page.get("items") or ())
        await self.repository.update_run(run_id, {"counters": next_counters})
        await self._append_event(
            run_id,
            "query.category_browse.added",
            {
                "created_count": len(payloads),
                "task_count": distribution.get("queued_task_count", 0),
                "category_ids": [payload.get("category_id") for payload in payloads],
            },
        )
        await self._wake_discovery(run_id)
        return len(payloads)

    async def _complete_exhausted_run(self, run_id: str) -> dict[str, Any] | None:
        """Continue an unmet target with fresh queries before completing a run."""

        normalized_run_id = _required_text(run_id, "run_id")
        lock = self._completion_locks.setdefault(normalized_run_id, asyncio.Lock())
        async with lock:
            run = await self.get_run(normalized_run_id)
            if _run_state(run.get("state")) is not BuyerRunState.RUNNING:
                return None
            pending_retry = self._replenishment_retry_tasks.get(normalized_run_id)
            if pending_retry is not None and not pending_retry.done():
                return None
            if not await self._query_tasks_are_exhausted(normalized_run_id):
                return None

            counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
            unique_projects = _integer(counters.get("unique_projects"), default=0)
            target_unique_projects = _integer(run.get("target_unique_projects"), default=0)
            if target_unique_projects > unique_projects:
                mode = _run_mode(run.get("mode"))
                if mode in {BuyerRunMode.CATEGORY, BuyerRunMode.HYBRID}:
                    created_browse_count = await self._ensure_category_browse_queries(run)
                    if created_browse_count:
                        return None
                if _live_project_refresh_enabled(counters):
                    await self._schedule_query_replenishment_retry(run, reason="awaiting_new_projects")
                    return None
                if mode is BuyerRunMode.CATEGORY:
                    refreshed = await self._enable_live_project_refresh(await self.get_run(normalized_run_id))
                    await self._schedule_query_replenishment_retry(refreshed, reason="category_browse_exhausted")
                    return None
                if not self.settings.auto_query_replenishment:
                    refreshed = await self._enable_live_project_refresh(run)
                    await self._schedule_query_replenishment_retry(refreshed, reason="initial_plan_exhausted")
                    return None
                if _should_switch_to_live_project_refresh(counters):
                    refreshed = await self._enable_live_project_refresh(run)
                    await self._schedule_query_replenishment_retry(refreshed, reason="source_saturated")
                    return None
                created_count = await self._replenish_query_queue(run)
                if created_count:
                    return None
                if _run_mode(run.get("mode")) is not BuyerRunMode.MANUAL:
                    refreshed = await self._enable_live_project_refresh(await self.get_run(normalized_run_id))
                    await self._schedule_query_replenishment_retry(refreshed, reason="no_new_queries")
                    return None

            complete = getattr(self.repository, "complete_run_if_exhausted", None)
            if not callable(complete):
                return None
            record = await complete(normalized_run_id)
            if not isinstance(record, Mapping):
                return None
            completed = dict(record)
            counters = completed.get("counters") if isinstance(completed.get("counters"), Mapping) else {}
            logger.info(
                format_buyer_event_log(
                    normalized_run_id,
                    "run.completed",
                    {
                        "state": completed.get("state"),
                        "unique_projects": counters.get("unique_projects"),
                        "target_unique_projects": completed.get("target_unique_projects"),
                    },
                )
            )
            if self.supervisor is not None and normalized_run_id in self.supervisor.run_ids:
                stop_reason = (
                    "target_reached"
                    if _integer(completed.get("target_unique_projects"), default=0)
                    and _integer(counters.get("unique_projects"), default=0)
                    >= _integer(completed.get("target_unique_projects"), default=0)
                    else "sources_exhausted"
                )
                try:
                    await self.supervisor.stop(normalized_run_id, reason=stop_reason)
                except BuyerDiscoverySupervisorError as exc:
                    logger.warning(f"Buyer Search fleet release skipped for {normalized_run_id[:8]}: {exc}")
            if self.runtime_finalizer is not None:
                try:
                    await self.runtime_finalizer(normalized_run_id, "buyer_run_completed")
                except Exception as exc:  # noqa: BLE001 - completion must remain durable even if cleanup fails.
                    logger.warning(f"Buyer Search backing job finalization skipped for {normalized_run_id[:8]}: {exc}")
            return completed

    async def _query_tasks_are_exhausted(self, run_id: str) -> bool:
        """Return whether every durable source task has reached a terminal state."""

        list_tasks = getattr(self.repository, "list_query_tasks", None)
        if not callable(list_tasks):
            return False
        records = await list_tasks(_required_text(run_id, "run_id"), limit=5_000)
        items = records.get("items") if isinstance(records, Mapping) else records
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)) or not items:
            return False
        active_states = {"queued", "leased", "retry_wait"}
        return not any(
            isinstance(item, Mapping) and str(item.get("state") or "").casefold() in active_states
            for item in items
        )

    async def _has_pending_query_tasks(self, run_id: str) -> bool:
        """Tell a terminal-run recovery whether durable work is still waiting to be consumed."""

        list_tasks = getattr(self.repository, "list_query_tasks", None)
        if not callable(list_tasks):
            return False
        records = await list_tasks(_required_text(run_id, "run_id"), limit=5_000)
        items = records.get("items") if isinstance(records, Mapping) else records
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            return False
        active_states = {"queued", "leased", "retry_wait"}
        return any(
            isinstance(item, Mapping) and str(item.get("state") or "").casefold() in active_states
            for item in items
        )

    async def _enable_live_project_refresh(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the switch from redundant query expansion to new-listing monitoring."""

        run_id = _required_text(run.get("run_id"), "run_id")
        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        next_counters = dict(counters)
        if _live_project_refresh_enabled(next_counters):
            return dict(run)
        next_counters["auto_project_refresh_mode"] = True
        next_counters["auto_project_refresh_cycles"] = _integer(
            next_counters.get("auto_project_refresh_cycles"),
            default=0,
        )
        next_counters["auto_query_replenishment_empty_attempts"] = 0
        updated = await self.repository.update_run(run_id, {"counters": next_counters, "last_error": None})
        await self._append_event(
            run_id,
            "query.refresh.enabled",
            {
                "reason": "no_new_unique_projects",
                "unique_projects": _integer(next_counters.get("unique_projects"), default=0),
                "target_unique_projects": _integer(run.get("target_unique_projects"), default=0),
            },
        )
        return dict(updated)

    async def _enqueue_live_project_refresh_tasks(self, run: Mapping[str, Any]) -> int:
        """Re-read a few broad rubric anchors so a target can accumulate new Kwork orders over time."""

        run_id = _required_text(run.get("run_id"), "run_id")
        if not await self._query_tasks_are_exhausted(run_id):
            return 0
        enqueue = getattr(self.repository, "enqueue_query_task", None)
        if not callable(enqueue):
            return 0

        queries_page = await self.list_queries(run_id, limit=1_000)
        category_scope = _mapping_or_empty(run.get("category_scope"))
        category_name = _optional_text(category_scope.get("category_name"))
        anchors = _live_project_refresh_anchors(
            queries_page.get("items") if isinstance(queries_page, Mapping) else (),
            category_name=category_name,
            limit=self.settings.auto_project_refresh_query_limit,
        )
        if not anchors:
            return 0

        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        cycle = _integer(counters.get("auto_project_refresh_cycles"), default=0) + 1
        epoch = _integer(counters.get("auto_project_refresh_epoch"), default=0)
        enqueued = 0
        for query in anchors:
            query_id = _required_text(query.get("query_id"), "query_id")
            for source in self.settings.discovery_sources:
                task = {
                    "task_id": f"buyer_refresh_{uuid4().hex}",
                    "run_id": run_id,
                    "query_id": query_id,
                    "source": source,
                    "endpoint": source,
                    "page": 1,
                    "state": "queued",
                    # The source does not consume this value.  It makes every
                    # scheduled refresh a new durable request fingerprint.
                    "cursor": {"refresh_epoch": epoch, "refresh_cycle": cycle},
                    "priority": _integer(query.get("priority"), default=0),
                }
                try:
                    await enqueue(run_id, task)
                except BuyerRepositoryValidationError:
                    continue
                enqueued += 1
        if not enqueued:
            return 0

        refreshed = await self.get_run(run_id)
        refreshed_counters = refreshed.get("counters") if isinstance(refreshed.get("counters"), Mapping) else {}
        next_counters = dict(refreshed_counters)
        next_counters["auto_project_refresh_mode"] = True
        next_counters["auto_project_refresh_cycles"] = cycle
        next_counters["auto_project_refresh_tasks"] = _integer(
            refreshed_counters.get("auto_project_refresh_tasks"),
            default=0,
        ) + enqueued
        await self.repository.update_run(run_id, {"counters": next_counters, "last_error": None})
        await self._append_event(
            run_id,
            "query.refresh.enqueued",
            {
                "cycle": cycle,
                "anchor_count": len(anchors),
                "task_count": enqueued,
                "unique_projects": _integer(refreshed_counters.get("unique_projects"), default=0),
                "target_unique_projects": _integer(refreshed.get("target_unique_projects"), default=0),
            },
        )
        return enqueued

    async def _replenish_query_queue(self, run: Mapping[str, Any], *, force: bool = False) -> int:
        """Append a fresh, approved query wave when a target still needs projects."""

        if not force and not self.settings.auto_query_replenishment:
            return 0
        mode = _run_mode(run.get("mode"))
        if mode in {BuyerRunMode.CATEGORY, BuyerRunMode.MANUAL}:
            return 0
        run_id = _required_text(run.get("run_id"), "run_id")
        counters = run.get("counters") if isinstance(run.get("counters"), Mapping) else {}
        batch_number = _integer(counters.get("auto_query_replenishment_batches"), default=0)
        attempt_number = _integer(counters.get("auto_query_replenishment_attempts"), default=0) + 1
        empty_attempts = _integer(counters.get("auto_query_replenishment_empty_attempts"), default=0)
        existing_query_page = await self.list_queries(run_id, limit=1_000)
        active_query_count = sum(
            1
            for query in existing_query_page.get("items") or []
            if isinstance(query, Mapping) and query.get("enabled") is not False
        )
        remaining_query_capacity = self.settings.max_query_plan_size - active_query_count
        if remaining_query_capacity <= 0:
            next_counters = dict(counters)
            next_counters["auto_query_replenishment_attempts"] = attempt_number
            next_counters["auto_query_replenishment_empty_attempts"] = empty_attempts + 1
            await self.repository.update_run(run_id, {"counters": next_counters})
            await self._append_event(
                run_id,
                "query.replenishment.exhausted",
                {
                    "batch_count": batch_number,
                    "attempt_number": attempt_number,
                    "reason": "query_plan_limit",
                    "query_plan_limit": self.settings.max_query_plan_size,
                },
            )
            return 0
        if not force and batch_number >= self.settings.auto_query_replenishment_max_batches:
            next_counters = dict(counters)
            next_counters["auto_query_replenishment_attempts"] = attempt_number
            next_counters["auto_query_replenishment_empty_attempts"] = empty_attempts + 1
            await self.repository.update_run(run_id, {"counters": next_counters})
            await self._append_event(
                run_id,
                "query.replenishment.exhausted",
                {
                    "batch_count": batch_number,
                    "attempt_number": attempt_number,
                    "reason": "auto_batch_limit",
                },
            )
            return 0

        minimum_count = _probe_query_count(
            scope_count=_selected_category_scope_count(_mapping_or_empty(run.get("category_scope"))),
            queries_per_scope=self.settings.auto_query_replenishment_minimum_count,
            maximum_queries=remaining_query_capacity,
        )
        base_brief = _optional_text(run.get("brief")) or ""
        brief = (
            f"{base_brief}\n"
            f"Автоматическая волна поиска {batch_number + 1}, попытка {attempt_number}: "
            "используй только новые конкретные русские фразы для Kwork."
        )
        try:
            generation = await self.generate_queries(
                run_id,
                {
                    "mode": mode.value,
                    "brief": brief,
                    "requested_workers": 1,
                    "query_batch_size": minimum_count,
                    "minimum_count": minimum_count,
                    "auto_approve": True,
                },
                created_by="automatic_replenishment",
            )
        except Exception as exc:  # noqa: BLE001 - a later automatic attempt must keep an unmet run alive.
            next_counters = dict(counters)
            next_counters["auto_query_replenishment_attempts"] = attempt_number
            next_counters["auto_query_replenishment_empty_attempts"] = empty_attempts + 1
            await self.repository.update_run(run_id, {"counters": next_counters})
            await self._append_event(
                run_id,
                "query.replenishment.failed",
                {
                    "batch_number": batch_number + 1,
                    "attempt_number": attempt_number,
                    "error": str(exc)[:500],
                },
            )
            return 0

        created = [item for item in generation.get("items") or () if isinstance(item, Mapping)]
        refreshed = await self.get_run(run_id)
        refreshed_counters = refreshed.get("counters") if isinstance(refreshed.get("counters"), Mapping) else {}
        query_page = await self.list_queries(run_id, limit=1_000)
        query_count = len(query_page.get("items") or [])
        next_counters = dict(refreshed_counters)
        refreshed_batch_number = _integer(
            refreshed_counters.get("auto_query_replenishment_batches"),
            default=batch_number,
        )
        refreshed_empty_attempts = _integer(
            refreshed_counters.get("auto_query_replenishment_empty_attempts"),
            default=empty_attempts,
        )
        next_counters["planned_queries"] = query_count
        next_counters["auto_query_replenishment_attempts"] = _integer(
            refreshed_counters.get("auto_query_replenishment_attempts"),
            default=attempt_number - 1,
        ) + 1
        next_counters["auto_query_replenishment_empty_attempts"] = 0 if created else refreshed_empty_attempts + 1
        next_counters["auto_query_replenishment_batches"] = refreshed_batch_number + (1 if created else 0)
        next_counters["auto_query_replenished_queries"] = _integer(
            refreshed_counters.get("auto_query_replenished_queries"), default=0
        ) + len(created)
        if created:
            next_counters["auto_query_replenishment_last_unique_projects"] = _integer(
                refreshed_counters.get("unique_projects"),
                default=0,
            )
        await self.repository.update_run(run_id, {"counters": next_counters, "last_error": None})
        await self._append_event(
            run_id,
            "query.replenished",
            {
                "batch_number": next_counters["auto_query_replenishment_batches"],
                "attempt_number": next_counters["auto_query_replenishment_attempts"],
                "created_count": len(created),
                "minimum_count": minimum_count,
                "unique_projects": refreshed_counters.get("unique_projects"),
                "target_unique_projects": refreshed.get("target_unique_projects"),
            },
        )
        if created:
            await self._wake_discovery(run_id)
        return len(created)

    async def _query_coverage_summary(self, run_id: str) -> dict[str, int]:
        """Summarise project IDs repeatedly returned by completed query phrases."""

        list_coverage = getattr(self.repository, "list_query_coverage", None)
        if not callable(list_coverage):
            return {}
        records = await list_coverage(_required_text(run_id, "run_id"))
        rows = records.get("items") if isinstance(records, Mapping) else records
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return {}
        query_count = 0
        observations = 0
        project_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            values = row.get("project_ids")
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                continue
            normalized_ids = {str(value).strip() for value in values if str(value).strip()}
            if not normalized_ids:
                continue
            query_count += 1
            observations += len(normalized_ids)
            project_ids.update(normalized_ids)
        return {
            "coverage_query_count": query_count,
            "coverage_observations": observations,
            "coverage_unique_projects": len(project_ids),
            "coverage_duplicate_observations": max(0, observations - len(project_ids)),
        }

    async def _wake_discovery(self, run_id: str) -> None:
        if self.supervisor is None or run_id not in self.supervisor.run_ids:
            return
        try:
            await self.supervisor.wake(run_id)
        except BuyerDiscoverySupervisorError:
            return

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise BuyerSearchServiceError("Buyer Search is disabled by feature flag")


def _live_project_refresh_enabled(counters: Mapping[str, Any]) -> bool:
    value = counters.get("auto_project_refresh_mode")
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return value is True


def _bounded_plan_shape(worker_count: int, query_batch_size: int, *, maximum_queries: int) -> tuple[int, int]:
    """Keep planner invariants intact while never creating an unbounded query plan."""

    requested = worker_count * query_batch_size
    if requested <= maximum_queries:
        return worker_count, query_batch_size
    # Assignments are only initial hints; discovery workers pull durable tasks
    # from the shared queue.  One planner lane preserves that queue behavior
    # while imposing a strict cap on the number of generated phrases.
    return 1, maximum_queries


def _probe_query_count(*, scope_count: int, queries_per_scope: int, maximum_queries: int) -> int:
    """Bound an automatic phrase wave independently from the worker count."""

    return max(1, min(maximum_queries, max(1, scope_count) * queries_per_scope))


def _bounded_generated_candidates(candidates: Sequence[Any], *, maximum: int) -> tuple[Any, ...]:
    """Keep the first distinct automatic phrases needed for one measured wave."""

    selected: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = _optional_text(getattr(candidate, "text", None))
        if text is None:
            continue
        normalized = normalize_query_text(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(candidate)
        if len(selected) >= maximum:
            break
    return tuple(selected)


def _query_coverage_is_saturated(coverage: Mapping[str, int]) -> bool:
    """Reject a new phrase wave once at least 60% of observed IDs are repeats."""

    query_count = _integer(coverage.get("coverage_query_count"), default=0)
    observations = _integer(coverage.get("coverage_observations"), default=0)
    duplicates = _integer(coverage.get("coverage_duplicate_observations"), default=0)
    return query_count >= 2 and observations > 0 and duplicates * 100 >= observations * 60


def _select_query_plan_representatives(
    items: Sequence[Mapping[str, Any]],
    *,
    coverage: Mapping[str, set[str]],
    limit: int,
) -> list[str]:
    """Greedily retain the smallest high-coverage set of enabled search phrases."""

    candidates = [
        dict(item)
        for item in items
        if _optional_text(item.get("query_id")) and item.get("enabled") is not False
    ]
    if not candidates:
        return []
    universe = set().union(*(coverage.get(_required_text(item.get("query_id"), "query_id"), set()) for item in candidates))
    remaining = set(universe)
    selected: list[dict[str, Any]] = []
    available = list(candidates)

    def tie_breaker(item: Mapping[str, Any]) -> tuple[int, int, int, str]:
        text = _optional_text(item.get("text")) or ""
        return (
            -_integer(item.get("unique_projects"), default=0),
            -_integer(item.get("priority"), default=0),
            len(text.split()),
            text.casefold(),
        )

    while remaining and available and len(selected) < limit:
        candidate = min(
            available,
            key=lambda item: (-len(coverage.get(_required_text(item.get("query_id"), "query_id"), set()) & remaining), *tie_breaker(item)),
        )
        candidate_coverage = coverage.get(_required_text(candidate.get("query_id"), "query_id"), set())
        if not (candidate_coverage & remaining):
            break
        selected.append(candidate)
        remaining.difference_update(candidate_coverage)
        available.remove(candidate)

    if not selected:
        for candidate in sorted(available, key=tie_breaker):
            if len(selected) >= limit:
                break
            selected.append(candidate)

    return [_required_text(item.get("query_id"), "query_id") for item in selected]


def _should_switch_to_live_project_refresh(counters: Mapping[str, Any]) -> bool:
    """Stop broadening phrases once an entire automatic wave adds no new project IDs."""

    if _integer(counters.get("auto_query_replenishment_batches"), default=0) < 2:
        return False
    unique_projects = _integer(counters.get("unique_projects"), default=0)
    baseline = counters.get("auto_query_replenishment_last_unique_projects")
    if baseline is None:
        # Historical runs did not persist a per-wave baseline.  At two or more
        # completed automatic waves, treating them as saturated is safer than
        # sending an unbounded number of near-identical Kwork searches.
        return True
    return unique_projects <= _integer(baseline, default=unique_projects)


def _live_project_refresh_anchors(
    items: Any,
    *,
    category_name: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Choose broad, enabled queries that are suitable as periodic category probes."""

    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    normalized_category_name = _optional_text(category_name)
    category_key = normalized_category_name.casefold() if normalized_category_name else None
    candidates = [
        dict(item)
        for item in items
        if isinstance(item, Mapping)
        and _optional_text(item.get("query_id"))
        and item.get("enabled") is not False
        and item.get("approved") is not False
    ]
    category_browse_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("origin") or "") == BuyerQueryOrigin.CATEGORY_BROWSE.value
    ]
    if category_browse_candidates:
        candidates = category_browse_candidates

    def rank(query: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
        text = _optional_text(query.get("text")) or ""
        origin = _optional_text(query.get("origin")) or ""
        exact_category = category_key is not None and text.casefold() == category_key
        return (
            0 if exact_category else 1,
            0 if origin in {"category", "hybrid_seed"} else 1,
            -_integer(query.get("unique_projects"), default=0),
            len(text.split()),
            text.casefold(),
        )

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=rank):
        query_id = _required_text(candidate.get("query_id"), "query_id")
        if query_id in seen:
            continue
        seen.add(query_id)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _query_dedupe_key(query: Mapping[str, Any]) -> tuple[str, str, str]:
    """Use the same durable uniqueness inputs when filtering a generated batch."""

    text = _required_text(query.get("normalized_text") or query.get("text"), "query text").casefold()
    category_id = str(query.get("category_id") or "")
    filters = _mapping_or_empty(query.get("filters"))
    return text, category_id, _stable_hash(filters)


def _export_project_record(project: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the detail projection into the full record expected by exporters."""

    result = dict(project)
    matches = result.get("matches") if isinstance(result.get("matches"), Sequence) else ()
    result["matched_queries"] = [
        str(match.get("query_text") or match.get("normalized_query_text") or match.get("query_id"))
        for match in matches
        if isinstance(match, Mapping)
    ]
    attachments = result.get("attachments") if isinstance(result.get("attachments"), Sequence) else ()
    result["attachment_manifest"] = [
        {
            "attachment_id": attachment.get("attachment_id"),
            "filename": attachment.get("filename"),
            "sha256": attachment.get("sha256"),
            "state": attachment.get("state"),
            "detected_type": attachment.get("detected_type"),
            "size_bytes": attachment.get("size_bytes"),
        }
        for attachment in attachments
        if isinstance(attachment, Mapping)
    ]
    return result


def _initial_task(query: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    query_id = _required_text(query.get("query_id"), "query_id")
    run_id = _required_text(query.get("run_id"), "run_id")
    normalized_source = _required_text(source, "source")
    if normalized_source not in BUYER_DISCOVERY_SOURCES:
        raise ValueError(f"unsupported Buyer discovery source: {normalized_source}")
    page = 1
    fingerprint = _stable_hash({"query_id": query_id, "source": normalized_source, "page": page})
    return {
        "task_id": str(uuid4()),
        "run_id": run_id,
        "query_id": query_id,
        "source": normalized_source,
        "page": page,
        "cursor": None,
        "request_fingerprint": fingerprint,
        "priority": int(query.get("priority") or 0),
        "state": "queued",
        "not_before": _utc_now(),
        "query_text": query.get("text"),
        "query_origin": query.get("origin"),
        "category_id": query.get("category_id"),
        "filters": dict(query.get("filters") or {}),
    }


def _initial_counters(plan: BuyerQueryPlan) -> dict[str, int]:
    return {
        "planned_queries": len(plan.queries),
        "scheduled_tasks": 0,
        "queued_tasks": 0,
        "active_tasks": 0,
        "retry_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "unique_projects": 0,
        "projects_seen": 0,
        "errors": 0,
    }


def _rollout_gate_result(value: object) -> tuple[bool, str | None]:
    """Normalize a persisted gate decision without allowing optimistic truthiness."""

    if value is True:
        return True, None
    if value is False or value is None:
        return False, None
    if not isinstance(value, Mapping):
        raise BuyerSearchServiceError("rollout_gate_checker must return a bool or object")
    allowed = value.get("allowed")
    if allowed is not True:
        blockers = value.get("blockers")
        if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes, bytearray)):
            detail = "; ".join(str(item) for item in blockers if str(item).strip())
            return False, detail or None
        reason = _optional_text(value.get("reason") or value.get("detail"))
        return False, reason
    return True, None


def _validate_transition(current: BuyerRunState, target: BuyerRunState) -> None:
    allowed: dict[BuyerRunState, set[BuyerRunState]] = {
        BuyerRunState.DRAFT: {BuyerRunState.PLANNING, BuyerRunState.STOPPED},
        BuyerRunState.PLANNING: {BuyerRunState.READY, BuyerRunState.FAILED, BuyerRunState.STOPPED},
        BuyerRunState.READY: {BuyerRunState.RUNNING, BuyerRunState.STOPPED},
        BuyerRunState.RUNNING: {BuyerRunState.PAUSED, BuyerRunState.COMPLETING, BuyerRunState.STOPPED, BuyerRunState.FAILED, BuyerRunState.BLOCKED},
        BuyerRunState.PAUSING: {BuyerRunState.PAUSED, BuyerRunState.STOPPED},
        BuyerRunState.PAUSED: {BuyerRunState.RUNNING, BuyerRunState.STOPPED},
        BuyerRunState.COMPLETING: {BuyerRunState.COMPLETED, BuyerRunState.FAILED, BuyerRunState.STOPPED},
        BuyerRunState.COMPLETED: set(),
        BuyerRunState.STOPPING: {BuyerRunState.STOPPED},
        BuyerRunState.STOPPED: set(),
        BuyerRunState.BLOCKED: {BuyerRunState.RUNNING, BuyerRunState.STOPPED, BuyerRunState.FAILED},
        BuyerRunState.FAILED: {BuyerRunState.PLANNING, BuyerRunState.STOPPED},
    }
    if target is current:
        return
    if target not in allowed[current]:
        raise BuyerSearchStateError(f"cannot transition Buyer Search run from {current.value} to {target.value}")


def _run_mode(value: Any) -> BuyerRunMode:
    try:
        return BuyerRunMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("mode must be brief, category, manual, or hybrid") from exc


def _run_state(value: Any) -> BuyerRunState:
    try:
        return BuyerRunState(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("run state is invalid") from exc


def _run_name(value: Any, run_id: str) -> str:
    text = _optional_text(value) or f"Buyer Search {run_id[:8]}"
    if len(text) > 240:
        raise ValueError("name cannot exceed 240 characters")
    return text


_BUYER_EVENT_LABELS = {
    "query.plan.compacted": "план запросов очищен от повторов",
    "run.created": "создан запуск",
    "run.ready": "подготовлен план поиска",
    "run.started": "поиск запущен",
    "run.resumed": "поиск продолжен",
    "run.paused": "поиск поставлен на паузу",
    "run.stopped": "поиск остановлен",
    "run.completed": "поиск завершён",
    "run.blocked": "запуск заблокирован",
    "run.failed": "запуск завершился ошибкой",
    "run.restarted": "запуск подготовлен к повторному поиску",
    "run.continued_to_target": "поиск продолжен до заданной цели",
    "query.assigned": "запрос назначен воркеру",
    "query.generated": "сформированы новые запросы",
    "query.updated": "обновлён запрос",
    "query.legacy_text.repaired": "технические названия запросов заменены названиями рубрик",
    "query.collision": "обнаружено пересечение запросов",
    "capacity.changed": "обновлена доступная мощность воркеров",
    "runtime.warning": "предупреждение среды поиска",
    "worker.started": "запущен воркер поиска",
    "worker.failed": "ошибка воркера поиска",
    "query.page.completed": "получена страница результатов",
    "query.page.failed": "ошибка получения страницы",
    "query.page.retry": "повторная попытка получения страницы",
    "query.page.requeued": "некорректная страница поставлена на повторный поиск",
    "query.replenished": "добавлена следующая пачка запросов",
    "query.replenishment.exhausted": "не удалось расширить план новыми запросами",
    "query.replenishment.failed": "ошибка при добавлении следующей пачки запросов",
    "query.replenishment.waiting": "подбирается следующая пачка запросов",
    "query.refresh.enabled": "исчерпана текущая выдача, включена проверка новых заказов",
    "query.refresh.waiting": "ожидание новых заказов в выбранной рубрике",
    "query.refresh.enqueued": "назначена проверка новых заказов",
    "identity.released": "освобождён аккаунт и маршрут",
}

_BUYER_EVENT_LOG_SILENT_TYPES = frozenset({
    "worker.state",
    "worker.idle",
    "worker.heartbeat",
})

_BUYER_LOG_SOURCE_LABELS = {
    "mobile_projects": "мобильная выдача Kwork",
    "web_projects": "веб-выдача Kwork",
}

_BUYER_LOG_FAILURE_LABELS = {
    "http_403": "доступ ограничен (403)",
    "http_429": "слишком много запросов (429)",
    "timeout": "истекло время ожидания",
    "transient": "временный сбой",
    "fatal": "критическая ошибка",
}


def should_log_buyer_event(event_type: str) -> bool:
    """Keep the shared desktop log focused on actionable Buyer events."""

    return str(event_type or "").strip() not in _BUYER_EVENT_LOG_SILENT_TYPES


def _localized_buyer_error(value: Any) -> Any:
    """Keep legacy persisted task failures understandable in the Russian UI."""

    if not isinstance(value, str):
        return value
    return (
        value.replace(
            "mobile project payload has no project ID",
            "пустая карточка в мобильной выдаче Kwork: нет ID проекта",
        )
        .replace(
            "web project payload has no project ID",
            "пустая карточка в веб-выдаче Kwork: нет ID проекта",
        )
    )


def _buyer_log_source(value: Any) -> str:
    source = _optional_text(value)
    return _BUYER_LOG_SOURCE_LABELS.get(source or "", source or "не указан")


def _buyer_log_failure_kind(value: Any) -> str:
    kind = _optional_text(value)
    return _BUYER_LOG_FAILURE_LABELS.get(kind or "", kind or "не указан")


def format_buyer_event_log(run_id: str, event_type: str, payload: Mapping[str, Any]) -> str:
    """Return one concise Russian line for the shared desktop log stream."""

    label = _BUYER_EVENT_LABELS.get(event_type, f"событие поиска: {event_type}")
    prefix = f"Поиск заказов [{str(run_id)[:8]}]: {label}"
    if event_type in {"query.page.failed", "query.page.retry"}:
        error = _localized_buyer_error(payload.get("error"))
        details = [f"источник: {_buyer_log_source(payload.get('source'))}"]
        page = _positive_int_or_none(payload.get("page"))
        if page is not None:
            details.append(f"страница: {page}")
        failure_kind = _optional_text(payload.get("failure_kind"))
        if failure_kind:
            details.append(f"тип: {_buyer_log_failure_kind(failure_kind)}")
        return f"{prefix}: {error or 'причина не указана'} ({', '.join(details)})"
    if event_type == "query.page.requeued":
        task_count = _positive_int_or_none(payload.get("task_count"))
        count_text = f"{task_count} задач" if task_count is not None else "задачи"
        return f"{prefix}: {count_text} будут запрошены заново"
    if event_type == "query.replenished":
        raw_batch = _integer(payload.get("batch_number"), default=0)
        batch = raw_batch if raw_batch > 0 else None
        created = _integer(payload.get("created_count"), default=0)
        if batch is None:
            return f"{prefix}: новых запросов пока не добавлено"
        return f"{prefix}: пачка {batch}, добавлено запросов: {created}"
    if event_type == "query.replenishment.waiting":
        delay_seconds = _positive_int_or_none(payload.get("retry_after_seconds"))
        delay_text = f"; повтор через {delay_seconds} с" if delay_seconds is not None else ""
        return f"{prefix}: новых фраз пока нет{delay_text}"
    if event_type == "query.replenishment.exhausted" and payload.get("reason") == "source_overlap":
        duplicates = _integer(payload.get("coverage_duplicate_observations"), default=0)
        observations = _integer(payload.get("coverage_observations"), default=0)
        return f"{prefix}: выдача повторяется ({duplicates} повторов из {observations}); новые фразы не добавлены"
    if event_type == "query.refresh.waiting":
        delay_seconds = _positive_int_or_none(payload.get("retry_after_seconds"))
        delay_text = f"; следующая проверка через {delay_seconds} с" if delay_seconds is not None else ""
        return f"{prefix}: новых проектов пока нет{delay_text}"
    if event_type == "query.plan.compacted":
        retained = _integer(payload.get("retained_query_count"), default=0)
        archived = _integer(payload.get("archived_query_count"), default=0)
        return f"{prefix}: оставлено запросов: {retained}, убрано повторов: {archived}"
    if event_type == "query.refresh.enqueued":
        task_count = _integer(payload.get("task_count"), default=0)
        return f"{prefix}: поставлено задач: {task_count}"

    details: dict[str, Any] = {}
    for key in (
        "reason",
        "detail",
        "error",
        "state",
        "query_count",
        "task_count",
        "source",
        "page",
        "failure_kind",
        "unique_projects",
        "target_unique_projects",
        "created_count",
        "repaired_count",
        "project_id",
        "query_id",
        "worker_id",
    ):
        value = payload.get(key)
        if value not in (None, "", (), [], {}):
            if key == "error":
                details[key] = _localized_buyer_error(value)
            elif key == "source":
                details[key] = _buyer_log_source(value)
            elif key == "failure_kind":
                details[key] = _buyer_log_failure_kind(value)
            elif key == "reason" and value == "malformed_project_response":
                details[key] = "некорректная карточка проекта"
            else:
                details[key] = value
    suffix = f" — {json.dumps(details, ensure_ascii=False, default=str, separators=(',', ':'))[:900]}" if details else ""
    return f"{prefix}{suffix}"


def _category_names_by_id(category_scope: Mapping[str, Any]) -> dict[int, str]:
    """Read durable rubric names from both current and early run payloads."""

    result: dict[int, str] = {}
    raw_ids = category_scope.get("category_ids")
    raw_names = category_scope.get("category_names")
    if (
        isinstance(raw_ids, Sequence)
        and not isinstance(raw_ids, (str, bytes, bytearray))
        and isinstance(raw_names, Sequence)
        and not isinstance(raw_names, (str, bytes, bytearray))
    ):
        for raw_id, raw_name in zip(raw_ids, raw_names, strict=False):
            category_id = _positive_int_or_none(raw_id)
            category_name = _optional_text(raw_name)
            if category_id is not None and category_name:
                result[category_id] = category_name

    raw_scopes = category_scope.get("category_scopes")
    if isinstance(raw_scopes, Sequence) and not isinstance(raw_scopes, (str, bytes, bytearray)):
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping):
                continue
            category_id = _positive_int_or_none(raw_scope.get("category_id"))
            category_name = _optional_text(raw_scope.get("category_name") or raw_scope.get("name"))
            if category_id is not None and category_name:
                result[category_id] = category_name

    category_id = _positive_int_or_none(category_scope.get("category_id"))
    category_name = _optional_text(category_scope.get("category_name") or category_scope.get("name"))
    if category_id is not None and category_name:
        result.setdefault(category_id, category_name)
    return result


def _category_scopes(
    category_scope: Mapping[str, Any],
    *,
    fallback_category_id: int | None,
    fallback_category_path: tuple[int, ...],
) -> tuple[dict[str, Any], ...]:
    """Normalize one or more selected taxonomy scopes without dropping paths."""

    inherited = {
        key: value
        for key, value in category_scope.items()
        if key not in {"category_scopes", "category_ids", "category_names", "selected_kworks_total"}
    }
    raw_scopes = category_scope.get("category_scopes")
    source_scopes: Sequence[Any]
    if isinstance(raw_scopes, Sequence) and not isinstance(raw_scopes, (str, bytes, bytearray)) and raw_scopes:
        if len(raw_scopes) > 30:
            raise ValueError("category_scopes cannot exceed 30 items")
        source_scopes = raw_scopes
    else:
        source_scopes = ({
            **inherited,
            "category_id": fallback_category_id,
            "category_path": list(fallback_category_path),
        },)

    normalized: list[dict[str, Any]] = []
    seen_category_ids: set[int | None] = set()
    for raw_scope in source_scopes:
        if not isinstance(raw_scope, Mapping):
            raise TypeError("category_scopes must contain objects")
        scope = {**inherited, **dict(raw_scope)}
        category_id = _positive_int_or_none(scope.get("category_id"))
        category_path = _category_path(scope.get("category_path") or ())
        if category_id is not None and category_path and category_path[-1] != category_id:
            category_path = (*category_path, category_id)
        if category_id in seen_category_ids:
            continue
        seen_category_ids.add(category_id)
        normalized.append({**scope, "category_id": category_id, "category_path": list(category_path)})

    if normalized:
        return tuple(normalized)
    return ({**inherited, "category_id": fallback_category_id, "category_path": list(fallback_category_path)},)


def _selected_category_scope_count(category_scope: Mapping[str, Any]) -> int:
    category_id = _positive_int_or_none(category_scope.get("category_id"))
    category_path = _category_path(category_scope.get("category_path") or ())
    if category_id is not None and category_path and category_path[-1] != category_id:
        category_path = (*category_path, category_id)
    return len(
        _category_scopes(
            category_scope,
            fallback_category_id=category_id,
            fallback_category_path=category_path,
        )
    )


def _stored_category_scope(
    category_scope: Mapping[str, Any],
    *,
    category_id: int | None,
    category_path: tuple[int, ...],
    category_scopes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = {**category_scope, "category_id": category_id, "category_path": list(category_path)}
    selected_scopes = [dict(scope) for scope in category_scopes if _positive_int_or_none(scope.get("category_id")) is not None]
    if len(selected_scopes) > 1 or "category_scopes" in category_scope:
        result["category_scopes"] = selected_scopes
        result["category_ids"] = [scope["category_id"] for scope in selected_scopes]
    return result


def _category_browse_generation(
    category_scopes: Sequence[Mapping[str, Any]],
    *,
    filters: Mapping[str, Any],
) -> BuyerAIQueryGenerationResult:
    """Create one exhaustive, keyword-free feed anchor per selected rubric."""

    candidates: list[BuyerQueryCandidate] = []
    for scope in category_scopes:
        category_id = _positive_int_or_none(scope.get("category_id"))
        if category_id is None:
            continue
        category_path = _category_path(scope.get("category_path") or ())
        if category_path and category_path[-1] != category_id:
            category_path = (*category_path, category_id)
        category_name = _optional_text(scope.get("category_name") or scope.get("name")) or f"№ {category_id}"
        candidates.append(
            BuyerQueryCandidate(
                text=f"Все активные заказы: {category_name}",
                rationale="Полный обход выбранной рубрики Kwork без ограничения ключевой фразой.",
                origin=BuyerQueryOrigin.CATEGORY_BROWSE,
                category_id=category_id,
                category_path=category_path,
                filters=dict(filters),
                priority=10_000,
            )
        )
    if not candidates:
        raise BuyerSearchServiceError("category browse requires at least one selected rubric")
    return BuyerAIQueryGenerationResult(
        generation=BuyerQueryGenerationResult(
            candidates=tuple(candidates),
            generator="category_browse",
            used_fallback=False,
        ),
        prompt="",
        used_model=False,
    )


def _merge_query_generation_results(results: Sequence[BuyerAIQueryGenerationResult]) -> BuyerAIQueryGenerationResult:
    if not results:
        raise BuyerSearchServiceError("query generation requires at least one category scope")
    if len(results) == 1:
        return results[0]
    generators = tuple(dict.fromkeys(result.generation.generator for result in results))
    errors = tuple(dict.fromkeys(result.error for result in results if result.error))
    return BuyerAIQueryGenerationResult(
        generation=BuyerQueryGenerationResult(
            candidates=tuple(candidate for result in results for candidate in result.generation.candidates),
            generator=f"multi_scope:{'+'.join(generators)}",
            used_fallback=any(result.generation.used_fallback for result in results),
        ),
        prompt="\n\n".join(result.prompt for result in results),
        used_model=any(result.used_model for result in results),
        error="; ".join(errors)[:2_000] or None,
    )


def _query_inputs(value: Any) -> tuple[str | Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("queries must be an array")
    if len(value) > 300:
        raise ValueError("queries cannot exceed 300 entries")
    return tuple(value)


def _sequence_of_text(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    if len(value) > 300:
        raise ValueError(f"{name} cannot exceed 300 entries")
    return tuple(_required_text(item, name[:-1] if name.endswith("s") else name) for item in value)


def _task_page(task: Mapping[str, Any]) -> int:
    value = task.get("page")
    if isinstance(value, bool):
        return 0
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 0
    return page if page > 0 else 0


def _account_registration_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("account_registration_ids must be an array")
    normalized = tuple(dict.fromkeys(_required_text(item, "account_registration_id") for item in value))
    if len(normalized) > 30:
        raise ValueError("account_registration_ids cannot exceed 30 items")
    return normalized


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("value must be an object")
    return dict(value)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _non_negative_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _elapsed_seconds(start: Any, end: Any) -> float:
    try:
        started = datetime.fromisoformat(_required_text(start, "started_at").replace("Z", "+00:00"))
        finished = datetime.fromisoformat(_required_text(end, "finished_at").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _positive_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _bounded_int(value, "category_id", minimum=1, maximum=2_147_483_647)


def _category_path(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("category_path must be an array")
    return tuple(_bounded_int(item, "category_path item", minimum=1, maximum=2_147_483_647) for item in value)


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_after_seconds(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_discovery_sources() -> tuple[str, ...]:
    raw = os.getenv("BUYER_SEARCH_DISCOVERY_SOURCES")
    if raw is None:
        return DEFAULT_BUYER_DISCOVERY_SOURCES
    selected = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    return selected or DEFAULT_BUYER_DISCOVERY_SOURCES


__all__ = [
    "BUYER_DISCOVERY_SOURCES",
    "DEFAULT_BUYER_DISCOVERY_SOURCES",
    "BuyerSearchRunNotFoundError",
    "BuyerSearchService",
    "BuyerSearchServiceError",
    "BuyerSearchSettings",
    "BuyerSearchStateError",
    "format_buyer_event_log",
    "should_log_buyer_event",
]
