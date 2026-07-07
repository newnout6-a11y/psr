"""Dashboard metrics endpoints — thin wrappers around src/dashboard/queries."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from src.dashboard import queries as q

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class ConversationMessageRequest(BaseModel):
    text: str


class ConversationDraftRequest(BaseModel):
    tone: str = "friendly"


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


def _dialog_value(dialog: Any, *names: str, default: Any = "") -> Any:
    if isinstance(dialog, dict):
        for name in names:
            if name in dialog and dialog.get(name) not in (None, ""):
                return dialog.get(name)
        return default
    for name in names:
        value = getattr(dialog, name, None)
        if value not in (None, ""):
            return value
    return default


def _dialog_last_message(dialog: Any) -> str:
    text = _dialog_value(dialog, "last_message", default="")
    if text:
        return str(text)
    last_obj = _dialog_value(dialog, "lastMessage", "last_message_obj", default=None)
    if isinstance(last_obj, dict):
        return str(last_obj.get("message") or last_obj.get("text") or "")
    return str(getattr(last_obj, "message", "") or getattr(last_obj, "text", "") or "")


def _dialog_sender(dialog: Any) -> str:
    sender = str(_dialog_value(dialog, "sender", default="") or "")
    return sender if sender in {"customer", "freelancer"} else "customer"


def _dialog_unread_count(dialog: Any) -> int:
    value = _dialog_value(dialog, "unread_count", "unread", default=0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _dialog_sync_status(dialog: Any, username: str = "") -> str:
    sender = _dialog_sender(dialog)
    if sender == "freelancer":
        return "replied"
    if (username or "").strip().lower() == "support":
        return "system"
    return "awaiting_reply" if _dialog_unread_count(dialog) > 0 else "read"


async def _sync_kwork_dialogs(limit: int) -> None:
    """Best-effort read-only sync so the desktop page sees live Kwork dialogs."""
    try:
        from src.action.proposal_db import ProposalDB
        from src.platforms.kwork import get_kwork_service

        db = ProposalDB()
        service = get_kwork_service()
        dialogs: list[Any] = []
        if hasattr(service, "get_web_dialogs"):
            dialogs = await service.get_web_dialogs(limit)
        try:
            if not dialogs:
                api = await service.get_api()
            else:
                api = None
            if api and not dialogs:
                if hasattr(service, "_sync_session_hub_cookies"):
                    await service._sync_session_hub_cookies(api)
                dialogs = await api.get_all_dialogs()
        except Exception:
            dialogs = []

        for dialog in list(dialogs or [])[:limit]:
            username = str(_dialog_value(dialog, "username", "user_name", default="") or "")
            dialog_id = str(_dialog_value(dialog, "id", "dialog_id", "user_id", default=username) or username)
            project_id = str(
                _dialog_value(dialog, "project_id", "want_id", "order_id", "user_id", default="")
                or username
                or dialog_id
            )
            if not project_id:
                continue
            title = str(_dialog_value(dialog, "project_name", default="") or username or "Kwork dialog")
            conv_id = db.get_or_create_conversation(project_id, "kwork", project_title=title)
            last_message = _dialog_last_message(dialog)
            if last_message:
                msg_id = str(_dialog_value(dialog, "last_message_id", "message_id", default="") or dialog_id)
                db.add_conversation_message(
                    conv_id,
                    sender=_dialog_sender(dialog),
                    message_text=last_message,
                    platform_message_id=msg_id,
                )
                if hasattr(db, "update_conversation_status"):
                    db.update_conversation_status(conv_id, _dialog_sync_status(dialog, username))
    except Exception:
        return


async def _sync_kwork_dialogs_safe(limit: int, timeout: float = 8.0) -> None:
    try:
        await asyncio.wait_for(_sync_kwork_dialogs(limit), timeout=timeout)
    except TimeoutError:
        logger.debug(f"Dashboard: Kwork dialog sync timed out after {timeout}s")


@router.get("/conversations")
async def conversations(limit: int = Query(20, ge=1, le=100)):
    await _sync_kwork_dialogs_safe(limit)
    return q.active_conversations(limit)


@router.get("/conversations/{project_id}/{platform}")
async def conversation_history_route(project_id: str, platform: str):
    read_state: dict[str, Any] | None = None
    if platform == "kwork":
        await _sync_kwork_dialogs_safe(100)
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            read_state = await service.mark_web_dialog_read(project_id)
            if not read_state.get("ok"):
                from src.action.proposal_db import ProposalDB

                conv = ProposalDB().get_conversation(project_id, platform) or {}
                title = str(conv.get("project_title") or "").strip()
                if title and title != project_id:
                    fallback_state = await service.mark_web_dialog_read(title)
                    read_state = {
                        **read_state,
                        "fallback": fallback_state,
                        "ok": bool(fallback_state.get("ok")),
                    }
        except Exception:
            read_state = {"ok": False}
    return {"messages": q.conversation_history(project_id, platform), "username": "", "read_state": read_state}


@router.post("/conversations/{project_id}/{platform}/draft")
async def conversation_draft_route(project_id: str, platform: str, req: ConversationDraftRequest):
    messages = q.conversation_history(project_id, platform)
    if not messages:
        raise HTTPException(status_code=400, detail="No conversation messages to draft from")

    from src.action.proposal_db import ProposalDB
    from src.brain.llm_router import get_llm_router

    conv = ProposalDB().get_conversation(project_id, platform) or {}
    recent = messages[-12:]
    transcript = "\n".join(
        f"{'Клиент' if item.get('sender') == 'customer' else 'Мы'}: {item.get('message_text') or ''}"
        for item in recent
    )
    system_prompt = (
        "Ты помогаешь фрилансеру отвечать клиенту в чате Kwork. "
        "Пиши на русском, кратко, уверенно и по делу. "
        "Не выдумывай факты, цены, сроки или обещания, которых нет в переписке. "
        "Не используй markdown, подписи, приветствие с именем без имени клиента и эмодзи."
    )
    prompt = (
        f"Проект: {conv.get('project_title') or project_id}\n"
        f"Платформа: {platform}\n"
        f"Тон: {req.tone}\n\n"
        f"Переписка:\n{transcript}\n\n"
        "Составь один готовый ответ клиенту. Если нужно уточнение, задай 1-2 конкретных вопроса."
    )
    try:
        text = await get_llm_router().generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.35,
            max_tokens=450,
            task="conversation_reply_draft",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to generate reply: {exc}") from exc
    return {"ok": True, "text": str(text or "").strip()}


@router.post("/conversations/{project_id}/{platform}/message")
async def conversation_message_route(project_id: str, platform: str, req: ConversationMessageRequest):
    if platform != "kwork":
        raise HTTPException(status_code=400, detail="Message sending only supported for kwork")
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text is empty")

    from src.action.proposal_db import ProposalDB
    from src.platforms.kwork import get_kwork_service

    db = ProposalDB()
    conv = db.get_conversation(project_id, platform)
    recipient = project_id
    service = get_kwork_service()
    result = await service.send_web_message(recipient, text)
    if result is None:
        raise HTTPException(status_code=502, detail="Failed to send message")

    if not conv:
        conv_id = db.get_or_create_conversation(project_id, platform, project_title=str(project_id))
    else:
        conv_id = int(conv["conversation_id"])
    db.add_conversation_message(
        conv_id,
        sender="freelancer",
        message_text=text,
        platform_message_id=str(result.get("MID") or result.get("inbox_message_id") or ""),
    )
    return {"ok": True, "message_id": result.get("MID"), "result": result}


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
    from datetime import datetime, timezone

    alerts: list[dict[str, Any]] = []
    db = ProposalDB()
    now = datetime.now(timezone.utc)

    try:
        threshold_hours = int(os.getenv("KWORK_RESPONSE_TIME_ALERT_HOURS", "2"))
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
