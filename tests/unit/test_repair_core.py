"""Regression tests for the Kwork repair core."""

from pathlib import Path

from src.action.decision_policy import CandidateDecisionContext, DecisionPolicy, ExecutionMode
from src.action.proposal_db import ProposalDB
from src.action.proposal_sender import ProposalSender
from src.dashboard import queries as dashboard_queries
from src.parsers.base_parser import ProjectItem


def _project(project_id: str = "p1") -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title="Telegram bot task",
        description="Нужно сделать Telegram бота",
        budget=1000,
        currency="RUB",
        skills=["telegram", "python"],
        url=f"https://kwork.ru/projects/{project_id}",
        platform="kwork",
        created_at="2026-06-08 12:00:00",
        offers_count=7,
        client_hired_percent=43,
    )


def test_candidate_schema_persists_kwork_market_fields(tmp_path: Path):
    db = ProposalDB(db_path=str(tmp_path / "proposals.db"))
    candidate_id = db.upsert_candidate(_project(), stage="parsed", status="queued", priority=42)

    candidate = db.get_candidate(candidate_id)

    assert candidate is not None
    assert candidate["offers_count"] == 7
    assert candidate["client_hired_percent"] == 43
    assert candidate["priority"] == 42


def test_queue_snapshot_reads_queued_candidates_with_market_fields(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "proposals.db"
    db = ProposalDB(db_path=str(db_path))
    db.upsert_candidate(
        _project(),
        stage="vetted",
        status="queued",
        priority=42,
        ai_score=8,
        vet_score=70,
        proposal_text="Ready proposal",
    )

    monkeypatch.setattr(dashboard_queries, "PROPOSALS_DB", str(db_path))
    monkeypatch.setattr(dashboard_queries, "_ensure_proposals_schema", lambda: None)

    rows = dashboard_queries.queue_snapshot(limit=10)

    assert not rows.empty
    row = rows.iloc[0].to_dict()
    assert row["offers_count"] == 7
    assert row["client_hired_percent"] == 43
    assert row["proposal_text"] == "Ready proposal"
    assert row["priority"] == 42


def test_high_risk_is_not_sorted_above_low_risk_by_priority():
    policy = DecisionPolicy(auto_send_platforms={"kwork"})
    low_risk = CandidateDecisionContext(
        platform="kwork",
        ai_score=8,
        ai_score_source="llm",
        vet_score=80,
        offers_count=0,
        budget=500,
        red_flags=[],
    )
    high_risk = CandidateDecisionContext(
        platform="kwork",
        ai_score=8,
        ai_score_source="fallback_scored",
        vet_score=20,
        offers_count=100,
        budget=500,
        red_flags=["manual review required"],
    )

    low_decision = policy.evaluate(ExecutionMode.MANUAL.value, low_risk)
    high_decision = policy.evaluate(ExecutionMode.MANUAL.value, high_risk)

    assert low_decision.risk_level == "low"
    assert high_decision.risk_level == "high"
    assert low_decision.priority > high_decision.priority


def test_semiauto_ready_requires_explicit_approve():
    policy = DecisionPolicy(auto_send_platforms={"kwork"})
    ctx = CandidateDecisionContext(
        platform="kwork",
        ai_score=9,
        ai_score_source="llm",
        vet_score=85,
        offers_count=4,
        budget=7000,
        red_flags=[],
    )

    decision = policy.evaluate(ExecutionMode.SEMI_AUTO.value, ctx)

    assert decision.status == "auto_ready"
    assert decision.auto_eligible is True
    assert decision.auto_send is False


def test_kwork_web_submit_accepts_successful_2xx_without_strict_success_flag():
    assert ProposalSender._kwork_web_submit_succeeded({"status": 200, "json": {"id": 123}}) is True
    assert ProposalSender._kwork_web_submit_succeeded({"status": 201, "json": {"status": "ok"}}) is True
    assert ProposalSender._kwork_web_submit_succeeded({"status": 200, "json": {"success": False}}) is False
    assert ProposalSender._kwork_web_submit_succeeded({"status": 500, "json": None}) is False
