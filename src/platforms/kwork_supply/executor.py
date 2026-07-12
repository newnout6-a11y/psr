"""Concrete source-operation handlers for durable market workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import inspect
import json
import time
from typing import Any

import httpx

from src.platforms.kwork_market import KworkMarketClient

from .analyzer import MarketResultsAnalyzer
from .artifacts import LocalArtifactStore
from .contracts import BatchState, ContractState, CursorKind, ProtectionStatus, SourceCursor
from .coordinator import MarketScanCoordinator
from .exporter import MarketSnapshotExporter
from .mapper import ScopeMapper, ScopePartition
from .models import (
    JobPhase,
    JobState,
    MAX_ENRICHMENT_PRICE,
    MarketJob,
    MarketScope,
    NetworkPolicy,
    Operation,
    OperationKind,
    OperationState,
    SourcePolicy,
    utc_now,
)
from .planner import ShardPlanner
from .rate_control import (
    RateControlPolicy,
    RateControlState,
    TokenBucketPolicy,
    acquire_request,
    initial_rate_control_state,
    record_protection_response,
)
from .sources.mobile_kworks import KworkMobileKworksAdapter
from .sources.web_catalog import KworkWebCatalogAdapter
from .worker import MarketWorker, RetryableOperationError


JsonDict = dict[str, Any]
ClientFactory = Callable[[str | None], Any]
WebCookieProvider = Callable[[], Awaitable[Mapping[str, str]]]


DEFAULT_RATE_CONTROL_POLICY = RateControlPolicy(
    global_policy=TokenBucketPolicy(capacity=10, refill_per_second=2),
    default_source_policy=TokenBucketPolicy(capacity=6, refill_per_second=1),
    source_policies={
        "web_catalog": TokenBucketPolicy(capacity=6, refill_per_second=1),
        "mobile_kworks": TokenBucketPolicy(capacity=6, refill_per_second=1),
    },
    fallback_retry_seconds=15,
    max_retry_after_seconds=300,
)


def _cursor_payload(cursor: SourceCursor | None) -> JsonDict | None:
    if cursor is None:
        return None
    return {
        "kind": cursor.kind.value,
        "page": cursor.page,
        "exclude_ids": list(cursor.exclude_ids),
        "token": cursor.token,
    }


def _cursor_from_payload(payload: object) -> SourceCursor | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("cursor payload must be a mapping")
    kind = CursorKind(str(payload.get("kind") or ""))
    if kind is CursorKind.PAGE:
        return SourceCursor.page_cursor(int(payload["page"]))
    if kind is CursorKind.EXCLUDE_IDS:
        raw_ids = payload.get("exclude_ids") or payload.get("excludeIds") or ()
        if not isinstance(raw_ids, (list, tuple)):
            raise ValueError("exclude_ids cursor must contain a sequence")
        return SourceCursor.exclude_ids_cursor(raw_ids)
    return SourceCursor.opaque_cursor(str(payload["token"]))


def _cursor_key(cursor: SourceCursor | None) -> str:
    payload = _cursor_payload(cursor)
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _job_model(record: Mapping[str, Any]) -> MarketJob:
    scope_data = record["scope"]
    if not isinstance(scope_data, Mapping):
        raise TypeError("job scope must be a mapping")
    scope = MarketScope(
        category_id=int(scope_data["category_id"]),
        category_name=str(scope_data.get("category_name") or ""),
        classifier_id=(int(scope_data["classifier_id"]) if scope_data.get("classifier_id") is not None else None),
        classifier_name=str(scope_data.get("classifier_name") or ""),
        canonical_alias=(str(scope_data["canonical_alias"]) if scope_data.get("canonical_alias") else None),
        filters=dict(scope_data.get("filters") or {}),
    )
    return MarketJob(
        job_id=str(record["job_id"]),
        scope=scope,
        profile=str(record["profile"]),
        target_unique_cards=int(record["target_unique_cards"]),
        desired_workers=int(record["desired_workers"]),
        network_policy=NetworkPolicy(str(record["network_policy"])),
        source_policy=SourcePolicy(str(record["source_policy"])),
        include_ai=bool(record["include_ai"]),
        state=JobState(str(record["state"])),
        phase=JobPhase(str(record["phase"])),
        revision=int(record["revision"]),
        request_budget=record.get("request_budget"),
        time_budget_seconds=record.get("time_budget_seconds"),
        counters=dict(record.get("counters") or {}),
        created_at=str(record["created_at"]),
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
        latest_checkpoint_id=record.get("latest_checkpoint_id"),
        last_error=record.get("last_error"),
        last_warning=record.get("last_warning"),
    )


def _response_mapping(payload: object) -> JsonDict:
    if not isinstance(payload, Mapping):
        return {}
    response = payload.get("response")
    return dict(response) if isinstance(response, Mapping) else dict(payload)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _review_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    return _optional_text(value)


def _review_identity(review: Mapping[str, Any], index: int) -> str:
    explicit = _optional_text(review.get("id") or review.get("review_id"))
    if explicit is not None:
        return explicit
    stable = {
        "index": index,
        "time_added": review.get("time_added"),
        "writer": review.get("writer"),
        "text": review.get("text"),
    }
    encoded = json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"derived:{sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _listing_reviews(extra: Mapping[str, Any]) -> list[JsonDict]:
    raw_reviews = extra.get("last_reviews")
    if not isinstance(raw_reviews, list):
        return []
    normalized: list[JsonDict] = []
    for index, raw in enumerate(raw_reviews[:3]):
        if not isinstance(raw, Mapping):
            continue
        writer = raw.get("writer")
        writer_name = (
            _optional_text(writer.get("username") or writer.get("name") or writer.get("fullname"))
            if isinstance(writer, Mapping)
            else _optional_text(writer)
        )
        answer = raw.get("answer")
        answer_text = (
            _optional_text(answer.get("text") or answer.get("answer"))
            if isinstance(answer, Mapping)
            else _optional_text(answer)
        )
        normalized.append(
            {
                "review_key": _review_identity(raw, index),
                "time_added": _review_timestamp(raw.get("time_added")),
                "is_good": raw.get("good"),
                "is_bad": raw.get("bad"),
                "text": _optional_text(raw.get("text")),
                "writer": writer_name,
                "answer": answer_text,
                "raw": dict(raw),
            }
        )
    return normalized


def _seller_snapshot(username: str, profile: Mapping[str, Any], *, expires_at: str) -> JsonDict:
    user = profile.get("user") if isinstance(profile.get("user"), Mapping) else profile
    stats = profile.get("stats") if isinstance(profile.get("stats"), Mapping) else {}
    reviews = profile.get("reviews") if isinstance(profile.get("reviews"), Mapping) else {}
    return {
        "seller_key": username,
        "seller_id": _optional_text(user.get("id") or profile.get("user_id") or profile.get("id")),
        "status": "ok",
        "profile": dict(profile),
        "seller_rating": _optional_number(user.get("rating") or profile.get("rating") or stats.get("rating")),
        "seller_rating_count": _optional_int(
            user.get("rating_count") or profile.get("rating_count") or stats.get("rating_count")
        ),
        "seller_reviews_count": _optional_int(
            user.get("reviews_count")
            or profile.get("reviews_count")
            or stats.get("reviews_count")
            or reviews.get("count")
        ),
        "seller_addtime": _review_timestamp(user.get("addtime") or profile.get("addtime")),
        "completed_orders_count": _optional_int(
            stats.get("completed_orders_count")
            or stats.get("completed_orders")
            or profile.get("completed_orders_count")
            or profile.get("completed_orders")
            or user.get("orders_done")
        ),
        "active_kworks_count": _optional_int(
            stats.get("active_kworks_count")
            or profile.get("active_kworks_count")
            or profile.get("kworks_count")
        ),
        "fetched_at": utc_now(),
        "expires_at": expires_at,
    }


class MarketOperationExecutor:
    """Execute validated map and web batch operations for in-process workers."""

    def __init__(
        self,
        coordinator: MarketScanCoordinator,
        *,
        client_factory: ClientFactory | None = None,
        artifact_store: LocalArtifactStore | None = None,
        mapper: ScopeMapper | None = None,
        planner: ShardPlanner | None = None,
        analyzer: MarketResultsAnalyzer | None = None,
        exporter: MarketSnapshotExporter | None = None,
        rate_control_policy: RateControlPolicy | None = None,
        web_cookie_provider: WebCookieProvider | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.client_factory = client_factory or (
            lambda proxy_url: KworkMarketClient(proxy_url=proxy_url, use_environment_proxy=False)
        )
        self.artifact_store = artifact_store or LocalArtifactStore()
        self.mapper = mapper or ScopeMapper()
        self.planner = planner or ShardPlanner()
        self.analyzer = analyzer or MarketResultsAnalyzer(coordinator.repository)
        self.exporter = exporter or MarketSnapshotExporter(self.artifact_store)
        self.rate_control_policy = rate_control_policy or DEFAULT_RATE_CONTROL_POLICY
        self._web_cookie_provider = web_cookie_provider
        self._rate_control_state: RateControlState = initial_rate_control_state(
            self.rate_control_policy,
            now=time.monotonic(),
        )
        self._rate_control_lock = asyncio.Lock()
        self._seller_enrichment_locks: dict[str, asyncio.Lock] = {}

    @property
    def handlers(self) -> dict[OperationKind, Callable[[MarketWorker, Mapping[str, Any]], Any]]:
        return {
            OperationKind.MAP_SCOPE: self.handle_map_scope,
            OperationKind.FETCH_BATCH: self.handle_fetch_batch,
            OperationKind.ENRICH_LISTING: self.handle_enrich_listing,
            OperationKind.ANALYZE_SNAPSHOT: self.handle_analyze_snapshot,
            OperationKind.EXPORT_SNAPSHOT: self.handle_export_snapshot,
        }

    async def handle_map_scope(self, worker: MarketWorker, operation: Mapping[str, Any]) -> None:
        """Map the selected source policy and queue its initial bounded work."""

        job_record = await self._require_job(worker.job_id)
        job = _job_model(job_record)
        if job.source_policy is SourcePolicy.MOBILE_FIRST_PAGE_ONLY:
            await self._plan_mobile_first_page(worker, operation, job)
            return
        if not job.scope.canonical_alias:
            raise RetryableOperationError("canonical_alias is required for web mapping", failure_kind="scope_invalid")
        adapter, client = await self._web_adapter(worker)
        try:
            request = adapter.build_request(
                alias=job.scope.canonical_alias,
                category_id=job.scope.category_id,
                filters=job.scope.filters,
            )
            batch = await self._fetch_web_batch(worker, adapter, request)
            raw_ref = self._write_raw(batch, job_id=job.job_id, operation=operation)
            verdict = adapter.validate_batch(request, batch, previous=None)
            if verdict.state is ContractState.BLOCKED:
                retry = await self._protection_retry(worker, batch)
                if batch.protection_status is not ProtectionStatus.RATE_LIMITED:
                    await self._block_job(job_record, "web catalog protection signal during scope mapping")
                raise retry
            if verdict.state is ContractState.CONTRACT_VIOLATION:
                raise RetryableOperationError(
                    ",".join(verdict.reason_codes) or "web catalog contract violation",
                    failure_kind="contract_violation",
                )
            scope_map = self.mapper.map_scope(job.scope, batch, verdict)
            await self.mapper.persist_alias_validation(self.coordinator.repository, scope_map)
            if not scope_map.ready_for_planning:
                raise RetryableOperationError(
                    ",".join(scope_map.alias_validation.reason_codes) or "alias was not validated",
                    failure_kind="contract_violation",
                )
            mapping_evidence = await self._read_scope_mapping_evidence(client, job)
            partition_probes: list[JsonDict] = []
            if self._needs_partition_probes(job, batch):
                partitions, partition_probes = await self._validate_mapping_partitions(
                    worker,
                    operation,
                    adapter,
                    job,
                    root_batch=batch,
                    attributes=mapping_evidence.get("category_attributes"),
                )
                if partitions:
                    scope_map = scope_map.with_partitions((*scope_map.partitions, *partitions))
            current = await self._require_job(job.job_id)
            if current["state"] == JobState.COMPLETING.value:
                return
            planning = await self.coordinator.repository.update_job_state(
                job.job_id,
                JobState.PLANNING,
                phase=JobPhase.PLAN,
                expected_revision=current["revision"],
            )
            await self.coordinator.emit_state_changed(planning)
            mapping_evidence_ref = self._write_mapping_evidence(
                mapping_evidence,
                job_id=job.job_id,
                operation=operation,
            )
            mapping_summary = self._mapping_summary(
                mapping_evidence,
                raw_response_ref=raw_ref,
                mapping_evidence_ref=mapping_evidence_ref,
                partition_probes=partition_probes,
            )
            mapping_checkpoint = await self.coordinator.repository.create_checkpoint(
                job.job_id,
                frontier={"mapping": mapping_summary},
                metrics={"mapping": mapping_summary},
                set_latest=False,
            )
            await self.coordinator.emit(
                job.job_id,
                "checkpoint.saved",
                {"checkpoint_id": mapping_checkpoint["checkpoint_id"], "phase": JobPhase.MAP.value},
                revision=planning["revision"],
                worker_id=worker.worker_id,
                operation_id=str(operation["operation_id"]),
            )
            plan = self.planner.plan_initial(_job_model(planning), scope_map)
            shards = await self._persist_plan_idempotently(plan)
            mapping_payload = operation.get("payload")
            mapping_payload = mapping_payload if isinstance(mapping_payload, Mapping) else {}
            resume_from_completed = mapping_payload.get("resume_from_completed") is True
            partition_mapping = resume_from_completed or mapping_payload.get("partition_mapping") is True
            scheduled_fetches = 0
            for planned, shard in zip(plan.shards, shards, strict=True):
                if partition_mapping and shard["state"] == "exhausted" and shard.get("cursor") is None:
                    continue
                fetch = await self._enqueue_fetch_operation(
                    job.job_id,
                    shard,
                    cursor=None,
                    remaining_requests=planned.request_budget,
                    priority=int(shard["priority"]),
                )
                if fetch["state"] in {
                    OperationState.QUEUED.value,
                    OperationState.LEASED.value,
                    OperationState.RUNNING.value,
                    OperationState.RETRY_WAIT.value,
                }:
                    scheduled_fetches += 1
            current = await self._require_job(job.job_id)
            if partition_mapping and scheduled_fetches == 0:
                if resume_from_completed:
                    completed = await self.coordinator.repository.update_job_state(
                        job.job_id,
                        JobState.COMPLETED,
                        phase=JobPhase.EXPORT,
                        expected_revision=current["revision"],
                        last_warning="resume_no_validated_partitions",
                    )
                    await self.coordinator.emit_state_changed(completed, {"reason": "resume_no_validated_partitions"})
                    await self.coordinator.emit(
                        job.job_id,
                        "warning",
                        {"code": "resume_no_validated_partitions"},
                        revision=completed["revision"],
                        worker_id=worker.worker_id,
                        operation_id=str(operation["operation_id"]),
                    )
                    return
                collecting = await self.coordinator.repository.update_job_state(
                    job.job_id,
                    JobState.RUNNING,
                    phase=JobPhase.COLLECT,
                    expected_revision=current["revision"],
                    last_warning="target_unreached_source_exhausted",
                )
                await self.coordinator.emit_state_changed(collecting, {"reason": "target_unreached_source_exhausted"})
                await self.coordinator.emit(
                    job.job_id,
                    "warning",
                    {"code": "target_unreached_source_exhausted"},
                    revision=collecting["revision"],
                    worker_id=worker.worker_id,
                    operation_id=str(operation["operation_id"]),
                )
                return
            running = await self.coordinator.repository.update_job_state(
                job.job_id,
                JobState.RUNNING,
                phase=JobPhase.COLLECT,
                expected_revision=current["revision"],
                counters={
                    **dict(current.get("counters") or {}),
                    "aggregate_scope_total": scope_map.aggregate_scope_total or 0,
                    "observed_mapping_batch": scope_map.observed_first_batch_count,
                },
            )
            await self.coordinator.emit_state_changed(running)
            await self.coordinator.emit(
                job.job_id,
                "shard.progress",
                {
                    "planned_shards": len(shards),
                    "aggregate_scope_total": scope_map.aggregate_scope_total,
                    "observed_mapping_batch": scope_map.observed_first_batch_count,
                    "raw_response_ref": raw_ref,
                    "mapping_checkpoint_id": mapping_checkpoint["checkpoint_id"],
                    "validated_partition_count": len(scope_map.partitions) - 1,
                    "scheduled_fetches": scheduled_fetches,
                },
                revision=running["revision"],
                worker_id=worker.worker_id,
                operation_id=str(operation["operation_id"]),
            )
        finally:
            await self._close_client(client)

    async def handle_fetch_batch(self, worker: MarketWorker, operation: Mapping[str, Any]) -> None:
        """Accept one contract-valid source batch and queue validated continuation."""

        shard_id = operation.get("shard_id")
        if not isinstance(shard_id, str) or not shard_id:
            raise RetryableOperationError("fetch operation has no shard", failure_kind="scope_invalid")
        shard = await self.coordinator.repository.get_shard(shard_id)
        if shard is None or shard["job_id"] != worker.job_id:
            raise RetryableOperationError("fetch shard is missing", failure_kind="scope_invalid")
        job = await self._require_job(worker.job_id)
        if SourcePolicy(str(job["source_policy"])) is SourcePolicy.MOBILE_FIRST_PAGE_ONLY:
            await self._handle_mobile_first_page_fetch(worker, operation, job, shard)
            return
        if shard["source"] != "web_catalog":
            raise RetryableOperationError(
                f"unsupported source {shard['source']!r} for validated collection",
                failure_kind="scope_invalid",
            )
        payload = operation.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        cursor = _cursor_from_payload(payload.get("cursor") if "cursor" in payload else shard.get("cursor"))
        remaining_requests = max(int(payload.get("remaining_requests") or 1), 1)
        adapter, client = await self._web_adapter(worker)
        try:
            request = adapter.build_request(
                alias=str(shard["alias"]),
                category_id=_job_model(job).scope.category_id,
                cursor=cursor,
                filters=shard.get("filters") if isinstance(shard.get("filters"), Mapping) else None,
            )
            batch = await self._fetch_web_batch(worker, adapter, request)
            raw_ref = self._write_raw(batch, job_id=worker.job_id, operation=operation)
            previous = BatchState(
                seen_card_ids=frozenset(cursor.exclude_ids if cursor is not None else ()),
                last_fingerprint=shard.get("last_fingerprint"),
                accepted_batches=int((shard.get("counters") or {}).get("accepted_batches", 0)),
            )
            verdict = adapter.validate_batch(request, batch, previous)
            if verdict.state is ContractState.BLOCKED:
                retry = await self._protection_retry(worker, batch)
                if batch.protection_status is not ProtectionStatus.RATE_LIMITED:
                    job = await self._require_job(worker.job_id)
                    await self._block_job(job, "web catalog protection signal during collection")
                raise retry
            if verdict.state is ContractState.CONTRACT_VIOLATION:
                raise RetryableOperationError(
                    ",".join(verdict.reason_codes) or "web catalog contract violation",
                    failure_kind="contract_violation",
                )
            next_cursor = batch.next_cursor if verdict.state is ContractState.ACCEPTED else None
            next_operation: JsonDict | None = None
            if next_cursor is not None and remaining_requests > 1:
                next_operation = {
                    "kind": OperationKind.FETCH_BATCH,
                    "priority": int(operation["priority"]),
                    "idempotency_key": f"fetch:{shard_id}:{_cursor_key(next_cursor)}",
                    "payload": {
                        "cursor": _cursor_payload(next_cursor),
                        "remaining_requests": remaining_requests - 1,
                    },
                }
            result = await self.coordinator.repository.commit_accepted_batch(
                job_id=worker.job_id,
                shard_id=shard_id,
                operation_id=str(operation["operation_id"]),
                attempt_id=(str(operation["attempt_id"]) if operation.get("attempt_id") else None),
                listings=[adapter.normalize(card) for card in batch.cards],
                source=batch.source,
                idempotency_key=f"accepted:{operation['operation_id']}:{_cursor_key(cursor)}",
                requested_cursor=_cursor_payload(batch.requested_cursor),
                reported_cursor=_cursor_payload(batch.reported_cursor),
                next_cursor=_cursor_payload(next_cursor),
                raw_response_ref=raw_ref,
                fingerprint=batch.fingerprint,
                counter_deltas={"requests": 1},
                event_payloads=(
                    {
                        "event_type": "operation.completed",
                        "worker_id": worker.worker_id,
                        "payload": {"kind": OperationKind.FETCH_BATCH.value, "state": verdict.state.value},
                    },
                    {
                        "event_type": "shard.progress",
                        "worker_id": worker.worker_id,
                        "payload": {
                            "shard_id": shard_id,
                            "received": batch.actual_item_count,
                            "new_unique": verdict.novelty.new_unique if verdict.novelty else 0,
                            "duplicates": verdict.novelty.duplicate_count if verdict.novelty else 0,
                            "raw_response_ref": raw_ref,
                        },
                    },
                    {"event_type": "checkpoint.saved", "payload": {"shard_id": shard_id}},
                ),
                next_operation=next_operation,
                shard_state="active" if next_operation is not None else "exhausted",
            )
            await self.coordinator.publish_committed_events(worker.job_id, result["event_sequences"])
            if result["next_operation_id"] is None:
                await self._complete_job_if_finished(worker.job_id)
        finally:
            await self._close_client(client)

    async def _plan_mobile_first_page(
        self,
        worker: MarketWorker,
        operation: Mapping[str, Any],
        job: MarketJob,
    ) -> None:
        """Plan exactly one mobile page without entering the web-catalog path."""

        cursor = SourceCursor.page_cursor(1)
        shard_id = f"shard_mobile_{self._operation_hash(job.job_id, 'mobile_first_page', cursor)}"
        shard = await self.coordinator.repository.get_shard(shard_id)
        if shard is None:
            filters = dict(job.scope.filters)
            filters.update(
                {
                    "category_id": job.scope.category_id,
                    "classifier_id": job.scope.classifier_id,
                    "mobile_page_limit": 1,
                }
            )
            shard = await self.coordinator.repository.create_shard(
                {
                    "shard_id": shard_id,
                    "job_id": job.job_id,
                    "source": "mobile_kworks",
                    # A shard requires an alias even though the mobile endpoint
                    # is addressed solely by the numeric scope.
                    "alias": job.scope.canonical_alias or f"category-{job.scope.category_id}",
                    "filters": filters,
                    "expected_count": None,
                    "priority": 1_000,
                    "cursor": _cursor_payload(cursor),
                }
            )

        current = await self._require_job(job.job_id)
        if current["state"] == JobState.COMPLETING.value:
            return
        if current["state"] in {JobState.PREPARING.value, JobState.MAPPING.value}:
            planning = await self.coordinator.repository.update_job_state(
                job.job_id,
                JobState.PLANNING,
                phase=JobPhase.PLAN,
                expected_revision=current["revision"],
            )
            await self.coordinator.emit_state_changed(planning)
            current = planning

        fetch = await self._enqueue_fetch_operation(
            job.job_id,
            shard,
            cursor=cursor,
            remaining_requests=1,
            priority=int(shard["priority"]),
        )
        current = await self._require_job(job.job_id)
        if current["state"] == JobState.PLANNING.value:
            running = await self.coordinator.repository.update_job_state(
                job.job_id,
                JobState.RUNNING,
                phase=JobPhase.COLLECT,
                expected_revision=current["revision"],
                counters={
                    **dict(current.get("counters") or {}),
                    "planned_shards": 1,
                    "mobile_page_limit": 1,
                },
            )
            await self.coordinator.emit_state_changed(running)
            current = running
        await self.coordinator.emit(
            job.job_id,
            "shard.progress",
            {
                "shard_id": shard["shard_id"],
                "planned_shards": 1,
                "source": "mobile_kworks",
                "page": 1,
                "bounded": True,
                "operation_id": fetch["operation_id"],
            },
            revision=current["revision"],
            worker_id=worker.worker_id,
            operation_id=str(operation["operation_id"]),
        )

    async def _handle_mobile_first_page_fetch(
        self,
        worker: MarketWorker,
        operation: Mapping[str, Any],
        job: Mapping[str, Any],
        shard: Mapping[str, Any],
    ) -> None:
        """Collect and persist one mobile page, with no continuation mechanism."""

        if shard["source"] != "mobile_kworks":
            raise RetryableOperationError(
                "mobile_first_page_only requires a mobile_kworks shard",
                failure_kind="scope_invalid",
            )
        payload = operation.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        cursor = _cursor_from_payload(payload.get("cursor") if "cursor" in payload else shard.get("cursor"))
        cursor = cursor or SourceCursor.page_cursor(1)
        if cursor.kind is not CursorKind.PAGE or cursor.page != 1:
            raise RetryableOperationError(
                "mobile_first_page_only forbids pages other than 1",
                failure_kind="contract_violation",
            )
        if int(payload.get("remaining_requests") or 1) != 1:
            raise RetryableOperationError(
                "mobile_first_page_only forbids continuation request budgets",
                failure_kind="contract_violation",
            )

        scope = _job_model(job).scope
        adapter, client = self._mobile_adapter(worker)
        try:
            request = adapter.build_request(
                category_id=scope.category_id,
                classifier_id=scope.classifier_id,
                cursor=cursor,
            )
            batch = await self._fetch_mobile_batch(adapter, request)
            raw_ref = self._write_raw(batch, job_id=worker.job_id, operation=operation)
            verdict = adapter.validate_batch(request, batch, previous=None)
            if batch.next_cursor is not None:
                raise RetryableOperationError(
                    "mobile source returned an unsupported continuation cursor",
                    failure_kind="contract_violation",
                )
            if verdict.state is ContractState.BLOCKED:
                retry = await self._protection_retry(worker, batch)
                if batch.protection_status is not ProtectionStatus.RATE_LIMITED:
                    await self._block_job(job, "mobile source protection signal during bounded collection")
                raise retry
            if verdict.state is ContractState.CONTRACT_VIOLATION:
                raise RetryableOperationError(
                    ",".join(verdict.reason_codes) or "mobile source contract violation",
                    failure_kind="contract_violation",
                )
            result = await self.coordinator.repository.commit_accepted_batch(
                job_id=worker.job_id,
                shard_id=str(shard["shard_id"]),
                operation_id=str(operation["operation_id"]),
                attempt_id=(str(operation["attempt_id"]) if operation.get("attempt_id") else None),
                listings=[adapter.normalize(card) for card in batch.cards],
                source=batch.source,
                idempotency_key=f"accepted:{operation['operation_id']}:{_cursor_key(cursor)}",
                requested_cursor=_cursor_payload(batch.requested_cursor),
                reported_cursor=_cursor_payload(batch.reported_cursor),
                next_cursor=None,
                raw_response_ref=raw_ref,
                fingerprint=batch.fingerprint,
                counter_deltas={"requests": 1},
                event_payloads=(
                    {
                        "event_type": "operation.completed",
                        "worker_id": worker.worker_id,
                        "payload": {
                            "kind": OperationKind.FETCH_BATCH.value,
                            "state": verdict.state.value,
                            "source": batch.source,
                            "bounded": True,
                        },
                    },
                    {
                        "event_type": "shard.progress",
                        "worker_id": worker.worker_id,
                        "payload": {
                            "shard_id": shard["shard_id"],
                            "source": batch.source,
                            "page": 1,
                            "bounded": True,
                            "received": batch.actual_item_count,
                            "new_unique": verdict.novelty.new_unique if verdict.novelty else 0,
                            "duplicates": verdict.novelty.duplicate_count if verdict.novelty else 0,
                            "raw_response_ref": raw_ref,
                        },
                    },
                    {"event_type": "checkpoint.saved", "payload": {"shard_id": shard["shard_id"]}},
                ),
                next_operation=None,
                shard_state="exhausted",
            )
            await self.coordinator.publish_committed_events(worker.job_id, result["event_sequences"])
            await self._complete_job_if_finished(worker.job_id)
        finally:
            await self._close_client(client)

    async def handle_enrich_listing(self, worker: MarketWorker, operation: Mapping[str, Any]) -> None:
        """Fetch durable listing and seller facts for one price-eligible card."""

        payload = operation.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        listing_id = _optional_int(payload.get("listing_id"))
        if listing_id is None:
            raise RetryableOperationError("enrichment operation has no listing_id", failure_kind="scope_invalid")
        listing = await self.coordinator.repository.get_listing(worker.job_id, listing_id)
        if listing is None:
            raise RetryableOperationError("enrichment listing is missing", failure_kind="scope_invalid")

        requested_price_limit = _optional_number(payload.get("price_limit"))
        price_limit = min(requested_price_limit or MAX_ENRICHMENT_PRICE, MAX_ENRICHMENT_PRICE)
        price = _optional_number(listing.get("price"))
        if price is not None and price > price_limit:
            await self.coordinator.repository.persist_listing_enrichment(
                job_id=worker.job_id,
                listing_id=listing_id,
                listing_features={
                    "status": "reject_price",
                    "generation": _optional_int(payload.get("generation")) or 0,
                    "last_error": None,
                },
            )
            return

        expires_at = _optional_text(payload.get("cache_expires_at"))
        if expires_at is None:
            expires_at = (datetime.now(UTC) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        current = await self.coordinator.repository.get_listing_enrichment(worker.job_id, listing_id)
        now = utc_now()
        if (
            current is not None
            and current.get("status") in {"ok", "partial"}
            and isinstance(current.get("expires_at"), str)
            and current["expires_at"] > now
        ):
            return

        client = self.client_factory(worker.transport_proxy_url)
        try:
            listing_key = _optional_text(listing.get("listing_key"))
            if listing_key is None:
                raise RetryableOperationError("enrichment listing has no source key", failure_kind="scope_invalid")
            details = _response_mapping(await self._enrichment_request(client, "getKworkDetails", id=listing_key))
            extra = _response_mapping(await self._enrichment_request(client, "getKworkDetailsExtra", id=listing_key))
            short_user = details.get("short_user_info") if isinstance(details.get("short_user_info"), Mapping) else {}
            canonical = listing.get("canonical") if isinstance(listing.get("canonical"), Mapping) else {}
            seller_username = _optional_text(
                short_user.get("username")
                or details.get("username")
                or canonical.get("userName")
                or canonical.get("username")
                or listing.get("seller_key")
            )
            if seller_username is not None and seller_username.isdecimal():
                seller_username = _optional_text(canonical.get("userName") or canonical.get("username"))

            seller_features: JsonDict | None = None
            if seller_username is not None:
                seller_features = await self._load_or_fetch_seller_features(
                    worker,
                    client,
                    seller_username,
                    expires_at=expires_at,
                )

            reviews = _listing_reviews(extra)
            review_times = [review["time_added"] for review in reviews if isinstance(review.get("time_added"), str)]
            listing_features: JsonDict = {
                "status": "ok" if seller_username is not None else "partial",
                "generation": _optional_int(payload.get("generation")) or 1,
                "resolved_seller_key": seller_username,
                "detail": details,
                "extra": extra,
                "description": _optional_text(
                    details.get("kwork_description") or details.get("description") or details.get("gdesc")
                ),
                "instructions": _optional_text(
                    details.get("kwork_instructions") or details.get("instructions") or details.get("ginst")
                ),
                "service_size": _optional_text(
                    details.get("unit_and_quantity") or details.get("volume") or details.get("gwork")
                ),
                "queue_count": _optional_int(details.get("orders_in_queue_count") or details.get("queueCount")),
                "work_time_seconds": _optional_int(details.get("term") or details.get("avgWorkTime")),
                "listing_reviews_count": _optional_int(extra.get("reviews_count")),
                "good_reviews": _optional_int(extra.get("goodReviews")),
                "bad_reviews": _optional_int(extra.get("badReviews")),
                "last_review_at": max(review_times) if review_times else None,
                "fetched_at": now,
                "expires_at": expires_at,
            }
            stored = await self.coordinator.repository.persist_listing_enrichment(
                job_id=worker.job_id,
                listing_id=listing_id,
                listing_features=listing_features,
                seller_features=seller_features,
                reviews=reviews,
            )
            job = await self._require_job(worker.job_id)
            await self.coordinator.emit(
                worker.job_id,
                "enrichment.completed",
                {
                    "listing_id": listing_id,
                    "status": stored["status"],
                    "reviews": len(reviews),
                    "seller_cached": seller_features is None,
                },
                revision=job["revision"],
                worker_id=worker.worker_id,
                operation_id=str(operation["operation_id"]),
            )
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise RetryableOperationError(
                f"enrichment network error: {type(exc).__name__}: {exc}",
                retry_at=self._retry_at(Decimal(str(DEFAULT_RATE_CONTROL_POLICY.fallback_retry_seconds))),
                failure_kind="network_error",
            ) from exc
        finally:
            await self._close_client(client)

    async def _enrichment_request(self, client: Any, endpoint: str, **params: Any) -> JsonDict:
        await self._acquire_source_permit("mobile_kworks")
        request = getattr(client, "request", None)
        if not callable(request):
            raise RetryableOperationError(
                "market client does not expose request() for enrichment",
                failure_kind="client_contract",
            )
        result = request(endpoint, **params)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise RetryableOperationError(
                f"{endpoint} returned a non-mapping payload",
                failure_kind="contract_violation",
            )
        return dict(result)

    async def _load_or_fetch_seller_features(
        self,
        worker: MarketWorker,
        client: Any,
        username: str,
        *,
        expires_at: str,
    ) -> JsonDict | None:
        lock_key = f"{worker.job_id}:{username.casefold()}"
        lock = self._seller_enrichment_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            cached = await self.coordinator.repository.get_seller_enrichment(worker.job_id, username)
            now = utc_now()
            if (
                cached is not None
                and cached.get("status") in {"ok", "partial"}
                and isinstance(cached.get("expires_at"), str)
                and cached["expires_at"] > now
            ):
                return None
            profile = _response_mapping(await self._enrichment_request(client, "userByUsername", username=username))
            return _seller_snapshot(username, profile, expires_at=expires_at)

    async def handle_analyze_snapshot(self, worker: MarketWorker, operation: Mapping[str, Any]) -> None:
        """Project metrics, checkpoint them, then queue the deterministic export."""

        operation_id = str(operation["operation_id"])
        analysis = await self.analyzer.analyze(worker.job_id, operation_id=operation_id)
        job = await self._require_job(worker.job_id)
        selection = analysis.get("enrichment_selection")
        ai_evidence = analysis.get("ai_evidence")
        selection_summary = (
            {"selected_count": selection.get("selected_count", 0)} if isinstance(selection, Mapping) else {}
        )
        terra_dossier = ai_evidence.get("terra_dossier") if isinstance(ai_evidence, Mapping) else None
        coverage = terra_dossier.get("coverage") if isinstance(terra_dossier, Mapping) else None
        groups = terra_dossier.get("groups") if isinstance(terra_dossier, Mapping) else []
        included_evidence_count = sum(
            len(group.get("evidence_ids") or []) for group in groups if isinstance(group, Mapping)
        )
        ai_summary = {
            "enabled": ai_evidence.get("enabled", True) if isinstance(ai_evidence, Mapping) else False,
            "included_evidence_count": included_evidence_count,
            "included_group_count": coverage.get("included_group_count", len(groups))
            if isinstance(coverage, Mapping)
            else len(groups),
            "sample_based": ai_evidence.get("sample_based", False) if isinstance(ai_evidence, Mapping) else False,
        }
        await self.coordinator.emit(
            worker.job_id,
            "job.metrics",
            {
                "metrics": analysis["metrics"],
                "checkpoint_id": analysis["checkpoint"]["checkpoint_id"],
                "enrichment": selection_summary,
                "ai_evidence": ai_summary,
            },
            revision=job["revision"],
            worker_id=worker.worker_id,
            operation_id=operation_id,
        )
        await self.coordinator.emit(
            worker.job_id,
            "checkpoint.saved",
            {"checkpoint_id": analysis["checkpoint"]["checkpoint_id"], "phase": JobPhase.ANALYZE.value},
            revision=job["revision"],
            worker_id=worker.worker_id,
            operation_id=operation_id,
        )
        if job["state"] == JobState.ANALYZING.value:
            finalizing = await self.coordinator.repository.update_job_state(
                worker.job_id,
                JobState.FINALIZING,
                phase=JobPhase.EXPORT,
                expected_revision=job["revision"],
            )
            await self.coordinator.emit_state_changed(finalizing)
            job = finalizing
        await self._enqueue_export_operation(
            worker.job_id,
            checkpoint_id=str(analysis["checkpoint"]["checkpoint_id"]),
            revision=int(job["revision"]),
        )

    async def handle_export_snapshot(self, worker: MarketWorker, operation: Mapping[str, Any]) -> None:
        """Write stable artifacts from durable rows and only then finish the job."""

        snapshot = await self.coordinator.repository.load_export_snapshot(worker.job_id)
        payload = operation.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        analysis_metrics: JsonDict = {}
        checkpoint_id = payload.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            checkpoint = await self.coordinator.repository.get_checkpoint(checkpoint_id)
            if checkpoint is not None and isinstance(checkpoint.get("metrics"), Mapping):
                analysis_metrics = dict(checkpoint["metrics"])
        market_metrics = analysis_metrics.get("market_metrics")
        if not isinstance(market_metrics, Mapping):
            market_metrics = {}
        manifest = self.exporter.export(
            job=snapshot["job"],
            listings=snapshot["listings"],
            observations=snapshot["observations"],
            metrics=analysis_metrics,
            summary={
                "profile": snapshot["job"]["profile"],
                "target_unique_cards": snapshot["job"]["target_unique_cards"],
                "counters": snapshot["job"].get("counters") or {},
            },
            events=snapshot["events"],
        )
        export_summary = self._export_manifest_summary(manifest)
        checkpoint = await self.coordinator.repository.create_checkpoint(
            worker.job_id,
            frontier={"phase": JobPhase.EXPORT.value, "operation_id": operation["operation_id"]},
            metrics={**analysis_metrics, "market_metrics": market_metrics, "export": export_summary},
        )
        job = await self._require_job(worker.job_id)
        if job["state"] != JobState.COMPLETED.value:
            completed = await self.coordinator.repository.update_job_state(
                worker.job_id,
                JobState.COMPLETED,
                phase=JobPhase.EXPORT,
                expected_revision=job["revision"],
            )
            await self.coordinator.emit_state_changed(completed)
            job = completed
        await self.coordinator.emit(
            worker.job_id,
            "result.ready",
            {"counters": job["counters"], "checkpoint_id": checkpoint["checkpoint_id"], "export": export_summary},
            revision=job["revision"],
            worker_id=worker.worker_id,
            operation_id=str(operation["operation_id"]),
        )

    async def _persist_plan_idempotently(self, plan) -> list[JsonDict]:
        records: list[JsonDict] = []
        for planned in plan.shards:
            existing = await self.coordinator.repository.get_shard(planned.shard.shard_id)
            if existing is not None:
                records.append(existing)
            else:
                records.append(await self.coordinator.repository.create_shard(planned.shard))
        return records

    async def _enqueue_fetch_operation(
        self,
        job_id: str,
        shard: Mapping[str, Any],
        *,
        cursor: SourceCursor | None,
        remaining_requests: int,
        priority: int,
    ) -> JsonDict:
        operation = await self.coordinator.repository.enqueue_operation(
            Operation(
                operation_id=f"op_{self._operation_hash(job_id, str(shard['shard_id']), cursor)}",
                job_id=job_id,
                shard_id=str(shard["shard_id"]),
                kind=OperationKind.FETCH_BATCH,
                state=OperationState.QUEUED,
                priority=priority,
                idempotency_key=f"fetch:{shard['shard_id']}:{_cursor_key(cursor)}",
                payload={"cursor": _cursor_payload(cursor), "remaining_requests": remaining_requests},
            )
        )
        job = await self._require_job(job_id)
        await self.coordinator.emit(
            job_id,
            "operation.queued",
            {"operation_id": operation["operation_id"], "kind": operation["kind"], "shard_id": shard["shard_id"]},
            revision=job["revision"],
            operation_id=operation["operation_id"],
        )
        return operation

    async def _complete_job_if_finished(self, job_id: str) -> None:
        job = await self._require_job(job_id)
        counters = job.get("counters") or {}
        unique_cards = int(counters.get("unique_cards", counters.get("unique_listings", 0)) or 0)
        if (
            job["state"] == JobState.RUNNING.value
            and job["phase"] == JobPhase.COLLECT.value
            and job.get("request_budget") is None
            and unique_cards < int(job["target_unique_cards"])
        ):
            mapping = await self.coordinator.ensure_partition_mapping(job_id)
            if mapping is not None:
                return
        await self.coordinator.ensure_analysis_operation(job_id)

    @staticmethod
    def _needs_partition_probes(job: MarketJob, root_batch: Any) -> bool:
        stream_limit = getattr(root_batch, "source_total", None)
        return job.scope.classifier_id is not None or (
            isinstance(stream_limit, int) and stream_limit > 0 and job.target_unique_cards > stream_limit
        )

    async def _read_scope_mapping_evidence(self, client: Any, job: MarketJob) -> JsonDict:
        """Read bounded aggregate and classification evidence when the client supports it."""

        evidence: JsonDict = {"errors": {}}
        for field, method_name in (
            ("catalog_filters", "get_catalog_filters"),
            ("category_attributes", "get_category_attributes"),
        ):
            method = getattr(client, method_name, None)
            if not callable(method):
                continue
            try:
                await self._acquire_source_permit("scope_mapping")
                result = await method(job.scope.category_id)
            except RetryableOperationError:
                raise
            except Exception as exc:
                evidence["errors"][field] = f"{type(exc).__name__}: {exc}"
                continue
            if isinstance(result, Mapping):
                evidence[field] = dict(result)
            else:
                evidence["errors"][field] = "unexpected_mapping_response"
        return evidence

    async def _validate_mapping_partitions(
        self,
        worker: MarketWorker,
        operation: Mapping[str, Any],
        adapter: KworkWebCatalogAdapter,
        job: MarketJob,
        *,
        root_batch: Any,
        attributes: object,
    ) -> tuple[tuple[ScopePartition, ...], list[JsonDict]]:
        candidate_attributes = attributes if isinstance(attributes, Mapping) else None
        all_candidates = self.mapper.classification_partition_candidates(
            job.scope,
            candidate_attributes,
            max_candidates=100,
        )
        existing_shards = await self.coordinator.repository.list_shards(job.job_id, limit=500)
        existing_filters = {
            self._filter_signature(shard["filters"])
            for shard in existing_shards
            if isinstance(shard.get("filters"), Mapping)
        }
        candidates = tuple(
            candidate
            for candidate in all_candidates
            if self._filter_signature(candidate.filters) not in existing_filters
        )[:5]
        accepted: list[ScopePartition] = []
        probes: list[JsonDict] = []
        root_fingerprint = getattr(root_batch, "fingerprint", None)
        for index, candidate in enumerate(candidates, start=1):
            request = adapter.build_request(
                alias=job.scope.canonical_alias or "",
                category_id=job.scope.category_id,
                filters=candidate.filters,
            )
            batch = await self._fetch_web_batch(worker, adapter, request)
            raw_response_ref = self._write_mapping_probe_raw(
                batch,
                job_id=job.job_id,
                operation=operation,
                index=index,
            )
            verdict = adapter.validate_batch(request, batch, previous=None)
            probe: JsonDict = {
                "key": candidate.key,
                "filters": dict(candidate.filters),
                "contract_state": verdict.state.value,
                "reason_codes": list(verdict.reason_codes),
                "actual_item_count": batch.actual_item_count,
                "source_total_found": batch.source_total_found,
                "raw_response_ref": raw_response_ref,
            }
            if verdict.state is ContractState.BLOCKED:
                raise await self._protection_retry(worker, batch)
            if verdict.state is not ContractState.ACCEPTED:
                probe["accepted"] = False
                probes.append(probe)
                continue
            if root_fingerprint is not None and batch.fingerprint == root_fingerprint:
                probe["accepted"] = False
                probe["reason_codes"] = [*probe["reason_codes"], "filter_not_effective"]
                probes.append(probe)
                continue
            expected_count = batch.source_total_found
            accepted.append(
                ScopePartition(
                    key=candidate.key,
                    filters=candidate.filters,
                    expected_count=expected_count if expected_count is not None else candidate.expected_count,
                    priority=expected_count if expected_count is not None else candidate.priority,
                    source=candidate.source,
                    alias=candidate.alias,
                )
            )
            probe["accepted"] = True
            probes.append(probe)
        return tuple(accepted), probes

    def _write_mapping_probe_raw(
        self,
        batch: Any,
        *,
        job_id: str,
        operation: Mapping[str, Any],
        index: int,
    ) -> str | None:
        raw_payload = batch.metadata.get("_raw_payload")
        if raw_payload is None:
            return batch.raw_response_ref
        attempt = max(int(operation.get("current_attempt") or 1), 1)
        reference = self.artifact_store.write_raw(
            job_id=job_id,
            operation_id=f"{operation['operation_id']}-partition-{index}",
            attempt=attempt,
            payload=raw_payload,
        )
        return str(reference["relative_path"])

    @staticmethod
    def _filter_signature(filters: Mapping[str, Any]) -> str:
        return json.dumps(dict(filters), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _write_mapping_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        job_id: str,
        operation: Mapping[str, Any],
    ) -> str | None:
        payload: JsonDict = {}
        for key in ("catalog_filters", "category_attributes"):
            value = evidence.get(key)
            if not isinstance(value, Mapping):
                continue
            payload[key] = value.get("raw", value)
        if not payload:
            return None
        attempt = max(int(operation.get("current_attempt") or 1), 1)
        try:
            reference = self.artifact_store.write_raw(
                job_id=job_id,
                operation_id=f"{operation['operation_id']}-mapping-evidence",
                attempt=attempt,
                payload=payload,
            )
        except (TypeError, ValueError):
            return None
        return str(reference["relative_path"])

    @staticmethod
    def _mapping_summary(
        evidence: Mapping[str, Any],
        *,
        raw_response_ref: str | None,
        mapping_evidence_ref: str | None,
        partition_probes: list[JsonDict],
    ) -> JsonDict:
        catalog_filters = evidence.get("catalog_filters")
        category_attributes = evidence.get("category_attributes")
        filter_values = catalog_filters.get("filters") if isinstance(catalog_filters, Mapping) else None
        flat_attributes = category_attributes.get("flat") if isinstance(category_attributes, Mapping) else None
        errors = evidence.get("errors") if isinstance(evidence.get("errors"), Mapping) else {}
        return {
            "raw_response_ref": raw_response_ref,
            "mapping_evidence_ref": mapping_evidence_ref,
            "catalog_filter_keys": sorted(str(key) for key in filter_values)[:80]
            if isinstance(filter_values, Mapping)
            else [],
            "category_attribute_count": len(flat_attributes) if isinstance(flat_attributes, list) else 0,
            "partition_probes": partition_probes,
            "mapping_errors": dict(errors),
        }

    async def _fetch_web_batch(self, worker: MarketWorker, adapter: KworkWebCatalogAdapter, request) -> Any:
        await self._acquire_source_permit(adapter.name)
        return await adapter.fetch_batch(request)

    async def _fetch_mobile_batch(self, adapter: KworkMobileKworksAdapter, request) -> Any:
        await self._acquire_source_permit(adapter.name)
        return await adapter.fetch_batch(request)

    async def _acquire_source_permit(self, source: str) -> None:
        async with self._rate_control_lock:
            result = acquire_request(
                self.rate_control_policy,
                self._rate_control_state,
                source=source,
                now=time.monotonic(),
            )
            self._rate_control_state = result.next_state
        if not result.allowed:
            raise RetryableOperationError(
                f"rate limit delayed request for {source}",
                retry_at=self._retry_at(result.wait_seconds),
                failure_kind="rate_limited",
            )

    async def _protection_retry(self, worker: MarketWorker, batch: Any) -> RetryableOperationError:
        adapter_metadata = batch.metadata.get("adapter") if isinstance(batch.metadata, Mapping) else None
        status_code = 403 if batch.protection_status is ProtectionStatus.BLOCKED else 429
        retry_after = None
        if isinstance(adapter_metadata, Mapping):
            raw_status = adapter_metadata.get("status_code")
            if isinstance(raw_status, int) and raw_status in {403, 429}:
                status_code = raw_status
            retry_after = adapter_metadata.get("retry_after")
        async with self._rate_control_lock:
            result = record_protection_response(
                self.rate_control_policy,
                self._rate_control_state,
                source=batch.source,
                status_code=status_code,
                retry_after=retry_after,
                now=time.monotonic(),
            )
            self._rate_control_state = result.next_state
        retry_at = self._retry_at(result.delay_seconds)
        if result.source_quarantine_recommended:
            await worker.quarantine_current_transport(reason="http_403", until=retry_at)
        failure_kind = "protection" if status_code == 403 else "rate_limited"
        return RetryableOperationError(
            f"{batch.source} returned HTTP {status_code}",
            retry_at=retry_at,
            failure_kind=failure_kind,
        )

    async def _enqueue_export_operation(self, job_id: str, *, checkpoint_id: str, revision: int) -> JsonDict:
        existing = await self.coordinator.repository.list_operations(job_id, limit=500)
        for operation in existing:
            if operation["kind"] == OperationKind.EXPORT_SNAPSHOT.value and operation["state"] in {
                OperationState.QUEUED.value,
                OperationState.LEASED.value,
                OperationState.RUNNING.value,
                OperationState.RETRY_WAIT.value,
            }:
                return operation
        operation = await self.coordinator.repository.enqueue_operation(
            Operation(
                # A resumed collection produces a fresh checkpoint.  Keep the
                # operation ID checkpoint-specific so a prior completed export
                # cannot collide with this new durable export request.
                operation_id=f"op_export_{sha256(f'{job_id}:export:{checkpoint_id}'.encode()).hexdigest()[:24]}",
                job_id=job_id,
                shard_id=None,
                kind=OperationKind.EXPORT_SNAPSHOT,
                state=OperationState.QUEUED,
                priority=400,
                idempotency_key=f"export:{job_id}:{checkpoint_id}",
                payload={"checkpoint_id": checkpoint_id},
            )
        )
        await self.coordinator.emit(
            job_id,
            "operation.queued",
            {"operation_id": operation["operation_id"], "kind": operation["kind"]},
            revision=revision,
            operation_id=operation["operation_id"],
        )
        return operation

    async def _block_job(self, job: Mapping[str, Any], reason: str) -> None:
        if job["state"] == JobState.BLOCKED.value:
            return
        blocked = await self.coordinator.repository.update_job_state(
            str(job["job_id"]),
            JobState.BLOCKED,
            phase=str(job["phase"]),
            expected_revision=job["revision"],
            last_warning=reason,
        )
        await self.coordinator.emit_state_changed(blocked, {"reason": reason})

    async def _web_adapter(self, worker: MarketWorker) -> tuple[KworkWebCatalogAdapter, Any]:
        client = self.client_factory(worker.transport_proxy_url)
        cookies: dict[str, str] | None = None
        if self._web_cookie_provider is not None:
            provided = await self._web_cookie_provider()
            if isinstance(provided, Mapping):
                cookies = {
                    str(name): str(value)
                    for name, value in provided.items()
                    if isinstance(name, str) and isinstance(value, str) and name and value
                }
        return KworkWebCatalogAdapter(client=client, cookies=cookies), client

    def _mobile_adapter(self, worker: MarketWorker) -> tuple[KworkMobileKworksAdapter, Any]:
        client = self.client_factory(worker.transport_proxy_url)
        return KworkMobileKworksAdapter(client=client), client

    def _write_raw(self, batch, *, job_id: str, operation: Mapping[str, Any]) -> str | None:
        raw_payload = batch.metadata.get("_raw_payload")
        if raw_payload is None:
            return batch.raw_response_ref
        attempt = max(int(operation.get("current_attempt") or 1), 1)
        reference = self.artifact_store.write_raw(
            job_id=job_id,
            operation_id=str(operation["operation_id"]),
            attempt=attempt,
            payload=raw_payload,
        )
        return str(reference["relative_path"])

    async def _require_job(self, job_id: str) -> JsonDict:
        job = await self.coordinator.repository.get_job(job_id)
        if job is None:
            raise RuntimeError(f"market job {job_id!r} no longer exists")
        return job

    @staticmethod
    def _operation_hash(job_id: str, shard_id: str, cursor: SourceCursor | None) -> str:
        payload = f"{job_id}:{shard_id}:{_cursor_key(cursor)}"
        return sha256(payload.encode()).hexdigest()[:24]

    @staticmethod
    def _export_manifest_summary(manifest: Mapping[str, Any]) -> JsonDict:
        summary: JsonDict = {"schema_version": manifest["schema_version"], "job_id": manifest["job_id"]}
        for key in ("summary", "listings", "observations"):
            value = manifest.get(key)
            if isinstance(value, Mapping):
                summary[key] = {
                    field: value[field]
                    for field in ("relative_path", "sha256", "byte_size", "schema_version")
                    if field in value
                }
        return summary

    @staticmethod
    def _retry_at(wait_seconds: Decimal) -> str:
        delay = max(float(wait_seconds), 0.0)
        timestamp = datetime.now(UTC) + timedelta(seconds=delay)
        return timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    async def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
