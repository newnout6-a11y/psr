"""Log buffer API."""

from __future__ import annotations

from fastapi import APIRouter

from src.api.state import app_state

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.delete("")
def clear_logs():
    app_state.clear_logs()
    return {"ok": True}
