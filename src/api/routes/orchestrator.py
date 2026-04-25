"""Orchestrator control: start/stop cycle, mode, platform pause."""
from __future__ import annotations

import asyncio
import os
from contextlib import suppress

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api.state import app_state

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])

DEFAULT_PROBIV_PROVIDERS = "emailrep,whatsmyname,hibp,leakcheck,intelx"
_RESTORABLE_KEYS = {
    "TELEGRAM_TOKEN",
    "ADMIN_CHAT_ID",
    "OSINT_PROBIV_PROVIDERS",
}
_ENV_CACHE = {key: os.getenv(key, "") for key in _RESTORABLE_KEYS}


class ModeRequest(BaseModel):
    mode: str  # auto | semi_auto | manual | paused


class CycleRequest(BaseModel):
    dry_run: bool = False
    limit: int = 5
    continuous: bool = False
    platforms: list[str] | None = None
    pages_to_parse: int | None = None
    query_count: int | None = None
    top_projects: int | None = None
    search_brief: str | None = None
    browser_headless: bool | None = None
    osint_enabled: bool | None = None
    probiv_enabled: bool | None = None
    telegram_enabled: bool | None = None
    session_hub_required: bool | None = None


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _remember_current_values() -> None:
    for key in _RESTORABLE_KEYS:
        value = os.getenv(key, "")
        if value:
            _ENV_CACHE[key] = value


def _runtime_env_from_request(req: CycleRequest) -> dict[str, str]:
    updates: dict[str, str] = {}

    if req.platforms is not None:
        allowed = {"kwork", "freelance_ru", "hh_ru"}
        platforms = [p.strip().lower() for p in req.platforms if p and p.strip()]
        platforms = [p for p in platforms if p in allowed]
        if not platforms:
            raise HTTPException(status_code=400, detail="Выбери хотя бы одну платформу")
        updates["PLATFORMS"] = ",".join(dict.fromkeys(platforms))

    if req.pages_to_parse is not None:
        updates["PAGES_TO_PARSE"] = str(_clamp(req.pages_to_parse, 1, 10))
    if req.query_count is not None:
        updates["QUERY_COUNT"] = str(_clamp(req.query_count, 1, 20))
    if req.top_projects is not None:
        updates["TOP_PROJECTS"] = str(_clamp(req.top_projects, 0, 50))
    if req.search_brief is not None:
        updates["SEARCH_BRIEF"] = req.search_brief.strip()
    if req.browser_headless is not None:
        updates["BROWSER_HEADLESS"] = _bool_text(req.browser_headless)
    if req.osint_enabled is not None:
        updates["OSINT_ENABLED"] = _bool_text(req.osint_enabled)
    if req.session_hub_required is not None:
        updates["SESSION_HUB_REQUIRED"] = _bool_text(req.session_hub_required)
    if req.probiv_enabled is not None:
        remembered = _ENV_CACHE.get("OSINT_PROBIV_PROVIDERS") or DEFAULT_PROBIV_PROVIDERS
        updates["OSINT_PROBIV_PROVIDERS"] = remembered if req.probiv_enabled else ""
    if req.telegram_enabled is not None:
        if req.telegram_enabled:
            updates["TELEGRAM_TOKEN"] = _ENV_CACHE.get("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
            updates["ADMIN_CHAT_ID"] = _ENV_CACHE.get("ADMIN_CHAT_ID") or os.getenv("ADMIN_CHAT_ID", "")
        else:
            updates["TELEGRAM_TOKEN"] = ""
            updates["ADMIN_CHAT_ID"] = ""

    return updates


def _apply_runtime_env(updates: dict[str, str]) -> dict[str, str | None]:
    _remember_current_values()
    previous = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        os.environ[key] = value
    return previous


def _restore_runtime_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def _reset_runtime_instances() -> None:
    if app_state._orchestrator is not None:
        with suppress(Exception):
            await app_state._orchestrator.stop()
        app_state.reset_orchestrator()

    with suppress(Exception):
        from src.browser.browser_manager import BrowserManager
        BrowserManager.reset()

    with suppress(Exception):
        from src.utils.notifier import TelegramNotifier
        TelegramNotifier._instance = None

    with suppress(Exception):
        import src.brain.llm_router as llm_router_module
        llm_router_module.llm_router = None


@router.get("/status")
def get_status():
    db_mode = "semi_auto"
    paused_platforms: list[str] = []
    try:
        from src.action.proposal_db import ProposalDB
        db = ProposalDB()
        db_mode = db.get_runtime_mode()
        paused_platforms = db.get_paused_platforms()
    except Exception:
        pass
    return {
        **app_state.to_status_dict(),
        "execution_mode": db_mode,
        "paused_platforms": paused_platforms,
        "runtime_config": {
            "platforms": [p for p in os.getenv("PLATFORMS", "kwork,freelance_ru,hh_ru").split(",") if p],
            "pages_to_parse": _env_int("PAGES_TO_PARSE", 5),
            "query_count": _env_int("QUERY_COUNT", 6),
            "top_projects": _env_int("TOP_PROJECTS", 0),
            "browser_headless": _env_bool("BROWSER_HEADLESS", True),
            "osint_enabled": _env_bool("OSINT_ENABLED", True),
            "probiv_enabled": bool(os.getenv("OSINT_PROBIV_PROVIDERS", "").strip()),
            "telegram_configured": bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("ADMIN_CHAT_ID")),
            "session_hub_required": _env_bool("SESSION_HUB_REQUIRED", False),
        },
    }


