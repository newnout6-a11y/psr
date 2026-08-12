"""Kwork diagnostics and project inspection."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from kwork.exceptions import KworkRetryExceeded
from loguru import logger
from pydantic import BaseModel, Field

from src.paths import PROPOSAL_ASSETS_DIR, REFERENCE_DIR
from src.platforms.kwork import (
    KWORK_BASE_URL,
    KworkRegistrationError,
    KworkStateDataParser,
    get_kwork_service,
)
from src.platforms.kwork_autopublish import KworkAutopublishService
from src.platforms.kwork_ext import KworkExtensions
from src.platforms.kwork_form_contract import normalize_attribute_selection
from src.platforms.kwork_listing import KworkWebListingClient, manual_verification_evidence
from src.platforms.kwork_market import KworkMarketClient
from src.platforms.kwork_market_supply import KworkSupplyScanner, MarketAssistant
from src.utils.vpnte_proxy import kwork_http_proxy_url

router = APIRouter(prefix="/api/kwork", tags=["kwork"])

CATALOG_ALIASES_REFERENCE_FILE = REFERENCE_DIR / "kwork_catalog_aliases.json"


def _raise_kwork_market_error(exc: Exception, *, context: str) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ImportError) and "aiohttp-socks" in str(exc):
        raise HTTPException(
            status_code=503,
            detail=(
                "Kwork proxy dependency is missing in current Python runtime. "
                'Install aiohttp-socks or run: python -m pip install "kwork[proxy]". '
                f"{detail}"
            ),
        ) from exc
    if isinstance(exc, httpx.HTTPStatusError):
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    if isinstance(exc, (TimeoutError, KworkRetryExceeded)) or "TimeoutError" in detail:
        logger.warning(f"Kwork market {context} timed out: {detail}")
        raise HTTPException(
            status_code=504,
            detail="Kwork API timed out while loading market data. Try again or rotate VPN/proxy.",
        ) from exc
    logger.exception(f"Kwork market {context} failed")
    raise HTTPException(status_code=502, detail=detail) from exc


def _catalog_aliases_from_reference() -> tuple[int, list[dict[str, Any]]]:
    """Load the packaged category-to-catalog-path mapping without probing Kwork."""

    try:
        payload = json.loads(CATALOG_ALIASES_REFERENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Unable to load Kwork catalog alias reference")
        raise HTTPException(status_code=500, detail="Kwork catalog alias reference is unavailable") from exc

    raw_items = payload.get("aliases") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=500, detail="Kwork catalog alias reference has an invalid format")

    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            category_id = int(item.get("category_id") or 0)
        except (TypeError, ValueError):
            continue
        alias = str(item.get("alias") or "").strip().strip("/")
        if not category_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*", alias):
            continue
        category_name = str(item.get("category_name") or "").strip()
        label = str(item.get("label") or category_name or alias).strip()
        items.append(
            {
                "category_id": category_id,
                "category_name": category_name,
                "label": label,
                "alias": alias,
                "recommended": bool(item.get("recommended")),
            }
        )

    items.sort(key=lambda item: (item["category_id"], not item["recommended"], item["label"], item["alias"]))
    schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    return int(schema_version or 1), items


class KworkDraftRequest(BaseModel):
    category_id: int = Field(..., ge=1)
    category_name: str = ""
    classifier_id: int | None = None
    classifier_name: str = ""
    service_summary: str = ""
    brief: str = ""
    audience: str = ""
    market_context: dict[str, Any] = Field(default_factory=dict)
    portfolio_context: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    attribute_manifest: dict[str, Any] = Field(default_factory=dict)
    attribute_selection: dict[str, Any] = Field(default_factory=dict)
    price: int | None = Field(default=None, ge=1)
    work_time: int | None = Field(default=None, ge=1)
    lang: str = "ru"
    use_llm: bool = True
    generate_image: bool = False
    image_context: str = ""
    use_cover_prompt_llm: bool = True
    cover_prompt_provider: str | None = None
    cover_prompt_model: str | None = None
    cover_prompt_temperature: float | None = None
    use_competitor_image_analysis: bool = True
    cover_vision_provider: str | None = None
    cover_vision_model: str | None = None
    cover_vision_temperature: float | None = None
    cover_vision_max_tokens: int | None = Field(default=None, ge=1)
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None


class KworkPublishRequest(BaseModel):
    draft: dict[str, Any]
    dry_run: bool = True
    confirm_token: str = ""
    confirmation: str = ""


class KworkPublishPreflightRequest(BaseModel):
    draft: dict[str, Any]


class KworkRegisterRequest(BaseModel):
    """Explicit, manual email-only Kwork registration request."""

    email: str = Field(default="", max_length=90)
    mail_provider: str = Field(default="catchmail", pattern=r"^(catchmail|firstmail)$")
    mail_password: str = Field(default="", max_length=256)
    user_type: int = Field(default=1, ge=1, le=2)
    promo: str = Field(default="", max_length=512)
    use_simple: bool = False
    track_client_id: str = Field(default="", max_length=256)
    action_after: str = Field(default="", max_length=128)
    is_subscribed: bool = False
    captcha_token: str = Field(default="", max_length=4096)
    captcha_field: str = Field(default="smart-token", pattern=r"^(smart-token|g-recaptcha-response)$")
    firstmail_api_key: str = Field(default="", max_length=4096)
    dry_run: bool = False


class KworkRegisterBatchRequest(KworkRegisterRequest):
    """Multi-account registration through selected VPNTE slots."""

    account_count: int = Field(default=1, ge=1, le=100)
    vpnte_slots: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=1_000)
    avoid_used_ips: bool = False


class KworkRegistrationVerificationRequest(BaseModel):
    """Resume activation polling in the server-side session of a registration."""

    registration_id: str = Field(..., min_length=16, max_length=64)
    mail_password: str = Field(default="", max_length=256)
    firstmail_api_key: str = Field(default="", max_length=4096)


class KworkRegistrationCredentialsRequest(BaseModel):
    """Request generated credentials from the local encrypted account store."""

    registration_id: str = Field(..., min_length=16, max_length=64)


def _registration_management_error(exc: KworkRegistrationError) -> HTTPException:
    """Map local registration inventory errors to stable API responses."""

    status = {
        "registration_id_required": 422,
        "registration_not_found": 404,
        "account_persistence_failed": 500,
    }.get(exc.code, 502)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc), "payload": exc.payload})


class KworkFormManifestRequest(BaseModel):
    classifier_id: int | None = Field(default=None, ge=1)
    selection: dict[str, Any] = Field(default_factory=dict)
    lang: str = "ru"


class KworkMarketMetricsRequest(BaseModel):
    category_id: int = Field(..., ge=1)
    classifier_id: int | None = Field(default=None, ge=1)
    include_demand: bool = False
    include_competitor_details: bool = False
    competitor_detail_limit: int = Field(default=6, ge=0, le=12)
    page: int = Field(default=1, ge=1)
    attribute_selection: dict[str, Any] = Field(default_factory=dict)
    attribute_controls: list[dict[str, Any]] = Field(default_factory=list)


class KworkSupplyScanRequest(BaseModel):
    category_id: int = Field(..., ge=1)
    category_name: str = ""
    classifier_id: int | None = Field(default=None, ge=1)
    classifier_name: str = ""
    coverage: str = "balanced"
    include_llm: bool = True
    write_file: bool = True


class KworkMarketAssistantRequest(BaseModel):
    context_id: str = Field(..., min_length=8, max_length=128)
    message: str = Field(..., min_length=1, max_length=8000)


class KworkMarketIntelligenceRequest(BaseModel):
    seeds: list[dict[str, Any]] | None = None
    max_seeds: int = Field(default=8, ge=1, le=30)
    pages: int = Field(default=1, ge=1, le=3)
    include_demand: bool = True
    demand_queries: list[str] | None = None
    include_competitor_details: bool = False
    competitor_detail_limit: int = Field(default=0, ge=0, le=12)
    include_seller_details: bool = False
    seller_detail_limit: int = Field(default=8, ge=0, le=20)
    include_want_details: bool = False
    want_detail_limit: int = Field(default=2, ge=0, le=10)
    include_price_rules: bool = False
    include_account_context: bool = False
    write_file: bool = True


class KworkBuyerScoutRequest(BaseModel):
    probes: list[dict[str, Any]] | None = None
    category_id: int | None = Field(default=None, ge=1)
    classifier_id: int | None = Field(default=None, ge=1)
    category_name: str = ""
    classifier_name: str = ""
    attribute_selection: dict[str, Any] = Field(default_factory=dict)
    attribute_controls: list[dict[str, Any]] = Field(default_factory=list)
    max_probes: int = Field(default=10, ge=1, le=30)
    page: int = Field(default=1, ge=1, le=5)
    project_page_limit: int = Field(default=2, ge=1, le=3)
    per_probe_limit: int = Field(default=12, ge=1, le=50)
    top_limit: int = Field(default=20, ge=1, le=100)
    include_project_details: bool = False
    include_want_details: bool = False
    include_buyer_history: bool = False
    detail_limit: int = Field(default=8, ge=0, le=20)
    buyer_history_limit: int = Field(default=6, ge=0, le=20)
    budget_max: int = Field(default=5000, ge=0, le=150000)
    include_query_suggestions: bool = True
    query_suggestion_limit: int = Field(default=5, ge=0, le=20)
    include_control_windows: bool = False
    control_window_limit: int = Field(default=6, ge=0, le=20)
    write_file: bool = False


class KworkWebCatalogSnapshotRequest(BaseModel):
    aliases: list[str] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=50)
    delay_seconds: float = Field(default=2.0, ge=0, le=20)
    include_raw: bool = False
    write_file: bool = True


class KworkAttributeSuggestRequest(BaseModel):
    classifier_id: int | None = Field(default=None, ge=1)
    category_name: str = ""
    classifier_name: str = ""
    service_summary: str = ""
    audience: str = ""
    control: dict[str, Any]
    controls: list[dict[str, Any]] = Field(default_factory=list)
    selection: dict[str, Any] = Field(default_factory=dict)
    market_context: dict[str, Any] = Field(default_factory=dict)
    mode: str = ""
    use_llm: bool = True
    lang: str = "ru"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _attribute_option_ids(control: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for option in control.get("options") or []:
        if not isinstance(option, dict) or option.get("disabled"):
            continue
        try:
            result.add(int(option.get("id")))
        except (TypeError, ValueError):
            continue
    return result


def _attribute_suggestion_selection(
    payload: KworkAttributeSuggestRequest,
    selected_ids: list[int],
    reason: str,
    source: str,
    confidence: float = 0.5,
) -> dict[str, Any]:
    control = payload.control
    name = str(control.get("name") or "")
    if not name:
        raise HTTPException(status_code=400, detail="Control is missing a name")
    allowed = _attribute_option_ids(control)
    clean_ids = [item for item in selected_ids if item in allowed]
    if not clean_ids:
        raise HTTPException(status_code=422, detail="No valid option id was suggested")
    multiple = bool(control.get("multiple"))
    next_selection = dict(payload.selection or {})
    next_selection[name] = clean_ids if multiple else clean_ids[0]
    controls = [control]
    controls.extend(
        item
        for item in payload.controls
        if isinstance(item, dict) and str(item.get("name") or "").strip() != name
    )
    normalized = normalize_attribute_selection({"controls": controls, "lang": payload.lang}, next_selection)
    normalized_selection = normalized["selection"]
    normalized_values = normalized_selection.get(name)
    if normalized_values in (None, "") or normalized_values == []:
        raise HTTPException(status_code=422, detail="Suggested option is inactive or unavailable in this form state")
    return {
        "ok": True,
        "source": source,
        "control": name,
        "selected_ids": normalized_values if isinstance(normalized_values, list) else [normalized_values],
        "selection": normalized_selection,
        "selection_validation": {
            "manifest_hash": normalized["manifest_hash"],
            "selection_hash": normalized["selection_hash"],
            "valid": normalized["valid"],
            "issues": normalized["issues"],
            "unresolved_required": normalized["unresolved_required"],
        },
        "reason": reason,
        "confidence": confidence,
    }


def _fallback_attribute_suggestion(payload: KworkAttributeSuggestRequest) -> dict[str, Any]:
    control = payload.control
    options = [item for item in control.get("options") or [] if isinstance(item, dict) and not item.get("disabled")]
    if not options:
        raise HTTPException(status_code=422, detail="Control has no selectable options")
    context_parts = [
        payload.category_name,
        payload.classifier_name,
        payload.service_summary,
        payload.audience,
        payload.mode,
        str(control.get("question") or control.get("label") or control.get("custom_name") or control.get("name") or ""),
    ]
    for competitor in (payload.market_context or {}).get("competitors") or []:
        if isinstance(competitor, dict):
            context_parts.extend(
                [
                    str(competitor.get("title") or ""),
                    str(competitor.get("description") or ""),
                    str(competitor.get("service_size") or ""),
                ]
            )
    context = " ".join(context_parts).lower()

    def score(option: dict[str, Any]) -> int:
        label = str(option.get("label") or option.get("value") or option.get("id") or "").lower()
        points = 0
        if label and label in context:
            points += 10
        for word in re.findall(r"[\wа-яА-ЯёЁ#+]+", label):
            if len(word) >= 3 and word in context:
                points += 3
        if "telegram" in context or "телеграм" in context:
            if "telegram" in label or "телеграм" in label:
                points += 8
            if label == "python":
                points += 5
        if any(word in context for word in ["бот", "automation", "автоматизац"]):
            if label == "python":
                points += 4
        return points

    best = max(options, key=score)
    return _attribute_suggestion_selection(
        payload,
        [int(best.get("id"))],
        "Fallback picked the closest option by text overlap because AI did not return a usable choice.",
        "fallback",
        0.35,
    )


KWORK_VERIFICATION_CHECK_PATHS = ("/", "/new", "/manage_kworks", "/projects")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _summarize_kwork_verification(
    *,
    captcha_status: dict[str, Any],
    pages: list[dict[str, Any]],
    cookie_count: int,
    errors: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
    timings_ms: dict[str, int] | None = None,
) -> dict[str, Any]:
    checked_pages = [item for item in pages if isinstance(item, dict)]
    manual_pages = [
        item
        for item in checked_pages
        if isinstance(item.get("evidence"), dict) and item["evidence"].get("manual_required")
    ]
    smartcaptcha_scripts_seen = any(
        bool((item.get("evidence") or {}).get("script_matches")) for item in checked_pages if isinstance(item, dict)
    )
    web_session_ok = any(
        200 <= int(item.get("status_code") or 0) < 400
        and not ((item.get("evidence") or {}).get("manual_required"))
        for item in checked_pages
        if isinstance(item, dict)
    )
    api_required = bool(captcha_status.get("required")) if isinstance(captcha_status, dict) else False
    api_error = str(captcha_status.get("error") or "") if isinstance(captcha_status, dict) else ""

    if manual_pages:
        status = "manual_required"
        detail = "Kwork web pages currently show a manual SmartCaptcha/robot check."
    elif api_required:
        status = "api_flag_only"
        detail = "getCaptchaStatus returned a captcha flag, but checked web pages did not show a manual challenge."
    elif web_session_ok:
        status = "ok"
        detail = "Kwork web session opens normally; no manual captcha challenge was detected on checked pages."
    else:
        status = "error"
        detail = "Could not prove a working Kwork web session from checked pages."

    if smartcaptcha_scripts_seen and not manual_pages:
        detail += " SmartCaptcha scripts are present globally, which is not itself a captcha challenge."
    if api_error and status == "ok":
        detail += f" getCaptchaStatus was not usable: {api_error}"

    return {
        "generated_at": generated_at or _utc_now_iso(),
        "source": "psr.kwork_verification_status",
        "status": status,
        "detail": detail,
        "captcha_required": bool(manual_pages),
        "manual_verification_required": bool(manual_pages),
        "web_session_ok": web_session_ok,
        "smartcaptcha_scripts_seen": smartcaptcha_scripts_seen,
        "cookie_count": cookie_count,
        "captcha_status": captcha_status,
        "pages": checked_pages,
        "errors": errors or [],
        "timings_ms": timings_ms or {},
    }


@router.get("/verification-status")
async def kwork_verification_status(write_file: bool = Query(default=False)) -> dict[str, Any]:
    started = time.monotonic()
    service = get_kwork_service()
    errors: list[dict[str, Any]] = []
    cookies: dict[str, str] = {}
    captcha_status: dict[str, Any] = {
        "ok": False,
        "required": False,
        "source": "getCaptchaStatus",
        "error": "not_checked",
    }

    try:
        cookies = await service._fetch_session_hub_cookies()
    except Exception as exc:
        errors.append({"stage": "session_hub", "detail": f"{type(exc).__name__}: {exc}"})

    try:
        api = await service.get_api()
        if api:
            captcha_status = await KworkExtensions.get_captcha_status_detail(api)
        else:
            captcha_status["error"] = "api_not_available"
    except Exception as exc:
        captcha_status = {
            "ok": False,
            "required": False,
            "source": "getCaptchaStatus",
            "error": f"{type(exc).__name__}: {exc}",
        }

    pages: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=KWORK_BASE_URL,
        timeout=15,
        follow_redirects=True,
        proxy=kwork_http_proxy_url(rotate=False),
    ) as client:
        for path in KWORK_VERIFICATION_CHECK_PATHS:
            page_started = time.monotonic()
            try:
                response = await client.get(path, cookies=cookies)
                evidence = manual_verification_evidence(response.text or "", str(response.url), response.status_code)
                pages.append(
                    {
                        "path": path,
                        "status_code": response.status_code,
                        "final_url": str(response.url),
                        "evidence": evidence,
                        "timings_ms": {"total": int((time.monotonic() - page_started) * 1000)},
                    }
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                errors.append({"stage": "web_page", "path": path, "detail": detail})
                pages.append(
                    {
                        "path": path,
                        "status_code": 0,
                        "final_url": "",
                        "evidence": manual_verification_evidence("", path, 0),
                        "error": detail,
                        "timings_ms": {"total": int((time.monotonic() - page_started) * 1000)},
                    }
                )

    result = _summarize_kwork_verification(
        captcha_status=captcha_status,
        pages=pages,
        cookie_count=len(cookies),
        errors=errors,
        timings_ms={"total": int((time.monotonic() - started) * 1000)},
    )
    if write_file:
        root = Path("docs") / "kwork_market_snapshots"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"kwork_verification_status_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result["file_path"] = str(path)
    return result


@router.get("/status")
async def kwork_status():
    service = get_kwork_service()
    api_ok = False
    api_error = None
    auth_mode = None
    session_hub_cookie_count = 0
    session_hub_ok = False
    email_configured = bool(os.getenv("KWORK_EMAIL") and os.getenv("KWORK_PASSWORD"))

    try:
        cookies = await service._fetch_session_hub_cookies()
        session_hub_cookie_count = len(cookies)
        session_hub_ok = session_hub_cookie_count > 0
    except Exception as e:
        api_error = f"{type(e).__name__}: {e}"

    if session_hub_ok:
        api_ok = True
        auth_mode = "email+cookies" if email_configured else "cookie-only"
        api_error = None
    else:
        if api_error is None and os.getenv("SESSION_HUB_REQUIRED", "false").lower() == "true":
            api_error = "Session Hub did not return cookies"

    try:
        if not api_ok:
            api = await service.get_api()
            if api:
                try:
                    me = await api.get_me()
                    api_ok = bool(me)
                    if api_ok:
                        auth_mode = "email"
                        api_error = None
                except Exception as e:
                    api_error = f"{type(e).__name__}: {e}"
    except Exception as e:
        api_error = f"{type(e).__name__}: {e}"

    return {
        "configured": email_configured or session_hub_ok,
        "api_ok": api_ok,
        "api_error": api_error,
        "auth_mode": auth_mode,
        "session_hub_ok": session_hub_ok,
        "session_hub_cookie_count": session_hub_cookie_count,
        "session_hub_url": os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies"),
        "session_hub_required": os.getenv("SESSION_HUB_REQUIRED", "false").lower() == "true",
        "state_parser": "window.stateData",
    }


@router.post("/register")
async def kwork_register(payload: KworkRegisterRequest) -> dict[str, Any]:
    """Run the manual registration flow; never invoked by parsing loops."""

    service = get_kwork_service()
    try:
        result = await service.register_account(
            email=payload.email,
            mail_password=payload.mail_password,
            user_type=payload.user_type,
            promo=payload.promo,
            use_simple=payload.use_simple,
            track_client_id=payload.track_client_id,
            action_after=payload.action_after,
            is_subscribed=payload.is_subscribed,
            captcha_token=payload.captcha_token,
            captcha_field=payload.captcha_field,
            firstmail_api_key=payload.firstmail_api_key,
            mail_provider=payload.mail_provider,
            dry_run=payload.dry_run,
        )
        return result
    except KworkRegistrationError as exc:
        status = {
            "invalid_email": 422,
            "invalid_password": 422,
            "invalid_mail_provider": 422,
            "invalid_mail_credentials": 422,
            "invalid_registration_timestamp": 422,
            "invalid_user_type": 422,
            "invalid_username": 422,
            "forbidden_username": 422,
            "email_exists": 409,
            "email_stop_list": 409,
            "email_rejected": 422,
            "registration_disabled": 503,
            "login_unavailable": 422,
            "captcha_required": 428,
            "activation_failed": 502,
            "activation_pending": 202,
            "unsafe_activation_link": 502,
            "account_persistence_failed": 500,
            "invalid_proxy": 422,
        }.get(exc.code, 502)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "payload": exc.payload},
        ) from exc
    except Exception as exc:
        logger.exception("Kwork registration route failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/register/batch")
async def kwork_register_batch(payload: KworkRegisterBatchRequest) -> dict[str, Any]:
    """Create accounts through live, unique VPNTE egress routes."""

    service = get_kwork_service()
    try:
        return await service.register_accounts_batch(
            account_count=payload.account_count,
            vpnte_slots=payload.vpnte_slots,
            email=payload.email,
            mail_password=payload.mail_password,
            user_type=payload.user_type,
            promo=payload.promo,
            use_simple=payload.use_simple,
            track_client_id=payload.track_client_id,
            action_after=payload.action_after,
            is_subscribed=payload.is_subscribed,
            captcha_token=payload.captcha_token,
            captcha_field=payload.captcha_field,
            firstmail_api_key=payload.firstmail_api_key,
            mail_provider=payload.mail_provider,
            avoid_used_ips=payload.avoid_used_ips,
            dry_run=payload.dry_run,
        )
    except KworkRegistrationError as exc:
        status = {
            "invalid_account_count": 422,
            "invalid_proxy": 422,
            "vpnte_proxy_unavailable": 503,
            "vpnte_slot_unavailable": 422,
            "vpnte_capacity_insufficient": 503,
            "batch_mail_provider_not_supported": 422,
            "batch_email_not_supported": 422,
            "invalid_mail_provider": 422,
            "invalid_mail_credentials": 422,
            "invalid_user_type": 422,
        }.get(exc.code, 502)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "payload": exc.payload},
        ) from exc
    except Exception as exc:
        logger.exception("Kwork batch registration route failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/register/verify")
async def kwork_register_verify(payload: KworkRegistrationVerificationRequest) -> dict[str, Any]:
    """Resume activation polling without submitting another Kwork signup."""

    service = get_kwork_service()
    try:
        return await service.verify_registration_activation(
            registration_id=payload.registration_id,
            mail_password=payload.mail_password,
            firstmail_api_key=payload.firstmail_api_key,
        )
    except KworkRegistrationError as exc:
        status = {
            "invalid_email": 422,
            "invalid_password": 422,
            "invalid_mail_provider": 422,
            "invalid_mail_credentials": 422,
            "invalid_registration_timestamp": 422,
            "registration_id_required": 422,
            "registration_not_found": 404,
            "account_persistence_failed": 500,
        }.get(exc.code, 502)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "payload": exc.payload},
        ) from exc
    except Exception as exc:
        logger.exception("Kwork registration verification route failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/register/credentials")
async def kwork_register_credentials(payload: KworkRegistrationCredentialsRequest) -> dict[str, str]:
    """Return the generated login/password for one locally stored registration."""

    service = get_kwork_service()
    try:
        return service.get_registration_credentials(payload.registration_id)
    except KworkRegistrationError as exc:
        status = {
            "registration_id_required": 422,
            "registration_not_found": 404,
            "account_persistence_failed": 500,
        }.get(exc.code, 502)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "payload": exc.payload},
        ) from exc
    except Exception as exc:
        logger.exception("Kwork registration credentials route failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/register/accounts")
async def kwork_registration_accounts() -> dict[str, Any]:
    """List accounts saved by the local Kwork registration workflow."""

    service = get_kwork_service()
    try:
        accounts = service.list_registration_accounts()
        return {"accounts": accounts, "total": len(accounts)}
    except KworkRegistrationError as exc:
        raise _registration_management_error(exc) from exc
    except Exception as exc:
        logger.exception("Kwork registration accounts list failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/register/accounts/{registration_id}/session-check")
async def kwork_registration_account_session_check(registration_id: str) -> dict[str, Any]:
    """Check a saved account's cookie session without submitting a signup."""

    service = get_kwork_service()
    try:
        return await service.check_registration_account_session(registration_id)
    except KworkRegistrationError as exc:
        raise _registration_management_error(exc) from exc
    except Exception as exc:
        logger.exception("Kwork registration account session check failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.delete("/register/accounts/{registration_id}")
async def kwork_registration_account_delete(registration_id: str) -> dict[str, Any]:
    """Delete one local saved registration record; the Kwork account remains untouched."""

    service = get_kwork_service()
    try:
        return service.delete_registration_account(registration_id)
    except KworkRegistrationError as exc:
        raise _registration_management_error(exc) from exc
    except Exception as exc:
        logger.exception("Kwork registration account delete failed")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/market/categories")
async def kwork_market_categories():
    client = KworkMarketClient()
    try:
        return await client.get_categories_tree()
    except Exception as exc:
        _raise_kwork_market_error(exc, context="categories")
    finally:
        await client.close()


@router.get("/market/catalog-aliases")
async def kwork_market_catalog_aliases(category_id: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    """Return packaged canonical catalog paths for the category picker."""

    schema_version, items = _catalog_aliases_from_reference()
    if category_id is not None:
        items = [item for item in items if item["category_id"] == category_id]
    return {"schema_version": schema_version, "items": items, "total": len(items)}


@router.get("/market/category/{category_id}/attributes")
async def kwork_market_category_attributes(category_id: int):
    client = KworkMarketClient()
    try:
        return await client.get_category_attributes(category_id)
    except Exception as exc:
        _raise_kwork_market_error(exc, context=f"category attributes {category_id}")
    finally:
        await client.close()


@router.get("/market/category/{category_id}/prices")
async def kwork_market_category_prices(category_id: int, attribute_id: int | None = None):
    client = KworkMarketClient()
    try:
        return await client.get_price_rules(category_id, attribute_id)
    except Exception as exc:
        _raise_kwork_market_error(exc, context=f"category prices {category_id}")
    finally:
        await client.close()


@router.get("/market/web-catalog/{alias}")
async def kwork_market_web_catalog(
    alias: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    include_raw: bool = Query(default=False),
):
    cookies: dict[str, str] = {}
    try:
        cookies = await get_kwork_service()._fetch_session_hub_cookies()
    except Exception as exc:
        logger.debug(f"Kwork web catalog: Session Hub cookies unavailable: {type(exc).__name__}: {exc}")

    client = KworkMarketClient()
    try:
        return await client.get_web_catalog_filters(
            alias,
            page=page,
            page_size=page_size,
            include_raw=include_raw,
            cookies=cookies,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        _raise_kwork_market_error(exc, context=f"web catalog {alias}")
    finally:
        await client.close()


@router.post("/market/web-catalog-snapshot")
async def kwork_market_web_catalog_snapshot(payload: KworkWebCatalogSnapshotRequest) -> dict[str, Any]:
    cookies: dict[str, str] = {}
    try:
        cookies = await get_kwork_service()._fetch_session_hub_cookies()
    except Exception as exc:
        logger.debug(f"Kwork web catalog snapshot: Session Hub cookies unavailable: {type(exc).__name__}: {exc}")

    client = KworkMarketClient()
    try:
        return await client.get_web_catalog_alias_snapshot(
            aliases=payload.aliases or None,
            page=payload.page,
            page_size=payload.page_size,
            delay_seconds=payload.delay_seconds,
            include_raw=payload.include_raw,
            cookies=cookies,
            write_file=payload.write_file,
        )
    except Exception as exc:
        _raise_kwork_market_error(exc, context="web catalog snapshot")
    finally:
        await client.close()


@router.post("/market/category/{category_id}/form-manifest")
async def kwork_market_category_form_manifest(
    category_id: int,
    payload: KworkFormManifestRequest,
) -> dict[str, Any]:
    cookies: dict[str, str] = {}
    try:
        cookies = await get_kwork_service()._fetch_session_hub_cookies()
    except Exception as exc:
        logger.debug(f"Kwork form manifest: Session Hub cookies unavailable: {type(exc).__name__}: {exc}")

    client = KworkWebListingClient(cookies=cookies)
    try:
        return await client.build_attribute_manifest(
            category_id,
            selection=payload.selection,
            classifier_id=payload.classifier_id,
            lang=payload.lang or "ru",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Kwork form manifest failed")
        return {
            "category_id": category_id,
            "lang": payload.lang or "ru",
            "success": False,
            "code": "form_manifest_exception",
            "detail": f"{type(exc).__name__}: {exc}",
            "selected": payload.selection,
            "controls": [],
            "metadata": {},
            "fragments": [],
            "unresolved_required": [],
        }


@router.post("/market/category/{category_id}/attribute-suggest")
async def kwork_market_category_attribute_suggest(
    category_id: int,
    payload: KworkAttributeSuggestRequest,
) -> dict[str, Any]:
    control = payload.control
    if not control.get("name"):
        raise HTTPException(status_code=400, detail="Control is missing a name")
    if not control.get("options"):
        raise HTTPException(status_code=422, detail="Control has no options to choose from")

    if not payload.use_llm:
        return _fallback_attribute_suggestion(payload)

    prompt_payload = {
        "category_id": category_id,
        "category_name": payload.category_name,
        "classifier_id": payload.classifier_id,
        "classifier_name": payload.classifier_name,
        "service_summary": payload.service_summary,
        "audience": payload.audience,
        "mode": payload.mode,
        "current_selection": payload.selection,
        "target_control": control,
        "field_question": control.get("question") or control.get("label") or control.get("custom_name") or control.get("name"),
        "nearby_controls": payload.controls,
        "market_context": {
            "kworks_count": payload.market_context.get("kworks_count"),
            "demand": payload.market_context.get("demand"),
            "price_steps": payload.market_context.get("price_steps"),
            "competitors": (payload.market_context.get("competitors") or [])[:6],
        },
    }
    system_prompt = (
        "You choose Kwork form attributes for a freelancer listing. "
        "Return only JSON. Use only option ids from target_control.options. "
        "For radio/select choose one id. For checkbox choose one or more ids only when clearly relevant. "
        "Do not invent ids."
    )
    prompt = (
        "Pick the best option for the target Kwork field using the listing context, selected slice, "
        "competitors, and current form state.\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}\n\n"
        'Return JSON like {"selected_ids":[123],"reason":"short reason","confidence":0.0}.'
    )
    try:
        from src.brain.llm_router import get_llm_router

        text = await get_llm_router().generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.15,
            max_tokens=400,
            task="kwork_attribute_suggest",
        )
        parsed = _extract_json_object(str(text or "")) or {}
        raw_ids = parsed.get("selected_ids") or parsed.get("selectedIds") or [parsed.get("selected_id")]
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        selected_ids = []
        for item in raw_ids:
            try:
                selected_ids.append(int(item))
            except (TypeError, ValueError):
                continue
        if selected_ids:
            return _attribute_suggestion_selection(
                payload,
                selected_ids,
                str(parsed.get("reason") or "AI selected the closest Kwork attribute option."),
                "llm",
                float(parsed.get("confidence") or 0.65),
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug(f"Kwork attribute suggest LLM fallback: {exc}")
    return _fallback_attribute_suggestion(payload)


@router.get("/market/metrics")
async def kwork_market_metrics(
    category_id: int = Query(..., ge=1),
    classifier_id: int | None = Query(default=None, ge=1),
    include_demand: bool = False,
    include_competitor_details: bool = False,
    competitor_detail_limit: int = Query(default=6, ge=0, le=12),
    page: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    client = KworkMarketClient()
    try:
        return await client.get_market_metrics(
            category_id=category_id,
            classifier_id=classifier_id,
            include_demand=include_demand,
            include_competitor_details=include_competitor_details,
            competitor_detail_limit=competitor_detail_limit,
            page=page,
        )
    except Exception as exc:
        _raise_kwork_market_error(exc, context="metrics")
    finally:
        await client.close()


@router.post("/market/metrics")
async def kwork_market_metrics_post(payload: KworkMarketMetricsRequest) -> dict[str, Any]:
    client = KworkMarketClient()
    try:
        return await client.get_market_metrics(
            category_id=payload.category_id,
            classifier_id=payload.classifier_id,
            include_demand=payload.include_demand,
            include_competitor_details=payload.include_competitor_details,
            competitor_detail_limit=payload.competitor_detail_limit,
            page=payload.page,
            attribute_filters=payload.attribute_selection,
            attribute_controls=payload.attribute_controls,
        )
    except Exception as exc:
        _raise_kwork_market_error(exc, context="metrics post")
    finally:
        await client.close()


@router.post("/market/supply-scan", deprecated=True)
async def kwork_market_supply_scan(payload: KworkSupplyScanRequest) -> dict[str, Any]:
    scanner = KworkSupplyScanner()
    try:
        result = await scanner.scan(
            category_id=payload.category_id,
            category_name=payload.category_name,
            classifier_id=payload.classifier_id,
            classifier_name=payload.classifier_name,
            coverage=payload.coverage,
            include_llm=payload.include_llm,
            write_file=payload.write_file,
        )
        return {
            **result,
            "deprecation": {
                "code": "legacy_sync_supply_scan",
                "replacement": "/api/kwork/market/jobs",
                "message": "Use durable market jobs for new collection runs.",
            },
        }
    except Exception as exc:
        _raise_kwork_market_error(exc, context="supply scan")
    finally:
        await scanner.close()


@router.post("/market/assistant")
async def kwork_market_assistant(payload: KworkMarketAssistantRequest) -> dict[str, Any]:
    try:
        return await MarketAssistant().ask(payload.context_id, payload.message)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/market/intelligence-snapshot")
async def kwork_market_intelligence_snapshot(payload: KworkMarketIntelligenceRequest) -> dict[str, Any]:
    client = KworkMarketClient()
    try:
        return await client.get_market_intelligence_snapshot(
            seeds=payload.seeds,
            max_seeds=payload.max_seeds,
            pages=payload.pages,
            include_demand=payload.include_demand,
            demand_queries=payload.demand_queries,
            include_competitor_details=payload.include_competitor_details,
            competitor_detail_limit=payload.competitor_detail_limit,
            include_seller_details=payload.include_seller_details,
            seller_detail_limit=payload.seller_detail_limit,
            include_want_details=payload.include_want_details,
            want_detail_limit=payload.want_detail_limit,
            include_price_rules=payload.include_price_rules,
            include_account_context=payload.include_account_context,
            write_file=payload.write_file,
        )
    except Exception as exc:
        _raise_kwork_market_error(exc, context="intelligence snapshot")
    finally:
        await client.close()


@router.post(
    "/market/buyer-scout",
    deprecated=True,
    summary="Legacy buyer scout compatibility route",
)
async def kwork_market_buyer_scout(payload: KworkBuyerScoutRequest) -> dict[str, Any]:
    client = KworkMarketClient()
    try:
        return await client.get_buyer_scout(
            probes=payload.probes,
            category_id=payload.category_id,
            classifier_id=payload.classifier_id,
            category_name=payload.category_name,
            classifier_name=payload.classifier_name,
            attribute_selection=payload.attribute_selection,
            attribute_controls=payload.attribute_controls,
            max_probes=payload.max_probes,
            page=payload.page,
            project_page_limit=payload.project_page_limit,
            per_probe_limit=payload.per_probe_limit,
            top_limit=payload.top_limit,
            include_project_details=payload.include_project_details,
            include_want_details=payload.include_want_details,
            include_buyer_history=payload.include_buyer_history,
            detail_limit=payload.detail_limit,
            buyer_history_limit=payload.buyer_history_limit,
            budget_max=payload.budget_max,
            include_query_suggestions=payload.include_query_suggestions,
            query_suggestion_limit=payload.query_suggestion_limit,
            include_control_windows=payload.include_control_windows,
            control_window_limit=payload.control_window_limit,
            write_file=payload.write_file,
        )
    except Exception as exc:
        _raise_kwork_market_error(exc, context="buyer scout")
    finally:
        await client.close()


@router.get("/market/intelligence-history")
async def kwork_market_intelligence_history(limit: int = 50) -> dict[str, Any]:
    client = KworkMarketClient()
    try:
        return client.get_market_intelligence_history(limit=max(1, min(limit, 500)))
    except Exception as exc:
        _raise_kwork_market_error(exc, context="intelligence history")
    finally:
        await client.close()


@router.post("/autopublish/draft")
async def kwork_autopublish_draft(payload: KworkDraftRequest) -> dict[str, Any]:
    service = KworkAutopublishService()
    try:
        return await service.generate_draft(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/autopublish/preflight")
async def kwork_autopublish_preflight(payload: KworkPublishPreflightRequest) -> dict[str, Any]:
    service = KworkAutopublishService()
    return service.publish_preflight(payload.draft)


@router.get("/autopublish/session-check")
async def kwork_autopublish_session_check() -> dict[str, Any]:
    service = KworkAutopublishService()
    return await service.check_web_publish_session()


@router.post("/autopublish/publish")
async def kwork_autopublish_publish(payload: KworkPublishRequest) -> dict[str, Any]:
    service = KworkAutopublishService()
    try:
        return await service.publish_draft(
            payload.draft,
            dry_run=payload.dry_run,
            confirm_token=payload.confirm_token,
            confirmation=payload.confirmation,
        )
    except Exception as exc:
        logger.exception("Kwork autopublish route failed")
        return {
            "ok": False,
            "dry_run": payload.dry_run,
            "code": "publish_exception",
            "detail": f"{type(exc).__name__}: {exc}",
        }


@router.get("/autopublish/assets/{filename}")
async def kwork_autopublish_asset(filename: str):
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = (PROPOSAL_ASSETS_DIR / "kwork_autopublish" / filename).resolve()
    root = (PROPOSAL_ASSETS_DIR / "kwork_autopublish").resolve()
    if root not in path.parents or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path, media_type="image/png")


@router.get("/inspect/{project_id}")
async def inspect_kwork_project(project_id: str):
    if not project_id.isdigit():
        raise HTTPException(status_code=400, detail="project_id must be numeric")

    url = f"{KWORK_BASE_URL}/projects/{project_id}"
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        proxy=kwork_http_proxy_url(rotate=False),
        trust_env=False,
    ) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0 PSR-KworkInspector/1.0"})

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"Kwork returned HTTP {response.status_code}")

    project = KworkStateDataParser.project_from_html(response.text, project_id=project_id)
    state = KworkStateDataParser.extract(response.text)
    if not project:
        return {
            "ok": False,
            "url": url,
            "state_found": bool(state),
            "detail": "stateData found but wantData/wants were not parsed" if state else "stateData not found",
        }

    return {
        "ok": True,
        "url": url,
        "state_found": bool(state),
        "project": project.model_dump(),
    }
