"""
Парсер HH.ru (HeadHunter)
Открытое API, не требует авторизации для поиска.
"""

from typing import Optional, Dict, Any, List
from .base_parser import BaseParser, ProjectItem
from loguru import logger


class HHParser(BaseParser):
    """Парсер для HH.ru."""
    
    PLATFORM_NAME = "hh_ru"
    BASE_URL = "https://api.hh.ru"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Поиск вакансий через HH.ru API."""
        logger.info(f"Поиск вакансий на HH.ru (страница {page})...")
        filters = filters or {}
        
        search_text = filters.get("query", "python разработчик")
        
        params = {
            "text": search_text,
            "page": page - 1,  # HH.ru нумерует с 0
            "per_page": per_page,
            "area": filters.get("area", 113),  # Россия
            "professional_role": 96, # Программист, разработчик
            "order_by": "publication_time",
        }
        
        if filters.get("salary"):
            params["salary"] = filters["salary"]
        
        try:
            response = self._safe_get(
                f"{self.BASE_URL}/vacancies",
                params=params,
            )
            
            data = response.json()
            items = data.get("items", [])
            logger.info(f"Получено {len(items)} вакансий с HH.ru (всего: {data.get('found', 0)})")
            return self._normalize_vacancies(items)
        
        except Exception as e:
            logger.error(f"Исключение при парсинге HH.ru: {e}")
            return []
    
    async def get_project_details(self, vacancy_id: str) -> Optional[ProjectItem]:
        """Получить детали вакансии."""
        try:
            response = self._safe_get(
                f"{self.BASE_URL}/vacancies/{vacancy_id}",
            )
            return self._normalize_vacancy(response.json())
        except Exception as e:
            logger.error(f"Ошибка получения деталей вакансии {vacancy_id}: {e}")
            return None
    
    def _normalize_vacancies(self, raw_items: List[Dict]) -> List[ProjectItem]:
        """Нормализация вакансий."""
        normalized = []
        for raw in raw_items:
            salary = raw.get("salary")
            employer_name = raw.get("employer", {}).get("name", "")
            
            normalized.append(ProjectItem(
                id=str(raw.get("id", "")),
                title=raw.get("name", ""),
                description=raw.get("snippet", {}).get("requirement", "") or "",
                budget=salary.get("to") or salary.get("from") if salary else None,
                currency=salary.get("currency", "RUB") if salary else "RUB",
                skills=self._extract_skills(raw),
                created_at=raw.get("published_at", ""),
                url=raw.get("alternate_url", ""),
                platform=self.PLATFORM_NAME,
                client_id=str(raw.get("employer", {}).get("id", "")) if raw.get("employer", {}).get("id") else None,
            ))
        return normalized
    
    def _normalize_vacancy(self, raw: Dict) -> ProjectItem:
        return self._normalize_vacancies([raw])[0]
    
    def _extract_skills(self, raw: Dict) -> List[str]:
        """Извлечение ключевых навыков."""
        key_skills = raw.get("key_skills", [])
        return [s.get("name", "") for s in key_skills if s.get("name")]
