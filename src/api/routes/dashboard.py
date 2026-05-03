"""Dashboard metrics endpoints — thin wrappers around src/dashboard/queries."""
from __future__ import annotations

from typing import Any

import pandas as pd
from fastapi import APIRouter, Query

from src.dashboard import queries as q

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → list[dict] с пустым списком вместо None.

    Все Phase 3 endpoints возвращают одну и ту же форму, так что
    вынесли в один helper, чтобы не плодить тернарники в каждом
    декораторе и держать форму ответа консистентной.
    """
    return df.to_dict(orient="records") if not df.empty else []


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


# ---------------------------------------------------------------------------
# Phase 3 — conversion / feedback loop endpoints
# ---------------------------------------------------------------------------
#
# Тонкие обёртки над `src/dashboard/queries.conversion_*`, чтобы Desktop-фронт
# мог рисовать таб «Конверсия» через тот же FastAPI, что и остальные dashboard
# endpoints, не таская в себя SQLite и pandas. Защита от стейл-БД (нет phase3
# колонок) — внутри `queries._conversion_columns_available()`, эндпоинты в
# таком случае вернут `0`/`[]`, а не 500.


@router.get("/conversion/summary")
def conversion_summary(days: int = Query(30, ge=1, le=180)):
    """KPI-сводка: sent / replied / won / revenue + reply_rate / win_rate."""
    summary = q.conversion_summary(days)
    sent = int(summary.get("sent", 0))
    replied = int(summary.get("replied", 0))
    won = int(summary.get("won", 0))
    revenue = float(summary.get("revenue", 0.0))
    reply_rate = round(100.0 * replied / sent, 2) if sent else 0.0
    win_rate = round(100.0 * won / sent, 2) if sent else 0.0
    return {
        "days": days,
        "sent": sent,
        "replied": replied,
        "won": won,
        "revenue": revenue,
        "reply_rate": reply_rate,
        "win_rate": win_rate,
    }


@router.get("/conversion/by-provider")
def conversion_by_provider(days: int = Query(30, ge=1, le=180)):
    return _records(q.conversion_by_provider(days))


@router.get("/conversion/by-niche")
def conversion_by_niche(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(15, ge=1, le=100),
):
    return _records(q.conversion_by_niche(days, limit))


@router.get("/conversion/by-queue-position")
def conversion_by_queue_position(days: int = Query(30, ge=1, le=180)):
    return _records(q.conversion_by_queue_position(days))


@router.get("/conversion/by-response-time")
def conversion_by_response_time(days: int = Query(30, ge=1, le=180)):
    return _records(q.conversion_by_response_time(days))


@router.get("/conversion/by-prompt-variant")
def conversion_by_prompt_variant(days: int = Query(30, ge=1, le=180)):
    return _records(q.conversion_by_prompt_variant(days))


@router.get("/conversion/classification-breakdown")
def conversion_classification_breakdown(days: int = Query(30, ge=1, le=180)):
    return _records(q.reply_classification_breakdown(days))


@router.get("/conversion")
def conversion_aggregate(days: int = Query(30, ge=1, le=180)):
    """Один запрос — все таблицы Phase 3 для Desktop-таба «Конверсия».

    Десктоп-фронт обычно хочет нарисовать всё разом, и нам дешевле
    отдать один JSON, чем заставлять его делать 7 параллельных
    запросов и собирать состояние. Все поля имеют ту же форму, что и
    индивидуальные эндпоинты выше — фронт может выбрать любой стиль.
    """
    return {
        "days": days,
        "summary": conversion_summary(days),
        "by_provider": _records(q.conversion_by_provider(days)),
        "by_niche": _records(q.conversion_by_niche(days)),
        "by_queue_position": _records(q.conversion_by_queue_position(days)),
        "by_response_time": _records(q.conversion_by_response_time(days)),
        "by_prompt_variant": _records(q.conversion_by_prompt_variant(days)),
        "classification_breakdown": _records(q.reply_classification_breakdown(days)),
    }
