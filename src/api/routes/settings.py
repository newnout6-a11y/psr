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
    "KWORK_COVER_IMAGE_API_KEY",
    "KWORK_COVER_IMAGE_BASE_URL",
    "KWORK_COVER_IMAGE_API_PREFIX",
    "KWORK_COVER_IMAGE_CONN",
    "KWORK_COVER_IMAGE_MODEL",
    "KWORK_COVER_IMAGE_SIZE",
    "KWORK_COVER_IMAGE_QUALITY",
    "KWORK_PORTFOLIO_IMAGE_SIZE",
    "KWORK_PORTFOLIO_IMAGE_QUALITY",
    "KWORK_PORTFOLIO_IMAGE_CONCURRENCY",
    "KWORK_COVER_IMAGE_TIMEOUT",
    "KWORK_COVER_VISION_PROVIDER",
    "KWORK_COVER_VISION_MODEL",
    "KWORK_COVER_VISION_MAX_TOKENS",
    "KWORK_COVER_QA_ENABLED",
    "KWORK_COVER_QA_PROVIDER",
    "KWORK_COVER_QA_MODEL",
    "KWORK_COVER_QA_MIN_SCORE",
    "KWORK_COVER_PROMPT_PROVIDER",
    "KWORK_COVER_PROMPT_MODEL",
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
    "CATCHMAIL_DOMAIN",
    "CATCHMAIL_API_BASE_URL",
    "FIRSTMAIL_API_KEY",
    "KWORK_REGISTRATION_MAIL_TIMEOUT",
    "KWORK_REGISTRATION_MAIL_POLL_INTERVAL",
    "KWORK_REGISTRATION_MAIL_INITIAL_DELAY",
    "KWORK_REGISTRATION_ROUTE_TIMEOUT",
    "KWORK_REGISTRATION_ROUTE_CONCURRENCY",
    "KWORK_AUTO_APPROVE_ORDERS",
    "KWORK_FAST_INBOX_POLLING",
    "KWORK_INBOX_POLL_INTERVAL",
    "KWORK_RESPONSE_TIME_ALERT_HOURS",
    "KWORK_SUCCESS_RATE_WARN",
    "KWORK_SUCCESS_RATE_BLOCK",
    "KWORK_BUSY_THRESHOLD",
    "KWORK_AUTO_PAUSE_KWORKS",
    "KWORK_AUTO_REVIEW",
    "KWORK_AUTO_REVIEW_RATING",
    "KWORK_AUTO_REVIEW_TEXT",
    "KWORK_CONNECTS_WARN",
    "KWORK_CONNECTS_BLOCK",
    "SCRAPE_COMPETITOR_PRICES",
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
    "VPNTE_PROXY_ENABLED",
    "VPNTE_PROXY_ROTATE_ON_NEXT",
    "VPNTE_PROXY_STRICT",
    "VPNTE_PROXY_COUNTRY",
    "VPNTE_PROXY_PROFILE_ID",
    "VPNTE_PROXY_SLOT",
    "VPNTE_PROXY_PORT",
    "VPNTE_PROXY_TIMEOUT",
    "VPNTE_PROXY_HEALTH_TIMEOUT",
    "VPNTE_PROXY_HEALTH_WARMUP",
    "VPNTE_PROXY_CACHE_TTL",
    "VPNTE_CONTROL_URL",
    "VPNTE_CONTROL_TOKEN",
    "TIMEZONE_REGION",
    # Kwork pacing / TLS
    "KWORK_PACING",
    "KWORK_PACE_MIN",
    "KWORK_PACE_MAX",
    "KWORK_REGISTRATION_PACE_MIN",
    "KWORK_REGISTRATION_PACE_MAX",
    "KWORK_REGISTRATION_BURST_LIMIT",
    "KWORK_BURST_LIMIT",
    "KWORK_BURST_WINDOW",
    "KWORK_MARKET_USE_PROXY",
    "KWORK_MARKET_PROXY_STRICT",
    "KWORK_MARKET_API_TIMEOUT",
    "KWORK_MARKET_SEED_DISCOVERY_TIMEOUT",
    "KWORK_MARKET_SUPPLY_MIN_CARDS",
    "KWORK_MARKET_SUPPLY_MAX_REQUESTS",
    "KWORK_MARKET_SUPPLY_PROXY_PORTS",
    "KWORK_MARKET_ASSISTANT_MAP_CHARS",
    "KWORK_MARKET_ASSISTANT_MAP_CONCURRENCY",
    "KWORK_MARKET_ASSISTANT_MAP_ATTEMPTS",
    "KWORK_MARKET_ASSISTANT_LLM_TIMEOUT_SECONDS",
    "KWORK_MARKET_ASSISTANT_MAP_MODEL",
    "KWORK_MARKET_ASSISTANT_REDUCE_MODEL",
    "KWORK_MARKET_ASSISTANT_MAP_REASONING",
    "KWORK_MARKET_ASSISTANT_REDUCE_REASONING",
    "KWORK_SESSION_HUB_COOKIE_TTL",
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
    "VPNTE_CONTROL_TOKEN",
    "GITHUB_TOKEN",
    "HIBP_API_KEY",
    "LEAKCHECK_API_KEY",
    "INTELX_API_KEY",
    "EMAILREP_KEY",
    "SHODAN_API_KEY",
    "KWORK_EMAIL",
    "KWORK_PASSWORD",
    "FIRSTMAIL_API_KEY",
    "KWORK_COVER_IMAGE_API_KEY",
    "KWORK_COVER_IMAGE_CONN",
    "KWORK_PHONE",
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


