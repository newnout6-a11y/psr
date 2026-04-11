"""
Парсер Upwork.
Использует публичный RSS feed для обхода Cloudflare и капчи.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class UpworkParser(BaseParser):
    """Парсер для Upwork."""
    
    PLATFORM_NAME = "upwork"
    BASE_URL = "https://www.upwork.com"
    API_URL = "https://www.upwork.com/ab/feed/jobs/rss"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Поиск вакансий через RSS."""
        # RSS выдает обычно последние 20-50 айтемов и не пагинируется легко, 
        # поэтому параметр page здесь условный.
        logger.info(f"Поиск вакансий на Upwork RSS...")
        
        filters = filters or {}
        query = filters.get("query", "python")
        
        try:
            response = self._safe_get(
                self.API_URL,
                params={"q": query, "sort": "recency"},
            )
            
            return self._parse_rss(response.text)[:per_page]
        
        except Exception as e:
            logger.error(f"Исключение при парсинге Upwork RSS: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_rss(self, xml_text: str) -> List[ProjectItem]:
        """Нормализация XML в ProjectItem."""
        soup = BeautifulSoup(xml_text, "xml")
        items = soup.find_all("item")
        
        normalized = []
        for item in items:
            title = item.title.text if item.title else ""
            desc = item.description.text if item.description else ""
            link = item.link.text if item.link else ""
            pub_date = item.pubDate.text if item.pubDate else ""
            
            # Извлечение бюджета из текста
            budget = None
            budget_match = re.search(r'<b>Budget</b>:\s*\$([\d,]+)', desc)
            if budget_match:
                budget = float(budget_match.group(1).replace(",", ""))
            else:
                # Hourly range
                hourly_match = re.search(r'<b>Hourly Range</b>:\s*\$([\d.]+)\s*-\s*\$([\d.]+)', desc)
                if hourly_match:
                    budget = (float(hourly_match.group(1)) + float(hourly_match.group(2))) / 2
            
            # Навыки
            skills = []
            skills_match = re.search(r'<b>Skills</b>:\s*([^<]+)', desc)
            if skills_match:
                skills = [s.strip() for s in skills_match.group(1).split(",")]
            
            # ID
            project_id = ""
            id_match = re.search(r'~([a-zA-Z0-9]+)', link)
            if id_match:
                project_id = id_match.group(1)
            
            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description=BeautifulSoup(desc, "lxml").text, # clean HTML tags from description
                budget=budget,
                currency="USD",
                skills=skills,
                created_at=pub_date,
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        return normalized
