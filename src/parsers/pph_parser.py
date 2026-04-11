"""
Парсер PeoplePerHour.com
Использует HTML парсинг, обходит Cloudflare через curl_cffi.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class PeoplePerHourParser(BaseParser):
    """Парсер для PeoplePerHour."""
    
    PLATFORM_NAME = "peopleperhour"
    BASE_URL = "https://www.peopleperhour.com"
    API_URL = "https://www.peopleperhour.com/freelance-jobs/technology-programming"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на PeoplePerHour (страница {page})...")
        
        try:
            # Pagination on PPH is generally passed in URL as e.g. /technology-programming?page=1
            response = self._safe_get(
                self.API_URL,
                params={"page": page},
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Исключение при парсинге PeoplePerHour: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        """Парсинг карточек проектов."""
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", class_=re.compile(r"project-item\b", re.IGNORECASE))
        if not cards:
            # Another wrapper class from PPH layout
            cards = soup.find_all("li", class_=re.compile(r"project-list-item\b", re.IGNORECASE))
            
        if not cards:
            cards = soup.find_all("div", class_="item") # fallback generic assumption
            
        normalized = []
        for item in cards:
            # Need to find title link
            title_el = item.find("h6") or item.find("h5") or item.find("h3")
            if not title_el:
                continue
                
            a_tag = title_el.find("a")
            if not a_tag:
                 continue
                 
            title = a_tag.text.strip()
            link = a_tag['href']
            if link.startswith('/'):
                 link = f"{self.BASE_URL}{link}"
            
            # Budget
            budget = None
            currency = "USD"
            price_el = item.find("div", class_=re.compile(r"price\b", re.IGNORECASE))
            if price_el:
                price_text = price_el.text.strip()
                if "£" in price_text:
                    currency = "GBP"
                elif "€" in price_text:
                    currency = "EUR"
                    
                num_match = re.search(r'([\d.,]+)', price_text)
                if num_match:
                    try:
                        budget = float(num_match.group(1).replace(",", ""))
                    except:
                        pass
            
            normalized.append(ProjectItem(
                id="",
                title=title,
                description="", 
                budget=budget,
                currency=currency,
                skills=[],
                created_at="",
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        return normalized
