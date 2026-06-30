"""Kwork diagnostics and project inspection."""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException

from src.platforms.kwork import KWORK_BASE_URL, KworkStateDataParser, get_kwork_service

router = APIRouter(prefix="/api/kwork", tags=["kwork"])


@router.get("/status")
async def kwork_status():
    service = get_kwork_service()
    api_ok = False
    api_error = None
    try:
        api = await service.get_api()
        if api:
            try:
                me = await api.get_me()
                api_ok = bool(me)
            except Exception as e:
                api_error = f"{type(e).__name__}: {e}"
    except Exception as e:
        api_error = f"{type(e).__name__}: {e}"

    return {
        "configured": bool(os.getenv("KWORK_EMAIL") and os.getenv("KWORK_PASSWORD")),
        "api_ok": api_ok,
        "api_error": api_error,
        "session_hub_url": os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies"),
        "session_hub_required": os.getenv("SESSION_HUB_REQUIRED", "false").lower() == "true",
        "state_parser": "window.stateData",
    }


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
