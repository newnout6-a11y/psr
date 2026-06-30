"""Dashboard metrics endpoints — thin wrappers around src/dashboard/queries."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query

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


@router.get("/health")
async def kwork_health():
    """Полный health check аккаунта Kwork."""
    try:
        from src.platforms.kwork import get_kwork_service

        service = get_kwork_service()
        return await service.check_account_health()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/funnel")
def funnel(days: int = Query(30, ge=1, le=90)):
    return q.conversion_funnel(days)


@router.get("/earnings")
def earnings():
    return q.earnings_summary()


@router.get("/earnings/list")
def earnings_list(limit: int = Query(50, ge=1, le=200)):
    from src.action.proposal_db import ProposalDB

    return ProposalDB().get_earnings(limit=limit)


@router.get("/conversations")
def conversations(limit: int = Query(20, ge=1, le=100)):
    return q.active_conversations(limit)


@router.get("/conversations/{project_id}/{platform}")
def conversation_history_route(project_id: str, platform: str):
    return q.conversation_history(project_id, platform)


@router.get("/kwork-categories")
async def kwork_categories():
    """Получить дерево категорий Kwork (динамически через API)."""
    from src.platforms.kwork import get_kwork_service

    service = get_kwork_service()
    cats = await service.get_all_categories()
    return {"categories": cats}


@router.get("/kwork-connects")
async def kwork_connects():
    """Получить баланс connects (лимит откликов)."""
    from src.platforms.kwork import get_kwork_service

    service = get_kwork_service()
    info = await service.get_connects_info()
    return info


@router.get("/kwork-orders")
async def kwork_orders(status: str = "all"):
    """Получить заказы фрилансера для трекинга."""
    from src.platforms.kwork import get_kwork_service

    service = get_kwork_service()
    orders = await service.get_worker_orders(status_filter=status)
    return {"orders": orders}


@router.get("/kwork-connects-check")
async def kwork_connects_check():
    """Проверить баланс connects с предупреждениями."""
    from src.platforms.kwork import get_kwork_service
    from src.platforms.kwork_ext import get_connects_monitor

    service = get_kwork_service()
    info = await service.check_connects()
    monitor = get_connects_monitor()
    return {
        "connects": info,
        "free_amount": monitor.free_amount,
        "can_send": monitor.can_send(),
        "warn_threshold": monitor.warn_threshold,
        "block_threshold": monitor.block_threshold,
    }


@router.get("/skipped")
def skipped_candidates(limit: int = Query(50, ge=1, le=200)):
    """Скипнутые кандидаты из БД с причиной пропуска."""
    from src.action.proposal_db import ProposalDB

    rows = ProposalDB().get_candidates_by_status(["skipped"], limit=limit)
    result = []
    for r in rows:
        result.append(
            {
                "candidate_id": r.get("candidate_id"),
                "project_id": str(r.get("project_id", "")),
                "platform": r.get("platform", ""),
                "title": (r.get("title") or "")[:80],
                "budget": str(r.get("budget") or ""),
                "status": r.get("status") or "",
                "decision_reason": (r.get("decision_reason") or "")[:120],
                "ai_score": r.get("ai_score"),
                "ai_score_source": r.get("ai_score_source") or "",
                "updated_at": r.get("updated_at") or "",
                "search_query": r.get("search_query") or "",
            }
        )
    return {"items": result, "total": len(result)}


@router.get("/alerts")
def get_alerts():
    """Активные алерты: slow responses, pending reviews, auto-assignments."""
    from src.action.proposal_db import ProposalDB
    from datetime import datetime, timezone, timedelta

    alerts: list[dict[str, Any]] = []
    db = ProposalDB()

    try:
        threshold_hours = int(os.getenv("KWORK_RESPONSE_TIME_ALERT_HOURS", "2"))
        now = datetime.now(timezone.utc)
        convs = db.get_active_conversations(limit=50)
        for conv in convs:
            if conv.get("status") == "awaiting_reply" and conv.get("last_message_at"):
                try:
                    last_msg = conv["last_message_at"].replace(" ", "T")
                    msg_time = datetime.fromisoformat(last_msg)
                    if msg_time.tzinfo is None:
                        msg_time = msg_time.replace(tzinfo=timezone.utc)
                    elapsed = (now - msg_time).total_seconds() / 3600
                    if elapsed >= threshold_hours:
                        alerts.append(
                            {
                                "type": "slow_response",
                                "severity": "warning" if elapsed < 6 else "critical",
                                "project_id": conv.get("project_id", ""),
                                "platform": conv.get("platform", ""),
                                "title": conv.get("project_title") or conv.get("project_id", ""),
                                "detail": f"Нет ответа {elapsed:.0f}ч (порог {threshold_hours}ч)",
                            }
                        )
                except Exception:
                    pass
    except Exception:
        pass

    try:
        with db._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.candidate_id, c.project_id, c.platform, c.title, c.sent_at
                FROM candidates c
                WHERE c.status IN ('auto_sent', 'manual_sent')
                  AND c.sent_at IS NOT NULL
                ORDER BY c.sent_at DESC LIMIT 30
                """
            ).fetchall()
            for row in rows:
                sent_at = row["sent_at"]
                if not sent_at:
                    continue
                try:
                    sent_dt = datetime.fromisoformat(sent_at.replace(" ", "T"))
                    if sent_dt.tzinfo is None:
                        sent_dt = sent_dt.replace(tzinfo=timezone.utc)
                    age_hours = (now - sent_dt).total_seconds() / 3600
                    if age_hours >= 24:
                        has_review = conn.execute(
                            "SELECT 1 FROM earnings WHERE candidate_id = ? AND status = 'paid'",
                            (row["candidate_id"],),
                        ).fetchone()
                        has_completed = conn.execute(
                            "SELECT 1 FROM candidates WHERE candidate_id = ? AND status = 'completed'",
                            (row["candidate_id"],),
                        ).fetchone()
                        if not has_review and not has_completed:
                            alerts.append(
                                {
                                    "type": "pending_review",
                                    "severity": "info",
                                    "project_id": row["project_id"],
                                    "platform": row["platform"],
                                    "title": row["title"] or row["project_id"],
                                    "detail": f"Отправлено {age_hours:.0f}ч назад, отзыв не оставлен",
                                }
                            )
                except Exception:
                    pass
    except Exception:
        pass

    return {"alerts": alerts, "count": len(alerts)}
