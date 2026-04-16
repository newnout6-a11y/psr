"""
Оркестратор. Соединяет парсинг, фильтрацию, LLM и отправку.
"""

import asyncio
from typing import List, Dict, Any
from loguru import logger
import os

from src.parsers import (
    HHParser,
    RemoteOKParser,
    FreelancerComParser,
    FLRuParser,
    KworkParser,
    KworkAPIParser,
    FreelanceRuParser,
    PeoplePerHourParser,
    UpworkParser,
    WeblancerParser,
    OneCLancerParser,
    FiverrParser,
)
from src.filter.project_filter import ProjectFilter
from src.filter.ai_scorer import AIRelevanceScorer
from src.brain.nlp_filter import NLPFilter
from src.brain.rag_pipeline import RAGPipeline
from src.action.proposal_generator import ProposalGenerator
from src.action.proposal_sender import ProposalSender
from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem
from src.utils.notifier import TelegramNotifier
from src.utils.inbox_monitor import InboxMonitor
from src.utils.log_db import get_log_db
from src.brain.search_strategy import SearchStrategy


class FreelanceOrchestrator:
    """Управляет полным циклом работы автономного фрилансера."""

    def __init__(self):
        self.parsers = []
        self._init_parsers()

        self.filter = ProjectFilter()
        self.nlp = NLPFilter()
        self.rag = RAGPipeline()
        self.rag.initialize()
        self.ai_scorer = AIRelevanceScorer()
        self.generator = ProposalGenerator(rag_pipeline=self.rag)

        headless_env = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
        self.sender = ProposalSender(headless=headless_env)

        self.db = ProposalDB()
        self.notifier = TelegramNotifier()
        self.inbox_monitor = InboxMonitor(notifier=self.notifier)
        self.search_strategy = SearchStrategy()

        logger.info(f"Оркестратор инициализирован. Активно парсеров: {len(self.parsers)}")

    def _init_parsers(self):
        """Инициализация активных парсеров из .env"""
        platforms_env = os.getenv("PLATFORMS", "kwork,fl_ru")
        active_names = [p.strip().lower() for p in platforms_env.split(",") if p.strip()]

        parser_map = {
            "hh_ru": HHParser,
            "remoteok": RemoteOKParser,
            "freelancer_com": FreelancerComParser,
            "fl_ru": FLRuParser,
            "kwork": KworkAPIParser,
            "kwork_browser": KworkParser,
            "freelance_ru": FreelanceRuParser,
            "peopleperhour": PeoplePerHourParser,
            "upwork": UpworkParser,
            "weblancer": WeblancerParser,
            "oneclancer": OneCLancerParser,
            "fiverr": FiverrParser,
        }

        for name in active_names:
            if name in parser_map:
                try:
                    self.parsers.append(parser_map[name]())
                except Exception as e:
                    logger.error(f"Ошибка инициализации парсера {name}: {e}")
            else:
                logger.warning(f"Неизвестная платформа в PLATFORMS: {name}")

    async def run_cycle(self, dry_run: bool = False, limit_per_platform: int = 5):
        """Полный цикл работы."""
        logger.info("=== НАЧАЛО ЦИКЛА ОРКЕСТРАТОРА ===")

        # 0. Проверка входящих сообщений
        try:
            await self.inbox_monitor.check_all()
        except Exception as e:
            logger.debug(f"Ошибка проверки входящих: {e}")

        # 1. Сбор проектов со всех парсеров
        all_projects: List[ProjectItem] = []

        # AI-генерация поисковых запросов для каждой платформы
        search_queries_map = {}
        for parser in self.parsers:
            platform = parser.PLATFORM_NAME
            if platform not in search_queries_map:
                try:
                    queries = await self.search_strategy.generate_queries(platform, count=6)
                    search_queries_map[platform] = queries
                except Exception:
                    # Fallback на SEARCH_QUERY из .env
                    fallback = os.getenv("SEARCH_QUERY", "python")
                    search_queries_map[platform] = [q.strip() for q in fallback.split("|") if q.strip()] or ["python"]

        pages_to_parse = int(os.getenv("PAGES_TO_PARSE", "5"))

        task_labels = []
        tasks = []
        for parser in self.parsers:
            queries = search_queries_map.get(parser.PLATFORM_NAME, ["python"])
            # Каждый запрос отправляем как отдельный фильтр
            for query in queries:
                for p in range(1, pages_to_parse + 1):
                    tasks.append(parser.get_projects(page=p, per_page=20, filters={"query": query}))
                    task_labels.append((parser, query))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        log_db = get_log_db()

        for (parser, query), res in zip(task_labels, results):
            if isinstance(res, Exception):
                logger.error(f"Ошибка получения проектов от {parser.PLATFORM_NAME} (query='{query}'): {res}")
                log_db.log_parse(parser.PLATFORM_NAME, "error", 0, str(res))
            elif isinstance(res, list):
                all_projects.extend(res)
                log_db.log_parse(parser.PLATFORM_NAME, "success", len(res))

        logger.info(f"Всего собрано проектов: {len(all_projects)}")
        log_db.log_filter("keyword", len(all_projects), 0, "Предварительный подсчет")

        # 2. Фильтрация
        filtered_projects = await self.filter.filter_projects(all_projects)
        logger.info(f"Прошло keyword-фильтр: {len(filtered_projects)}")
        log_db.log_filter("keyword", len(all_projects), len(filtered_projects), "Keyword-фильтр")

        # 2.5 NLP-фильтрация: honeypot detection + tech stack enrichment
        nlp_passed = []
        for p in filtered_projects:
            project_dict = {"title": p.title, "description": p.description, "budget": p.budget}

            if self.nlp.is_honeypot(project_dict):
                logger.debug(f"NLP: honeypot обнаружен — '{p.title[:40]}'")
                continue

            analysis = self.nlp.analyze_project(project_dict)

            if analysis["is_spam"]:
                logger.debug(f"NLP: спам — '{p.title[:40]}' (score={analysis['spam_score']:.2f})")
                continue

            if analysis["tech_stack"] and not p.skills:
                p.skills = analysis["tech_stack"]
            elif analysis["tech_stack"]:
                combined = set(p.skills) | set(analysis["tech_stack"])
                p.skills = list(combined)

            nlp_passed.append(p)

        filtered_projects = nlp_passed
        logger.info(f"Прошло NLP-фильтр: {len(filtered_projects)}")
        log_db.log_filter(
            "nlp", len(nlp_passed) + len(filtered_projects) - len(nlp_passed), len(nlp_passed), "NLP-фильтр"
        )

        # 3. AI-скоринг релевантности
        ai_threshold = int(os.getenv("AI_SCORE_THRESHOLD", "6"))
        scored_projects = await self.ai_scorer.score_projects(filtered_projects, threshold=ai_threshold)

        ai_passed = [p for p, score in scored_projects]
        logger.info(f"Прошло AI-скоринг (порог {ai_threshold}/10): {len(ai_passed)}/{len(filtered_projects)}")
        log_db.log_filter("ai", len(filtered_projects), len(ai_passed), f"AI-скоринг с порогом {ai_threshold}")

        # 4. Генерация и Отправка (топ-3 проекта, каждый разной моделью)
        sent_counts = {}
        total_sent = 0

        # Модели для ротации
        providers = ["groq", "google", "glm"]
        provider_idx = 0

        # Берем топ-N проектов (из env или по умолчанию 3)
        top_n = int(os.getenv("TOP_PROJECTS", "3"))
        top_projects = scored_projects[:top_n]
        logger.info(f"Выбрано топ-{len(top_projects)} проектов для обработки разными моделями")

        for project, ai_score in top_projects:
            platform = project.platform
            if sent_counts.get(platform, 0) >= limit_per_platform:
                continue

            logger.info(f"Обработка подходящего проекта: [{platform}] {project.title} (AI: {ai_score}/10)")

            # Выбираем провайдера по очереди (ротация)
            current_provider = providers[provider_idx % len(providers)]
            provider_idx += 1
            logger.info(f"Используем модель: {current_provider}")

            # Генерация отклика с указанием провайдера
            proposal_str = await self.generator.generate(project, provider=current_provider)
            logger.debug(f"Сгенерирован отклик (длина {len(proposal_str)}) через {current_provider}")

            # Сохранение в txt для отладки
            with open("data/generated_proposals.txt", "a", encoding="utf-8") as f:
                f.write(f"=== Проект: {project.title} ({project.platform}) ===\n")
                f.write(f"URL: {project.url}\n")
                f.write(f"Отклик:\n{proposal_str}\n\n")

            success = False

            if project.platform in ["kwork", "fl_ru", "freelancer_com"]:
                # --- НОВЫЙ ЭТАП: ПОДТВЕРЖДЕНИЕ В ТЕЛЕГРАМ (только НЕ в dry-run) ---
                final_price = project.budget

                if project.platform == "kwork":
                    if dry_run:
                        # В dry-run отправляем уведомление с ценовыми кнопками + скриншот
                        logger.info(
                            f"[DRY-RUN] Отправляем уведомление в Telegram для {project.id} (модель: {current_provider})"
                        )

                        # Делаем скриншот страницы проекта
                        screenshot_path = None
                        try:
                            screenshot_path = await self.sender.get_project_preview(
                                project.url,
                                project.id,
                                project.title,
                            )
                        except Exception as e:
                            logger.debug(f"[DRY-RUN] Скриншот не получен: {e}")

                        await self.notifier.notify_new_project_with_prices(
                            project_title=project.title,
                            project_url=project.url,
                            budget=project.budget,
                            platform=project.platform,
                            proposal_text=proposal_str,
                            screenshot_path=screenshot_path,
                            project_id=project.id,
                        )
                        logger.info(f"[DRY-RUN] Уведомление с ценами отправлено ({current_provider})")
                    else:
                        # РЕАЛЬНЫЙ режим - ждем подтверждение в Telegram
                        logger.info(f"Ожидание одобрения цены в Telegram для {project.id}...")

                        # Делаем превью-скриншот ПЕРЕД отправкой запроса в ТГ
                        screenshot_path = await self.sender.get_project_preview(
                            project.url,
                            project.id,
                            project.title,
                        )

                        user_price = await self.notifier.request_approval(
                            project_id=project.id,
                            project_title=project.title,
                            proposal_text=proposal_str,
                            budget=project.budget,
                            screenshot_path=screenshot_path,
                        )

                        if user_price:
                            final_price = user_price
                            # Проверяем, был ли отредактирован текст
                            edited_text = self.notifier.get_edited_proposal(project.id)
                            if edited_text:
                                proposal_str = edited_text
                                logger.info(f"Используем отредактированный текст отклика")
                            logger.info(f"Цена одобрена пользователем: {final_price}")
                        else:
                            logger.warning(f"Проект {project.id} пропущен: нет ответа по цене")
                            continue

                success = await self.sender.send_proposal(
                    platform=project.platform,
                    project_url=project.url,
                    project_id=project.id,
                    proposal_text=proposal_str,
                    price=final_price,
                    dry_run=dry_run,
                )
            else:
                # Для других платформ пока не реализована авто-отправка
                logger.info(f"Авто-отправка на {project.platform} не реализована, сохраняем в черновики.")
                success = True  # Условно "отправлено" как драфт

            if success:
                status = "draft" if project.platform not in ["kwork", "fl_ru"] or dry_run else "sent"
                self.db.save_proposal(
                    project_id=project.id,
                    platform=project.platform,
                    title=project.title,
                    proposal_text=proposal_str,
                    budget=str(project.budget) if project.budget else None,
                    skills=",".join(project.skills),
                    url=project.url,
                    status=status,
                )
                sent_counts[platform] = sent_counts.get(platform, 0) + 1
                total_sent += 1
                log_db.log_send(platform, project.id, "success" if status == "sent" else "draft")
            else:
                log_db.log_send(platform, project.id, "error", "Ошибка отправки")

        logger.info(f"=== ЦИКЛ ЗАВЕРШЕН. Отправлено/Сохранено всего: {total_sent} ===")
        log_db.log_filter("total", len(all_projects), total_sent, "Общий итог цикла")

        return {
            "parsed": len(all_projects),
            "filtered": len(filtered_projects),
            "ai_passed": len(ai_passed),
            "sent": total_sent,
            "per_platform": sent_counts,
        }

    async def stop(self):
        """Гарантированное закрытие всех ресурсов."""
        from contextlib import suppress
        logger.info("Остановка оркестратора...")
        with suppress(Exception):
            await self.notifier.stop()
        with suppress(Exception):
            await self.sender.stop()
        with suppress(Exception):
            self.inbox_monitor.close()
        for parser in self.parsers:
            try:
                parser.close()
            except:
                pass
