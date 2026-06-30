"""Settings API: read/write .env and filters.yaml."""

from __future__ import annotations

import os
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.paths import ROOT_DIR

router = APIRouter(prefix="/api/settings", tags=["settings"])

ENV_FILE = ROOT_DIR / ".env"
FILTERS_FILE = ROOT_DIR / "config" / "filters.yaml"
SECRET_MASK = "********"

# Keys we expose in the UI (sensitive keys are shown but masked on read)
_ENV_KEYS = [
    # LLM
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "OPENAI_BASE_URL",
    "OPENAI_API_PREFIX",
    "OPENAI_WIRE_API",
    "OPENAI_MODEL",
    "OPENAI_REASONING_EFFORT",
    "OPENAI_DISABLE_RESPONSE_STORAGE",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_PREFIX",
    "DEEPSEEK_WIRE_API",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_REASONING_EFFORT",
    "DEEPSEEK_THINKING",
    "DEEPSEEK_MODEL_QUERY_GENERATION",
    "DEEPSEEK_MODEL_SCORING",
    "GROQ_API_KEY",
    "LLM_PROVIDER",
    "PARSER_LLM_PROVIDER",
    "PARSER_LLM_MODEL",
    "GROQ_MODEL",
    "AI_SCORE_THRESHOLD",
    "AI_SCORE_MODE",
    "AI_SCORE_BATCH_SIZE",
    "AI_SCORE_DEEPSEEK_BATCH_SIZE",
    "AI_SCORE_MAX_CANDIDATES",
    "QUERY_GENERATION_PROVIDER",
    "SCORING_PROVIDER",
    "PROPOSAL_WRITING_PROVIDER",
    "PROPOSAL_IMAGE_ENABLED",
    "PROPOSAL_IMAGE_MODEL",
    "PROPOSAL_IMAGE_MIN_AI_SCORE",
    "PROPOSAL_IMAGE_MIN_VET_SCORE",
    "KWORK_IMAGE_ATTACH_CONFIRMED",
    "ATTACHMENT_CONTEXT_ENABLED",
    "ATTACHMENT_MAX_FILES",
    "ATTACHMENT_MAX_BYTES",
    "ATTACHMENT_MAX_CHARS",
    # Platforms
    "PLATFORMS",
    "SEARCH_BRIEF",
    "SEARCH_QUERY",
    "DISCOVERY_MODE",
    "QUERY_COUNT",
    "PAGES_TO_PARSE",
    "MAX_PAGES_PER_QUERY",
    "MAX_PROJECTS_PER_CYCLE",
    "MAX_PARSE_SECONDS",
    "TOP_PROJECTS",
    "KWORK_EMAIL",
    "KWORK_PASSWORD",
    "KWORK_JWT_TOKEN",
    "KWORK_COOKIE_REMEMBERME",
    "KWORK_COOKIE_USERID",
    "KWORK_COOKIE_PHPSESSID",
    "KWORK_COOKIE_CSRF",
    "KWORK_CSRF_TOKEN",
    "FREELANCE_RU_COOKIES_JSON",
    "FREELANCE_RU_COOKIE_SESSION",
    "FREELANCE_RU_COOKIE_DUID",
    "FREELANCE_RU_COOKIE_REMEMBER",
    "FL_RU_COOKIE_ID",
    "FL_RU_COOKIE_PWD",
    "FL_RU_COOKIE_SESSION",
    "FL_RU_XSRF_TOKEN",
    "FREELANCER_OAUTH_TOKEN",
    # Execution
    "CONTINUOUS_MODE",
    "CYCLE_INTERVAL",
    "EXECUTION_MODE",
    "AUTO_SEND_SCORE_MIN",
    "AUTO_SEND_VET_MIN",
    "AUTO_SEND_MAX_OFFERS",
    "AUTO_SEND_MAX_BUDGET",
    # Telegram
    "TELEGRAM_TOKEN",
    "ADMIN_CHAT_ID",
    "TELEGRAM_TRANSPORT",
    "TELEGRAM_BOT_API_BASE",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_MTPROTO_SESSION",
    "APPROVAL_TIMEOUT",
    "TELEGRAM_DIGEST_ENABLED",
    "DIGEST_INTERVAL_MIN",
    "TELEGRAM_QUEUE_PAGE_SIZE",
    # Browser / session
    "BROWSER_HEADLESS",
    "SESSION_HUB_URL",
    "SESSION_HUB_REQUIRED",
    # OSINT
    "OSINT_ENABLED",
    "OSINT_PROVIDERS",
    "OSINT_PROBIV_PROVIDERS",
    "GITHUB_TOKEN",
    "HIBP_API_KEY",
    "LEAKCHECK_API_KEY",
    "INTELX_API_KEY",
    "INTELX_BASE_URL",
    "INTELX_BUCKETS",
    "INTELX_MAX_RESULTS",
    "EMAILREP_KEY",
    "OSINT_WMN_FULL",
    "SHODAN_API_KEY",
    # Misc
    "PROXY_URL",
    "TIMEZONE_REGION",
    # Kwork pacing / TLS
    "KWORK_PACING",
    "KWORK_PACE_MIN",
    "KWORK_PACE_MAX",
    "KWORK_BURST_LIMIT",
    "KWORK_BURST_WINDOW",
    "KWORK_PROXY_LIST",
    "KWORK_TLS_IMPERSONATE",
    "KWORK_TLS_BROWSER",
    "KWORK_PHONE",
]

