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
from src.utils.reply_linker import classify_and_persist_reply, link_inbox_response


AUTO_SEND_PLATFORMS = {"kwork", "freelance_ru"}
ACTIVE_PROCESSING_STATUSES = {"parsed", "filtered", "scored", "vetted", "auto_ready", "error"}
BLOCKING_STATUSES = {"queued", "snoozed", "manual_sent", "auto_sent", "draft", "skipped"}


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
        self.osint = OSINTAggregator() if os.getenv("OSINT_ENABLED", "true").lower() == "true" else None
        self.generator = ProposalGenerator(rag_pipeline=self.rag)

        headless_env = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
        self.sender = ProposalSender(headless=headless_env)
        self.search_strategy = SearchStrategy(db=self.db)
        self.breaker = get_breaker()
        self.notifier = TelegramNotifier(db=self.db)
        self.notifier.bind_runtime(db=self.db, candidate_executor=self.execute_candidate_action)
        self.inbox_monitor = InboxMonitor(notifier=self.notifier)

        self._ensure_runtime_defaults()
        logger.info(f"Оркестратор инициализирован. Активно парсеров: {len(self.parsers)}")

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

    def _project_key(self, project: ProjectItem) -> tuple[str, str]:
        return project.platform, project.id

    def _stage_candidate(self, project: ProjectItem, stage: str, status: str, **fields: Any) -> int:
        mode = self._execution_mode()
        payload = {"stage": stage, "status": status, "execution_mode": mode, **fields}
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

    def _assign_prompt_variant(self, candidate_id: int) -> tuple[str | None, float | None]:
        """Phase 3 A/B: детерминированное назначение варианта промпта.

        Управляется env-переменной `PROMPT_AB_VARIANTS`. Формат:
            "control:0.65,warm:0.85"
        Имя варианта — произвольное, число — temperature для генерации.
        Если переменная не задана или формат битый — A/B отключён,
        возвращаем (None, None) и вызывающий просто использует дефолты.

        Назначение детерминированное по `candidate_id % len(variants)`,
        чтобы повторный прогон того же проекта не «перебирал» варианты
        и распределение выборки было воспроизводимым.
        """
        spec = (os.getenv("PROMPT_AB_VARIANTS") or "").strip()
        if not spec:
            return None, None
        variants: list[tuple[str, float]] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            name, raw_temp = chunk.split(":", 1)
            name = name.strip()
            try:
                temp = float(raw_temp.strip())
            except ValueError:
                continue
            if name and 0.0 <= temp <= 2.0:
                variants.append((name, temp))
        if not variants:
            return None, None
        idx = max(int(candidate_id), 0) % len(variants)
        return variants[idx]

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
        if not existing:
            return False
        return existing.get("status") in BLOCKING_STATUSES

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
            )
        except Exception as e:
            self.breaker.record_failure(breaker_key, error=str(e))
            duration_ms = int((time.perf_counter() - send_started_at) * 1000)
            log_db.log_send(platform, project_id, "error", str(e), duration_ms)
            self.db.update_candidate_status(
                candidate_id,
                "error",
                actor="system",
                reason=str(e),
            )
            return f"Ошибка отправки #{candidate_id}: {e}"

        duration_ms = int((time.perf_counter() - send_started_at) * 1000)
        if not success:
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

        mode = self._execution_mode()
        log_db = get_log_db()
        self.db.requeue_due_candidates()

        try:
            responses = await self.inbox_monitor.check_all()
            for response in responses:
                # 1) Старый путь — обновляет `proposals.response`, чтобы не
                #    ломать аналитику, которая опирается на этот столбец.
                if response.get("project_id") and response.get("message"):
                    self.db.mark_response(response["project_id"], response["message"])

                # 2) Новый путь — Phase 3 feedback loop.
                #    Линкер находит кандидата по (platform, username[, project_id])
                #    и пишет replied_at/reply_text. Дальше зовём LLM-классификатор,
                #    но уже фоном: его падение не должно валить остальной цикл.
                try:
                    link_result = link_inbox_response(self.db, response)
                except Exception as link_err:
                    logger.debug(f"ReplyLinker: не удалось привязать реплай: {link_err}")
                    continue

                if not link_result.linked or link_result.candidate_id is None:
                    continue

                try:
                    await classify_and_persist_reply(
                        self.db,
                        link_result.candidate_id,
                        response.get("message", ""),
                    )
                except Exception as cls_err:
                    logger.debug(f"ReplyClassifier: классификация не удалась: {cls_err}")
        except Exception as e:
            logger.debug(f"Ошибка проверки входящих: {e}")

        search_queries_map: dict[str, list[str]] = {}
        query_count = _env_int("QUERY_COUNT", 6, low=1, high=20)
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

        pages_to_parse = _env_int("PAGES_TO_PARSE", 5, low=1, high=10)
        per_page = getattr(self.filter, "per_page", 20)
        tasks = []
        for parser in self.parsers:
            queries = search_queries_map.get(parser.PLATFORM_NAME, ["python"])
            for query in queries:
                for page in range(1, pages_to_parse + 1):
                    tasks.append(self._parse_projects(parser, page, per_page, query))

        results = await asyncio.gather(*tasks)

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
        logger.info(f"Собрано уникальных проектов: {len(all_projects)}")

        active_projects: list[ProjectItem] = []
        candidate_ids: dict[tuple[str, str], int] = {}
        for project in all_projects:
            if self._is_blocked_by_existing_status(project):
                continue
            candidate_id = self._stage_candidate(project, "parsed", "parsed", dry_run=1 if dry_run else 0)
            candidate_ids[self._project_key(project)] = candidate_id
            active_projects.append(project)

        logger.info(f"Новых/перерабатываемых проектов: {len(active_projects)}")

        filtered_projects = await self.filter.filter_projects(active_projects)
        filtered_keys = {self._project_key(project) for project in filtered_projects}
        for project in active_projects:
            key = self._project_key(project)
            candidate_id = candidate_ids.get(key)
            if not candidate_id:
                continue
            if key in filtered_keys:
                self.db.update_candidate(candidate_id, stage="filtered", status="filtered")
            else:
                self.db.update_candidate_status(candidate_id, "skipped", reason="keyword filter", actor="system")
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

            if analysis["tech_stack"] and not project.skills:
                project.skills = analysis["tech_stack"]
            elif analysis["tech_stack"]:
                project.skills = list(set(project.skills) | set(analysis["tech_stack"]))

            self.db.update_candidate(candidate_ids[self._project_key(project)], skills=project.skills)
            nlp_passed.append(project)

        logger.info(f"Прошло NLP-фильтр: {len(nlp_passed)}")
        log_db.log_filter("nlp", len(filtered_projects), len(nlp_passed), "NLP-фильтр")

        ai_threshold = int(os.getenv("AI_SCORE_THRESHOLD", "6"))
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
            client_username = ((client_data or {}).get("username") or "").strip() or None
            self.db.update_candidate(
                candidate_id,
                stage="vetted",
                status="vetted" if vet_result["passed"] else "skipped",
                vet_score=vet_result["score"],
                vet_passed=1 if vet_result["passed"] else 0,
                vet_reasons=vet_result["reasons"],
                vet_red_flags=vet_result["red_flags"],
                client_context=client_context,
                client_username=client_username,
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

        auto_send_counts: dict[str, int] = {}
        queued_count = 0
        auto_ready_count = 0
        final_sent_count = 0

        for payload in vetted_payloads:
            project = payload["project"]
            candidate_id = payload["candidate_id"]
            score_result = payload["score_result"]
            vet_result = payload["vet_result"]
            client_data = payload["client_data"]

            competitor_prices = []
            if project.platform == "kwork":
                try:
                    competitor_prices = await self.sender.scrape_competitor_prices(project.url)
                except Exception as e:
                    logger.debug(f"Не удалось скрейпить цены конкурентов: {e}")

            generation_started_at = time.perf_counter()
            requested_provider = os.getenv("PROPOSAL_WRITING_PROVIDER", "auto").lower()
            variant_name, variant_temp = self._assign_prompt_variant(candidate_id)
            try:
                proposal_text = await self.generator.generate(
                    project,
                    provider=requested_provider,
                    client_data=client_data,
                    competitor_prices=competitor_prices,
                    temperature=variant_temp,
                    prompt_variant=variant_name,
                )
                generation_ms = int((time.perf_counter() - generation_started_at) * 1000)
                provider_meta = dict(self.generator.last_generation_meta or {})
                provider_name = provider_meta.get("provider") or requested_provider
                self.db.update_candidate(
                    candidate_id,
                    proposal_text=proposal_text,
                    provider=provider_name,
                    competitor_prices=competitor_prices,
                    prompt_variant=variant_name,
                )
                self._write_generated_proposal(project, proposal_text)
                log_db.log_generation(project.platform, project.id, provider_name, "success", duration_ms=generation_ms)
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
                    await self._notify_candidate(candidate_id, project)
                    queued_count += 1
                    continue

                self.db.update_candidate_status(candidate_id, "auto_ready", actor="system", reason=decision.reason)
                self._record_query_signal(project, "auto_ready")
                auto_ready_count += 1
                auto_send_counts[project.platform] = auto_send_counts.get(project.platform, 0) + 1
                message = await self.execute_candidate_action(candidate_id, "auto_send")
                logger.info(message)
                if "отправлен" in message:
                    final_sent_count += 1
                continue

            self.db.update_candidate_status(candidate_id, "queued", actor="system", reason=decision.reason)
            queued_count += 1
            await self._notify_candidate(candidate_id, project)

        await self.notifier.maybe_send_digest()

        logger.info(
            f"=== ЦИКЛ ЗАВЕРШЕН. parsed={len(all_projects)} active={len(active_projects)} "
            f"queued={queued_count} auto_ready={auto_ready_count} sent={final_sent_count} ==="
        )
        log_db.log_filter("total", len(all_projects), final_sent_count + queued_count, "Общий итог цикла")

        return {
            "parsed": len(all_projects),
            "active": len(active_projects),
            "filtered": len(filtered_projects),
            "ai_passed": len(scored_passed),
            "vetted": len(vetted_payloads),
            "queued": queued_count,
            "auto_ready": auto_ready_count,
            "sent": final_sent_count,
        }

    async def stop(self):
        logger.info("Остановка оркестратора...")
        with suppress(Exception):
            await self.notifier.stop()
        with suppress(Exception):
            await self.sender.stop()
        with suppress(Exception):
            self.inbox_monitor.close()
        for parser in self.parsers:
            with suppress(Exception):
                parser.close()
