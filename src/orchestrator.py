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
    FreelanceRuParser,
    PeoplePerHourParser,
    UpworkParser,
    WeblancerParser,
    OneCLancerParser,
)
from src.filter.project_filter import ProjectFilter
from src.filter.ai_scorer import AIRelevanceScorer
from src.action.proposal_generator import ProposalGenerator
from src.action.proposal_sender import ProposalSender
from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem
from src.utils.notifier import TelegramNotifier

class FreelanceOrchestrator:
    """Управляет полным циклом работы автономного фрилансера."""

    def __init__(self):
        self.parsers = []
        self._init_parsers()
        
        self.filter = ProjectFilter()
        self.ai_scorer = AIRelevanceScorer()
        self.generator = ProposalGenerator()
        self.sender = ProposalSender(headless=True)
        self.db = ProposalDB()
        self.notifier = TelegramNotifier()
        
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
            "kwork": KworkParser,
            "freelance_ru": FreelanceRuParser,
            "peopleperhour": PeoplePerHourParser,
            "upwork": UpworkParser,
            "weblancer": WeblancerParser,
            "oneclancer": OneCLancerParser,
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
        
        # 1. Сбор проектов со всех парсеров асинхронно
        all_projects: List[ProjectItem] = []
        
        search_query = os.getenv("SEARCH_QUERY", "python")
        filters_map = {"query": search_query}
        
        tasks = []
        for parser in self.parsers:
            tasks.append(parser.get_projects(page=1, per_page=20, filters=filters_map))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for parser, res in zip(self.parsers, results):
            if isinstance(res, Exception):
                logger.error(f"Ошибка получения проектов от {parser.PLATFORM_NAME}: {res}")
            elif isinstance(res, list):
                all_projects.extend(res)
                
        logger.info(f"Всего собрано проектов: {len(all_projects)}")
        
        # 2. Фильтрация
        filtered_projects = self.filter.filter_projects(all_projects)
        logger.info(f"Прошло фильтрацию: {len(filtered_projects)}")
        
        # 3. AI-скоринг релевантности
        ai_threshold = int(os.getenv("AI_SCORE_THRESHOLD", "6"))
        scored_projects = await self.ai_scorer.score_projects(filtered_projects, threshold=ai_threshold)
        
        ai_passed = [p for p, score in scored_projects]
        logger.info(f"Прошло AI-скоринг (порог {ai_threshold}/10): {len(ai_passed)}/{len(filtered_projects)}")
        
        # 4. Генерация и Отправка
        sent_counts = {} # status tracking per platform
        total_sent = 0
        
        for project, ai_score in scored_projects:
            platform = project.platform
            if sent_counts.get(platform, 0) >= limit_per_platform:
                continue
                
            logger.info(f"Обработка подходящего проекта: [{platform}] {project.title} (AI: {ai_score}/10)")
            
            # Генерация отклика
            proposal_str = await self.generator.generate(project)
            logger.debug(f"Сгенерирован отклик (длина {len(proposal_str)})")
            
            # Сохранение в txt для отладки
            with open("data/generated_proposals.txt", "a", encoding="utf-8") as f:
                f.write(f"=== Проект: {project.title} ({project.platform}) ===\n")
                f.write(f"URL: {project.url}\n")
                f.write(f"Отклик:\n{proposal_str}\n\n")
            
            success = False
            if dry_run:
                logger.info("[DRY-RUN] Пропуск реальной отправки")
                # Для dry-run считаем что отправили
                success = True 
            else:
                # Пытаемся отправить
                if project.platform in ["kwork", "fl_ru", "freelancer_com"]:
                    # --- НОВЫЙ ЭТАП: ПОДТВЕРЖДЕНИЕ В ТЕЛЕГРАМ ---
                    final_price = project.budget
                    
                    if project.platform == "kwork":
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
                            screenshot_path=screenshot_path
                        )
                        
                        if user_price:
                            final_price = user_price
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
                    )
                else:
                    # Для других платформ пока не реализована авто-отправка
                    logger.info(f"Авто-отправка на {project.platform} не реализована, сохраняем в черновики.")
                    success = True # Условно "отправлено" как драфт
            
            if success:
                # Пишем в БД
                self.db.save_proposal(
                    project_id=project.id,
                    platform=project.platform,
                    title=project.title,
                    proposal_text=proposal_str,
                    budget=str(project.budget) if project.budget else None,
                    skills=",".join(project.skills),
                    url=project.url,
                    status="draft" if project.platform not in ["kwork", "fl_ru"] or dry_run else "sent",
                )
                sent_counts[platform] = sent_counts.get(platform, 0) + 1
                total_sent += 1
                
        logger.info(f"=== ЦИКЛ ЗАВЕРШЕН. Отправлено/Сохранено всего: {total_sent} ===")
        return {
            "parsed": len(all_projects),
            "filtered": len(filtered_projects),
            "ai_passed": len(ai_passed),
            "sent": total_sent,
            "per_platform": sent_counts,
        }

    async def stop(self):
        """Гарантированное закрытие всех ресурсов."""
        logger.info("Остановка оркестратора...")
        await self.notifier.stop()
        await self.sender.stop()
        for parser in self.parsers:
            try:
                parser.close()
            except:
                pass
