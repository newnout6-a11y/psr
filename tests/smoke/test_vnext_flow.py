"""Smoke-тесты для vNext runtime/brain.

Запуск:
    python tests/smoke/test_vnext_flow.py
"""

import asyncio
import gc
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.action.decision_policy import CandidateDecisionContext, DecisionPolicy, ExecutionMode
from src.action.proposal_db import ProposalDB
from src.filter.ai_scorer import AIRelevanceScorer
from src.parsers.base_parser import ProjectItem


def _project(
    *,
    project_id: str,
    title: str,
    description: str,
    platform: str = "kwork",
    budget: float = 5000,
    skills: list[str] | None = None,
    offers_count: int = 0,
) -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title=title,
        description=description,
        budget=budget,
        currency="RUB",
        skills=skills or [],
        url=f"https://example.com/{project_id}",
        platform=platform,
        created_at="2026-04-21 12:00:00",
        offers_count=offers_count,
        client_hired_percent=60,
        search_query="python",
    )


def test_decision_policy_auto_ready():
    policy = DecisionPolicy(auto_send_platforms={"kwork"})
    ctx = CandidateDecisionContext(
        platform="kwork",
        ai_score=9,
        ai_score_source="llm",
        vet_score=85,
        offers_count=4,
        budget=7000,
        red_flags=[],
        valid_proposal=True,
        auto_platform=True,
        platform_paused=False,
        dry_run=False,
    )
    decision = policy.evaluate(ExecutionMode.SEMI_AUTO.value, ctx)
    assert decision.status == "auto_ready"
    assert decision.auto_send is True
    assert decision.auto_eligible is True


def test_decision_policy_blocks_fallback_auto_send():
    policy = DecisionPolicy(auto_send_platforms={"kwork"})
    ctx = CandidateDecisionContext(
        platform="kwork",
        ai_score=9,
        ai_score_source="fallback_scored",
        vet_score=85,
        offers_count=4,
        budget=7000,
        red_flags=[],
        valid_proposal=True,
        auto_platform=True,
        platform_paused=False,
        dry_run=False,
    )
    decision = policy.evaluate(ExecutionMode.SEMI_AUTO.value, ctx)
    assert decision.status == "queued"
    assert decision.auto_send is False


def test_query_memory_and_runtime_state():
    db_path = Path(tempfile.gettempdir()) / "psr_vnext_smoke.db"
    if db_path.exists():
        db_path.unlink()

    db = ProposalDB(db_path=str(db_path))
    db.set_runtime_mode("manual")
    assert db.get_runtime_mode() == "manual"

    db.record_query_run("kwork", "python", 10)
    db.record_query_signal("kwork", "python", "shortlisted", 3)
    db.record_query_signal("kwork", "python", "sent", 2)
    db.record_query_signal("kwork", "python", "responded", 1)
    db.mark_query_preferred("kwork", "python")

    rows = db.get_query_memory("kwork", limit=5)
    assert rows
    top = rows[0]
    assert top["query_text"] == "python"
    assert top["responded_count"] == 1
    assert top["user_preferred"] == 1

    del db
    gc.collect()
    if db_path.exists():
        db_path.unlink()


def test_ai_scorer_heuristic_and_fallback():
    scorer = AIRelevanceScorer()
    strong = _project(
        project_id="strong",
        title="Telegram бот для уведомлений",
        description="Нужен Python / aiogram бот с webhook и API интеграцией.",
        skills=["Python", "aiogram", "API"],
    )
    weak = _project(
        project_id="weak",
        title="Senior Data Scientist full-time",
        description="Штатная вакансия, офис, ML research, full-time, PhD приветствуется.",
        platform="hh_ru",
        budget=250000,
    )
    borderline = _project(
        project_id="border",
        title="Python backend разработчик",
        description="Нужен разработчик помочь с внутренним сервисом и интеграцией API.",
        skills=["Python"],
        offers_count=8,
    )

    async def run():
        strong_results = await scorer.evaluate_projects([strong])
        assert strong_results[0].passed is True
        assert strong_results[0].source == "heuristic"

        weak_results = await scorer.evaluate_projects([weak])
        assert weak_results[0].passed is False
        assert weak_results[0].source == "heuristic"

        fake_router = type("FakeRouter", (), {"generate": AsyncMock(side_effect=RuntimeError("llm down"))})()
        with patch("src.brain.llm_router.get_llm_router", return_value=fake_router), patch.object(
            AIRelevanceScorer, "_needs_llm_review", return_value=True
        ):
            fallback_results = await scorer.evaluate_projects([borderline])
            assert fallback_results[0].fallback_scored is True
            assert fallback_results[0].source == "fallback"

    asyncio.run(run())


if __name__ == "__main__":
    test_decision_policy_auto_ready()
    print("[ok] decision policy auto_ready")
    test_decision_policy_blocks_fallback_auto_send()
    print("[ok] decision policy blocks fallback auto-send")
    test_query_memory_and_runtime_state()
    print("[ok] proposal db runtime/query memory")
    test_ai_scorer_heuristic_and_fallback()
    print("[ok] ai scorer heuristic + fallback")
    print("\nВсе проверки vNext прошли.")
