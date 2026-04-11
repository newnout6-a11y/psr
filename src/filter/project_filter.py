"""
Модуль фильтрации проектов.
Загружает фильтры из config/filters.yaml и применяет к парсингу.
"""

import yaml
from pathlib import Path
from typing import List, Dict, Any, Set
from loguru import logger
from src.parsers.base_parser import ProjectItem
from src.action.proposal_db import ProposalDB


class ProjectFilter:
    def __init__(self, config_path: str = "config/filters.yaml"):
        self.config_path = config_path
        self._load_config()
        self.db = ProposalDB()
    
    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
                
            self.required_skills = [s.lower() for s in config.get("required_skills", [])]
            self.stop_words = [s.lower() for s in config.get("stop_words", [])]
            self.min_budget = config.get("min_budget", 0)
            self.max_age_hours = config.get("max_age_hours", 48)
            logger.info("Фильтры успешно загружены")
        except Exception as e:
            logger.error(f"Ошибка загрузки фильтров: {e}")
            self.required_skills = []
            self.stop_words = []
            self.min_budget = 0
            self.max_age_hours = 0
    
    def filter_projects(self, projects: List[ProjectItem]) -> List[ProjectItem]:
        """Фильтрует список проектов."""
        filtered = []
        seen_urls = set()
        
        # Получаем уже отправленные URL из БД чтобы не спамить
        sent_proposals = self.db.get_sent_proposals(limit=1000)
        sent_urls = {p.get("url") for p in sent_proposals if p.get("url")}
        
        for p in projects:
            # 1. Дедупликация в рамках одной пачки
            if getattr(p, 'url', None) in seen_urls:
                continue
            if getattr(p, 'url', None) in sent_urls:
                continue
            
            seen_urls.add(getattr(p, 'url', None))
            
            # Строим общий текст для анализа
            text = f"{p.title} {p.description}".lower()
            skills_text = " ".join([s.lower() for s in p.skills])
            full_text = f"{text} {skills_text}"
            
            # 2. Stop words check
            if any(word in full_text for word in self.stop_words):
                continue
                
            # 3. Required skills check
            found_skills = []
            for rs in self.required_skills:
                if rs in full_text:
                    found_skills.append(rs)
                    
            if not found_skills:
                continue
                
            # 4. Budget check
            if self.min_budget > 0 and p.budget is not None:
                # В идеале нужна конвертация валют, но упростим
                if p.budget < self.min_budget and p.currency in ["RUB", "RUR"]:
                    continue
            
            # Добавляем к существующим навыкам найденные из фильтра, без дубликатов
            p.skills = list(set(p.skills + found_skills))
            filtered.append(p)
            
        return filtered
