"""
Оркестратор PSR vNext.

Основной поток теперь queue-driven:
parsed -> filtered -> scored -> vetted -> queued|auto_ready -> sent|skipped|draft
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any

from loguru import logger

from src.action.decision_policy import CandidateDecisionContext, DecisionPolicy
from src.action.proposal_db import ProposalDB
from src.action.proposal_generator import ProposalGenerator
from src.action.proposal_image import ProposalImageGenerator
from src.action.proposal_sender import ProposalSender
from src.brain.nlp_filter import NLPFilter
from src.brain.rag_pipeline import RAGPipeline
from src.brain.search_strategy import SearchStrategy
from src.filter.ai_scorer import AIRelevanceScorer, ScoreResult
from src.filter.client_vetter import ClientVetter
from src.filter.project_filter import ProjectFilter
from src.osint import OSINTAggregator
from src.parsers import FreelanceRuParser, HHParser, KworkAPIParser
from src.parsers.base_parser import ProjectItem
from src.paths import GENERATED_PROPOSALS_FILE
from src.utils.circuit_breaker import get_breaker
from src.utils.inbox_monitor import InboxMonitor
from src.utils.log_db import get_log_db
from src.utils.notifier import TelegramNotifier


AUTO_SEND_PLATFORMS = {"kwork", "freelance_ru"}
ACTIVE_PROCESSING_STATUSES = {"parsed", "filtered", "scored", "vetted", "auto_ready", "error", "sending"}
BLOCKING_STATUSES = {"queued", "snoozed", "manual_sent", "auto_sent", "draft", "skipped", "sending"}

_BUSINESS_REJECTION_MARKERS = {
    "project closed",
    "закрыт",
    "already responded",
    "уже отправл",
    "too short",
    "слишком коротк",
    "минимальная цена",
    "min price",
    "not enough connects",
    "недостаточно connect",
}


def _is_business_rejection(error_msg: str) -> bool:
    """Check if error is a per-project business rejection (not infrastructure failure).

    Business rejections (project closed, already responded, too short)
    should NOT count toward the circuit breaker threshold.
    """
    msg_lower = (error_msg or "").lower()
    return any(marker in msg_lower for marker in _BUSINESS_REJECTION_MARKERS)


REPROCESS_RESET_FIELDS: dict[str, Any] = {
    "ai_pre_score": None,
    "ai_score": None,
    "ai_score_source": None,
    "ai_reason": None,
    "vet_score": None,
    "vet_passed": None,
    "vet_reasons": [],
    "vet_red_flags": [],
    "decision_reason": None,
    "auto_eligible": 0,
    "risk_level": "medium",
    "priority": 0,
    "provider": None,
    "proposal_text": None,
    "competitor_prices": [],
    "client_context": {},
    "chosen_price": None,
    "manual_override": 0,
    "last_actor": "system",
    "snoozed_until": None,
    "sent_at": None,
}


def _env_int(name: str, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _normalize_route_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    return "" if provider == "auto" else provider


def _route_provider_from_env(task: str) -> str:
    task_key = task.upper().replace("-", "_")
    provider = _normalize_route_provider(os.getenv(f"{task_key}_PROVIDER"))
    if provider:
        return provider
    if task in {"query_generation", "scoring"}:
        provider = _normalize_route_provider(os.getenv("PARSER_LLM_PROVIDER"))
        if provider:
            return provider
    return os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto"


def _model_compatible_with_provider(provider: str, model: str) -> bool:
    normalized = (model or "").strip().lower()
    if not normalized:
        return True
    if provider == "deepseek":
        return normalized.startswith("deepseek-")
    if provider == "openai":
        return not normalized.startswith(("deepseek-", "llama-", "mixtral-", "gemma-"))
    if provider == "groq":
        return not normalized.startswith(("deepseek-", "gpt-"))
    return True


def _provider_model_from_env(provider: str, *, task: str | None = None) -> str:
    provider = (provider or "auto").strip().lower()
    task_key = task.upper().replace("-", "_") if task else ""
    provider_key = provider.upper()

    candidate_keys: list[str] = []
    if task_key:
        candidate_keys.extend(
            [
                f"{provider_key}_MODEL_{task_key}",
                f"{task_key}_MODEL_{provider_key}",
            ]
        )
        parser_provider = _normalize_route_provider(os.getenv("PARSER_LLM_PROVIDER"))
        if task in {"query_generation", "scoring"} and parser_provider == provider:
            candidate_keys.append("PARSER_LLM_MODEL")
    candidate_keys.append(f"{provider_key}_MODEL")
    if task_key:
        candidate_keys.append(f"{task_key}_MODEL")

    for env_key in candidate_keys:
        value = os.getenv(env_key, "").strip()
        if value and _model_compatible_with_provider(provider, value):
            return value

    if provider == "openai":
        return "gpt-5.5"
    if provider == "deepseek":
        return "deepseek-v4-pro"
    if provider == "groq":
        if task == "query_generation":
            return "llama-3.1-8b-instant"
        return "llama-3.3-70b-versatile"
    return "auto"


def _limit_payloads_per_platform(
    payloads: list[dict[str, Any]], limit_per_platform: int
) -> tuple[list[dict[str, Any]], int]:
    limit = max(1, int(limit_per_platform or 1))
    counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    limited_out = 0

    for payload in payloads:
        platform = payload["project"].platform
        if counts.get(platform, 0) >= limit:
            limited_out += 1
            continue
        selected.append(payload)
        counts[platform] = counts.get(platform, 0) + 1

    return selected, limited_out


class FreelanceOrchestrator:
    """Управляет поиском, оценкой, постановкой в очередь и отправкой откликов."""

    def __init__(self):
        self.parsers: list[Any] = []
        self._init_parsers()

        self.db = ProposalDB()
        self.policy = DecisionPolicy(auto_send_platforms=AUTO_SEND_PLATFORMS)
        self.filter = ProjectFilter()
        self.nlp = NLPFilter()
        self.rag = RAGPipeline()
        self.rag.initialize()
        self.ai_scorer = AIRelevanceScorer()
        self.client_vetter = ClientVetter()
        self.osint = OSINTAggregator() if os.getenv("OSINT_ENABLED", "false").lower() == "true" else None
        self.generator = ProposalGenerator(rag_pipeline=self.rag)
        self.image_generator = ProposalImageGenerator()

        headless_env = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
        self.sender = ProposalSender(headless=headless_env)
        self.search_strategy = SearchStrategy(db=self.db)
        self.breaker = get_breaker()

        async def _on_breaker_open(key: str, state: str, seconds: int, error: str):
            logger.warning(f"Breaker alert: {key} {state} for {seconds}s: {error}")
            if self.notifier:
                await self.notifier.send_text(f"BREAKER: {key} → {state} на {seconds}s\nПричина: {error}")

        from src.utils.circuit_breaker import CircuitBreaker

        CircuitBreaker._on_state_change = _on_breaker_open

        self.notifier = TelegramNotifier(db=self.db)
        self.notifier.bind_runtime(db=self.db, candidate_executor=self.execute_candidate_action)
        self.inbox_monitor = InboxMonitor(notifier=self.notifier, db=self.db)

        self._cycle_sent_count = 0
        self._cycle_sent_per_platform: dict[str, int] = {}

        self._ensure_runtime_defaults()
        logger.info(f"Оркестратор инициализирован. Активно парсеров: {len(self.parsers)}")

        if os.getenv("KWORK_FAST_INBOX_POLLING", "true").lower() in {"1", "true", "yes", "on"}:
            asyncio.create_task(self.inbox_monitor.start_fast_polling())

    def _ensure_runtime_defaults(self) -> None:
        if not self.db.get_runtime_state("execution_mode"):
            self.db.set_runtime_mode(os.getenv("EXECUTION_MODE", "semi_auto").strip().lower())

    def _execution_mode(self) -> str:
        return self.db.get_runtime_mode(os.getenv("EXECUTION_MODE", "semi_auto"))

    def _init_parsers(self) -> None:
        platforms_env = os.getenv("PLATFORMS", "kwork,freelance_ru,hh_ru")
        active_names = [p.strip().lower() for p in platforms_env.split(",") if p.strip()]

        parser_map = {
            "hh_ru": HHParser,
            "kwork": KworkAPIParser,
            "freelance_ru": FreelanceRuParser,
        }

        for name in active_names:
            if name not in parser_map:
                logger.warning(f"Неизвестная платформа в PLATFORMS: {name}")
                continue
            try:
                parser = parser_map[name]()
                self.parsers.append(parser)
            except Exception as e:
                logger.error(f"Ошибка инициализации парсера {name}: {e}")

    async def _parse_projects(self, parser, page: int, per_page: int, query: str):
        started_at = time.perf_counter()
        try:
            projects = await parser.get_projects(page=page, per_page=per_page, filters={"query": query})
            for project in projects:
                project.search_query = query
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return parser.PLATFORM_NAME, query, page, projects, duration_ms, None
        except Exception as e:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return parser.PLATFORM_NAME, query, page, [], duration_ms, e

    async def _discover_wide_kwork(
        self,
        parser,
        queries: list[str],
        *,
        per_page: int,
        max_pages_per_query: int,
        max_projects_per_cycle: int,
        max_parse_seconds: int,
    ) -> tuple[list[tuple[str, str, int, list[ProjectItem], int, Any]], dict[str, Any]]:
        started_at = time.perf_counter()
        results: list[tuple[str, str, int, list[ProjectItem], int, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        seen_keys_per_query: dict[str, set[tuple[str, str]]] = {}
        query_stops: dict[str, str] = {}
        stop_reason = "completed"

        duplicate_ratio_limit = float(os.getenv("WIDE_DUPLICATE_STOP_RATIO", "0.8"))
        duplicate_page_limit = _env_int("WIDE_DUPLICATE_STOP_PAGES", 2, low=1, high=5)

        for query in queries:
            duplicate_pages = 0
            query_stop = "max_pages"
            for page in range(1, max_pages_per_query + 1):
                if time.perf_counter() - started_at >= max_parse_seconds:
                    query_stop = "time_limit"
                    stop_reason = "time_limit"
                    break

                platform, q, p, projects, duration_ms, error = await self._parse_projects(
                    parser,
                    page,
                    per_page,
                    query,
                )
                results.append((platform, q, p, projects, duration_ms, error))

                if error:
                    query_stop = f"error:{type(error).__name__}"
                    break

                if not projects:
                    query_stop = "empty_page"
                    break

                new_on_page = 0
                query_seen = seen_keys_per_query.setdefault(query, set())
                for project in projects:
                    key = self._project_key(project)
                    if key not in query_seen:
                        query_seen.add(key)
                        seen_keys.add(key)
                        new_on_page += 1

                if len(seen_keys) >= max_projects_per_cycle:
                    query_stop = "max_projects"
                    stop_reason = "max_projects"
                    break

                duplicate_ratio = 1.0 - (new_on_page / max(len(projects), 1))
                if duplicate_ratio >= duplicate_ratio_limit:
                    duplicate_pages += 1
                else:
                    duplicate_pages = 0

                if duplicate_pages >= duplicate_page_limit:
                    query_stop = "duplicate_heavy"
                    break

            query_stops[query] = query_stop
            if stop_reason in {"time_limit", "max_projects"}:
                break

        stats = {
            "mode": "wide",
            "queries": len(queries),
            "pages_scanned": len(results),
            "raw_found": sum(len(item[3]) for item in results),
            "deduped": len(seen_keys),
            "stop_reason": stop_reason,
            "query_stops": query_stops,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        }
        logger.info(
            "Discovery wide kwork: "
            f"queries={stats['queries']} pages={stats['pages_scanned']} raw={stats['raw_found']} "
            f"deduped={stats['deduped']} stop={stats['stop_reason']} elapsed={stats['elapsed_ms']}ms"
        )
        return results, stats

    def _project_key(self, project: ProjectItem) -> tuple[str, str]:
        return project.platform, project.id

    def _stage_candidate(self, project: ProjectItem, stage: str, status: str, **fields: Any) -> int:
        mode = self._execution_mode()
        payload = {"stage": stage, "status": status, "execution_mode": mode}
        if stage == "parsed" and status == "parsed":
            payload.update(REPROCESS_RESET_FIELDS)
        payload.update(fields)
        return self.db.upsert_candidate(project, **payload)

    def _safe_client_context(
        self,
        client_data: dict[str, Any] | None,
        osint_result: Any,
        vet_result: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "vet_score": vet_result.get("score"),
            "reasons": vet_result.get("reasons", []),
            "red_flags": vet_result.get("red_flags", []),
        }
        if client_data:
            payload["client"] = {
                "username": client_data.get("username"),
                "rating": client_data.get("rating"),
                "completed_orders_count": client_data.get("completed_orders_count"),
                "good_reviews": client_data.get("good_reviews"),
                "bad_reviews": client_data.get("bad_reviews"),
                "online": client_data.get("online"),
            }
        if osint_result is not None:
            payload["osint"] = {
                "summary": getattr(osint_result, "summary", ""),
                "reputation_score": getattr(osint_result, "reputation_score", 50),
                "positive_signals": getattr(osint_result, "positive_signals", []),
                "red_flags": getattr(osint_result, "red_flags", []),
            }
        return payload

    def _client_hint_from_project(self, project: ProjectItem) -> dict[str, Any] | None:
        """Build a weak buyer profile from Kwork stateData before API enrichment."""
        platform_data = getattr(project, "platform_data", {}) or {}
        user = platform_data.get("user") if isinstance(platform_data, dict) else None
        if not isinstance(user, dict):
            return None
        data = user.get("data") if isinstance(user.get("data"), dict) else {}
        return {
            "username": user.get("username") or "",
            "completed_orders_count": None,
            "order_done_repeat_persent": project.client_hired_percent or data.get("wants_hired_percent") or 0,
            "wants_count": data.get("wants_count"),
            "badges": user.get("badges") or [],
            "profile_url": user.get("profile_url"),
            "source": "kwork_state_data",
        }

    def _is_blocked_by_existing_status(self, project: ProjectItem) -> bool:
        existing = self.db.get_candidate_by_project(project.id, project.platform)
        return self._is_blocked_existing_candidate(existing)

    def _is_blocked_existing_candidate(self, existing: dict[str, Any] | None) -> bool:
        if not existing:
            return False
        status = existing.get("status")
        if status == "skipped":
            return bool(existing.get("manual_override")) or existing.get("last_actor") in {"ui", "telegram"}
        return status in BLOCKING_STATUSES

    def _record_query_signal(self, project: ProjectItem, signal: str, amount: int = 1) -> None:
        if getattr(project, "search_query", None):
            self.db.record_query_signal(project.platform, project.search_query, signal, amount=amount)

    def _write_generated_proposal(self, project: ProjectItem, proposal_text: str) -> None:
        with GENERATED_PROPOSALS_FILE.open("a", encoding="utf-8") as f:
            f.write(f"=== Проект: {project.title} ({project.platform}) ===\n")
            f.write(f"URL: {project.url}\n")
            f.write(f"Search query: {project.search_query or 'n/a'}\n")
            f.write(f"Отклик:\n{proposal_text}\n\n")

    async def _notify_candidate(self, candidate_id: int, project: ProjectItem) -> None:
        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            return

        # Enrich candidate with files from platform_data if missing
        platform_data = candidate.get("platform_data")
        if not platform_data or not isinstance(platform_data, dict) or not platform_data.get("files"):
            # Try to get files from project's platform_data
            proj_pd = getattr(project, "platform_data", None)
            if isinstance(proj_pd, dict) and proj_pd.get("files"):
                candidate["platform_data"] = proj_pd
                self.db.update_candidate(candidate_id, platform_data=proj_pd)

        if project.url:
            with suppress(Exception):
                screenshot_path = await self.sender.get_project_preview(
                    project.url,
                    project.id,
                    project.platform,
                )
                if screenshot_path:
                    candidate["screenshot_path"] = screenshot_path

        await self.notifier.notify_candidate(candidate)

    async def execute_candidate_action(
        self,
        candidate_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        payload = payload or {}
        candidate = self.db.get_candidate(candidate_id)
        if not candidate:
            return f"Кандидат #{candidate_id} не найден."

        if candidate["status"] in {"manual_sent", "auto_sent", "draft", "sending"}:
            return f"Кандидат #{candidate_id} уже отправляется или отправлен."

        if not self.db.claim_candidate_for_sending(candidate_id):
            return f"Кандидат #{candidate_id} уже обрабатывается."

        candidate = self.db.get_candidate(candidate_id)

        proposal_text = payload.get("proposal_text") or candidate.get("proposal_text") or ""
        if not proposal_text.strip():
            self.db.update_candidate_status(
                candidate_id,
                "error",
                actor="system",
                reason="proposal text missing",
            )
            return f"У кандидата #{candidate_id} нет текста отклика."

        price = payload.get("chosen_price") or candidate.get("chosen_price") or candidate.get("budget")
        platform = candidate["platform"]
        project_id = candidate["project_id"]
        manual = action == "approve"
        dry_run = bool(candidate.get("dry_run"))
        breaker_key = f"send:{platform}"
        proposal_attachments: list[str] = []
        platform_data = candidate.get("platform_data") if isinstance(candidate.get("platform_data"), dict) else {}
        image_meta = platform_data.get("proposal_image") if isinstance(platform_data, dict) else None
        if isinstance(image_meta, dict) and image_meta.get("attachable") and image_meta.get("path"):
            proposal_attachments.append(str(image_meta["path"]))

        if dry_run or platform not in AUTO_SEND_PLATFORMS:
            status = "draft"
            self.db.save_proposal(
                project_id=project_id,
                platform=platform,
                title=candidate["title"],
                proposal_text=proposal_text,
                budget=str(price) if price is not None else None,
                skills=",".join(candidate.get("skills", [])),
                url=candidate.get("url"),
                status=status,
                candidate_id=candidate_id,
                decision_source="manual" if manual else "auto",
                ai_score=candidate.get("ai_score"),
                vet_score=candidate.get("vet_score"),
                decision_reason=candidate.get("decision_reason"),
                offers_count=candidate.get("offers_count", 0),
                client_hired_percent=candidate.get("client_hired_percent", 0),
                provider=candidate.get("provider"),
                query_text=candidate.get("search_query"),
                manual_override=manual or bool(candidate.get("manual_override")),
            )
            self.db.update_candidate_status(
                candidate_id,
                status,
                actor="telegram" if manual else "system",
                reason="dry run or unsupported platform",
                chosen_price=str(price) if price is not None else None,
                manual_override=manual or bool(candidate.get("manual_override")),
            )
            return f"Кандидат #{candidate_id} сохранён как draft."

        if not self.breaker.allow(breaker_key):
            status = self.breaker.status(breaker_key)
            self.db.update_candidate_status(
                candidate_id,
                "queued",
                actor="system",
                reason=f"breaker open for {platform}",
            )
            return (
                f"Отправка временно закрыта breaker для {platform}. "
                f"Пауза ещё {status.get('paused_seconds_left', 0)} сек."
            )

        if platform == "kwork" and not dry_run:
            from src.platforms.kwork import get_kwork_service
            from src.platforms.kwork_ext import get_connects_monitor, get_success_rate_monitor

            kwork_svc = get_kwork_service()
            if not kwork_svc.can_send_proposal():
                reason = "insufficient connects"
                if not get_success_rate_monitor().can_send():
                    rate = get_success_rate_monitor().success_rate
                    reason = f"low success rate ({rate:.1f}%)"
                self.db.update_candidate_status(
                    candidate_id,
                    "queued",
                    actor="system",
                    reason=reason,
                )
                return f"Отправка приостановлена: {reason}. Кандидат #{candidate_id} возвращён в очередь."

        log_db = get_log_db()
        send_started_at = time.perf_counter()
        try:
            success = await self.sender.send_proposal(
                platform=platform,
                project_url=candidate.get("url"),
                project_id=project_id,
                proposal_text=proposal_text,
                price=str(price) if price is not None else None,
                dry_run=False,
                attachments=proposal_attachments,
                platform_data=candidate.get("platform_data")
                if isinstance(candidate.get("platform_data"), dict)
                else None,
            )
        except Exception as e:
            error_str = str(e)
            if not _is_business_rejection(error_str):
                self.breaker.record_failure(breaker_key, error=error_str)
            duration_ms = int((time.perf_counter() - send_started_at) * 1000)
            log_db.log_send(platform, project_id, "error", error_str, duration_ms)
            self.db.update_candidate_status(
                candidate_id,
                "error",
                actor="system",
                reason=error_str,
            )
            return f"Ошибка отправки #{candidate_id}: {e}"

        duration_ms = int((time.perf_counter() - send_started_at) * 1000)
        if not success:
            if not _is_business_rejection("send returned False"):
                self.breaker.record_failure(breaker_key, error="send returned False")
            log_db.log_send(platform, project_id, "error", "send returned False", duration_ms)
            self.db.update_candidate_status(
                candidate_id,
                "error",
                actor="system",
                reason="send returned False",
            )
            return f"Отправка #{candidate_id} не подтверждена платформой."

        self.breaker.record_success(breaker_key)
        status = "manual_sent" if manual else "auto_sent"
        self._cycle_sent_count += 1
        self._cycle_sent_per_platform[platform] = self._cycle_sent_per_platform.get(platform, 0) + 1
        if platform == "kwork":
            from src.platforms.kwork_ext import get_connects_monitor

            monitor = get_connects_monitor()
            monitor.decrement(1)
        self.db.save_proposal(
            project_id=project_id,
            platform=platform,
            title=candidate["title"],
            proposal_text=proposal_text,
            budget=str(price) if price is not None else None,
            skills=",".join(candidate.get("skills", [])),
            url=candidate.get("url"),
            status=status,
            candidate_id=candidate_id,
            decision_source="manual" if manual else "auto",
            ai_score=candidate.get("ai_score"),
            vet_score=candidate.get("vet_score"),
            decision_reason=candidate.get("decision_reason"),
            offers_count=candidate.get("offers_count", 0),
            client_hired_percent=candidate.get("client_hired_percent", 0),
            provider=candidate.get("provider"),
            query_text=candidate.get("search_query"),
            manual_override=manual or bool(candidate.get("manual_override")),
        )
        self.db.update_candidate_status(
            candidate_id,
            status,
            actor="telegram" if manual else "system",
            reason="proposal sent",
            chosen_price=str(price) if price is not None else None,
            manual_override=manual or bool(candidate.get("manual_override")),
        )
        self._record_query_signal(
            ProjectItem(
                id=project_id,
                title=candidate["title"],
                description=candidate.get("description") or "",
                budget=candidate.get("budget"),
                currency=candidate.get("currency") or "RUB",
                skills=candidate.get("skills", []),
                url=candidate.get("url") or "",
                platform=platform,
                created_at=candidate.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                offers_count=candidate.get("offers_count", 0),
                client_hired_percent=candidate.get("client_hired_percent", 0),
                search_query=candidate.get("search_query"),
            ),
            "sent",
        )
        log_db.log_send(platform, project_id, status, duration_ms=duration_ms)
        return f"Кандидат #{candidate_id} отправлен со статусом {status}."

    async def run_cycle(self, dry_run: bool = False, limit_per_platform: int = 5):
        logger.info("=== НАЧАЛО ЦИКЛА ОРКЕСТРАТОРА ===")

        self._cycle_sent_count = 0
        self._cycle_sent_per_platform = {}

        mode = self._execution_mode()
        log_db = get_log_db()
        query_provider = _route_provider_from_env("query_generation")
        scoring_provider = _route_provider_from_env("scoring")
        proposal_provider = _route_provider_from_env("proposal_writing")
        fallback_provider = os.getenv("LLM_PROVIDER", "auto").strip().lower()
        logger.info(
            "LLM active routes: "
            f"query={query_provider}/{_provider_model_from_env(query_provider, task='query_generation')}, "
            f"scoring={scoring_provider}/{_provider_model_from_env(scoring_provider, task='scoring')}, "
            f"proposal={proposal_provider}/{_provider_model_from_env(proposal_provider, task='proposal_writing')}, "
            f"fallback={fallback_provider}/{_provider_model_from_env(fallback_provider)}"
        )
        self.db.requeue_due_candidates()
        self.db.recover_stale_sending()
        self.search_strategy.invalidate_cache()

        try:
            from src.utils.schedule import get_schedule_manager

            sched = get_schedule_manager()
            if not sched.is_work_time():
                logger.info("Вне рабочих часов — цикл пропущен")
                return {
                    "parsed": 0,
                    "active": 0,
                    "filtered": 0,
                    "ai_passed": 0,
                    "vetted": 0,
                    "proposal_selected": 0,
                    "proposal_limited": 0,
                    "queued": 0,
                    "auto_ready": 0,
                    "sent": 0,
                    "per_platform": {},
                    "discovery": {},
                    "skipped_reason": "outside_work_hours",
                }
        except Exception:
            pass
        for parser in self.parsers:
            service = getattr(parser, "service", None)
            if service is not None and hasattr(service, "reset_cycle"):
                service.reset_cycle()

        if os.getenv("PLATFORMS", "kwork").lower().find("kwork") >= 0:
            try:
                from src.platforms.kwork import get_kwork_service

                kwork_svc = get_kwork_service()
                await kwork_svc.check_connects()
                await kwork_svc.check_success_rate()
            except Exception as e:
                logger.debug(f"Health check (connects/rate) failed: {e}")

        try:
            responses = await self.inbox_monitor.check_all()
            for response in responses:
                if response.get("project_id") and response.get("message"):
                    self.db.mark_response(response["project_id"], response["message"])
        except Exception as e:
            logger.debug(f"Ошибка проверки входящих: {e}")

        discovery_mode = os.getenv("DISCOVERY_MODE", "wide").strip().lower()
        wide_mode = discovery_mode == "wide"
        search_queries_map: dict[str, list[str]] = {}
        query_count = _env_int("QUERY_COUNT", 8 if wide_mode else 6, low=1, high=50 if wide_mode else 20)
        for parser in self.parsers:
            platform = parser.PLATFORM_NAME
            if platform in search_queries_map:
                continue
            started_at = time.perf_counter()
            try:
                queries = await self.search_strategy.generate_queries(platform, count=query_count)
                search_queries_map[platform] = queries
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                logger.info(f"SearchStrategy: {platform} -> {queries} ({duration_ms}мс)")
            except Exception as e:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                fallback = os.getenv("SEARCH_QUERY", "python")
                queries = [q.strip() for q in fallback.split("|") if q.strip()] or ["python"]
                search_queries_map[platform] = queries
                logger.warning(f"SearchStrategy fallback для {platform} ({duration_ms}мс): {e}")

        pages_to_parse = _env_int("PAGES_TO_PARSE", 5, low=1, high=50 if wide_mode else 10)
        max_pages_per_query = _env_int(
            "MAX_PAGES_PER_QUERY",
            50 if wide_mode else pages_to_parse,
            low=1,
            high=100,
        )
        max_projects_per_cycle = _env_int("MAX_PROJECTS_PER_CYCLE", 500, low=20, high=2000)
        max_parse_seconds = _env_int("MAX_PARSE_SECONDS", 180, low=10, high=600)
        per_page = getattr(self.filter, "per_page", 20)
        tasks = []
        results = []
        discovery_stats: dict[str, Any] = {
            "mode": discovery_mode,
            "platforms": {},
            "queries": 0,
            "pages_scanned": 0,
            "raw_found": 0,
            "deduped": 0,
            "new_candidates": 0,
        }
        for parser in self.parsers:
            queries = search_queries_map.get(parser.PLATFORM_NAME, ["python"])
            if wide_mode and parser.PLATFORM_NAME == "kwork":
                parser_results, parser_stats = await self._discover_wide_kwork(
                    parser,
                    queries,
                    per_page=per_page,
                    max_pages_per_query=max_pages_per_query,
                    max_projects_per_cycle=max_projects_per_cycle,
                    max_parse_seconds=max_parse_seconds,
                )
                results.extend(parser_results)
                discovery_stats["platforms"][parser.PLATFORM_NAME] = parser_stats
                discovery_stats["queries"] += int(parser_stats.get("queries", 0) or 0)
                discovery_stats["pages_scanned"] += int(parser_stats.get("pages_scanned", 0) or 0)
                discovery_stats["raw_found"] += int(parser_stats.get("raw_found", 0) or 0)
                continue

            for query in queries:
                for page in range(1, pages_to_parse + 1):
                    tasks.append(self._parse_projects(parser, page, per_page, query))

        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for tr in task_results:
                if isinstance(tr, BaseException):
                    logger.error(f"Parse task exception: {tr}")
                    results.append(("unknown", "", 0, [], 0, tr))
                else:
                    results.append(tr)
            for platform, _query, _page, projects, _duration_ms, error in results:
                platform_stats = discovery_stats["platforms"].setdefault(
                    platform,
                    {
                        "mode": "standard",
                        "queries": 0,
                        "pages_scanned": 0,
                        "raw_found": 0,
                        "stop_reason": "fixed_pages",
                    },
                )
                platform_stats["pages_scanned"] += 1
                platform_stats["raw_found"] += len(projects) if not error else 0
                discovery_stats["pages_scanned"] += 1
                discovery_stats["raw_found"] += len(projects) if not error else 0
            for platform, queries in search_queries_map.items():
                platform_stats = discovery_stats["platforms"].get(platform)
                if platform_stats and platform_stats.get("mode") == "standard":
                    discovery_stats["platforms"][platform]["queries"] = len(queries)
                    discovery_stats["queries"] += len(queries)

        deduped: dict[tuple[str, str], ProjectItem] = {}
        for platform, query, page, projects, duration_ms, error in results:
            self.db.record_query_run(platform, query, len(projects))
            if error:
                logger.error(
                    f"Ошибка получения проектов от {platform} (query='{query}', page={page}, {duration_ms}мс): {error}"
                )
                log_db.log_parse(platform, "error", 0, str(error), duration_ms)
                continue

            log_db.log_parse(platform, "success", len(projects), duration_ms=duration_ms)
            for project in projects:
                key = self._project_key(project)
                if key not in deduped:
                    deduped[key] = project

        all_projects = list(deduped.values())
        discovery_stats["deduped"] = len(all_projects)
        logger.info(f"Собрано уникальных проектов: {len(all_projects)}")

        active_projects: list[ProjectItem] = []
        candidate_ids: dict[tuple[str, str], int] = {}
        blocked_existing: dict[str, int] = {}
        for project in all_projects:
            existing = self.db.get_candidate_by_project(project.id, project.platform)
            if self._is_blocked_existing_candidate(existing):
                status = str(existing.get("status") or "unknown") if existing else "unknown"
                blocked_existing[status] = blocked_existing.get(status, 0) + 1
                continue
            client_uid = str(getattr(project, "client_user_id", "") or "")
            if client_uid and self.db.is_client_blacklisted(client_uid, project.platform):
                logger.info(f"Клиент {client_uid} в чёрном списке — пропуск проекта {project.id}")
                blocked_existing["blacklisted"] = blocked_existing.get("blacklisted", 0) + 1
                continue
            candidate_id = self._stage_candidate(project, "parsed", "parsed", dry_run=1 if dry_run else 0)
            candidate_ids[self._project_key(project)] = candidate_id
            active_projects.append(project)

        logger.info(f"Новых/перерабатываемых проектов: {len(active_projects)}")

        if blocked_existing:
            logger.info(f"Existing candidates blocked before processing: {blocked_existing}")

        discovery_stats["new_candidates"] = len(active_projects)
        logger.info(f"Discovery stats: {discovery_stats}")

        filtered_projects, keyword_decisions = await self.filter.filter_projects_with_reasons(active_projects)
        filtered_keys = {self._project_key(project) for project in filtered_projects}
        for project in active_projects:
            key = self._project_key(project)
            candidate_id = candidate_ids.get(key)
            if not candidate_id:
                continue
            if key in filtered_keys:
                self.db.update_candidate(candidate_id, stage="filtered", status="filtered")
            else:
                decision = keyword_decisions.get(key)
                reason = decision.reason if decision else "keyword filter"
                self.db.update_candidate_status(candidate_id, "skipped", reason=reason, actor="system")
                self._record_query_signal(project, "skipped")

        logger.info(f"Прошло keyword-фильтр: {len(filtered_projects)}")
        log_db.log_filter("keyword", len(active_projects), len(filtered_projects), "Keyword-фильтр")

        nlp_passed: list[ProjectItem] = []
        for project in filtered_projects:
            project_dict = {
                "title": project.title,
                "description": project.description,
                "budget": project.budget,
            }

            if self.nlp.is_honeypot(project_dict):
                candidate_id = candidate_ids[self._project_key(project)]
                self.db.update_candidate_status(candidate_id, "skipped", reason="honeypot", actor="system")
                self._record_query_signal(project, "skipped")
                continue

            analysis = self.nlp.analyze_project(project_dict)
            if analysis["is_spam"]:
                candidate_id = candidate_ids[self._project_key(project)]
                self.db.update_candidate_status(candidate_id, "skipped", reason="nlp spam", actor="system")
                self._record_query_signal(project, "skipped")
                continue

            if not analysis.get("is_relevant", True):
                candidate_id = candidate_ids[self._project_key(project)]
                self.db.update_candidate_status(candidate_id, "skipped", reason="nlp irrelevant", actor="system")
                self._record_query_signal(project, "skipped")
                continue

            if analysis["tech_stack"] and not project.skills:
                project.skills = analysis["tech_stack"]
            elif analysis["tech_stack"]:
                project.skills = list(set(project.skills) | set(analysis["tech_stack"]))

            self.db.update_candidate(candidate_ids[self._project_key(project)], skills=project.skills)
            nlp_passed.append(project)

        logger.info(f"Прошло NLP-фильтр: {len(nlp_passed)}")
        log_db.log_filter("nlp", len(filtered_projects), len(nlp_passed), "NLP-фильтр")

        ai_threshold = _env_int("AI_SCORE_THRESHOLD", 6, low=1, high=10)
        score_results = await self.ai_scorer.evaluate_projects(nlp_passed, threshold=ai_threshold)
        scored_passed: list[tuple[ProjectItem, ScoreResult]] = []

        for result in score_results:
            project = result.project
            key = self._project_key(project)
            candidate_id = candidate_ids[key]
            ai_source = "fallback_scored" if result.fallback_scored else result.source
            self.db.update_candidate(
                candidate_id,
                stage="scored",
                status="scored" if result.passed else "skipped",
                ai_pre_score=result.pre_score,
                ai_score=result.final_score,
                ai_score_source=ai_source,
                ai_reason=result.summary,
                decision_reason=result.summary,
            )

            if result.passed:
                scored_passed.append((project, result))
                self._record_query_signal(project, "shortlisted")
            else:
                self._record_query_signal(project, "skipped")

        rejected_scores = [result for result in score_results if not result.passed]
        if rejected_scores:
            logger.info(
                "AIScorer rejected examples: "
                + "; ".join(
                    f"id={item.project.id} title='{item.project.title[:50]}' "
                    f"score={item.final_score}/{item.threshold} reason='{item.summary[:100]}'"
                    for item in rejected_scores[:10]
                )
            )

        logger.info(f"Прошло AI-скоринг: {len(scored_passed)}/{len(nlp_passed)}")
        log_db.log_filter("ai", len(nlp_passed), len(scored_passed), f"AI-скоринг с порогом {ai_threshold}")

        vetted_payloads: list[dict[str, Any]] = []
        for project, score_result in scored_passed:
            candidate_id = candidate_ids[self._project_key(project)]
            client_data = None
            osint_result = None

            if project.platform == "kwork" and project.id:
                client_data = self._client_hint_from_project(project)
                api_client_data = await self.client_vetter.fetch_kwork_client_data(
                    project.id, user_id=project.client_user_id or None
                )
                if api_client_data:
                    client_data = {**(client_data or {}), **api_client_data}
                if self.osint:
                    username = (client_data or {}).get("username") or ""
                    if username:
                        try:
                            osint_result = await self.osint.gather(
                                username,
                                context=project.title[:60],
                                description=project.description or "",
                            )
                        except Exception as e:
                            logger.debug(f"OSINT: сбор не удался для {username}: {e}")

            vet_result = await self.client_vetter.vet_client(
                project,
                client_data,
                osint_result=osint_result,
            )
            client_context = self._safe_client_context(client_data, osint_result, vet_result)
            self.db.update_candidate(
                candidate_id,
                stage="vetted",
                status="vetted" if vet_result["passed"] else "skipped",
                vet_score=vet_result["score"],
                vet_passed=1 if vet_result["passed"] else 0,
                vet_reasons=vet_result["reasons"],
                vet_red_flags=vet_result["red_flags"],
                client_context=client_context,
            )

            if vet_result["passed"]:
                vetted_payloads.append(
                    {
                        "project": project,
                        "score_result": score_result,
                        "candidate_id": candidate_id,
                        "client_data": client_data,
                        "osint_result": osint_result,
                        "vet_result": vet_result,
                    }
                )
            else:
                self._record_query_signal(project, "skipped")

        logger.info(f"Прошло веттинг заказчиков: {len(vetted_payloads)}/{len(scored_passed)}")
        log_db.log_filter("vetting", len(scored_passed), len(vetted_payloads), "Веттинг заказчиков")

        vetted_payloads.sort(
            key=lambda item: (
                item["score_result"].final_score,
                item["vet_result"]["score"],
                -item["project"].offers_count,
            ),
            reverse=True,
        )

        top_n = _env_int("TOP_PROJECTS", 0, low=0, high=50)
        if top_n > 0:
            vetted_payloads = vetted_payloads[:top_n]

        vetted_total = len(vetted_payloads)
        vetted_payloads, proposal_limited = _limit_payloads_per_platform(vetted_payloads, limit_per_platform)
        if proposal_limited:
            logger.info(
                f"Лимит генерации откликов: selected={len(vetted_payloads)} "
                f"limited_out={proposal_limited} per_platform={max(1, int(limit_per_platform or 1))}"
            )

        auto_send_counts: dict[str, int] = {}
        queued_count = 0
        auto_ready_count = 0

        for payload in vetted_payloads:
            project = payload["project"]
            candidate_id = payload["candidate_id"]
            score_result = payload["score_result"]
            vet_result = payload["vet_result"]
            client_data = payload["client_data"]

            competitor_prices = []
            scrape_competitors = os.getenv("SCRAPE_COMPETITOR_PRICES", "false").lower() in {"1", "true", "yes", "on"}
            if project.platform == "kwork" and scrape_competitors:
                try:
                    competitor_prices = await self.sender.scrape_competitor_prices(project.url)
                except Exception as e:
                    logger.debug(f"Не удалось скрейпить цены конкурентов: {e}")

            generation_started_at = time.perf_counter()
            requested_provider = os.getenv("PROPOSAL_WRITING_PROVIDER", "auto").lower()
            try:
                proposal_text = await self.generator.generate(
                    project,
                    provider=requested_provider,
                    client_data=client_data,
                    competitor_prices=competitor_prices,
                )
                generation_ms = int((time.perf_counter() - generation_started_at) * 1000)
                provider_meta = dict(self.generator.last_generation_meta or {})
                provider_name = provider_meta.get("provider") or requested_provider
                self.db.update_candidate(
                    candidate_id,
                    proposal_text=proposal_text,
                    provider=provider_name,
                    competitor_prices=competitor_prices,
                )
                self._write_generated_proposal(project, proposal_text)
                log_db.log_generation(project.platform, project.id, provider_name, "success", duration_ms=generation_ms)

                image_asset = await self.image_generator.maybe_generate(
                    project,
                    proposal_text,
                    ai_score=score_result.final_score,
                    vet_score=vet_result["score"],
                )
                if image_asset:
                    platform_data = dict(project.platform_data or {})
                    platform_data["proposal_image"] = image_asset.as_dict()
                    project.platform_data = platform_data
                    self.db.update_candidate(candidate_id, platform_data=platform_data)
            except Exception as e:
                generation_ms = int((time.perf_counter() - generation_started_at) * 1000)
                provider_name = requested_provider
                log_db.log_generation(project.platform, project.id, provider_name, "error", str(e), generation_ms)
                self.db.update_candidate_status(candidate_id, "error", actor="system", reason=str(e))
                continue

            ai_score_source = "fallback_scored" if score_result.fallback_scored else score_result.source
            decision_ctx = CandidateDecisionContext(
                platform=project.platform,
                ai_score=score_result.final_score,
                ai_score_source=ai_score_source,
                vet_score=vet_result["score"],
                offers_count=project.offers_count,
                budget=project.budget,
                red_flags=list(vet_result["red_flags"]),
                valid_proposal=bool(proposal_text and len(proposal_text.strip()) >= 40),
                auto_platform=project.platform in AUTO_SEND_PLATFORMS,
                platform_paused=self.db.is_platform_paused(project.platform),
                dry_run=dry_run,
            )
            decision = self.policy.evaluate(mode, decision_ctx)
            self.db.update_candidate(
                candidate_id,
                risk_level=decision.risk_level,
                priority=decision.priority,
                auto_eligible=1 if decision.auto_eligible else 0,
                decision_reason=decision.reason,
            )

            if decision.status == "skipped":
                self.db.update_candidate_status(candidate_id, "skipped", actor="system", reason=decision.reason)
                self._record_query_signal(project, "skipped")
                continue

            if decision.status == "auto_ready":
                if auto_send_counts.get(project.platform, 0) >= limit_per_platform:
                    self.db.update_candidate_status(
                        candidate_id,
                        "queued",
                        actor="system",
                        reason="cycle auto-send limit reached",
                    )
                    with suppress(Exception):
                        await self._notify_candidate(candidate_id, project)
                    queued_count += 1
                    continue

                self.db.update_candidate_status(candidate_id, "auto_ready", actor="system", reason=decision.reason)
                self._record_query_signal(project, "auto_ready")
                auto_ready_count += 1
                auto_send_counts[project.platform] = auto_send_counts.get(project.platform, 0) + 1
                with suppress(Exception):
                    await self._notify_candidate(candidate_id, project)
                continue

            self.db.update_candidate_status(candidate_id, "queued", actor="system", reason=decision.reason)
            queued_count += 1
            with suppress(Exception):
                await self._notify_candidate(candidate_id, project)

        await self.notifier.maybe_send_digest()

        sent_count = self._cycle_sent_count
        sent_per_platform = dict(self._cycle_sent_per_platform)

        logger.info(
            f"=== ЦИКЛ ЗАВЕРШЕН. parsed={len(all_projects)} active={len(active_projects)} "
            f"queued={queued_count} auto_ready={auto_ready_count} sent={sent_count} ==="
        )
        log_db.log_filter("total", len(all_projects), sent_count + queued_count + auto_ready_count, "Общий итог цикла")

        return {
            "parsed": len(all_projects),
            "active": len(active_projects),
            "filtered": len(filtered_projects),
            "ai_passed": len(scored_passed),
            "vetted": vetted_total,
            "proposal_selected": len(vetted_payloads),
            "proposal_limited": proposal_limited,
            "queued": queued_count,
            "auto_ready": auto_ready_count,
            "sent": sent_count,
            "per_platform": sent_per_platform,
            "discovery": discovery_stats,
        }

    async def stop(self):
        logger.info("Остановка оркестратора...")
        with suppress(Exception):
            await self.inbox_monitor.stop_fast_polling()
        with suppress(Exception):
            await self.notifier.stop()
        with suppress(Exception):
            await self.sender.stop()
        with suppress(Exception):
            self.inbox_monitor.close()
        for parser in self.parsers:
            with suppress(Exception):
                parser.close()
