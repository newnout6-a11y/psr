"""Telegram delivery helpers with direct Bot API and MTProto fallback.

Bot API is tried without environment proxies (`trust_env=False`) so a global
Windows/proxy setting cannot silently change the route. MTProto is an optional
fallback for networks where `api.telegram.org` is unstable but Telegram DCs are
reachable.
"""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from src.paths import RUNTIME_DIR, ensure_parent


@dataclass
class TelegramAttempt:
    method: str
    ok: bool
    detail: str = ""
    status_code: int | None = None
    response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "ok": self.ok,
            "detail": self.detail,
            "status_code": self.status_code,
            "response": self.response,
        }


@dataclass
class TelegramDeliveryResult:
    ok: bool
    method: str = ""
    attempts: list[TelegramAttempt] = field(default_factory=list)

    @property
    def detail(self) -> str:
        if not self.attempts:
            return ""
        return self.attempts[-1].detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "detail": self.detail,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def _token() -> str:
    return os.getenv("TELEGRAM_TOKEN", "").strip()


def _admin_chat_id() -> str:
    return os.getenv("ADMIN_CHAT_ID", "").strip()


def _bot_api_base() -> str:
    return (os.getenv("TELEGRAM_BOT_API_BASE") or os.getenv("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip(
        "/"
    )


def _normalize_chat_id(chat_id: str | int | None) -> str:
    value = str(chat_id or _admin_chat_id()).strip()
    return value


def _normalize_markup(reply_markup: Any) -> Any:
    if reply_markup is None:
        return None
    if isinstance(reply_markup, dict):
        return reply_markup
    if hasattr(reply_markup, "model_dump"):
        return reply_markup.model_dump(exclude_none=True)
    if hasattr(reply_markup, "dict"):
        return reply_markup.dict(exclude_none=True)
    return reply_markup


async def bot_api_request(method: str, payload: dict[str, Any] | None = None) -> TelegramAttempt:
    token = _token()
    if not token:
        return TelegramAttempt("bot_api", False, "TELEGRAM_TOKEN is empty")

    url = f"{_bot_api_base()}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            if payload is None:
                response = await client.get(url)
            else:
                response = await client.post(url, json=payload)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:500]}

        ok = response.status_code == 200 and bool(data.get("ok"))
        detail = "ok" if ok else str(data.get("description") or data)[:500]
        return TelegramAttempt("bot_api", ok, detail, response.status_code, data)
    except Exception as e:
        return TelegramAttempt("bot_api", False, f"{type(e).__name__}: {e}")


async def bot_api_get_me() -> TelegramAttempt:
    return await bot_api_request("getMe")


async def bot_api_send_message(
    text: str,
    *,
    chat_id: str | int | None = None,
    reply_markup: Any = None,
) -> TelegramAttempt:
    target = _normalize_chat_id(chat_id)
    if not target:
        return TelegramAttempt("bot_api", False, "ADMIN_CHAT_ID is empty")

    payload: dict[str, Any] = {
        "chat_id": target,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    markup = _normalize_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await bot_api_request("sendMessage", payload)


async def bot_api_send_photo(
    photo_path: str | Path,
    caption: str,
    *,
    chat_id: str | int | None = None,
    reply_markup: Any = None,
) -> TelegramAttempt:
    token = _token()
    if not token:
        return TelegramAttempt("bot_api", False, "TELEGRAM_TOKEN is empty")

    target = _normalize_chat_id(chat_id)
    if not target:
        return TelegramAttempt("bot_api", False, "ADMIN_CHAT_ID is empty")

    path = Path(photo_path)
    if not path.exists():
        return TelegramAttempt("bot_api", False, f"Photo not found: {path}")

    data: dict[str, str] = {
        "chat_id": target,
        "caption": caption[:1024],
    }
    markup = _normalize_markup(reply_markup)
    if markup is not None:
        data["reply_markup"] = json.dumps(markup, ensure_ascii=False)

    url = f"{_bot_api_base()}/bot{token}/sendPhoto"
    try:
        with path.open("rb") as file:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(
                    url,
                    data=data,
                    files={"photo": (path.name, file, "image/png")},
                )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}

        ok = response.status_code == 200 and bool(payload.get("ok"))
        detail = "ok" if ok else str(payload.get("description") or payload)[:500]
        return TelegramAttempt("bot_api", ok, detail, response.status_code, payload)
    except Exception as e:
        return TelegramAttempt("bot_api", False, f"{type(e).__name__}: {e}")


async def bot_api_send_document(
    document_path: str | Path,
    caption: str = "",
    *,
    chat_id: str | int | None = None,
    reply_markup: Any = None,
) -> TelegramAttempt:
    """Send a document (any file type) to a Telegram chat via Bot API."""
    token = _token()
    if not token:
        return TelegramAttempt("bot_api", False, "TELEGRAM_TOKEN is empty")

    target = _normalize_chat_id(chat_id)
    if not target:
        return TelegramAttempt("bot_api", False, "ADMIN_CHAT_ID is empty")

    path = Path(document_path)
    if not path.exists():
        return TelegramAttempt("bot_api", False, f"File not found: {path}")

    data: dict[str, str] = {
        "chat_id": target,
        "caption": caption[:1024],
    }
    markup = _normalize_markup(reply_markup)
    if markup is not None:
        data["reply_markup"] = json.dumps(markup, ensure_ascii=False)

    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    url = f"{_bot_api_base()}/bot{token}/sendDocument"
    try:
        with path.open("rb") as file:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.post(
                    url,
                    data=data,
                    files={"document": (path.name, file, mime)},
                )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}

        ok = response.status_code == 200 and bool(payload.get("ok"))
        detail = "ok" if ok else str(payload.get("description") or payload)[:500]
        return TelegramAttempt("bot_api", ok, detail, response.status_code, payload)
    except Exception as e:
        return TelegramAttempt("bot_api", False, f"{type(e).__name__}: {e}")


