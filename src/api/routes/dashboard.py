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
