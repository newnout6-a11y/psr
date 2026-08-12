"""Deterministic shadow comparison and guarded Buyer Search rollout helpers.

The legacy scout is intentionally kept outside this module.  Callers pass its
already captured rows and endpoint observations here, so the comparison is
reproducible, has no network side effects, and can be stored as an audit
artifact before enabling a larger Buyer Search canary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import math
from typing import Any
import unicodedata
from urllib.parse import urlsplit, urlunsplit


CANARY_WORKER_TARGETS: tuple[int, ...] = (2, 5, 10, 20, 30)
"""The only supported live-discovery expansion stages."""

DEFAULT_BUYER_SHADOW_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "budget_min",
    "budget_max",
    "offers",
    "views",
    "status",
    "category_id",
    "buyer_remote_user_id",
    "buyer_username",
    "canonical_url",
    "remote_updated_at",
)
"""Canonical project fields reported by a default shadow comparison."""

DEFAULT_REQUIRED_BUYER_SHADOW_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "budget_min",
    "budget_max",
    "offers",
    "views",
    "canonical_url",
)
"""Fields which must not silently disappear during default acceptance."""


class BuyerShadowInputError(ValueError):
    """Raised when a supplied shadow artifact cannot be compared safely."""


@dataclass(frozen=True, slots=True)
class BuyerShadowFieldMismatch:
    """A value mismatch for one canonical project and normalized field."""

    project_id: str
    field: str
    legacy_value: Any
    new_value: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "field": self.field,
            "legacy_value": self.legacy_value,
            "new_value": self.new_value,
        }


@dataclass(frozen=True, slots=True)
class BuyerShadowFieldParity:
    """Per-field parity report across the canonical project intersection."""

    field: str
    matched_project_ids: tuple[str, ...]
    missing_in_legacy_project_ids: tuple[str, ...]
    missing_in_new_project_ids: tuple[str, ...]
    missing_in_both_project_ids: tuple[str, ...]
    mismatches: tuple[BuyerShadowFieldMismatch, ...]

    @property
    def is_exact(self) -> bool:
        """Whether every shared project has an equal, present value."""

        return not (
            self.missing_in_legacy_project_ids
            or self.missing_in_new_project_ids
            or self.missing_in_both_project_ids
            or self.mismatches
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "matched_project_ids": list(self.matched_project_ids),
            "missing_in_legacy_project_ids": list(self.missing_in_legacy_project_ids),
            "missing_in_new_project_ids": list(self.missing_in_new_project_ids),
            "missing_in_both_project_ids": list(self.missing_in_both_project_ids),
            "mismatches": [item.as_dict() for item in self.mismatches],
        }


@dataclass(frozen=True, slots=True)
class BuyerShadowDuplicateProject:
    """Duplicate canonical IDs observed within one captured flow."""

    side: str
    project_id: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {"side": self.side, "project_id": self.project_id, "count": self.count}


@dataclass(frozen=True, slots=True)
class BuyerShadowEndpointSample:
    """One captured read endpoint outcome from a legacy or Buyer run."""

    endpoint: str
    status_code: int | None = None
    result_count: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        endpoint = normalize_buyer_shadow_endpoint(self.endpoint)
        status_code = _optional_status_code(self.status_code)
        result_count = _optional_nonnegative_int(self.result_count, "result_count")
        error = _optional_text(self.error)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "status_code", status_code)
        object.__setattr__(self, "result_count", result_count)
        object.__setattr__(self, "error", error)

    @property
    def is_error(self) -> bool:
        """Return whether the sample represents a failed endpoint outcome."""

        return self.error is not None or (self.status_code is not None and self.status_code >= 400)


@dataclass(frozen=True, slots=True)
class BuyerShadowEndpointDrift:
    """Aggregated endpoint differences between legacy and new captures."""

    endpoint: str
    legacy_calls: int
    new_calls: int
    legacy_status_counts: tuple[tuple[int, int], ...]
    new_status_counts: tuple[tuple[int, int], ...]
    legacy_result_count: int
    new_result_count: int
    legacy_error_count: int
    new_error_count: int

    @property
    def missing_in_legacy(self) -> bool:
        return self.legacy_calls == 0 and self.new_calls > 0

    @property
    def missing_in_new(self) -> bool:
        return self.new_calls == 0 and self.legacy_calls > 0

    @property
    def call_count_mismatch(self) -> bool:
        return self.legacy_calls != self.new_calls

    @property
    def status_code_mismatch(self) -> bool:
        # A distributed Buyer run may legitimately make more successful calls
        # than the one-shot scout.  Drift here means a different status class,
        # while call-volume changes remain visible through call_count_mismatch.
        return tuple(status for status, _ in self.legacy_status_counts) != tuple(status for status, _ in self.new_status_counts)

    @property
    def result_count_mismatch(self) -> bool:
        return self.legacy_result_count != self.new_result_count

    @property
    def error_count_mismatch(self) -> bool:
        return self.legacy_error_count != self.new_error_count

    @property
    def has_drift(self) -> bool:
        """Whether any captured endpoint behavior differs."""

        return (
            self.missing_in_legacy
            or self.missing_in_new
            or self.call_count_mismatch
            or self.status_code_mismatch
            or self.result_count_mismatch
            or self.error_count_mismatch
        )

    @property
    def is_critical(self) -> bool:
        """Whether drift can hide data loss or a degraded read endpoint."""

        return (
            self.missing_in_new
            or (not self.missing_in_legacy and self.status_code_mismatch)
            or self.new_error_count > self.legacy_error_count
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "legacy_calls": self.legacy_calls,
            "new_calls": self.new_calls,
            "legacy_status_counts": dict(self.legacy_status_counts),
            "new_status_counts": dict(self.new_status_counts),
            "legacy_result_count": self.legacy_result_count,
            "new_result_count": self.new_result_count,
            "legacy_error_count": self.legacy_error_count,
            "new_error_count": self.new_error_count,
            "missing_in_legacy": self.missing_in_legacy,
            "missing_in_new": self.missing_in_new,
            "call_count_mismatch": self.call_count_mismatch,
            "status_code_mismatch": self.status_code_mismatch,
            "result_count_mismatch": self.result_count_mismatch,
            "error_count_mismatch": self.error_count_mismatch,
            "is_critical": self.is_critical,
        }


@dataclass(frozen=True, slots=True)
class BuyerShadowComparison:
    """Immutable result of comparing one legacy scout capture to a Buyer run."""

    legacy_project_ids: tuple[str, ...]
    new_project_ids: tuple[str, ...]
    matched_project_ids: tuple[str, ...]
    legacy_only_project_ids: tuple[str, ...]
    new_only_project_ids: tuple[str, ...]
    duplicate_projects: tuple[BuyerShadowDuplicateProject, ...]
    field_parity: tuple[BuyerShadowFieldParity, ...]
    endpoint_drifts: tuple[BuyerShadowEndpointDrift, ...]

    @property
    def has_silent_data_loss(self) -> bool:
        """Legacy canonical projects absent from the new run are a hard signal."""

        return bool(self.legacy_only_project_ids)

    @property
    def has_critical_endpoint_drift(self) -> bool:
        return any(item.is_critical for item in self.endpoint_drifts)

    def parity_for(self, field: str) -> BuyerShadowFieldParity:
        """Return the report for a normalized field name."""

        normalized = _required_field_name(field)
        for report in self.field_parity:
            if report.field == normalized:
                return report
        raise KeyError(normalized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "legacy_project_ids": list(self.legacy_project_ids),
            "new_project_ids": list(self.new_project_ids),
            "matched_project_ids": list(self.matched_project_ids),
            "legacy_only_project_ids": list(self.legacy_only_project_ids),
            "new_only_project_ids": list(self.new_only_project_ids),
            "duplicate_projects": [item.as_dict() for item in self.duplicate_projects],
            "field_parity": [item.as_dict() for item in self.field_parity],
            "endpoint_drifts": [item.as_dict() for item in self.endpoint_drifts],
        }


@dataclass(frozen=True, slots=True)
class BuyerShadowAcceptanceSummary:
    """Explicit operator/rollback acceptance result for a shadow comparison."""

    accepted: bool
    operator_accepted: bool
    rollback_documented: bool
    required_fields: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    legacy_project_count: int
    new_project_count: int
    matched_project_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "operator_accepted": self.operator_accepted,
            "rollback_documented": self.rollback_documented,
            "required_fields": list(self.required_fields),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "legacy_project_count": self.legacy_project_count,
            "new_project_count": self.new_project_count,
            "matched_project_count": self.matched_project_count,
        }


@dataclass(frozen=True, slots=True)
class BuyerWorkerIdentityEvidence:
    """Verified identity/route tuple used to approve one live worker slot."""

    worker_id: str
    account_registration_id: str
    transport_id: str
    egress_ip: str
    healthy: bool = True
    verified_egress: bool = True
    route_generation: int | None = None

    def __post_init__(self) -> None:
        for name in ("worker_id", "account_registration_id", "transport_id", "egress_ip"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.healthy, bool) or not isinstance(self.verified_egress, bool):
            raise TypeError("healthy and verified_egress must be booleans")
        if self.route_generation is not None and (
            isinstance(self.route_generation, bool) or not isinstance(self.route_generation, int) or self.route_generation < 0
        ):
            raise ValueError("route_generation must be a non-negative integer or None")

    @property
    def normalized_egress_ip(self) -> str | None:
        """Return an RFC-normalized IP or ``None`` if it is not an address."""

        try:
            return ipaddress.ip_address(self.egress_ip).compressed
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class BuyerShadowRolloutConfig:
    """Fail-closed configuration for staged Buyer Search discovery rollout."""

    enabled: bool = False
    live_discovery: bool = False
    max_workers: int = 2
    canary_targets: tuple[int, ...] = CANARY_WORKER_TARGETS
    completed_canary_targets: tuple[int, ...] = ()
    require_shadow_acceptance: bool = True
    require_verified_egress: bool = True
    require_unique_identity_evidence: bool = True
    legacy_retirement_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "enabled",
            "live_discovery",
            "require_shadow_acceptance",
            "require_verified_egress",
            "require_unique_identity_evidence",
            "legacy_retirement_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        if isinstance(self.max_workers, bool) or not isinstance(self.max_workers, int) or not 1 <= self.max_workers <= 30:
            raise ValueError("max_workers must be an integer between 1 and 30")
        targets = _normalize_canary_targets(self.canary_targets)
        completed = tuple(sorted(set(_positive_int(value, "completed_canary_targets") for value in self.completed_canary_targets)))
        if any(value not in targets for value in completed):
            raise ValueError("completed_canary_targets must be configured canary targets")
        object.__setattr__(self, "canary_targets", targets)
        object.__setattr__(self, "completed_canary_targets", completed)


@dataclass(frozen=True, slots=True)
class BuyerShadowRolloutGate:
    """Decision and evidence snapshot for one requested canary stage."""

    target_workers: int
    allowed: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_count: int
    eligible_evidence_count: int
    worker_ids: tuple[str, ...]
    account_registration_ids: tuple[str, ...]
    transport_ids: tuple[str, ...]
    egress_ips: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_workers": self.target_workers,
            "allowed": self.allowed,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "evidence_count": self.evidence_count,
            "eligible_evidence_count": self.eligible_evidence_count,
            "worker_ids": list(self.worker_ids),
            "account_registration_ids": list(self.account_registration_ids),
            "transport_ids": list(self.transport_ids),
            "egress_ips": list(self.egress_ips),
        }


def canonical_buyer_project_id(project: Mapping[str, Any], *, default_platform: str = "kwork") -> str:
    """Return ``platform:remote_project_id`` from legacy or new project shapes.

    The function deliberately prefers a remote project identifier over a local
    durable ``project_id``.  It accepts the nested ``canonical`` shape emitted
    by the mapper and an already-canonical ``project_id`` as a fallback.
    """

    values = _project_sections(project)
    platform = _first_present(values, ("platform",)) or default_platform
    platform_text = _required_text(platform, "platform").casefold()
    explicit = _first_present(values, ("canonical_project_id", "canonical_id", "buyer_project_id"))
    if isinstance(explicit, str) and ":" in explicit:
        prefix, remote_id = explicit.split(":", 1)
        if prefix.strip() and remote_id.strip():
            return f"{prefix.strip().casefold()}:{remote_id.strip()}"
    remote_id = _first_present(
        values,
        ("remote_project_id", "remote_id", "want_id", "wantId", "projectId", "id", "buyer_project_id"),
    )
    if remote_id is None:
        project_id = _first_present(values, ("project_id",))
        if isinstance(project_id, str) and ":" in project_id:
            prefix, remote_id = project_id.split(":", 1)
            if prefix.strip() and remote_id.strip():
                return f"{prefix.strip().casefold()}:{remote_id.strip()}"
        remote_id = project_id
    return f"{platform_text}:{_required_text(remote_id, 'remote_project_id')}"


def compare_buyer_shadow(
    legacy_projects: Iterable[Mapping[str, Any]],
    new_projects: Iterable[Mapping[str, Any]],
    *,
    fields: Sequence[str] = DEFAULT_BUYER_SHADOW_FIELDS,
    legacy_endpoints: Iterable[BuyerShadowEndpointSample | Mapping[str, Any]] | Mapping[str, Any] = (),
    new_endpoints: Iterable[BuyerShadowEndpointSample | Mapping[str, Any]] | Mapping[str, Any] = (),
    endpoint_aliases: Mapping[str, str] | None = None,
) -> BuyerShadowComparison:
    """Compare captured legacy scout rows against one new Buyer Search run.

    Inputs are treated as immutable snapshots.  The result is independent of
    input order, including duplicate project records and endpoint sample order.
    """

    normalized_fields = _normalize_fields(fields)
    legacy_by_id, legacy_duplicates = _index_projects(legacy_projects, side="legacy")
    new_by_id, new_duplicates = _index_projects(new_projects, side="new")
    legacy_ids = tuple(sorted(legacy_by_id))
    new_ids = tuple(sorted(new_by_id))
    matched_ids = tuple(sorted(set(legacy_ids) & set(new_ids)))
    legacy_only = tuple(sorted(set(legacy_ids) - set(new_ids)))
    new_only = tuple(sorted(set(new_ids) - set(legacy_ids)))
    field_parity = tuple(
        _compare_field(field, matched_ids=matched_ids, legacy_by_id=legacy_by_id, new_by_id=new_by_id)
        for field in normalized_fields
    )
    endpoint_drifts = compare_buyer_endpoint_drift(
        legacy_endpoints,
        new_endpoints,
        endpoint_aliases=endpoint_aliases,
    )
    duplicates = tuple(sorted((*legacy_duplicates, *new_duplicates), key=lambda item: (item.side, item.project_id)))
    return BuyerShadowComparison(
        legacy_project_ids=legacy_ids,
        new_project_ids=new_ids,
        matched_project_ids=matched_ids,
        legacy_only_project_ids=legacy_only,
        new_only_project_ids=new_only,
        duplicate_projects=duplicates,
        field_parity=field_parity,
        endpoint_drifts=endpoint_drifts,
    )


def compare_buyer_endpoint_drift(
    legacy_events: Iterable[BuyerShadowEndpointSample | Mapping[str, Any]] | Mapping[str, Any],
    new_events: Iterable[BuyerShadowEndpointSample | Mapping[str, Any]] | Mapping[str, Any],
    *,
    endpoint_aliases: Mapping[str, str] | None = None,
) -> tuple[BuyerShadowEndpointDrift, ...]:
    """Aggregate endpoint call/status/result drift without making remote calls."""

    aliases = _normalize_endpoint_aliases(endpoint_aliases)
    legacy = _group_endpoint_samples(_coerce_endpoint_samples(legacy_events), aliases=aliases)
    new = _group_endpoint_samples(_coerce_endpoint_samples(new_events), aliases=aliases)
    result: list[BuyerShadowEndpointDrift] = []
    for endpoint in sorted(set(legacy) | set(new)):
        legacy_samples = legacy.get(endpoint, ())
        new_samples = new.get(endpoint, ())
        result.append(
            BuyerShadowEndpointDrift(
                endpoint=endpoint,
                legacy_calls=len(legacy_samples),
                new_calls=len(new_samples),
                legacy_status_counts=_status_counts(legacy_samples),
                new_status_counts=_status_counts(new_samples),
                legacy_result_count=sum(sample.result_count or 0 for sample in legacy_samples),
                new_result_count=sum(sample.result_count or 0 for sample in new_samples),
                legacy_error_count=sum(sample.is_error for sample in legacy_samples),
                new_error_count=sum(sample.is_error for sample in new_samples),
            )
        )
    return tuple(result)


def summarize_buyer_shadow_acceptance(
    comparison: BuyerShadowComparison,
    *,
    operator_accepted: bool,
    rollback_documented: bool,
    required_fields: Sequence[str] = DEFAULT_REQUIRED_BUYER_SHADOW_FIELDS,
) -> BuyerShadowAcceptanceSummary:
    """Produce a fail-closed acceptance decision from a stored comparison."""

    if not isinstance(comparison, BuyerShadowComparison):
        raise TypeError("comparison must be a BuyerShadowComparison")
    required = _normalize_fields(required_fields)
    blockers: list[str] = []
    warnings: list[str] = []
    if not operator_accepted:
        blockers.append("operator acceptance is required")
    if not rollback_documented:
        blockers.append("rollback documentation is required")
    if comparison.legacy_only_project_ids:
        blockers.append(f"new Buyer Search is missing {len(comparison.legacy_only_project_ids)} legacy canonical project(s)")
    if comparison.duplicate_projects:
        blockers.append("duplicate canonical project IDs exist in the shadow artifacts")
    if comparison.new_only_project_ids:
        warnings.append(f"new Buyer Search discovered {len(comparison.new_only_project_ids)} additional canonical project(s)")
    reports = {report.field: report for report in comparison.field_parity}
    for field in required:
        report = reports.get(field)
        if report is None:
            blockers.append(f"required field {field!r} was not included in the comparison")
            continue
        missing_new = len(report.missing_in_new_project_ids) + len(report.missing_in_both_project_ids)
        if missing_new:
            blockers.append(f"required field {field!r} is missing from {missing_new} new Buyer Search project(s)")
        if report.mismatches:
            blockers.append(f"required field {field!r} differs for {len(report.mismatches)} canonical project(s)")
        if report.missing_in_legacy_project_ids:
            warnings.append(
                f"legacy scout lacks required field {field!r} for {len(report.missing_in_legacy_project_ids)} project(s)"
            )
    for drift in comparison.endpoint_drifts:
        if drift.is_critical:
            blockers.append(f"critical endpoint drift at {drift.endpoint}")
        elif drift.has_drift:
            warnings.append(f"non-critical endpoint drift at {drift.endpoint}")
    return BuyerShadowAcceptanceSummary(
        accepted=not blockers,
        operator_accepted=operator_accepted,
        rollback_documented=rollback_documented,
        required_fields=required,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        legacy_project_count=len(comparison.legacy_project_ids),
        new_project_count=len(comparison.new_project_ids),
        matched_project_count=len(comparison.matched_project_ids),
    )


def evaluate_buyer_shadow_rollout_gate(
    target_workers: int,
    evidence: Iterable[BuyerWorkerIdentityEvidence | Mapping[str, Any]],
    *,
    config: BuyerShadowRolloutConfig | None = None,
    acceptance: BuyerShadowAcceptanceSummary | None = None,
) -> BuyerShadowRolloutGate:
    """Allow a canary stage only with sufficient independent route evidence.

    A stage is rejected unless it has one healthy worker evidence record per
    target worker and those records have pairwise unique account, transport and
    verified egress IP values.  The function does not mutate configuration or
    remember a stage; callers persist completed stages in the durable control
    plane and pass them back through ``completed_canary_targets``.
    """

    if config is None:
        config = BuyerShadowRolloutConfig()
    if not isinstance(config, BuyerShadowRolloutConfig):
        raise TypeError("config must be a BuyerShadowRolloutConfig")
    target = _positive_int(target_workers, "target_workers")
    records = tuple(_coerce_identity_evidence(item) for item in evidence)
    blockers: list[str] = []
    warnings: list[str] = []
    if not config.enabled:
        blockers.append("BUYER_SEARCH_ENABLED is disabled")
    if not config.live_discovery:
        blockers.append("BUYER_SEARCH_LIVE_DISCOVERY is disabled")
    if target not in config.canary_targets:
        blockers.append(f"{target} is not an approved canary target")
    if target > config.max_workers:
        blockers.append(f"target {target} exceeds configured max_workers {config.max_workers}")
    previous = _previous_canary_target(target, config.canary_targets)
    if previous is not None and previous not in config.completed_canary_targets:
        blockers.append(f"canary {previous} must be completed before {target}")
    if config.require_shadow_acceptance:
        if acceptance is None or not acceptance.accepted:
            blockers.append("accepted shadow comparison is required")
    elif acceptance is not None and not acceptance.accepted:
        warnings.append("shadow comparison is not accepted")

    invalid_egress = tuple(sorted({item.egress_ip for item in records if item.normalized_egress_ip is None}))
    if invalid_egress:
        blockers.append("identity evidence contains an invalid egress IP")
    healthy = tuple(item for item in records if item.healthy and item.normalized_egress_ip is not None)
    eligible = (
        tuple(item for item in healthy if item.verified_egress)
        if config.require_verified_egress
        else healthy
    )
    excluded = len(records) - len(eligible)
    if excluded:
        warnings.append(f"{excluded} identity evidence record(s) are not eligible for effective capacity")
    worker_ids = tuple(sorted({item.worker_id for item in eligible}))
    account_ids = tuple(sorted({item.account_registration_id for item in eligible}))
    transport_ids = tuple(sorted({item.transport_id for item in eligible}))
    egress_ips = tuple(sorted({item.normalized_egress_ip for item in eligible if item.normalized_egress_ip is not None}))
    if len(worker_ids) < target:
        blockers.append(f"requires {target} unique worker evidence record(s), found {len(worker_ids)}")
    if config.require_unique_identity_evidence:
        for label, values in (
            ("account", account_ids),
            ("transport", transport_ids),
            ("verified egress IP", egress_ips),
        ):
            if len(values) < target:
                blockers.append(f"requires {target} unique {label} value(s), found {len(values)}")
        duplicate_labels = _duplicate_identity_labels(eligible)
        if duplicate_labels:
            blockers.append("identity collision in evidence: " + ", ".join(duplicate_labels))
    return BuyerShadowRolloutGate(
        target_workers=target,
        allowed=not blockers,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        evidence_count=len(records),
        eligible_evidence_count=len(eligible),
        worker_ids=worker_ids,
        account_registration_ids=account_ids,
        transport_ids=transport_ids,
        egress_ips=egress_ips,
    )


def normalize_buyer_shadow_endpoint(value: str) -> str:
    """Normalize a path or endpoint name for deterministic drift grouping."""

    endpoint = _required_text(value, "endpoint")
    parsed = urlsplit(endpoint)
    if parsed.scheme or parsed.netloc:
        endpoint = parsed.path or "/"
    elif parsed.path:
        # Paths are commonly captured as ``/projects?page=2``.  Query and
        # fragment values are request-specific and must not split drift keys.
        endpoint = parsed.path
    endpoint = " ".join(unicodedata.normalize("NFKC", endpoint).split())
    if endpoint.startswith("/"):
        endpoint = "/" + "/".join(part for part in endpoint.split("/") if part)
        return endpoint.casefold() or "/"
    return endpoint.casefold()


def _index_projects(
    projects: Iterable[Mapping[str, Any]],
    *,
    side: str,
) -> tuple[dict[str, Mapping[str, Any]], tuple[BuyerShadowDuplicateProject, ...]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for project in projects:
        if not isinstance(project, Mapping):
            raise BuyerShadowInputError(f"{side} project records must be mappings")
        project_id = canonical_buyer_project_id(project)
        grouped.setdefault(project_id, []).append(project)
    selected: dict[str, Mapping[str, Any]] = {}
    duplicates: list[BuyerShadowDuplicateProject] = []
    for project_id, records in grouped.items():
        if len(records) > 1:
            duplicates.append(BuyerShadowDuplicateProject(side=side, project_id=project_id, count=len(records)))
        selected[project_id] = min(records, key=_stable_mapping_key)
    return selected, tuple(sorted(duplicates, key=lambda item: item.project_id))


def _compare_field(
    field: str,
    *,
    matched_ids: tuple[str, ...],
    legacy_by_id: Mapping[str, Mapping[str, Any]],
    new_by_id: Mapping[str, Mapping[str, Any]],
) -> BuyerShadowFieldParity:
    matched: list[str] = []
    missing_legacy: list[str] = []
    missing_new: list[str] = []
    missing_both: list[str] = []
    mismatches: list[BuyerShadowFieldMismatch] = []
    for project_id in matched_ids:
        legacy_value = _project_field_value(legacy_by_id[project_id], field)
        new_value = _project_field_value(new_by_id[project_id], field)
        if legacy_value is _MISSING and new_value is _MISSING:
            missing_both.append(project_id)
        elif legacy_value is _MISSING:
            missing_legacy.append(project_id)
        elif new_value is _MISSING:
            missing_new.append(project_id)
        elif legacy_value == new_value:
            matched.append(project_id)
        else:
            mismatches.append(
                BuyerShadowFieldMismatch(
                    project_id=project_id,
                    field=field,
                    legacy_value=legacy_value,
                    new_value=new_value,
                )
            )
    return BuyerShadowFieldParity(
        field=field,
        matched_project_ids=tuple(matched),
        missing_in_legacy_project_ids=tuple(missing_legacy),
        missing_in_new_project_ids=tuple(missing_new),
        missing_in_both_project_ids=tuple(missing_both),
        mismatches=tuple(mismatches),
    )


def _project_sections(project: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(project, Mapping):
        raise BuyerShadowInputError("project must be a mapping")
    sections: list[Mapping[str, Any]] = [project]
    for name in ("canonical", "observation", "project"):
        nested = project.get(name)
        if isinstance(nested, Mapping):
            sections.append(nested)
    return tuple(sections)


def _first_present(sections: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Any:
    for section in sections:
        for key in keys:
            value = section.get(key, _MISSING)
            if not _is_missing(value):
                return value
    return None


_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("title", "latest_title", "project_title"),
    "description": ("description", "latest_description", "project_description"),
    "budget_min": ("budget_min", "price_from", "price", "price_limit"),
    "budget_max": ("budget_max", "price_to", "possible_price_limit", "price_limit"),
    "offers": ("offers", "offers_count", "kwork_count"),
    "views": ("views", "views_dirty", "view_count"),
    "status": ("status", "latest_status", "remote_status"),
    "category_id": ("category_id", "latest_category_id", "categoryId"),
    "buyer_remote_user_id": ("buyer_remote_user_id", "buyer_id", "user_id"),
    "buyer_username": ("buyer_username", "username", "buyer_login"),
    "canonical_url": ("canonical_url", "url", "project_url"),
    "remote_updated_at": ("remote_updated_at", "latest_remote_updated_at", "updated_at", "date_updated"),
}
_NUMERIC_FIELDS = frozenset(
    {"budget_min", "budget_max", "offers", "views", "category_id", "buyer_remote_user_id"}
)
_CASEFOLDED_FIELDS = frozenset({"status", "buyer_username"})


def _project_field_value(project: Mapping[str, Any], field: str) -> Any:
    aliases = _FIELD_ALIASES.get(field, (field,))
    value = _first_present(_project_sections(project), aliases)
    if _is_missing(value):
        return _MISSING
    return _normalize_field_value(field, value)


def _normalize_field_value(field: str, value: Any) -> Any:
    if field in _NUMERIC_FIELDS:
        return _normalized_number(value)
    if field == "canonical_url":
        return _normalized_url(value)
    if isinstance(value, str):
        text = " ".join(unicodedata.normalize("NFKC", value).split())
        return text.casefold() if field in _CASEFOLDED_FIELDS else text
    if isinstance(value, Mapping):
        return tuple((str(key), _normalize_field_value("", item)) for key, item in sorted(value.items(), key=lambda item: str(item[0])))
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_field_value("", item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _normalized_number(value: Any) -> str | int | bool:
    if isinstance(value, bool):
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _normalize_field_value("", value)
    if not number.is_finite():
        return str(value)
    normalized = number.normalize()
    value_text = format(normalized, "f")
    if "." in value_text:
        value_text = value_text.rstrip("0").rstrip(".")
    return value_text or "0"


def _normalized_url(value: Any) -> str:
    text = _required_text(value, "canonical_url")
    parsed = urlsplit(text)
    if not parsed.scheme and not parsed.netloc:
        return text.rstrip("/")
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def _coerce_endpoint_samples(
    values: Iterable[BuyerShadowEndpointSample | Mapping[str, Any]] | Mapping[str, Any],
) -> tuple[BuyerShadowEndpointSample, ...]:
    if isinstance(values, Mapping):
        if _looks_like_endpoint_sample(values):
            raw_samples: Iterable[Any] = (values,)
        else:
            expanded: list[Any] = []
            for endpoint, payload in values.items():
                if isinstance(payload, Mapping):
                    expanded.append({"endpoint": endpoint, **payload})
                elif isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
                    for item in payload:
                        if not isinstance(item, Mapping):
                            raise BuyerShadowInputError("endpoint samples must be mappings")
                        expanded.append({"endpoint": endpoint, **item})
                else:
                    expanded.append({"endpoint": endpoint, "result_count": payload})
            raw_samples = expanded
    else:
        raw_samples = values
    samples: list[BuyerShadowEndpointSample] = []
    for value in raw_samples:
        if isinstance(value, BuyerShadowEndpointSample):
            samples.append(value)
        elif isinstance(value, Mapping):
            samples.append(
                BuyerShadowEndpointSample(
                    endpoint=_first_mapping_value(value, ("endpoint", "path", "name"), name="endpoint"),
                    status_code=_first_mapping_value(value, ("status_code", "status", "http_status"), optional=True),
                    result_count=_first_mapping_value(
                        value,
                        ("result_count", "project_count", "item_count", "items_count"),
                        optional=True,
                    ),
                    error=_first_mapping_value(value, ("error", "error_class", "exception"), optional=True),
                )
            )
        else:
            raise BuyerShadowInputError("endpoint samples must be BuyerShadowEndpointSample values or mappings")
    return tuple(samples)


def _looks_like_endpoint_sample(value: Mapping[str, Any]) -> bool:
    return any(name in value for name in ("endpoint", "path", "name"))


def _first_mapping_value(value: Mapping[str, Any], keys: Sequence[str], *, name: str = "value", optional: bool = False) -> Any:
    for key in keys:
        item = value.get(key, _MISSING)
        if not _is_missing(item):
            return item
    if optional:
        return None
    raise BuyerShadowInputError(f"endpoint sample requires {name}")


def _group_endpoint_samples(
    samples: Iterable[BuyerShadowEndpointSample],
    *,
    aliases: Mapping[str, str],
) -> dict[str, tuple[BuyerShadowEndpointSample, ...]]:
    grouped: dict[str, list[BuyerShadowEndpointSample]] = {}
    for sample in samples:
        endpoint = aliases.get(sample.endpoint, sample.endpoint)
        grouped.setdefault(endpoint, []).append(sample)
    return {
        endpoint: tuple(sorted(values, key=lambda item: (item.status_code or -1, item.result_count or -1, item.error or "")))
        for endpoint, values in grouped.items()
    }


def _normalize_endpoint_aliases(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("endpoint_aliases must be a mapping")
    return {normalize_buyer_shadow_endpoint(key): normalize_buyer_shadow_endpoint(item) for key, item in value.items()}


def _status_counts(samples: Iterable[BuyerShadowEndpointSample]) -> tuple[tuple[int, int], ...]:
    counts = Counter(sample.status_code for sample in samples if sample.status_code is not None)
    return tuple(sorted((int(status), count) for status, count in counts.items()))


def _coerce_identity_evidence(value: BuyerWorkerIdentityEvidence | Mapping[str, Any]) -> BuyerWorkerIdentityEvidence:
    if isinstance(value, BuyerWorkerIdentityEvidence):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("identity evidence must be a BuyerWorkerIdentityEvidence or mapping")
    return BuyerWorkerIdentityEvidence(
        worker_id=_first_mapping_value(value, ("worker_id", "worker"), name="worker_id"),
        account_registration_id=_first_mapping_value(
            value,
            ("account_registration_id", "account_id", "account"),
            name="account_registration_id",
        ),
        transport_id=_first_mapping_value(value, ("transport_id", "transport", "slot_id"), name="transport_id"),
        egress_ip=_first_mapping_value(value, ("egress_ip", "ip"), name="egress_ip"),
        healthy=value.get("healthy", True),
        verified_egress=value.get("verified_egress", value.get("egress_verified", True)),
        route_generation=value.get("route_generation"),
    )


def _duplicate_identity_labels(records: Iterable[BuyerWorkerIdentityEvidence]) -> tuple[str, ...]:
    values = tuple(records)
    pairs = (
        ("worker", (item.worker_id for item in values)),
        ("account", (item.account_registration_id for item in values)),
        ("transport", (item.transport_id for item in values)),
        ("egress_ip", (item.normalized_egress_ip for item in values)),
    )
    result: list[str] = []
    for label, identifiers in pairs:
        normalized = tuple(identifier for identifier in identifiers if identifier is not None)
        if len(set(normalized)) != len(normalized):
            result.append(label)
    return tuple(result)


def _previous_canary_target(target: int, canary_targets: Sequence[int]) -> int | None:
    try:
        index = tuple(canary_targets).index(target)
    except ValueError:
        return None
    return canary_targets[index - 1] if index else None


def _normalize_canary_targets(values: Iterable[int]) -> tuple[int, ...]:
    targets = tuple(_positive_int(value, "canary_targets") for value in values)
    if targets != tuple(sorted(set(targets))):
        raise ValueError("canary_targets must be strictly increasing and unique")
    if not targets or any(value not in CANARY_WORKER_TARGETS for value in targets):
        raise ValueError("canary_targets must be selected from 2, 5, 10, 20, 30")
    return targets


def _normalize_fields(values: Sequence[str]) -> tuple[str, ...]:
    fields = tuple(_required_field_name(value) for value in values)
    if not fields:
        raise ValueError("at least one comparison field is required")
    if len(set(fields)) != len(fields):
        raise ValueError("comparison fields must be unique")
    return fields


def _required_field_name(value: str) -> str:
    return _required_text(value, "field").casefold()


def _stable_mapping_key(value: Mapping[str, Any]) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        value = str(value) if value is not None and not isinstance(value, bool) else ""
    result = value.strip()
    if not result:
        raise BuyerShadowInputError(f"{name} cannot be blank")
    return result


def _optional_text(value: Any) -> str | None:
    if _is_missing(value):
        return None
    result = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return result or None


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if _is_missing(value):
        return None
    return _nonnegative_int(value, name)


def _optional_status_code(value: Any) -> int | None:
    if _is_missing(value):
        return None
    status = _positive_int(value, "status_code")
    if not 100 <= status <= 599:
        raise BuyerShadowInputError("status_code must be between 100 and 599")
    return status


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BuyerShadowInputError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerShadowInputError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise BuyerShadowInputError(f"{name} must be a positive integer")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BuyerShadowInputError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerShadowInputError(f"{name} must be a non-negative integer") from exc
    if result < 0:
        raise BuyerShadowInputError(f"{name} must be a non-negative integer")
    return result


_MISSING = object()


def _is_missing(value: Any) -> bool:
    return value is _MISSING or value is None or (isinstance(value, str) and not value.strip())


__all__ = [
    "CANARY_WORKER_TARGETS",
    "DEFAULT_BUYER_SHADOW_FIELDS",
    "DEFAULT_REQUIRED_BUYER_SHADOW_FIELDS",
    "BuyerShadowAcceptanceSummary",
    "BuyerShadowComparison",
    "BuyerShadowDuplicateProject",
    "BuyerShadowEndpointDrift",
    "BuyerShadowEndpointSample",
    "BuyerShadowFieldMismatch",
    "BuyerShadowFieldParity",
    "BuyerShadowInputError",
    "BuyerShadowRolloutConfig",
    "BuyerShadowRolloutGate",
    "BuyerWorkerIdentityEvidence",
    "canonical_buyer_project_id",
    "compare_buyer_endpoint_drift",
    "compare_buyer_shadow",
    "evaluate_buyer_shadow_rollout_gate",
    "normalize_buyer_shadow_endpoint",
    "summarize_buyer_shadow_acceptance",
]