async def bot_api_send_media_group(
    media: list[dict[str, Any]],
    *,
    chat_id: str | int | None = None,
) -> TelegramAttempt:
    """Send multiple photos/documents as a media group."""
    token = _token()
    if not token:
        return TelegramAttempt("bot_api", False, "TELEGRAM_TOKEN is empty")

    target = _normalize_chat_id(chat_id)
    if not target:
        return TelegramAttempt("bot_api", False, "ADMIN_CHAT_ID is empty")

    url = f"{_bot_api_base()}/bot{token}/sendMediaGroup"
    try:
        import json as _json

        data = {
            "chat_id": target,
            "media": _json.dumps(media, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.post(url, data=data)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}

        ok = response.status_code == 200 and bool(payload.get("ok"))
        detail = "ok" if ok else str(payload.get("description") or payload)[:500]
        return TelegramAttempt("bot_api", ok, detail, response.status_code, payload)
    except Exception as e:
        return TelegramAttempt("bot_api", False, f"{type(e).__name__}: {e}")


def mtproto_configured() -> bool:
    return bool(_token() and os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH"))


def _mtproto_target(chat_id: str | int | None) -> str | int:
    value = _normalize_chat_id(chat_id)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


async def mtproto_send_message(text: str, *, chat_id: str | int | None = None) -> TelegramAttempt:
    if not mtproto_configured():
        return TelegramAttempt(
            "mtproto",
            False,
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH for MTProto fallback",
        )

    try:
        from telethon import TelegramClient

        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        session = os.getenv("TELEGRAM_MTPROTO_SESSION", "").strip()
        if not session:
            session = str(ensure_parent(RUNTIME_DIR / "telegram_mtproto_bot.session").with_suffix(""))

        client = TelegramClient(
            session,
            api_id,
            api_hash,
            connection_retries=2,
            request_retries=2,
            timeout=20,
        )
        await client.start(bot_token=_token())
        try:
            sent = await client.send_message(_mtproto_target(chat_id), text[:4096])
            return TelegramAttempt(
                "mtproto",
                True,
                "ok",
                response={"message_id": getattr(sent, "id", None)},
            )
        finally:
            await client.disconnect()
    except Exception as e:
        return TelegramAttempt("mtproto", False, f"{type(e).__name__}: {e}")


async def send_telegram(
    text: str,
    *,
    chat_id: str | int | None = None,
    reply_markup: Any = None,
    transport: str | None = None,
) -> TelegramDeliveryResult:
    mode = (transport or os.getenv("TELEGRAM_TRANSPORT", "auto")).strip().lower()
    if mode in {"", "direct"}:
        mode = "bot_api"

    attempts: list[TelegramAttempt] = []
    if mode in {"auto", "bot_api"}:
        attempt = await bot_api_send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        attempts.append(attempt)
        if attempt.ok:
            return TelegramDeliveryResult(True, "bot_api", attempts)
        logger.warning(f"Telegram Bot API send failed: {attempt.detail}")
        if mode == "bot_api":
            return TelegramDeliveryResult(False, "bot_api", attempts)

    if mode in {"auto", "mtproto"}:
        attempt = await mtproto_send_message(text, chat_id=chat_id)
        attempts.append(attempt)
        if attempt.ok:
            return TelegramDeliveryResult(True, "mtproto", attempts)
        logger.warning(f"Telegram MTProto send failed: {attempt.detail}")
        return TelegramDeliveryResult(False, "mtproto", attempts)

    attempts.append(TelegramAttempt(mode, False, f"Unknown TELEGRAM_TRANSPORT={mode}"))
    return TelegramDeliveryResult(False, mode, attempts)


async def send_telegram_photo(
    photo_path: str | Path,
    caption: str,
    *,
    chat_id: str | int | None = None,
    reply_markup: Any = None,
    transport: str | None = None,
) -> TelegramDeliveryResult:
    mode = (transport or os.getenv("TELEGRAM_TRANSPORT", "auto")).strip().lower()
    if mode in {"", "direct"}:
        mode = "bot_api"

    attempts: list[TelegramAttempt] = []
    if mode in {"auto", "bot_api"}:
        attempt = await bot_api_send_photo(
            photo_path,
            caption,
            chat_id=chat_id,
            reply_markup=reply_markup,
        )
        attempts.append(attempt)
        if attempt.ok:
            return TelegramDeliveryResult(True, "bot_api", attempts)
        logger.warning(f"Telegram Bot API photo send failed: {attempt.detail}")
        if mode == "bot_api":
            return TelegramDeliveryResult(False, "bot_api", attempts)

    fallback = await send_telegram(
        caption,
        chat_id=chat_id,
        reply_markup=reply_markup,
        transport="mtproto" if mode in {"auto", "mtproto"} else mode,
    )
    attempts.extend(fallback.attempts)
    return TelegramDeliveryResult(fallback.ok, fallback.method, attempts)