class VpnteActionRequest(BaseModel):
    slot: int | None = None
    country: str | None = None
    profile_id: str | None = None
    port: int | None = None
    id: str | None = None


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
    if "KWORK_COVER_IMAGE_MODEL" in safe and safe["KWORK_COVER_IMAGE_MODEL"].strip() != "gpt-image-2":
        raise HTTPException(status_code=422, detail="KWORK_COVER_IMAGE_MODEL is locked to gpt-image-2")
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
    with suppress(Exception):
        from src.utils.vpnte_proxy import clear_vpnte_proxy_cache

        clear_vpnte_proxy_cache()


def _session_hub_health_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies").strip()
    if not hub_url:
        hub_url = "http://127.0.0.1:8669/cookies"
    parts = urlsplit(hub_url)
    base_path = parts.path.rsplit("/", 1)[0] if parts.path else ""
    health_path = f"{base_path}/health" if base_path else "/health"
    return urlunsplit((parts.scheme or "http", parts.netloc, health_path, "", ""))


def _session_hub_status() -> dict[str, Any]:
    import httpx

    hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies").strip()
    health_url = _session_hub_health_url()
    try:
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            response = client.get(health_url)
        ok = response.status_code == 200
        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            payload = {"text": response.text[:200]}
        return {
            "ok": ok,
            "url": hub_url,
            "health_url": health_url,
            "status_code": response.status_code,
            "detail": payload.get("message") or payload.get("status") or "",
            "cache_entries": payload.get("cache_entries"),
            "proxy_isolated": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": hub_url,
            "health_url": health_url,
            "status_code": None,
            "detail": f"{type(exc).__name__}: {exc}",
            "cache_entries": None,
            "proxy_isolated": True,
        }


def _vpnte_status(*, probe: bool = True) -> dict[str, Any]:
    from src.utils.vpnte_proxy import VpnteProxyClient, vpnte_config_snapshot

    config = vpnte_config_snapshot()
    result: dict[str, Any] = {"ok": False, "running": False, "proxy_url": "", "detail": "", **config}
    if not probe:
        return result
    try:
        client = VpnteProxyClient()
        client.timeout = min(client.timeout, 3.0)
        instances = client.instances()
        configured_slot = os.getenv("VPNTE_PROXY_SLOT", "").strip()
        selected = client._select_instance(
            instances,
            slot=int(configured_slot) if configured_slot.isdigit() else None,
        )
        diagnostic_status: dict[str, Any] = selected or {}
        if selected is None and configured_slot.isdigit():
            diagnostic_status = client.status(int(configured_slot))
        result.update(
            {
                "ok": True,
                "running": bool(selected and selected.get("running") and selected.get("proxyUrl")),
                "proxy_url": str(selected.get("proxyUrl") or "") if selected else "",
                "instances": instances,
                "raw": diagnostic_status,
            }
        )
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result


