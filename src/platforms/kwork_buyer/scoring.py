"""Deterministic preliminary scoring for durable Buyer Search rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class BuyerScoreProfile:
    """Versioned, explainable weights used before optional AI scoring."""

    profile_id: str = "buyer-default"
    version: str = "1"
    target_budget: float = 25_000.0
    weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "budget": 0.30,
            "competition": 0.18,
            "freshness": 0.16,
            "buyer_history": 0.16,
            "description": 0.12,
            "attachments": 0.08,
        }
    )

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.version.strip():
            raise ValueError("profile_id and version cannot be blank")
        if not math.isfinite(self.target_budget) or self.target_budget <= 0:
            raise ValueError("target_budget must be positive and finite")
        weights = {str(key): float(value) for key, value in self.weights.items()}
        if not weights or any(not math.isfinite(value) or value < 0 for value in weights.values()):
            raise ValueError("weights must contain finite non-negative values")
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("at least one score weight must be positive")
        object.__setattr__(self, "weights", weights)


@dataclass(frozen=True, slots=True)
class BuyerScore:
    """A serializable deterministic score and its complete input evidence."""

    profile_id: str
    profile_version: str
    total: float
    breakdown: Mapping[str, float]
    input_hash: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "total": self.total,
            "breakdown": dict(self.breakdown),
            "input_hash": self.input_hash,
            "rationale": self.rationale,
        }


DEFAULT_BUYER_SCORE_PROFILE = BuyerScoreProfile()


def score_buyer_project(
    project: Mapping[str, Any],
    *,
    profile: BuyerScoreProfile = DEFAULT_BUYER_SCORE_PROFILE,
) -> BuyerScore:
    """Score one normalized buyer project without remote or model calls."""

    if not isinstance(project, Mapping):
        raise TypeError("project must be a mapping")
    if not isinstance(profile, BuyerScoreProfile):
        raise TypeError("profile must be a BuyerScoreProfile")

    normalized = _score_input(project)
    factors = {
        "budget": _budget_score(normalized["budget_max"] or normalized["budget_min"], profile.target_budget),
        "competition": _inverse_score(normalized["offers"], ceiling=30),
        "freshness": _inverse_score(normalized["age_seconds"], ceiling=7 * 24 * 60 * 60),
        "buyer_history": _clamp(normalized["buyer_hired_percent"] / 100),
        "description": _clamp(normalized["description_length"] / 500),
        "attachments": 1.0 if normalized["attachment_count"] > 0 else 0.0,
    }
    weighted = {name: round(100 * factors.get(name, 0.0) * weight, 4) for name, weight in profile.weights.items()}
    total_weight = sum(profile.weights.values())
    total = round(sum(weighted.values()) / total_weight, 2)
    input_hash = _hash_input(normalized, profile)
    positive = [name for name, value in factors.items() if value >= 0.7 and profile.weights.get(name, 0) > 0]
    negative = [name for name, value in factors.items() if value <= 0.3 and profile.weights.get(name, 0) > 0]
    rationale = _rationale(positive, negative)
    return BuyerScore(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        total=total,
        breakdown=weighted,
        input_hash=input_hash,
        rationale=rationale,
    )


def _score_input(project: Mapping[str, Any]) -> dict[str, float | int]:
    description = project.get("description") or project.get("latest_description") or ""
    return {
        "budget_min": _number(project.get("budget_min")),
        "budget_max": _number(project.get("budget_max")),
        "offers": _non_negative_number(project.get("offers") or project.get("offers_count")),
        "age_seconds": _non_negative_number(project.get("age_seconds")),
        "buyer_hired_percent": _bounded_number(project.get("buyer_hired_percent"), upper=100),
        "description_length": len(str(description).strip()),
        "attachment_count": int(_non_negative_number(project.get("attachment_count"))),
    }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _non_negative_number(value: Any) -> float:
    number = _number(value)
    return 0.0 if number is None else max(0.0, number)


def _bounded_number(value: Any, *, upper: float) -> float:
    return min(upper, _non_negative_number(value))


def _budget_score(budget: float | None, target: float) -> float:
    if budget is None or budget <= 0:
        return 0.0
    return _clamp(math.log1p(budget) / math.log1p(target * 2))


def _inverse_score(value: float, *, ceiling: float) -> float:
    return _clamp(1 - min(value, ceiling) / ceiling)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _hash_input(normalized: Mapping[str, float | int], profile: BuyerScoreProfile) -> str:
    payload = {
        "profile_id": profile.profile_id,
        "version": profile.version,
        "target_budget": profile.target_budget,
        "weights": dict(sorted(profile.weights.items())),
        "input": dict(sorted(normalized.items())),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


def _rationale(positive: list[str], negative: list[str]) -> str:
    if not positive and not negative:
        return "Insufficient deterministic signals"
    parts: list[str] = []
    if positive:
        parts.append("strong " + ", ".join(positive))
    if negative:
        parts.append("weak " + ", ".join(negative))
    return "; ".join(parts)


__all__ = ["BuyerScore", "BuyerScoreProfile", "DEFAULT_BUYER_SCORE_PROFILE", "score_buyer_project"]
