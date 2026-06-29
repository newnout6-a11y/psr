"""Execution mode и policy выбора auto/manual/reject."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class ExecutionMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    SEMI_AUTO = "semi_auto"
    PAUSED = "paused"


@dataclass
class CandidateDecisionContext:
    platform: str
    ai_score: int
    ai_score_source: str
    vet_score: int
    offers_count: int
    budget: float | None
    red_flags: list[str] = field(default_factory=list)
    valid_proposal: bool = True
    auto_platform: bool = True
    platform_paused: bool = False
    dry_run: bool = False


@dataclass
class CandidateDecision:
    status: str
    auto_send: bool
    reason: str
    risk_level: str
    priority: int
    auto_eligible: bool


class DecisionPolicy:
    """Правила полуавтономного режима."""

    def __init__(self, auto_send_platforms: set[str] | None = None):
        self.auto_send_platforms = auto_send_platforms or {"kwork", "freelance_ru"}

    @property
    def auto_send_score_min(self) -> int:
        return int(os.getenv("AUTO_SEND_SCORE_MIN", "8"))

    @property
    def auto_send_vet_min(self) -> int:
        return int(os.getenv("AUTO_SEND_VET_MIN", "60"))

    @property
    def auto_send_max_offers(self) -> int:
        return int(os.getenv("AUTO_SEND_MAX_OFFERS", "20"))

    @property
    def auto_send_max_budget(self) -> float:
        return float(os.getenv("AUTO_SEND_MAX_BUDGET", "10000"))

    def evaluate(
        self,
        mode: str,
        ctx: CandidateDecisionContext,
    ) -> CandidateDecision:
        normalized_mode = (mode or ExecutionMode.SEMI_AUTO.value).strip().lower()
        if normalized_mode not in {m.value for m in ExecutionMode}:
            normalized_mode = ExecutionMode.SEMI_AUTO.value

        risk_level = self._risk_level(ctx)
        priority = self._priority(ctx, risk_level)

        if not ctx.valid_proposal:
            return CandidateDecision("error", False, "proposal text is invalid", "high", 0, False)

        if ctx.ai_score <= 2 or ctx.vet_score <= 10:
            return CandidateDecision("skipped", False, "rejected by hard risk floor", "high", 0, False)

        if normalized_mode == ExecutionMode.PAUSED.value:
            return CandidateDecision("queued", False, "system paused", "high", priority, False)

        if ctx.platform_paused:
            return CandidateDecision("queued", False, f"{ctx.platform} paused", "high", priority, False)

        if normalized_mode == ExecutionMode.MANUAL.value:
            return CandidateDecision("queued", False, "manual mode", risk_level, priority, False)

        auto_eligible = self._is_auto_eligible(ctx)
        if auto_eligible:
            return CandidateDecision("auto_ready", False, "ready for explicit approval", risk_level, priority, True)

        if ctx.ai_score < max(self.auto_send_score_min - 2, 1) and ctx.vet_score < max(self.auto_send_vet_min - 10, 1):
            return CandidateDecision("skipped", False, "low confidence after scoring", risk_level, 0, False)

        return CandidateDecision("queued", False, "needs human review", risk_level, priority, False)

    def _is_auto_eligible(self, ctx: CandidateDecisionContext) -> bool:
        if ctx.dry_run:
            return False
        if ctx.platform not in self.auto_send_platforms or not ctx.auto_platform:
            return False
        if ctx.ai_score_source == "fallback_scored":
            return False
        if ctx.ai_score < self.auto_send_score_min:
            return False
        if ctx.vet_score < self.auto_send_vet_min:
            return False
        if ctx.red_flags:
            return False
        if ctx.offers_count > self.auto_send_max_offers:
            return False
        if ctx.budget and ctx.budget > self.auto_send_max_budget:
            return False
        return True

    def _risk_level(self, ctx: CandidateDecisionContext) -> str:
        risk = 0
        if ctx.ai_score_source == "fallback_scored":
            risk += 2
        if ctx.red_flags:
            risk += min(len(ctx.red_flags), 3)
        if ctx.offers_count > self.auto_send_max_offers:
            risk += 2
        if ctx.budget and ctx.budget > self.auto_send_max_budget:
            risk += 1
        if ctx.vet_score < self.auto_send_vet_min:
            risk += 1

        if risk >= 4:
            return "high"
        if risk >= 2:
            return "medium"
        return "low"

    def _priority(self, ctx: CandidateDecisionContext, risk_level: str) -> int:
        """Higher priority means "show this earlier", not "more dangerous"."""
        value = ctx.ai_score * 10 + min(max(ctx.vet_score, 0), 100) // 5

        if ctx.offers_count == 0:
            value += 12
        elif ctx.offers_count <= self.auto_send_max_offers:
            value += 6
        elif ctx.offers_count > self.auto_send_max_offers * 2:
            value -= 12
        else:
            value -= 6

        if risk_level == "high":
            value -= 25
        elif risk_level == "medium":
            value -= 10

        if ctx.ai_score_source == "fallback_scored":
            value -= 10

        return max(0, min(100, int(value)))
