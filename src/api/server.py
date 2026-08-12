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
    kwork_buyer_attachments,
    kwork_buyer_conversations,
    kwork_buyer_search,
    kwork_buyer_outreach,
    kwork_buyer_shadow,
    kwork_buyer_taxonomy,
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
from src.platforms.kwork_buyer.repository import BuyerRunNotFoundError, BuyerSearchRepository
from src.platforms.kwork_buyer.models import BuyerRunState
from src.platforms.kwork_supply.models import JobState
from src.platforms.kwork_buyer.conversation_persistence import SQLiteBuyerConversationStore
from src.platforms.kwork_buyer.conversation_copilot import (
    BuyerConversationCopilotController,
    LLMRouterBuyerConversationReplyGateway,
)
from src.platforms.kwork_buyer.conversation_sync import BuyerConversationSyncController
from src.platforms.kwork_buyer.conversation_send import BuyerConversationSendController
from src.platforms.kwork_buyer.conversation_send_runtime import AccountBoundBuyerConversationSendCapabilitiesFactory
from src.platforms.kwork_buyer.attachment_object_store import LocalBuyerAttachmentObjectStore
from src.platforms.kwork_buyer.attachment_vision import LLMRouterBuyerAttachmentVisionGateway
from src.platforms.kwork_buyer.enrichment import BuyerAttachmentEnrichmentPolicy
from src.platforms.kwork_buyer.enrichment_controller import BuyerAttachmentEnrichmentController
from src.platforms.kwork_buyer.outreach_controller import BuyerOutreachController
from src.platforms.kwork_buyer.outreach_preflight import BuyerAccountBoundPreflightVerifier
from src.platforms.kwork_buyer.outreach_persistence import SQLiteBuyerOutreachStore
from src.platforms.kwork_buyer.outreach_service import BuyerOutreachService
from src.platforms.kwork_buyer.proposal_delivery import BuyerAccountBoundProposalDeliveryController
from src.platforms.kwork_buyer.proposal_delivery_runtime import AccountBoundBuyerProposalDeliveryGatewayFactory
from src.platforms.kwork_buyer.proposal_composer import LLMRouterBuyerProposalGateway
from src.platforms.kwork_buyer.ai_scoring import LLMRouterBuyerFinalScoringGateway
from src.platforms.kwork_buyer.query_ai import LLMRouterBuyerQueryGenerationGateway
from src.platforms.kwork_buyer.runtime_adapters import (
    AccountBoundBuyerReadSourceFactory,
    MarketIdentityPoolDiscoveryAdapter,
)
from src.platforms.kwork_buyer.runtime_composition import (
    AccountBoundBuyerAttachmentCapabilitiesFactory,
    AccountBoundBuyerConversationCapabilitiesFactory,
    AccountBoundBuyerTaxonomyCapabilitiesFactory,
    AccountBoundBuyerKworkClientFactory,
    BuyerRunMarketJobResolver,
)
from src.platforms.kwork_buyer.service import (
    BuyerSearchService,
    BuyerSearchSettings,
    format_buyer_event_log,
    should_log_buyer_event,
)
from src.platforms.kwork_buyer.supervisor import BuyerDiscoverySupervisor
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity, BuyerDiscoveryRunResult
from src.platforms.kwork_buyer.taxonomy import BuyerTaxonomyService
from src.platforms.kwork_buyer.taxonomy_persistence import SQLiteBuyerTaxonomyStore
from src.platforms.kwork_buyer.taxonomy_refresh import BuyerTaxonomyRefreshController
from src.platforms.kwork_buyer.shadow_persistence import SQLiteBuyerShadowStore
from src.utils.log_db import get_log_db


class _LazyBuyerProposalGateway:
    """Construct the task-routed LLM gateway only when an operator asks for a draft."""

    async def generate(self, *, prompt: str, task: str):
        from src.brain.llm_router import get_llm_router

        return await LLMRouterBuyerProposalGateway(get_llm_router()).generate(prompt=prompt, task=task)


class _LazyBuyerConversationReplyGateway:
    """Resolve the task-routed conversation model only for an explicit AI draft."""

    async def generate(self, *, prompt: str, task: str):
        from src.brain.llm_router import get_llm_router

        return await LLMRouterBuyerConversationReplyGateway(get_llm_router()).generate(prompt=prompt, task=task)


