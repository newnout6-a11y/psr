"""Pure bounded, JSON-safe evidence packets for sample-based AI analysis.

The packet keeps deterministic aggregate metrics separate from a small
stratified evidence sample. It intentionally excludes raw request/response
material and does not perform LLM, network, database, clock, or worker calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
import json
import math
from typing import Any


_MAX_EVIDENCE_ITEMS = 100
_MAX_METRIC_ITEMS = 1_000
_MAX_STRING_CHARS = 1_000
_MAX_DEPTH = 8
_FORBIDDEN_KEYS = {
    "authorization",
    "body",
    "cookie",
    "cookies",
    "html",
    "password",
    "payload",
    "raw",
    "raw_payload",
    "raw_response",
    "raw_response_ref",
    "request",
    "response_body",
    "token",
}


class AiEvidenceError(ValueError):
    """Raised when evidence inputs cannot form a bounded deterministic packet."""


@dataclass(frozen=True, slots=True)
class AiEvidencePolicy:
    """Hard limits for one AI evidence packet."""

    max_evidence_items: int = 40
    hard_max_evidence_items: int = _MAX_EVIDENCE_ITEMS
    max_metric_items: int = 200
    max_string_chars: int = 300
    max_depth: int = 5

    def __post_init__(self) -> None:
        for field_name in (
            "max_evidence_items",
            "hard_max_evidence_items",
            "max_metric_items",
            "max_string_chars",
            "max_depth",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AiEvidenceError(f"{field_name} must be an integer")
        if self.hard_max_evidence_items <= 0 or self.hard_max_evidence_items > _MAX_EVIDENCE_ITEMS:
            raise AiEvidenceError(
                f"hard_max_evidence_items must be between 1 and {_MAX_EVIDENCE_ITEMS}"
            )
        if self.max_evidence_items <= 0 or self.max_evidence_items > self.hard_max_evidence_items:
            raise AiEvidenceError("max_evidence_items must be between 1 and hard_max_evidence_items")
        if self.max_metric_items <= 0 or self.max_metric_items > _MAX_METRIC_ITEMS:
            raise AiEvidenceError(f"max_metric_items must be between 1 and {_MAX_METRIC_ITEMS}")
        if self.max_string_chars <= 0 or self.max_string_chars > _MAX_STRING_CHARS:
            raise AiEvidenceError(f"max_string_chars must be between 1 and {_MAX_STRING_CHARS}")
        if self.max_depth <= 0 or self.max_depth > _MAX_DEPTH:
            raise AiEvidenceError(f"max_depth must be between 1 and {_MAX_DEPTH}")


def build_ai_evidence_packet(
    listings: Iterable[Mapping[str, Any]],
    aggregate_metrics: Mapping[str, Any],
    enrichment_candidates: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    policy: AiEvidencePolicy | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic, bounded evidence packet for hypothesis prompts.

    ``aggregate_metrics`` describes deterministic observed-data aggregates.
    ``enrichment_candidates`` accepts an enrichment manifest with ``candidates``
    or an iterable of candidate mappings. Only compact allowlisted evidence
    fields are emitted, never raw listing mappings or arbitrary candidate data.
    """

    effective_policy = policy or AiEvidencePolicy()
    if not isinstance(effective_policy, AiEvidencePolicy):
        raise TypeError("policy must be an AiEvidencePolicy")
    evidence_limit = _resolve_limit(effective_policy, max_items)
    listing_index = _listing_index(listings)
    metrics = _safe_json_value(_require_mapping(aggregate_metrics, field="aggregate_metrics"), effective_policy)
    candidates, enrichment_summary = _candidate_rows(enrichment_candidates)
    evidence_rows = _evidence_rows(candidates, listing_index, limit=evidence_limit, policy=effective_policy)
    observed_listing_count = len(listing_index)
    candidate_count = len(candidates)
    packet = {
        "schema_version": 1,
        "sample_based": True,
        "coverage_label": "Stratified evidence sample from observed listings; not market coverage.",
        "aggregate_metrics": metrics,
        "evidence_sample": {
            "observed_listing_count": observed_listing_count,
            "enrichment_candidate_count": candidate_count,
            "included_evidence_count": len(evidence_rows),
            "omitted_evidence_candidate_count": max(candidate_count - len(evidence_rows), 0),
            "evidence_limit": evidence_limit,
            "sample_is_bounded": candidate_count > len(evidence_rows),
            "enrichment_manifest": enrichment_summary,
            "records": evidence_rows,
        },
        "hypothesis_inputs": {
            "sample_based": True,
            "claims_boundary": (
                "Use the evidence records for sample-based hypotheses only; "
                "do not claim market-wide coverage or causal certainty."
            ),
            "aggregate_metric_scope": (
                "Aggregate metrics are deterministic summaries of observed listings; "
                "reported aggregate scope volume is not observed-card coverage."
            ),
            "observed_listing_count": observed_listing_count,
            "included_evidence_count": len(evidence_rows),
            "enrichment_candidate_count": candidate_count,
            "evidence_dimensions": ["shard", "price_band", "seller_frequency"],
        },
    }
    # This also validates the public JSON-safe contract before callers use it.
    json.dumps(packet, ensure_ascii=True, sort_keys=True, allow_nan=False)
    return packet


