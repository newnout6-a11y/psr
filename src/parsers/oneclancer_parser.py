"""
Парсер 1CLancer.ru.
Использует HTML парсинг списка проектов.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class OneCLancerParser(BaseParser):
    """Парсер для 1CLancer.ru."""
    
    PLATFORM_NAME = "oneclancer"
    BASE_URL = "https://1clancer.ru"
    API_URL = "https://1clancer.ru/zakazy/"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на 1CLancer.ru (страница {page})...")
        
        try:
            response = self._safe_get(
                self.API_URL, # No easy pagination params, simple approach for now
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Исключение при парсинге 1CLancer.ru: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        soup = BeautifulSoup(html_text, "lxml")
        table = soup.find("table", class_="table")
        if not table:
            return []
            
        rows = table.find_all("tr")
        
        normalized = []
        for row in rows:
            title_a = row.find("a", class_="order_title")
            if not title_a:
                continue
                 
            title = title_a.text.strip()
            link = f"{self.BASE_URL}{title_a['href']}" if title_a['href'].startswith('/') else title_a['href']
            
            # ID
            project_id = ""
            id_match = re.search(r'id=(\d+)', link)
            if id_match:
                project_id = id_match.group(1)
            
            budget = None
            price_td = row.find("td", class_="order_price")
            if price_td:
                price_text = price_td.text.strip().replace(" ", "")
                num_match = re.search(r'([\d]+)', price_text)
                if num_match:
                    budget = float(num_match.group(1))
            
            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description="", # In list view description is absent or small block
                budget=budget,
                currency="RUB",
                skills=[],
                created_at="",
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        return normalized
