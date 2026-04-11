"""
Парсер RemoteOK.com
Открытый API для поиска удалённых IT-вакансий.
"""

from typing import Optional, Dict, Any, List
from .base_parser import BaseParser, ProjectItem
from loguru import logger


class RemoteOKParser(BaseParser):
    """Парсер для RemoteOK.com."""
    
    PLATFORM_NAME = "remoteok"
    BASE_URL = "https://remoteok.com"
    API_URL = "https://remoteok.com/api"
    
    def _default_headers(self) -> Dict[str, str]:
        headers = super()._default_headers()
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        return headers
        
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Поиск вакансий через RemoteOK API."""
        logger.info(f"Поиск вакансий на RemoteOK (страница {page})...")
        
        filters = filters or {}
        tag = filters.get("tag", "python")
        
        try:
            response = self._safe_get(
                f"{self.API_URL}",
                params={"tag": tag},
            )
            
            data = response.json()
            # RemoteOK возвращает массив, первый элемент - метаданные
            if isinstance(data, list) and len(data) > 0:
                items = data[1:] if isinstance(data[0], dict) and 'last_updated' in data[0] else data
                logger.info(f"Получено {len(items)} вакансий с RemoteOK")
                return self._normalize_vacancies(items[:per_page])
            
            return []
        
        except Exception as e:
            logger.error(f"Исключение при парсинге RemoteOK: {e}")
            return []
    
    async def get_project_details(self, vacancy_id: str) -> Optional[ProjectItem]:
        return None
    
    def _normalize_vacancies(self, raw_items: List[Dict]) -> List[ProjectItem]:
        """Нормализация вакансий."""
        normalized = []
        for raw in raw_items:
            salary = raw.get("salary") or raw.get("salaryminmax", "")
            tags = raw.get("tags", [])
            
            # Simple parsing for budget if needed, RemoteOK usually gives e.g. "50k-100k" or similar in strings if not structured.
            
            normalized.append(ProjectItem(
                id=str(raw.get("id", raw.get("slug", ""))),
                title=raw.get("position", raw.get("title", "")),
                description=raw.get("description", ""),
                budget=None, # RemoteOK provides complex budget string
                currency="USD",
                skills=tags if isinstance(tags, list) else [],
                created_at=raw.get("date", ""),
                url=raw.get("url", f"https://remoteok.com{raw.get('slug', '')}"),
                platform=self.PLATFORM_NAME,
                client_id=raw.get("company", ""),
            ))
        return normalized
