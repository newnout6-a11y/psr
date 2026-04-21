"""
Модуль фильтрации проектов.
Загружает фильтры из config/filters.yaml и применяет к парсингу.
"""

import yaml
from loguru import logger
from typing import List
from datetime import datetime, timezone

from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem
from src.utils.currency import get_converter
from src.utils.time_utils import parse_created_at_datetime


class ProjectFilter:
    def __init__(self, config_path: str = "config/filters.yaml"):
        self.config_path = config_path
        self._load_config()
        self.db = ProposalDB()
        self._converter = None
    
    async def _get_converter(self):
        if self._converter is None:
            self._converter = await get_converter()
        return self._converter

    def _project_age_hours(self, project: ProjectItem) -> float | None:
        created_dt = parse_created_at_datetime(project.created_at, project.platform)
        if created_dt is None:
            return None

        if created_dt.tzinfo is not None:
            age_seconds = (datetime.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds()
        else:
            age_seconds = (datetime.now() - created_dt).total_seconds()

        return max(age_seconds, 0) / 3600

    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
                
            self.required_skills = [s.lower() for s in config.get("required_skills", [])]
            self.stop_words = [s.lower() for s in config.get("stop_words", [])]
            self.min_budget = config.get("min_budget", 0)
            self.max_budget = config.get("max_budget", 0)
            self.per_page = config.get("per_page", 20)
            self.max_age_hours = config.get("max_age_hours", 48)
            self.max_proposals = config.get("max_proposals", 0)
            logger.info(
                f"Фильтры загружены: бюджет {self.min_budget}-{self.max_budget or '∞'}₽, "
                f"возраст ≤{self.max_age_hours}ч, per_page={self.per_page}"
            )
        except Exception as e:
            logger.error(f"Ошибка загрузки фильтров: {e}")
            self.required_skills = []
            self.stop_words = []
            self.min_budget = 0
            self.max_budget = 0
            self.per_page = 20
            self.max_age_hours = 0
            self.max_proposals = 0
    
    async def filter_projects(self, projects: List[ProjectItem]) -> List[ProjectItem]:
        filtered = []
        seen_urls = set()
        
        sent_proposals = self.db.get_sent_proposals(limit=1000)
        sent_urls = {p.get("url") for p in sent_proposals if p.get("url")}
        
        converter = await self._get_converter()
        
        for p in projects:
            if getattr(p, 'url', None) in seen_urls:
                continue
            if getattr(p, 'url', None) in sent_urls:
                continue
            
            seen_urls.add(getattr(p, 'url', None))

            if self.max_proposals > 0 and (p.offers_count or 0) > self.max_proposals:
                logger.debug(
                    f"Проект '{p.title[:30]}' отсеян: откликов {p.offers_count} > лимита {self.max_proposals}"
                )
                continue

            if self.max_age_hours > 0:
                age_hours = self._project_age_hours(p)
                if age_hours is not None and age_hours > self.max_age_hours:
                    logger.debug(
                        f"Проект '{p.title[:30]}' отсеян: возраст {age_hours:.1f}ч > {self.max_age_hours}ч"
                    )
                    continue
            
            text = f"{p.title} {p.description}".lower()
            skills_text = " ".join([s.lower() for s in p.skills])
            full_text = f"{text} {skills_text}"
            
            if any(word in full_text for word in self.stop_words):
                continue
                
            found_skills = []
            for rs in self.required_skills:
                if rs in full_text:
                    found_skills.append(rs)
                    
            if not found_skills:
                continue
                
            # Budget check with currency conversion
            if p.budget is not None:
                budget_rub = await converter.to_rub(p.budget, p.currency)
                if self.min_budget > 0 and budget_rub < self.min_budget:
                    logger.debug(f"Проект '{p.title[:30]}' отсеян: бюджет {budget_rub:.0f}₽ < {self.min_budget}₽")
                    continue
                if self.max_budget > 0 and budget_rub > self.max_budget:
                    logger.debug(f"Проект '{p.title[:30]}' отсеян: бюджет {budget_rub:.0f}₽ > {self.max_budget}₽")
                    continue
            
            p.skills = list(set(p.skills + found_skills))
            filtered.append(p)
            
        return filtered
