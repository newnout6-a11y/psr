"""
Парсер Freelancer.com
Через официальный REST API.
"""

from typing import Optional, Dict, Any, List
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import html


class FreelancerComParser(BaseParser):
    """Парсер для Freelancer.com."""
    
    PLATFORM_NAME = "freelancer_com"
    BASE_URL = "https://www.freelancer.com"
    API_URL = "https://www.freelancer.com/api/projects/0.1/projects/active"
    
    def _default_headers(self) -> Dict[str, str]:
        headers = super()._default_headers()
        # Freelancer checks typical browser headers strictly sometimes, we rely on curl_cffi Chrome 120
        return headers
        
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг проектов через REST API."""
        logger.info(f"Парсинг Freelancer.com API (страница {page})...")
        filters = filters or {}
        
        # job_details to get skills, full_description to get description, limit max 100
        params = {
            "compact": "true",
            "job_details": "true",
            "full_description": "true",
            "limit": per_page,
            "offset": (page - 1) * per_page,
            "jobs[]": [3, 7, 335, 336, 17, 323], # Some Web Dev / SE job categories
            "languages[]": ["en", "ru"],
        }
        
        try:
            response = self._safe_get(
                self.API_URL, 
                params=params, 
            )
            
            data = response.json()
            if data.get("status") == "success":
                projects = data.get("result", {}).get("projects", [])
                logger.info(f"Получено {len(projects)} проектов с Freelancer.com")
                return self._normalize_projects(projects)
            
            logger.warning(f"Freelancer.com API returned non-success: {data.get('status')}")
            return []
        
        except Exception as e:
            logger.error(f"Freelancer.com exception: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
        
    def _normalize_projects(self, raw_projects: List[Dict]) -> List[ProjectItem]:
        """Нормализация через API схему."""
        normalized = []
        for raw in raw_projects:
            budget = raw.get("budget", {})
            min_b = budget.get("minimum")
            max_b = budget.get("maximum")
            avg_b = (min_b + max_b)/2 if min_b and max_b else (min_b or max_b)
            
            jobs = raw.get("jobs", [])
            skills = [j.get("name") for j in jobs if j.get("name")]
            
            desc = html.unescape(raw.get("description", ""))
            
            normalized.append(ProjectItem(
                id=str(raw.get("id")),
                title=html.unescape(raw.get("title", "")),
                description=desc,
                budget=avg_b,
                currency=raw.get("currency", {}).get("code", "USD"),
                skills=skills,
                created_at=str(raw.get("submitdate", "")), # unix timestamp
                url=f"{self.BASE_URL}/projects/{raw.get('seo_url', '')}",
                platform=self.PLATFORM_NAME,
                client_id=str(raw.get("owner_id", "")),
            ))
        
        return normalized