@router.post("/start")
async def start_cycle(req: CycleRequest):
    if app_state.cycle_running:
        raise HTTPException(status_code=409, detail="Cycle already running")
    runtime_env = _runtime_env_from_request(req)

    async def _run():
        previous_env: dict[str, str | None] = {}
        app_state.cycle_running = True
        app_state.last_error = None
        app_state.broadcast_status_sync(app_state.to_status_dict())
        try:
            previous_env = _apply_runtime_env(runtime_env)
            await _reset_runtime_instances()
            orch = app_state.get_orchestrator()
            await orch.notifier.start()
            await asyncio.sleep(1)

            cycle_interval = int(os.getenv("CYCLE_INTERVAL", "1800"))
            while True:
                stats = await orch.run_cycle(
                    dry_run=req.dry_run,
                    limit_per_platform=req.limit,
                )
                app_state.last_cycle_stats = stats
                app_state.broadcast_status_sync(app_state.to_status_dict())
                if not req.continuous:
                    break
                stop_evt = asyncio.Event()
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=cycle_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            app_state.last_error = str(e)
        finally:
            app_state.cycle_running = False
            app_state.broadcast_status_sync(app_state.to_status_dict())
            try:
                await app_state._orchestrator.stop()
            except Exception:
                pass
            app_state.reset_orchestrator()
            await _reset_runtime_instances()
            _restore_runtime_env(previous_env)

    app_state.cycle_task = asyncio.create_task(_run())
    return {"ok": True, "message": "Cycle started"}


@router.post("/stop")
async def stop_cycle():
    if app_state.cycle_task and not app_state.cycle_task.done():
        app_state.cycle_task.cancel()
        try:
            await app_state.cycle_task
        except asyncio.CancelledError:
            pass
    app_state.cycle_running = False
    app_state.broadcast_status_sync(app_state.to_status_dict())
    return {"ok": True, "message": "Cycle stopped"}


@router.post("/mode")
def set_mode(req: ModeRequest):
    allowed = {"auto", "semi_auto", "manual", "paused"}
    if req.mode not in allowed:
        raise HTTPException(status_code=400, detail=f"mode must be one of {allowed}")
    try:
        from src.action.proposal_db import ProposalDB
        ProposalDB().set_runtime_mode(req.mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    app_state.broadcast_status_sync({"execution_mode": req.mode})
    return {"ok": True, "mode": req.mode}


@router.post("/pause/{platform}")
def pause_platform(platform: str):
    try:
        from src.action.proposal_db import ProposalDB
        ProposalDB().set_platform_paused(platform, True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "platform": platform, "paused": True}


@router.post("/resume/{platform}")
def resume_platform(platform: str):
    try:
        from src.action.proposal_db import ProposalDB
        ProposalDB().set_platform_paused(platform, False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "platform": platform, "paused": False}