class _LazyBuyerFinalScoringGateway:
    """Resolve the configured scoring route only for an explicit final-score request."""

    async def generate(self, *, prompt: str, task: str):
        from src.brain.llm_router import get_llm_router

        return await LLMRouterBuyerFinalScoringGateway(get_llm_router()).generate(prompt=prompt, task=task)


class _LazyBuyerQueryGenerationGateway:
    """Resolve the configured planner route only for non-manual query generation."""

    async def generate(self, *, prompt: str, task: str):
        from src.brain.llm_router import get_llm_router

        return await LLMRouterBuyerQueryGenerationGateway(get_llm_router()).generate(prompt=prompt, task=task)


class _LazyBuyerAttachmentVisionGateway:
    """Create the multimodal route only when an enabled attachment reaches vision."""

    async def analyze_image(self, *, content: bytes, metadata):
        from src.brain.llm_router import get_llm_router

        return await LLMRouterBuyerAttachmentVisionGateway(get_llm_router()).analyze_image(
            content=content,
            metadata=metadata,
        )


def _capped_buyer_recovery_workers(value: object, *, max_workers: int) -> int:
    """Clamp stale durable runs to the live Buyer Search worker ceiling."""

    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 30:
        raise ValueError("max_workers must be an integer between 1 and 30")
    if isinstance(value, bool):
        requested = 1
    else:
        try:
            requested = int(value)
        except (TypeError, ValueError):
            requested = 1
    return min(max(1, requested), max_workers)


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
    # Start the shared log stream before runtime recovery so automatic Buyer
    # fleet events are retained for the desktop Logs view as well.
    logger.add(make_ws_sink(app_state), format="{time:HH:mm:ss} | {level} | {message}", level="DEBUG")
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
    buyer_repository = BuyerSearchRepository(DATA_DIR / "kwork_buyer_search.db")
    await buyer_repository.initialize()
    buyer_shadow_store = SQLiteBuyerShadowStore(DATA_DIR / "kwork_buyer_shadow.db")
    await buyer_shadow_store.initialize()
    buyer_taxonomy_store = SQLiteBuyerTaxonomyStore(DATA_DIR / "kwork_buyer_taxonomy.db")
    await buyer_taxonomy_store.initialize()
    buyer_taxonomy = BuyerTaxonomyService(buyer_taxonomy_store)
    buyer_settings = BuyerSearchSettings.from_env()
    buyer_job_resolver = BuyerRunMarketJobResolver(market_repository)
    buyer_identity_adapter = MarketIdentityPoolDiscoveryAdapter(
        market_identity_pool,
        job_id_for_run=buyer_job_resolver,
    )
    buyer_account_client_factory = AccountBoundBuyerKworkClientFactory(market_transports)
    buyer_source_factory = AccountBoundBuyerReadSourceFactory(
        market_identity_pool,
        account_client_factory=buyer_account_client_factory,
    )

    async def _buyer_event_hook(run_id: str, event_type: str, payload: dict[str, object]) -> None:
        await buyer_repository.append_event(run_id, event_type, payload)
        if should_log_buyer_event(event_type):
            log = logger.error if event_type in {"query.page.failed", "worker.failed"} else logger.info
            log(format_buyer_event_log(run_id, event_type, payload))
        if event_type in {"query.page.completed", "query.page.failed"}:
            buyer_service.schedule_completion_check(run_id)

    async def _buyer_result_hook(
        run_id: str,
        identity: BuyerDiscoveryIdentity,
        result: BuyerDiscoveryRunResult,
    ) -> None:
        outcome = getattr(getattr(result, "outcome", None), "value", None)
        event_type = {
            "committed": "query.page.completed",
            "retry": "query.page.retry",
            "failed": "query.page.failed",
        }.get(outcome)
        if event_type and should_log_buyer_event(event_type):
            payload = {
                "worker_id": getattr(identity, "worker_id", None),
                "task_id": getattr(result, "task_id", None),
                "project_count": getattr(result, "project_count", 0),
                "error": getattr(result, "error", None),
            }
            log = logger.error if outcome == "failed" else logger.info
            log(format_buyer_event_log(run_id, event_type, payload))
        if outcome in {"committed", "failed"}:
            buyer_service.schedule_completion_check(run_id)

    buyer_supervisor = BuyerDiscoverySupervisor(
        buyer_repository,
        identity_allocator=buyer_identity_adapter,
        identity_releaser=buyer_identity_adapter,
        source_factory=buyer_source_factory,
        capacity_inventory=buyer_identity_adapter,
        event_hook=_buyer_event_hook,
        heartbeat_hook=buyer_identity_adapter.heartbeat,
        result_hook=_buyer_result_hook,
        max_workers=buyer_settings.max_workers,
    )

    async def _initialize_buyer_runtime(run: dict[str, object], account_registration_ids: tuple[str, ...]) -> str:
        return await buyer_job_resolver.ensure_for_run(
            run,
            account_registration_ids=account_registration_ids,
        )

    async def _buyer_rollout_gate_checker(run_id: str, requested_workers: int) -> dict[str, object]:
        gate = await buyer_shadow_store.latest_approved_gate(run_id, target_workers=requested_workers)
        if gate is not None:
            return gate
        return {
            "allowed": False,
            "blockers": [f"no approved durable shadow gate for {requested_workers} workers"],
        }

    async def _buyer_taxonomy_context(category_scope: dict[str, object]) -> dict[str, object]:
        """Supply a bounded durable taxonomy summary to the query planner."""

        raw_category_id = category_scope.get("category_id")
        if isinstance(raw_category_id, bool):
            return {}
        try:
            category_id = int(raw_category_id) if raw_category_id is not None else 0
        except (TypeError, ValueError):
            return {}
        if category_id <= 0:
            return {}
        raw_snapshot_id = category_scope.get("taxonomy_snapshot_id")
        snapshot_id = raw_snapshot_id.strip() if isinstance(raw_snapshot_id, str) and raw_snapshot_id.strip() else None
        try:
            context = await buyer_taxonomy.get_category_context(
                category_id,
                snapshot_id=snapshot_id,
                child_limit=50,
                term_limit=150,
            )
        except Exception as exc:  # noqa: BLE001 - a stale snapshot must not block deterministic planning.
            logger.debug(f"Buyer taxonomy context unavailable for category {category_id}: {type(exc).__name__}: {exc}")
            return {}

        snapshot = context.get("snapshot") if isinstance(context.get("snapshot"), dict) else {}
        category = context.get("category") if isinstance(context.get("category"), dict) else {}
        vocabulary: list[dict[str, object]] = []
        raw_vocabulary = context.get("vocabulary")
        if isinstance(raw_vocabulary, list):
            for item in raw_vocabulary[:200]:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                vocabulary.append(
                    {
                        "text": text.strip()[:300],
                        "source": str(item.get("source") or "taxonomy")[:100],
                        "category_id": item.get("category_id"),
                    }
                )
        return {
            "snapshot_id": snapshot.get("snapshot_id"),
            "snapshot_source": snapshot.get("source"),
            "category": {
                "category_id": category.get("category_id"),
                "name": category.get("name"),
                "category_path": category.get("category_path"),
            },
            "vocabulary": vocabulary,
        }

    buyer_service = BuyerSearchService(
        buyer_repository,
        export_root=DATA_DIR / "kwork_buyer_exports",
        settings=buyer_settings,
        supervisor=buyer_supervisor,
        runtime_initializer=_initialize_buyer_runtime,
        runtime_finalizer=buyer_job_resolver.finalize_for_run,
        query_generation_gateway=_LazyBuyerQueryGenerationGateway(),
        taxonomy_context_provider=_buyer_taxonomy_context,
        final_scoring_gateway=_LazyBuyerFinalScoringGateway(),
        rollout_gate_checker=_buyer_rollout_gate_checker if buyer_settings.require_shadow_gate else None,
    )
    buyer_outreach_store = SQLiteBuyerOutreachStore(DATA_DIR / "kwork_buyer_outreach.db")
    await buyer_outreach_store.initialize()
    buyer_conversation_store = SQLiteBuyerConversationStore(DATA_DIR / "kwork_buyer_conversations.db")
    await buyer_conversation_store.initialize()
    buyer_attachment_store = LocalBuyerAttachmentObjectStore(DATA_DIR / "kwork_buyer_attachments")
    buyer_attachment_capabilities_factory = AccountBoundBuyerAttachmentCapabilitiesFactory(
        buyer_supervisor,
        market_identity_pool,
        buyer_account_client_factory,
    )
    buyer_taxonomy_refresh = BuyerTaxonomyRefreshController(
        buyer_taxonomy,
        reader_factory=AccountBoundBuyerTaxonomyCapabilitiesFactory(
            buyer_supervisor,
            market_identity_pool,
            buyer_account_client_factory,
        ),
        settings=buyer_service.settings,
    )
    buyer_enrichment = BuyerAttachmentEnrichmentController(
        buyer_service,
        capabilities_factory=buyer_attachment_capabilities_factory,
        settings=buyer_service.settings,
        policy=BuyerAttachmentEnrichmentPolicy(require_original_object_ref=True),
        original_writer=buyer_attachment_store,
        vision_gateway=_LazyBuyerAttachmentVisionGateway(),
    )
    buyer_outreach_service = BuyerOutreachService(buyer_outreach_store, _LazyBuyerProposalGateway())
    buyer_outreach = BuyerOutreachController(
        buyer_service,
        buyer_outreach_service,
        settings=buyer_service.settings,
        preflight_evidence_verifier=BuyerAccountBoundPreflightVerifier(buyer_attachment_capabilities_factory),
    )
    buyer_proposal_delivery = BuyerAccountBoundProposalDeliveryController(
        buyer_outreach_service,
        AccountBoundBuyerProposalDeliveryGatewayFactory(
            buyer_supervisor,
            market_identity_pool,
            buyer_account_client_factory,
            project_resolver=buyer_service.get_project,
        ),
        delivery_enabled=buyer_settings.proposal_send,
    )
    buyer_conversation_copilot = BuyerConversationCopilotController(
        buyer_conversation_store,
        _LazyBuyerConversationReplyGateway(),
        project_loader=buyer_service,
        proposal_loader=buyer_outreach.outreach_service,
    )
    buyer_conversation_sync = BuyerConversationSyncController(
        buyer_conversation_store,
        reader_factory=AccountBoundBuyerConversationCapabilitiesFactory(
            buyer_supervisor,
            market_identity_pool,
            buyer_account_client_factory,
        ),
        settings=buyer_service.settings,
    )
    buyer_conversation_send = BuyerConversationSendController(
        buyer_conversation_store,
        sender_factory=AccountBoundBuyerConversationSendCapabilitiesFactory(
            buyer_supervisor,
            market_identity_pool,
            buyer_account_client_factory,
        ),
        enabled=buyer_service.settings.conversation_send,
    )
    await market_supervisor.start()
    recent_buyer_runs = await buyer_repository.list_runs(limit=500)

    # Buyer backing jobs own account/VPNTE leases only. Older versions did not
    # close them when a Buyer run ended or was deleted, leaving stale active
    # rows in the Market workspace after restarts.
    active_market_states = (
        JobState.PREPARING,
        JobState.MAPPING,
        JobState.PLANNING,
        JobState.RUNNING,
        JobState.PAUSING,
        JobState.COMPLETING,
        JobState.ENRICHING,
        JobState.ANALYZING,
        JobState.FINALIZING,
        JobState.STOPPING,
    )
    active_backing_jobs = await market_repository.list_jobs(states=active_market_states, limit=1_000)
    terminal_buyer_states = {
        BuyerRunState.COMPLETED.value,
        BuyerRunState.STOPPED.value,
        BuyerRunState.FAILED.value,
    }
    for backing_job in active_backing_jobs:
        if str(backing_job.get("job_kind") or "") != "buyer_search":
            continue
        config = backing_job.get("workflow_config")
        run_id = str(config.get("buyer_run_id") or "").strip() if isinstance(config, dict) else ""
        terminal_reason: str | None = None
        if not run_id:
            terminal_reason = "orphaned_buyer_backing_job"
        else:
            try:
                durable_run = await buyer_repository.get_run(run_id)
            except BuyerRunNotFoundError:
                terminal_reason = "orphaned_buyer_backing_job"
            else:
                if str(durable_run.get("state") or "") in terminal_buyer_states:
                    terminal_reason = f"buyer_run_{durable_run.get('state')}"
        if terminal_reason and run_id:
            await buyer_job_resolver.finalize_for_run(run_id, terminal_reason)
        elif terminal_reason:
            await market_repository.update_job_state(
                str(backing_job["job_id"]),
                JobState.STOPPED,
                phase=str(backing_job.get("phase") or "prepare"),
                expected_revision=int(backing_job.get("revision") or 0),
                last_warning=terminal_reason,
            )
    for run in recent_buyer_runs:
        await buyer_service.reconcile_task_counters(str(run["run_id"]))

    # The desktop log is intentionally in-memory.  Rehydrate a bounded set of
    # durable terminal task errors so an operator can still see why the last
    # runs ended after the API process is restarted.
    diagnostic_budget = 100
    for run in recent_buyer_runs:
        if diagnostic_budget <= 0:
            break
        emitted = await buyer_service.replay_failed_task_diagnostics(
            str(run["run_id"]),
            limit=diagnostic_budget,
        )
        diagnostic_budget -= emitted

    if buyer_service.settings.live_discovery:
        recovery_runs = [run for run in recent_buyer_runs if run.get("state") == "running"]
        for run in recovery_runs:
            await buyer_service.recover_malformed_page_tasks(str(run["run_id"]))
        for run in recovery_runs:
            try:
                config = run.get("config") if isinstance(run.get("config"), dict) else {}
                account_registration_ids = tuple(
                    str(value).strip()
                    for value in config.get("account_registration_ids", [])
                    if str(value).strip()
                )
                job_id = await _initialize_buyer_runtime(dict(run), account_registration_ids)
                if run.get("job_id") != job_id:
                    await buyer_repository.update_run(str(run["run_id"]), {"job_id": job_id})
                requested_workers = _capped_buyer_recovery_workers(
                    run.get("requested_workers"),
                    max_workers=buyer_settings.max_workers,
                )
                if buyer_settings.require_shadow_gate:
                    gate = await _buyer_rollout_gate_checker(str(run["run_id"]), requested_workers)
                    if gate.get("allowed") is not True:
                        blockers = gate.get("blockers")
                        detail = "; ".join(str(item) for item in blockers or () if str(item).strip())
                        await buyer_repository.update_run(
                            str(run["run_id"]),
                            {"state": "blocked", "last_error": detail or "shadow rollout gate has not approved this canary"},
                        )
                        continue
                fleet = await buyer_supervisor.start(
                    str(run["run_id"]),
                    requested_workers=requested_workers,
                )
                if fleet.state.value == "blocked":
                    await buyer_repository.update_run(str(run["run_id"]), {"state": "blocked"})
                else:
                    # A run may have been restarted while its previous wave was
                    # already terminal.  Re-enter the target controller so it
                    # immediately creates the next wave instead of staying idle.
                    buyer_service.schedule_completion_check(str(run["run_id"]))
            except Exception as exc:  # noqa: BLE001 - one stale Buyer run must not prevent API startup.
                logger.warning(f"Buyer Search runtime recovery skipped for {run.get('run_id')}: {type(exc).__name__}: {exc}")
    app.state.market_jobs = market_coordinator
    app.state.market_worker_supervisor = market_supervisor
    app.state.market_identity_pool = market_identity_pool
    app.state.buyer_search = buyer_service
    app.state.buyer_outreach = buyer_outreach
    app.state.buyer_proposal_delivery = buyer_proposal_delivery
    app.state.buyer_conversations = buyer_conversation_store
    app.state.buyer_conversation_copilot = buyer_conversation_copilot
    app.state.buyer_conversation_sync = buyer_conversation_sync
    app.state.buyer_conversation_send = buyer_conversation_send
    app.state.buyer_enrichment = buyer_enrichment
    app.state.buyer_attachment_store = buyer_attachment_store
    app.state.buyer_shadow = buyer_shadow_store
    app.state.buyer_taxonomy = buyer_taxonomy
    app.state.buyer_taxonomy_refresh = buyer_taxonomy_refresh
    logger.info("PSR API server started on port 7788")
    try:
        yield
    finally:
        logger.info("PSR API server shutting down")
        await buyer_taxonomy_store.close()
        await buyer_shadow_store.close()
        await buyer_conversation_store.close()
        await buyer_outreach_store.close()
        await buyer_service.close()
        await buyer_repository.close()
        await market_supervisor.close()
        await market_repository.close()
        app.state.market_jobs = None
        app.state.market_worker_supervisor = None
        app.state.market_identity_pool = None
        app.state.buyer_search = None
        app.state.buyer_outreach = None
        app.state.buyer_proposal_delivery = None
        app.state.buyer_conversations = None
        app.state.buyer_conversation_copilot = None
        app.state.buyer_conversation_sync = None
        app.state.buyer_conversation_send = None
        app.state.buyer_enrichment = None
        app.state.buyer_attachment_store = None
        app.state.buyer_shadow = None
        app.state.buyer_taxonomy = None
        app.state.buyer_taxonomy_refresh = None
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
app.include_router(kwork_buyer_attachments.router)
app.include_router(kwork_buyer_search.router)
app.include_router(kwork_buyer_search.ws_router)
app.include_router(kwork_buyer_outreach.router)
app.include_router(kwork_buyer_conversations.router)
app.include_router(kwork_buyer_shadow.router)
app.include_router(kwork_buyer_taxonomy.router)
app.include_router(kwork_buyer_taxonomy.refresh_router)
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
