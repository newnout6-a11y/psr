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

load_dotenv(override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.log_sink import make_ws_sink
from src.api.state import app_state
from src.api.routes import (
    orchestrator,
    candidates,
    settings,
    dashboard,
    osint,
    telegram,
    kwork,
    kwork_market_jobs,
    logs,
    chat,
)
from src.api import ws as ws_module
from src.paths import DATA_DIR, ensure_layout
from src.action.proposal_db import ProposalDB
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.identity_pool import MarketIdentityPool
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor
from src.platforms.kwork_supply.transports.vpnte import VpnteTransportManager
from src.utils.log_db import get_log_db


async def _market_web_cookies() -> dict[str, str]:
    """Read the current Session Hub cookies for durable web-catalog workers."""

    try:
        from src.platforms.kwork import get_kwork_service

        cookies = await get_kwork_service()._fetch_session_hub_cookies()
    except Exception as exc:
        logger.debug(f"Kwork market Session Hub cookies unavailable: {type(exc).__name__}: {exc}")
        return {}
    return {str(name): str(value) for name, value in cookies.items() if name and value}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_layout()
    ProposalDB()
    get_log_db()
    market_repository = MarketJobRepository(DATA_DIR / "kwork_market_jobs.db")
    market_coordinator = MarketScanCoordinator(market_repository)
    market_transports = VpnteTransportManager()
    market_identity_pool = MarketIdentityPool(market_repository, market_transports)
    market_executor = MarketOperationExecutor(
        market_coordinator,
        web_cookie_provider=_market_web_cookies,
        identity_pool=market_identity_pool,
    )
    market_supervisor = MarketWorkerSupervisor(
        market_coordinator,
        handlers=market_executor.handlers,
        transport_manager=market_transports,
        identity_pool=market_identity_pool,
    )
    await market_supervisor.start()
    app.state.market_jobs = market_coordinator
    app.state.market_worker_supervisor = market_supervisor
    app.state.market_identity_pool = market_identity_pool
    # Register loguru WebSocket sink
    logger.add(make_ws_sink(app_state), format="{time:HH:mm:ss} | {level} | {message}", level="DEBUG")
    logger.info("PSR API server started on port 7788")
    try:
        yield
    finally:
        logger.info("PSR API server shutting down")
        await market_supervisor.close()
        await market_repository.close()
        app.state.market_jobs = None
        app.state.market_worker_supervisor = None
        app.state.market_identity_pool = None
        if app_state.cycle_task and not app_state.cycle_task.done():
            app_state.cycle_task.cancel()
        try:
            from src.platforms.kwork import get_kwork_service

            await get_kwork_service().close()
        except Exception as exc:
            logger.debug(f"Kwork service shutdown cleanup skipped: {exc}")


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
app.include_router(kwork_market_jobs.router)
app.include_router(logs.router)
app.include_router(chat.router)
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
