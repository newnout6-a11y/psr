"""WebSocket endpoints for real-time logs and status updates."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.state import app_state

router = APIRouter(tags=["ws"])


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    app_state.log_queues.add(q)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_text(json.dumps(msg, ensure_ascii=False))
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        app_state.log_queues.discard(q)


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    app_state.status_queues.add(q)
    # Send current status immediately on connect
    try:
        await websocket.send_text(
            json.dumps({"type": "status", **app_state.to_status_dict()})
        )
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_text(json.dumps(msg, ensure_ascii=False))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        app_state.status_queues.discard(q)
