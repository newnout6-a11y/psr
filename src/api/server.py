"""
FastAPI backend for PSR Desktop App.

Run:
    python -m src.api.server
or:
    uvicorn src.api.server:app --host 127.0.0.1 --port 7788 --reload
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.log_sink import make_ws_sink
from src.api.state import app_state
from src.api.routes import orchestrator, candidates, settings, dashboard, osint, telegram, kwork, logs
from src.api import ws as ws_module
from src.paths import ensure_layout
from src.action.proposal_db import ProposalDB
from src.utils.log_db import get_log_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_layout()
    ProposalDB()
    get_log_db()
    # Register loguru WebSocket sink
    logger.add(make_ws_sink(app_state), format="{time:HH:mm:ss} | {level} | {message}", level="DEBUG")
    logger.info("PSR API server started on port 7788")
    yield
    logger.info("PSR API server shutting down")
    if app_state.cycle_task and not app_state.cycle_task.done():
        app_state.cycle_task.cancel()


app = FastAPI(title="PSR Desktop API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(orchestrator.router)
app.include_router(candidates.router)
app.include_router(settings.router)
app.include_router(dashboard.router)
app.include_router(osint.router)
app.include_router(telegram.router)
app.include_router(kwork.router)
app.include_router(logs.router)
app.include_router(ws_module.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.api.server:app",
        host="127.0.0.1",
        port=7788,
        reload=False,
        log_level="info",
    )