@router.get("/network/status")
def get_network_status(probe: bool = True):
    return {
        "vpnte": _vpnte_status(probe=probe),
        "session_hub": _session_hub_status(),
    }


@router.post("/network/vpnte/start")
def start_vpnte_proxy(req: VpnteActionRequest | None = None):
    from src.utils.vpnte_proxy import VpnteProxyClient, clear_vpnte_proxy_cache

    updates: dict[str, str] = {}
    if req:
        if req.country is not None:
            updates["VPNTE_PROXY_COUNTRY"] = req.country
        if req.profile_id is not None:
            updates["VPNTE_PROXY_PROFILE_ID"] = req.profile_id
        if req.port is not None:
            updates["VPNTE_PROXY_PORT"] = str(req.port)
    if updates:
        _write_env_file(updates)
        for key, value in updates.items():
            os.environ[key] = value
    clear_vpnte_proxy_cache()
    try:
        payload = VpnteProxyClient().start(
            slot=req.slot if req else None,
            country=req.country if req else None,
            profile_id=req.profile_id if req else None,
            port=req.port if req else None,
        )
        clear_vpnte_proxy_cache()
        return {"ok": True, "status": payload, "network": get_network_status(probe=True)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "network": get_network_status(probe=False)}


@router.post("/network/vpnte/rotate")
def rotate_vpnte_proxy(req: VpnteActionRequest | None = None):
    from src.utils.vpnte_proxy import VpnteProxyClient, clear_vpnte_proxy_cache

    updates: dict[str, str] = {}
    if req:
        if req.country is not None:
            updates["VPNTE_PROXY_COUNTRY"] = req.country
        if req.profile_id is not None:
            updates["VPNTE_PROXY_PROFILE_ID"] = req.profile_id
        if req.port is not None:
            updates["VPNTE_PROXY_PORT"] = str(req.port)
    if updates:
        _write_env_file(updates)
        for key, value in updates.items():
            os.environ[key] = value
    clear_vpnte_proxy_cache()
    try:
        payload = VpnteProxyClient().rotate(
            slot=req.slot if req else None,
            country=req.country if req else None,
            profile_id=req.profile_id if req else None,
            port=req.port if req else None,
        )
        clear_vpnte_proxy_cache()
        return {"ok": True, "status": payload, "network": get_network_status(probe=True)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "network": get_network_status(probe=False)}


@router.post("/network/vpnte/connect")
def connect_vpnte_proxy(req: VpnteActionRequest):
    from src.utils.vpnte_proxy import VpnteProxyClient

    if req.slot is None or not (req.id or req.profile_id):
        raise HTTPException(status_code=422, detail="slot and id are required")
    try:
        payload = VpnteProxyClient().connect(req.slot, req.id or req.profile_id or "")
        return {"ok": True, "status": payload, "network": get_network_status(probe=True)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "network": get_network_status(probe=False)}


@router.post("/network/vpnte/trigger")
def trigger_vpnte_proxy(req: VpnteActionRequest):
    from src.utils.vpnte_proxy import VpnteProxyClient

    if req.slot is None:
        raise HTTPException(status_code=422, detail="slot is required")
    try:
        payload = VpnteProxyClient().trigger(req.slot, req.id or req.profile_id)
        return {"ok": True, "status": payload, "network": get_network_status(probe=True)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "network": get_network_status(probe=False)}


@router.post("/network/vpnte/stop")
def stop_vpnte_proxy(req: VpnteActionRequest):
    from src.utils.vpnte_proxy import VpnteProxyClient

    if req.slot is None:
        raise HTTPException(status_code=422, detail="slot is required")
    try:
        payload = VpnteProxyClient().stop(req.slot)
        return {"ok": True, "status": payload, "network": get_network_status(probe=True)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "network": get_network_status(probe=False)}


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