_SECRET_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_TOKEN",
    "TELEGRAM_API_HASH",
    "GITHUB_TOKEN",
    "HIBP_API_KEY",
    "LEAKCHECK_API_KEY",
    "INTELX_API_KEY",
    "EMAILREP_KEY",
    "SHODAN_API_KEY",
    "KWORK_EMAIL",
    "KWORK_PASSWORD",
    "KWORK_JWT_TOKEN",
    "KWORK_COOKIE_REMEMBERME",
    "KWORK_COOKIE_USERID",
    "KWORK_COOKIE_PHPSESSID",
    "KWORK_COOKIE_CSRF",
    "KWORK_CSRF_TOKEN",
    "FREELANCE_RU_COOKIES_JSON",
    "FREELANCE_RU_COOKIE_SESSION",
    "FREELANCE_RU_COOKIE_DUID",
    "FREELANCE_RU_COOKIE_REMEMBER",
    "FL_RU_COOKIE_ID",
    "FL_RU_COOKIE_PWD",
    "FL_RU_COOKIE_SESSION",
    "FL_RU_XSRF_TOKEN",
    "FREELANCER_OAUTH_TOKEN",
}


def _read_env_file() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _write_env_file(data: dict[str, str]) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    original = ENV_FILE.read_text(encoding="utf-8")
    lines = original.splitlines()
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in data:
            new_lines.append(f"{key}={data[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)
    for key, value in data.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class EnvUpdateRequest(BaseModel):
    values: dict[str, str]


class FiltersUpdateRequest(BaseModel):
    data: dict[str, Any]


@router.get("/env")
def get_env():
    raw = _read_env_file()
    result: dict[str, str] = {}
    for key in _ENV_KEYS:
        val = raw.get(key, os.getenv(key, ""))
        result[key] = SECRET_MASK if key in _SECRET_KEYS and val else val
    return {"values": result, "secret_keys": sorted(_SECRET_KEYS), "mask": SECRET_MASK}


@router.get("/env/reveal/{key}")
def reveal_env_value(key: str):
    if key not in _ENV_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if key not in _SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Setting is not secret: {key}")
    raw = _read_env_file()
    return {"key": key, "value": raw.get(key, os.getenv(key, ""))}


@router.put("/env")
def update_env(req: EnvUpdateRequest):
    # Only allow updating known keys
    safe = {k: v for k, v in req.values.items() if k in _ENV_KEYS and not (k in _SECRET_KEYS and v == SECRET_MASK)}
    if not safe:
        return {"ok": True, "updated": []}
    try:
        _write_env_file(safe)
        # Also update current process env (override=True semantics)
        for k, v in safe.items():
            os.environ[k] = v
        # Reset cached singletons so they pick up new values
        _reset_caches()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "updated": list(safe.keys())}


def _reset_caches() -> None:
    """Reset singletons that cache env-derived config (LLMRouter, etc.)."""
    from contextlib import suppress

    with suppress(Exception):
        import src.brain.llm_router as llm_router_module

        llm_router_module.llm_router = None
    with suppress(Exception):
        from src.utils.notifier import TelegramNotifier

        TelegramNotifier._instance = None
    with suppress(Exception):
        from src.browser.browser_manager import BrowserManager

        BrowserManager.reset()
    with suppress(Exception):
        from src.api.state import app_state

        app_state.reset_orchestrator()


@router.get("/filters")
def get_filters():
    if not FILTERS_FILE.exists():
        return {"data": {}}
    try:
        data = yaml.safe_load(FILTERS_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"data": data}


@router.put("/filters")
def update_filters(req: FiltersUpdateRequest):
    try:
        FILTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FILTERS_FILE.write_text(
            yaml.dump(req.data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True}
