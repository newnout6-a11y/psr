"""
Парсер Freelance.ru.
Использует HTML парсинг списка проектов.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class FreelanceRuParser(BaseParser):
    """Парсер для Freelance.ru."""
    
    PLATFORM_NAME = "freelance_ru"
    BASE_URL = "https://freelance.ru"
    API_URL = "https://freelance.ru/projects/"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на Freelance.ru (страница {page})...")
        
        # spec=4 это категория Программирование / IT
        params = {
            "spec": 4,
            "page": page,
        }
        
        try:
            response = self._safe_get(
                self.API_URL,
                params=params,
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Исключение при парсинге Freelance.ru: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        """Парсинг .project-item-default-card"""
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", class_="projects-list")
        if not cards:
            return []
            
        items = cards[0].find_all("div", class_="project-item-default-card")
        
        normalized = []
        for item in items:
            title_el = item.find("h2", class_="title")
            if not title_el:
                continue
                
            a_tag = title_el.find("a")
            if not a_tag:
                continue
                
            title = a_tag.text.strip()
            link = f"{self.BASE_URL}{a_tag['href']}" if a_tag['href'].startswith('/') else a_tag['href']
            
            # ID
            project_id = ""
            id_match = re.search(r'/projects/(\d+)', link)
            if id_match:
                project_id = id_match.group(1)
            
            # Budget
            budget = None
            price_el = item.find("h2", class_="cost")
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                num_match = re.search(r'(\d+)', price_text)
                if num_match:
                    budget = float(num_match.group(1))
            
            # Description
            desc_el = item.find("div", class_="description")
            desc = desc_el.text.strip() if desc_el else ""
            
            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description=desc,
                budget=budget,
                currency="RUB",
                skills=[],
                created_at="",
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        return normalized
