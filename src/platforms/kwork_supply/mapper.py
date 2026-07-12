"""Validated market-scope mapping for durable collection planning."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .contracts import BatchResult, ContractState, ContractVerdict, ProtectionStatus
from .models import MarketScope
from .repository import MarketJobRepository


class AliasValidationStatus(StrEnum):
    """Persistent status of a canonical category alias."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _is_true(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value == 1


@dataclass(frozen=True, slots=True)
class AliasValidation:
    """Evidence that a source alias belongs to the requested category."""

    category_id: int
    canonical_alias: str
    source: str
    status: AliasValidationStatus
    active_category_id: int | None
    source_url: str | None = None
    protection_status: ProtectionStatus = ProtectionStatus.UNKNOWN
    schema_version: int = 1
    reason_codes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is AliasValidationStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class ScopePartition:
    """A candidate shard discovered from scope mapping, before planning."""

    key: str
    filters: Mapping[str, Any] = field(default_factory=dict)
    expected_count: int | None = None
    priority: int = 0
    source: str | None = None
    alias: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("partition key is required")
        if self.expected_count is not None and self.expected_count < 0:
            raise ValueError("expected_count cannot be negative")


@dataclass(frozen=True, slots=True)
class ScopeMap:
    """Mapping evidence with aggregate and observed values deliberately separated."""

    scope: MarketScope
    source: str
    alias_validation: AliasValidation
    aggregate_scope_total: int | None
    reported_stream_total: int | None
    observed_first_batch_count: int
    partitions: tuple[ScopePartition, ...]

    @property
    def ready_for_planning(self) -> bool:
        if not self.alias_validation.accepted or self.source != "web_catalog":
            return False
        return all(
            partition.source == self.source and partition.alias == self.alias_validation.canonical_alias
            for partition in self.partitions
        )

    def with_partitions(self, partitions: Iterable[ScopePartition]) -> ScopeMap:
        return replace(self, partitions=tuple(partitions))


