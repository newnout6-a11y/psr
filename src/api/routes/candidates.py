"""Candidate queue management: list, approve, skip, snooze, edit."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api.state import app_state

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


class SnoozeRequest(BaseModel):
    minutes: int = 60


class EditTextRequest(BaseModel):
    proposal_text: str


class EditPriceRequest(BaseModel):
    chosen_price: str


class ApproveRequest(BaseModel):
    proposal_text: Optional[str] = None
    chosen_price: Optional[str] = None


def _db():
    from src.action.proposal_db import ProposalDB
    return ProposalDB()


@router.get("")
def list_candidates(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    page: int = 0,
    page_size: int = 30,
):
    import sqlite3
    from src.paths import PROPOSALS_DB_FILE

    if not PROPOSALS_DB_FILE.exists():
        return {"items": [], "total": 0}

    conditions = []
    params: list = []

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        placeholders = ", ".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if platform:
        conditions.append("platform = ?")
        params.append(platform)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = page * page_size

    with sqlite3.connect(str(PROPOSALS_DB_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        total_row = conn.execute(f"SELECT COUNT(*) AS n FROM candidates {where}", params).fetchone()
        total = int(total_row["n"]) if total_row else 0
        rows = conn.execute(
            f"""
            SELECT * FROM candidates {where}
            ORDER BY
                CASE status WHEN 'auto_ready' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                priority DESC, updated_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()

    items = []
    import json
    for row in rows:
        d = dict(row)
        for key in ("skills", "vet_reasons", "vet_red_flags", "competitor_prices", "client_context", "platform_data"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
        items.append(d)

    return {"items": items, "total": total}


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int):
    candidate = _db().get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Not found")
    return candidate


@router.post("/{candidate_id}/approve")
async def approve_candidate(candidate_id: int, req: ApproveRequest):
    orch = app_state.get_orchestrator()
    payload: dict = {}
    if req.proposal_text:
        payload["proposal_text"] = req.proposal_text
        _db().update_candidate(candidate_id, proposal_text=req.proposal_text)
    if req.chosen_price:
        payload["chosen_price"] = req.chosen_price
    msg = await orch.execute_candidate_action(candidate_id, "approve", payload or None)
    return {"ok": True, "message": msg}


@router.post("/{candidate_id}/skip")
def skip_candidate(candidate_id: int):
    db = _db()
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Not found")
    db.update_candidate_status(
        candidate_id, "skipped", actor="ui", reason="skipped by operator", manual_override=True
    )
    return {"ok": True}


@router.post("/{candidate_id}/snooze")
def snooze_candidate(candidate_id: int, req: SnoozeRequest):
    db = _db()
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Not found")
    db.snooze_candidate(candidate_id, req.minutes, actor="ui")
    return {"ok": True, "snoozed_minutes": req.minutes}


@router.patch("/{candidate_id}/text")
def edit_text(candidate_id: int, req: EditTextRequest):
    db = _db()
    if not db.get_candidate(candidate_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.update_candidate(candidate_id, proposal_text=req.proposal_text)
    return {"ok": True}


@router.patch("/{candidate_id}/price")
def edit_price(candidate_id: int, req: EditPriceRequest):
    db = _db()
    if not db.get_candidate(candidate_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.update_candidate(candidate_id, chosen_price=req.chosen_price)
    return {"ok": True}
