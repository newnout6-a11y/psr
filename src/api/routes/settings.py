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
    "GROQ_API_KEY", "GOOGLE_API_KEY", "GLM_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK",
    "GROQ_MODEL", "GOOGLE_MODEL", "GLM_MODEL",
    "AI_SCORE_THRESHOLD", "QUERY_GENERATION_PROVIDER", "SCORING_PROVIDER",
    "PROPOSAL_WRITING_PROVIDER",
    # Platforms
    "PLATFORMS", "SEARCH_BRIEF", "SEARCH_QUERY", "QUERY_COUNT", "PAGES_TO_PARSE", "TOP_PROJECTS",
    "KWORK_EMAIL", "KWORK_PASSWORD", "KWORK_JWT_TOKEN",
    "KWORK_COOKIE_REMEMBERME", "KWORK_COOKIE_USERID", "KWORK_COOKIE_PHPSESSID",
    "KWORK_COOKIE_CSRF", "KWORK_CSRF_TOKEN",
    "FREELANCE_RU_COOKIES_JSON", "FREELANCE_RU_COOKIE_SESSION",
    "FREELANCE_RU_COOKIE_DUID", "FREELANCE_RU_COOKIE_REMEMBER",
    "FL_RU_COOKIE_ID", "FL_RU_COOKIE_PWD", "FL_RU_COOKIE_SESSION", "FL_RU_XSRF_TOKEN",
    "FREELANCER_OAUTH_TOKEN",
    # Execution
    "CONTINUOUS_MODE", "CYCLE_INTERVAL", "EXECUTION_MODE",
    "AUTO_SEND_SCORE_MIN", "AUTO_SEND_VET_MIN", "AUTO_SEND_MAX_OFFERS",
    "AUTO_SEND_MAX_BUDGET",
    # Telegram
    "TELEGRAM_TOKEN", "ADMIN_CHAT_ID", "TELEGRAM_TRANSPORT",
    "TELEGRAM_BOT_API_BASE", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
    "TELEGRAM_MTPROTO_SESSION", "APPROVAL_TIMEOUT",
    "TELEGRAM_DIGEST_ENABLED", "DIGEST_INTERVAL_MIN", "TELEGRAM_QUEUE_PAGE_SIZE",
    # Browser / session
    "BROWSER_HEADLESS", "SESSION_HUB_URL", "SESSION_HUB_REQUIRED",
    # OSINT
    "OSINT_ENABLED", "OSINT_PROVIDERS", "OSINT_PROBIV_PROVIDERS",
    "GITHUB_TOKEN", "HIBP_API_KEY", "LEAKCHECK_API_KEY", "INTELX_API_KEY",
    "INTELX_BASE_URL", "INTELX_BUCKETS", "INTELX_MAX_RESULTS",
    "EMAILREP_KEY", "OSINT_WMN_FULL", "SHODAN_API_KEY",
    # Misc
    "PROXY_URL", "TIMEZONE_REGION",
]

_SECRET_KEYS = {
    "GROQ_API_KEY", "GOOGLE_API_KEY", "GLM_API_KEY",
    "TELEGRAM_TOKEN", "TELEGRAM_API_HASH",
    "GITHUB_TOKEN", "HIBP_API_KEY", "LEAKCHECK_API_KEY",
    "INTELX_API_KEY", "EMAILREP_KEY", "SHODAN_API_KEY",
    "KWORK_EMAIL", "KWORK_PASSWORD", "KWORK_JWT_TOKEN",
    "KWORK_COOKIE_REMEMBERME", "KWORK_COOKIE_USERID", "KWORK_COOKIE_PHPSESSID",
    "KWORK_COOKIE_CSRF", "KWORK_CSRF_TOKEN",
    "FREELANCE_RU_COOKIES_JSON", "FREELANCE_RU_COOKIE_SESSION",
    "FREELANCE_RU_COOKIE_DUID", "FREELANCE_RU_COOKIE_REMEMBER",
    "FL_RU_COOKIE_ID", "FL_RU_COOKIE_PWD", "FL_RU_COOKIE_SESSION", "FL_RU_XSRF_TOKEN",
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
    safe = {
        k: v
        for k, v in req.values.items()
        if k in _ENV_KEYS and not (k in _SECRET_KEYS and v == SECRET_MASK)
    }
    if not safe:
        return {"ok": True, "updated": []}
    try:
        _write_env_file(safe)
        # Also update current process env
        for k, v in safe.items():
            os.environ[k] = v
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "updated": list(safe.keys())}


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