def build_stratified_ai_evidence(
    listings: Iterable[Mapping[str, Any]],
    aggregate_metrics: Mapping[str, Any],
    enrichment_candidates: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    policy: AiEvidencePolicy | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Compatibility name for :func:`build_ai_evidence_packet`."""

    return build_ai_evidence_packet(
        listings,
        aggregate_metrics,
        enrichment_candidates,
        policy=policy,
        max_items=max_items,
    )


def _resolve_limit(policy: AiEvidencePolicy, max_items: int | None) -> int:
    if max_items is None:
        return policy.max_evidence_items
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise AiEvidenceError("max_items must be an integer")
    if max_items <= 0 or max_items > policy.hard_max_evidence_items:
        raise AiEvidenceError("max_items must be between 1 and policy.hard_max_evidence_items")
    return max_items


def _listing_index(listings: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, listing in enumerate(listings):
        if not isinstance(listing, Mapping):
            raise TypeError(f"listing at index {index} must be a mapping")
        identity = _listing_identity(listing)
        if identity in result:
            raise AiEvidenceError(f"duplicate listing identity: {identity}")
        result[identity] = listing
    return result


def _candidate_rows(
    candidates: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, Any]]:
    if isinstance(candidates, Mapping):
        source_rows = candidates.get("candidates", ())
        summary = {
            "raw_listing_count": _nonnegative_int(candidates.get("raw_listing_count")),
            "selected_count": _nonnegative_int(candidates.get("selected_count")),
            "selection_is_bounded": bool(candidates.get("selection_is_bounded", False)),
            "strata": _safe_manifest_strata(candidates.get("strata")),
        }
    else:
        source_rows = candidates
        summary = {"raw_listing_count": None, "selected_count": None, "selection_is_bounded": None, "strata": {}}
    if not isinstance(source_rows, Iterable) or isinstance(source_rows, (str, bytes)):
        raise TypeError("enrichment candidates must be an iterable of mappings")
    by_identity: dict[str, Mapping[str, Any]] = {}
    for index, candidate in enumerate(source_rows):
        if not isinstance(candidate, Mapping):
            raise TypeError(f"enrichment candidate at index {index} must be a mapping")
        identity = _candidate_identity(candidate)
        if identity in by_identity:
            raise AiEvidenceError(f"duplicate enrichment candidate identity: {identity}")
        by_identity[identity] = candidate
    rows = tuple(by_identity[identity] for identity in sorted(by_identity))
    if summary["selected_count"] is None:
        summary["selected_count"] = len(rows)
    return rows, summary


def _evidence_rows(
    candidates: tuple[Mapping[str, Any], ...],
    listing_index: Mapping[str, Mapping[str, Any]],
    *,
    limit: int,
    policy: AiEvidencePolicy,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        identity = _candidate_identity(candidate)
        listing = listing_index.get(identity)
        evidence = candidate.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        listing_id = _first_value(candidate.get("listing_id"), listing.get("listing_id") if listing else None)
        listing_key = _text(_first_value(candidate.get("listing_key"), listing.get("listing_key") if listing else None))
        title = _bounded_text(_first_value(listing.get("title") if listing else None, listing.get("name") if listing else None), policy)
        price = _safe_scalar(_first_value(listing.get("price") if listing else None, evidence.get("price")), policy)
        seller_key = _bounded_text(
            _first_value(
                listing.get("seller_key") if listing else None,
                evidence.get("seller_key"),
                listing.get("seller_id") if listing else None,
            ),
            policy,
        )
        shard_id = _bounded_text(
            _first_value(listing.get("shard_id") if listing else None, evidence.get("shard_id")), policy
        )
        rows.append(
            {
                "listing_identity": identity,
                "listing_id": _safe_scalar(listing_id, policy),
                "listing_key": listing_key,
                "title": title,
                "price": price,
                "seller_key": seller_key,
                "shard_id": shard_id,
                "selection_reasons": _selection_reasons(candidate.get("selection_reasons"), policy),
                "strata": {
                    "price_band": _bounded_text(evidence.get("price_band"), policy),
                    "seller_frequency_band": _bounded_text(evidence.get("seller_frequency_band"), policy),
                    "seller_observed_count": _nonnegative_int(evidence.get("seller_observed_count")),
                },
            }
        )
    return rows


def _safe_manifest_strata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "input_shard_counts",
        "input_price_band_counts",
        "input_seller_frequency_counts",
        "selected_shard_counts",
        "selected_price_band_counts",
        "selected_seller_frequency_counts",
    }
    return {
        key: _count_mapping(value[key])
        for key in sorted(allowed)
        if key in value and isinstance(value[key], Mapping)
    }


def _count_mapping(value: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key in sorted(value, key=lambda item: str(item)):
        count = _nonnegative_int(value[key])
        if count is not None:
            result[str(key)] = count
    return result


def _safe_json_value(value: Any, policy: AiEvidencePolicy, *, depth: int = 0) -> Any:
    if depth >= policy.max_depth:
        return "<truncated-depth>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _truncate(value, policy.max_string_chars)
    if isinstance(value, Mapping):
        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            key_text = str(key)
            if _is_forbidden_key(key_text):
                continue
            items.append((key_text, item))
        items.sort(key=lambda item: item[0])
        output = {
            key: _safe_json_value(item, policy, depth=depth + 1)
            for key, item in items[: policy.max_metric_items]
        }
        if len(items) > policy.max_metric_items:
            output["_truncated_item_count"] = len(items) - policy.max_metric_items
        return output
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        items = [_safe_json_value(item, policy, depth=depth + 1) for item in value]
        items.sort(key=_canonical_sort_key)
        if len(items) > policy.max_metric_items:
            return [*items[: policy.max_metric_items], {"_truncated_item_count": len(items) - policy.max_metric_items}]
        return items
    return _truncate(str(value), policy.max_string_chars)


def _candidate_identity(candidate: Mapping[str, Any]) -> str:
    direct = _text(candidate.get("listing_identity"))
    if direct is not None:
        return direct
    return _listing_identity(candidate)


def _listing_identity(listing: Mapping[str, Any]) -> str:
    listing_id = _text(listing.get("listing_id"))
    if listing_id is not None:
        return f"listing_id:{listing_id}"
    listing_key = _text(listing.get("listing_key"))
    if listing_key is not None:
        return f"listing_key:{listing_key}"
    for field in ("id", "PID", "share_url", "url"):
        value = _text(listing.get(field))
        if value is not None:
            return f"{field}:{value}"
    raise AiEvidenceError("listing requires a stable listing identity")


def _selection_reasons(value: object, policy: AiEvidencePolicy) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    reasons = {_bounded_text(reason, policy) for reason in value}
    return sorted(reason for reason in reasons if reason is not None)


def _bounded_text(value: object, policy: AiEvidencePolicy) -> str | None:
    text = _text(value)
    return _truncate(text, policy.max_string_chars) if text is not None else None


def _safe_scalar(value: object, policy: AiEvidencePolicy) -> Any:
    return _safe_json_value(value, policy)


def _first_value(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, (int, Decimal)):
        return str(value)
    return None


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _is_forbidden_key(key: str) -> bool:
    normalized = key.strip().lower()
    return normalized in _FORBIDDEN_KEYS or normalized.startswith("raw_")


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _require_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return value


__all__ = [
    "AiEvidenceError",
    "AiEvidencePolicy",
    "build_ai_evidence_packet",
    "build_stratified_ai_evidence",
]
