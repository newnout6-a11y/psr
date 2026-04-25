"""Dashboard metrics endpoints — thin wrappers around src/dashboard/queries."""
from __future__ import annotations

from fastapi import APIRouter, Query

from src.dashboard import queries as q

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def overview(days: int = Query(14, ge=1, le=90)):
    return q.overview_metrics(days)


@router.get("/timeline")
def timeline(days: int = Query(14, ge=1, le=90)):
    df = q.decisions_timeline(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/scoring")
def scoring(days: int = Query(14, ge=1, le=90)):
    df = q.scoring_quality(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/status-breakdown")
def status_breakdown(days: int = Query(14, ge=1, le=90)):
    df = q.candidate_status_breakdown(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/query-memory")
def query_memory(limit: int = Query(20, ge=1, le=100)):
    df = q.query_memory_top(limit)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/parse-stats")
def parse_stats(days: int = Query(14, ge=1, le=90)):
    df = q.parse_stats(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/send-stats")
def send_stats(days: int = Query(14, ge=1, le=90)):
    df = q.send_stats(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/breaker")
def breaker():
    df = q.breaker_snapshot()
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/generation-stats")
def generation_stats(days: int = Query(14, ge=1, le=90)):
    df = q.provider_generation_stats(days)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/activity")
def activity():
    return q.last_activity()


@router.get("/proposals-by-hour")
def proposals_by_hour():
    df = q.proposals_by_hour()
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/errors")
def errors(limit: int = Query(50, ge=1, le=200)):
    df = q.errors_recent(limit)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/runtime-state")
def runtime_state():
    return q.runtime_state_snapshot()
