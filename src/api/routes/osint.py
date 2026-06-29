"""OSINT manual client check endpoint."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/osint", tags=["osint"])


class OSINTCheckRequest(BaseModel):
    username: str
    description: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    telegram: Optional[str] = None


@router.post("/check")
async def check_client(req: OSINTCheckRequest):
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="username is required")
    try:
        from src.osint import OSINTAggregator

        agg = OSINTAggregator()
        result = await agg.gather(
            req.username.strip(),
            email=req.email or None,
            phone=req.phone or None,
            telegram=req.telegram or None,
            description=req.description,
        )
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
