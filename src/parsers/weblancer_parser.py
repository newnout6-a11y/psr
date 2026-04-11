"""
Парсер Weblancer.net.
Использует HTML парсинг списка проектов.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class WeblancerParser(BaseParser):
    """Парсер для Weblancer.net."""
    
    PLATFORM_NAME = "weblancer"
    BASE_URL = "https://www.weblancer.net"
    API_URL = "https://www.weblancer.net/jobs/"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на Weblancer.net (страница {page})...")
        
        try:
            # Weblancer uses URLs like /jobs/?page=2
            response = self._safe_get(
                self.API_URL,
                params={"page": page},
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Исключение при парсинге Weblancer.net: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        soup = BeautifulSoup(html_text, "lxml")
        rows = soup.find_all("div", class_="row")
        
        normalized = []
        for row in rows:
            title_el = row.find("h2")
            if not title_el:
                continue
                
            a_tag = title_el.find("a")
            if not a_tag:
                 continue
                 
            title = a_tag.text.strip()
            link = f"{self.BASE_URL}{a_tag['href']}"
            
            # ID
            project_id = ""
            id_match = re.search(r'-(\d+)/', link)
            if id_match:
                project_id = id_match.group(1)
            
            budget = None
            currency = "USD"
            price_el = row.find("div", class_="amount")
            if price_el:
                price_text = price_el.text.strip()
                if "€" in price_text:
                    currency = "EUR"
                elif "₽" in price_text or "руб" in price_text:
                    currency = "RUB"
                    
                num_match = re.search(r'([\d\s]+)', price_text)
                if num_match:
                    budget_str = num_match.group(1).replace(" ", "")
                    if budget_str.isdigit():
                        budget = float(budget_str)
            
            desc_el = row.find("p", class_="text-muted")
            desc = desc_el.text.strip() if desc_el else ""
            
            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description=desc,
                budget=budget,
                currency=currency,
                skills=[],
                created_at="",
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        # Weblancer returns lots of empty matches sometimes, filter ones with empty title
        return [p for p in normalized if p.title]
