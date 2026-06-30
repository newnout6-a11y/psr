"""Telegram diagnostics and test delivery."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.utils.telegram_transport import (
    bot_api_get_me,
    mtproto_configured,
    send_telegram,
)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


class TelegramTestRequest(BaseModel):
    text: str | None = None
    chat_id: str | None = None
    transport: str | None = None


@router.get("/status")
async def telegram_status():
    try:
        bot_api = await bot_api_get_me()
    except Exception as e:
        bot_api = type("BotAPIStatus", (), {"to_dict": lambda self: {"ok": False, "detail": str(e)}})()
    return {
        "configured": bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("ADMIN_CHAT_ID")),
        "admin_chat_id_set": bool(os.getenv("ADMIN_CHAT_ID")),
        "transport": os.getenv("TELEGRAM_TRANSPORT", "auto"),
        "bot_api_base": os.getenv("TELEGRAM_BOT_API_BASE") or "https://api.telegram.org",
        "bot_api": bot_api.to_dict(),
        "mtproto_configured": mtproto_configured(),
    }


@router.post("/test")
async def telegram_test(req: TelegramTestRequest):
    text = req.text or "PSR test: Telegram delivery works."
    try:
        result = await send_telegram(text, chat_id=req.chat_id, transport=req.transport)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram send failed: {e}") from e
    return result.to_dict()