class ScopeMapper:
    """Turn source evidence into a planner-safe, auditable market scope map."""

    def map_scope(
        self,
        scope: MarketScope,
        batch: BatchResult,
        verdict: ContractVerdict,
        *,
        source_url: str | None = None,
        partitions: Iterable[ScopePartition] = (),
    ) -> ScopeMap:
        if not isinstance(scope, MarketScope):
            raise TypeError("scope must be a MarketScope")
        if not isinstance(batch, BatchResult):
            raise TypeError("batch must be a BatchResult")
        if not isinstance(verdict, ContractVerdict):
            raise TypeError("verdict must be a ContractVerdict")

        canonical_alias = (scope.canonical_alias or "").strip()
        active_category_id = _as_nonnegative_int(batch.metadata.get("active_category_id"))
        metadata_adapter = batch.metadata.get("adapter")
        if source_url is None and isinstance(metadata_adapter, Mapping):
            adapter_url = metadata_adapter.get("url")
            source_url = str(adapter_url) if adapter_url else None
        schema_version = _as_nonnegative_int(batch.metadata.get("schema_version")) or 1
        reasons: list[str] = []
        if batch.protection_status.blocks_collection:
            status = AliasValidationStatus.BLOCKED
            reasons.append("protection_signal")
        else:
            status = AliasValidationStatus.ACCEPTED
            if batch.source != "web_catalog":
                reasons.append("primary_source_not_web_catalog")
            if not canonical_alias:
                reasons.append("canonical_alias_missing")
            if verdict.state not in {ContractState.ACCEPTED, ContractState.EXHAUSTED}:
                reasons.append("source_contract_not_accepted")
            if batch.metadata.get("shape_valid") is not True:
                reasons.append("card_shape_invalid")
            if active_category_id != scope.category_id:
                reasons.append("active_category_mismatch")
            if reasons:
                status = AliasValidationStatus.REJECTED

        alias_validation = AliasValidation(
            category_id=scope.category_id,
            canonical_alias=canonical_alias,
            source=batch.source,
            status=status,
            active_category_id=active_category_id,
            source_url=source_url,
            protection_status=batch.protection_status,
            schema_version=schema_version,
            reason_codes=tuple(reasons),
        )
        aggregate_scope_total = batch.source_total_found
        if aggregate_scope_total is None:
            aggregate_scope_total = batch.source_total
        normalized_partitions = self._normalize_partitions(
            scope,
            batch.source,
            canonical_alias,
            aggregate_scope_total,
            partitions,
        )
        return ScopeMap(
            scope=scope,
            source=batch.source,
            alias_validation=alias_validation,
            aggregate_scope_total=aggregate_scope_total,
            reported_stream_total=batch.source_total,
            observed_first_batch_count=batch.actual_item_count or 0,
            partitions=normalized_partitions,
        )

    async def persist_alias_validation(self, repository: MarketJobRepository, scope_map: ScopeMap) -> dict[str, Any]:
        """Persist validation evidence without coupling mapping to a coordinator."""

        validation = scope_map.alias_validation
        return await repository.upsert_category_alias(
            validation.category_id,
            validation.canonical_alias,
            validation_status=validation.status.value,
            active_category_id=validation.active_category_id,
            source_url=validation.source_url,
            protection_state=validation.protection_status.value,
            schema_version=validation.schema_version,
        )

    @staticmethod
    def classification_partition_candidates(
        scope: MarketScope,
        attributes: Mapping[str, Any] | None,
        *,
        max_candidates: int = 5,
    ) -> tuple[ScopePartition, ...]:
        """Build conservative web-filter candidates from classification values.

        These are candidates only: the executor must still prove that the web
        catalog filter changes a contract-valid batch before it creates a shard.
        Seller and price dimensions are deliberately excluded because their
        coverage semantics have not been proven.
        """

        if max_candidates <= 0 or not isinstance(attributes, Mapping):
            return ()
        flat = attributes.get("flat")
        if not isinstance(flat, list):
            return ()

        rows_by_id: dict[int, Mapping[str, Any]] = {}
        for row in flat:
            if not isinstance(row, Mapping):
                continue
            identifier = _as_nonnegative_int(row.get("id"))
            if identifier is not None:
                rows_by_id[identifier] = row

        candidates: list[ScopePartition] = []
        seen: set[tuple[int, int]] = set()
        for row in flat:
            if not isinstance(row, Mapping):
                continue
            value_id = _as_nonnegative_int(row.get("id"))
            path_ids = row.get("path_ids")
            if value_id is None or not isinstance(path_ids, list) or len(path_ids) < 2:
                continue
            parent_id = _as_nonnegative_int(path_ids[-2])
            if parent_id is None or (parent_id, value_id) in seen:
                continue
            parent = rows_by_id.get(parent_id)
            parent_raw = parent.get("raw") if isinstance(parent, Mapping) else None
            if not isinstance(parent_raw, Mapping) or not _is_true(
                parent_raw.get("is_classification", parent_raw.get("isClassification"))
            ):
                continue
            seen.add((parent_id, value_id))
            expected_count = _as_nonnegative_int(row.get("kworks_count"))
            filters = dict(scope.filters)
            filters[f"attribute[{parent_id}]"] = str(value_id)
            candidates.append(
                ScopePartition(
                    key=f"attribute:{parent_id}:{value_id}",
                    filters=filters,
                    expected_count=expected_count,
                    priority=expected_count or 0,
                    source="web_catalog",
                    alias=scope.canonical_alias,
                )
            )

        if scope.classifier_id is not None:
            classifier_filters = dict(scope.filters)
            classifier_filters["classifierId"] = str(scope.classifier_id)
            candidates.append(
                ScopePartition(
                    key=f"classifier:{scope.classifier_id}",
                    filters=classifier_filters,
                    priority=1_000_000,
                    source="web_catalog",
                    alias=scope.canonical_alias,
                )
            )
        candidates.sort(key=lambda item: (-item.priority, item.key))
        return tuple(candidates[:max_candidates])

    @staticmethod
    def _normalize_partitions(
        scope: MarketScope,
        source: str,
        canonical_alias: str,
        aggregate_scope_total: int | None,
        partitions: Iterable[ScopePartition],
    ) -> tuple[ScopePartition, ...]:
        supplied = tuple(partitions)
        if not supplied:
            supplied = (ScopePartition(key="root", filters=scope.filters, expected_count=aggregate_scope_total),)
        normalized: list[ScopePartition] = []
        for partition in supplied:
            if not isinstance(partition, ScopePartition):
                raise TypeError("partitions must contain ScopePartition values")
            filters = dict(scope.filters)
            filters.update(partition.filters)
            normalized.append(
                ScopePartition(
                    key=partition.key,
                    filters=filters,
                    expected_count=(
                        partition.expected_count if partition.expected_count is not None else aggregate_scope_total
                    ),
                    priority=partition.priority,
                    source=partition.source or source,
                    alias=partition.alias or canonical_alias,
                )
            )
        return tuple(normalized)
