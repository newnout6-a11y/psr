"""Kwork diagnostics and project inspection."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field

from src.paths import PROPOSAL_ASSETS_DIR
from src.platforms.kwork import KWORK_BASE_URL, KworkStateDataParser, get_kwork_service
from src.platforms.kwork_autopublish import KworkAutopublishService
from src.platforms.kwork_listing import KworkWebListingClient
from src.platforms.kwork_market import KworkMarketClient

router = APIRouter(prefix="/api/kwork", tags=["kwork"])


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
    cover_text: str = ""
    cover_subtitle: str = ""
    cover_text_overlay: bool | None = None
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


class KworkFormManifestRequest(BaseModel):
    classifier_id: int | None = Field(default=None, ge=1)
    selection: dict[str, Any] = Field(default_factory=dict)
    lang: str = "ru"


class KworkMarketMetricsRequest(BaseModel):
    category_id: int = Field(..., ge=1)
    classifier_id: int | None = Field(default=None, ge=1)
    include_demand: bool = True
    include_competitor_details: bool = True
    competitor_detail_limit: int = Field(default=2, ge=0, le=12)
    page: int = Field(default=1, ge=1)
    attribute_selection: dict[str, Any] = Field(default_factory=dict)
    attribute_controls: list[dict[str, Any]] = Field(default_factory=list)


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
    return {
        "ok": True,
        "source": source,
        "control": name,
        "selected_ids": clean_ids,
        "selection": next_selection,
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


@router.get("/market/categories")
async def kwork_market_categories():
    client = KworkMarketClient()
    try:
        return await client.get_categories_tree()
    finally:
        await client.close()


@router.get("/market/category/{category_id}/attributes")
async def kwork_market_category_attributes(category_id: int):
    client = KworkMarketClient()
    try:
        return await client.get_category_attributes(category_id)
    finally:
        await client.close()


@router.get("/market/category/{category_id}/prices")
async def kwork_market_category_prices(category_id: int, attribute_id: int | None = None):
    client = KworkMarketClient()
    try:
        return await client.get_price_rules(category_id, attribute_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc)) from exc
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
    include_demand: bool = True,
    include_competitor_details: bool = True,
    competitor_detail_limit: int = Query(default=2, ge=0, le=12),
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
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
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
