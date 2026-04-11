"""
Модуль для реверс-инжиниринга скрытых API фриланс-платформ.
Автоматический анализ HAR-дампов, извлечение эндпоинтов и генерация клиентов.
"""

import json
from typing import Optional, Dict, List, Any
from pathlib import Path
from loguru import logger


class APIReverser:
    """
    Реверс-инжиниринг API через анализ HAR-дампов и DevTools.
    Автоматическое извлечение эндпоинтов, токенов, CSRF.
    """
    
    def __init__(self):
        self.endpoints = {}
        self.auth_tokens = {}
        self.csrf_tokens = {}
    
    def analyze_har(self, har_file: str) -> Dict[str, Any]:
        """
        Анализ HAR-файла (экспорт из DevTools/mitmproxy).
        Извлекает все API-вызовы, авторизацию, параметры.
        """
        logger.info(f"Анализ HAR-файла: {har_file}")
        
        with open(har_file, "r", encoding="utf-8") as f:
            har_data = json.load(f)
        
        entries = har_data.get("log", {}).get("entries", [])
        api_calls = []
        
        for entry in entries:
            request = entry.get("request", {})
            url = request.get("url", "")
            method = request.get("method", "")
            
            # Фильтруем только API-вызовы
            if self._is_api_call(url):
                api_call = {
                    "url": url,
                    "method": method,
                    "headers": {h["name"]: h["value"] for h in request.get("headers", [])},
                    "query_string": self._parse_query_string(request.get("queryString", [])),
                    "post_data": request.get("postData", {}).get("text"),
                    "response_status": entry.get("response", {}).get("status"),
                }
                api_calls.append(api_call)
        
        logger.info(f"Найдено {len(api_calls)} API-вызовов")
        self.endpoints = self._group_endpoints(api_calls)
        return self.endpoints
    
    def extract_csrf_tokens(self, html_content: str) -> Dict[str, str]:
        """
        Извлечение CSRF/CSRF-токенов из HTML.
        Ищет в <meta> тегах, скрытых <input>, JavaScript-переменных.
        """
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html_content, "lxml")
        tokens = {}
        
        # <meta name="csrf-token" content="...">
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        if csrf_meta:
            tokens["csrf_token"] = csrf_meta.get("content", "")
        
        # <input type="hidden" name="_token" value="...">
        csrf_input = soup.find("input", attrs={"name": lambda x: x and "token" in x.lower()})
        if csrf_input:
            tokens["hidden_token"] = csrf_input.get("value", "")
        
        # JavaScript-переменные (window.CSRF_TOKEN = "...")
        import re
        script_tags = soup.find_all("script")
        for script in script_tags:
            if script.string:
                match = re.search(r'(?:csrf|token|secret)["\s:=]+["\']([^"\']{10,})["\']', script.string, re.IGNORECASE)
                if match:
                    tokens["js_token"] = match.group(1)
        
        self.csrf_tokens = tokens
        logger.info(f"Извлечено CSRF-токенов: {len(tokens)}")
        return tokens
    
    def extract_dynamic_tokens(self, html_content: str) -> Dict[str, str]:
        """
        Извлечение динамических токенов из JavaScript (JWT, Bearer, session).
        """
        import re
        
        tokens = {}
        
        # JWT токены
        jwt_pattern = r'["\']?token["\']?\s*:\s*["\']([eyJ][a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)["\']'
        matches = re.findall(jwt_pattern, html_content)
        if matches:
            tokens["jwt"] = matches[0]
        
        # Bearer токены
        bearer_pattern = r'["\']?(?:bearer|api_key|authorization)["\']?\s*:\s*["\']([a-zA-Z0-9_-]{20,})["\']'
        matches = re.findall(bearer_pattern, html_content, re.IGNORECASE)
        if matches:
            tokens["bearer"] = matches[0]
        
        # Session ID
        session_pattern = r'["\']?(?:session|sid|session_id)["\']?\s*:\s*["\']([a-zA-Z0-9]{16,})["\']'
        matches = re.findall(session_pattern, html_content, re.IGNORECASE)
        if matches:
            tokens["session"] = matches[0]
        
        self.auth_tokens = tokens
        logger.info(f"Извлечено динамических токенов: {len(tokens)}")
        return tokens
    
    def generate_client(self, platform: str, output_dir: str = "src/api"):
        """
        Генерация Python-клиента для API на основе извлечённых данных.
        """
        logger.info(f"Генерация API клиента для {platform}...")
        
        client_code = self._build_client_template(platform)
        output_path = Path(output_dir) / f"{platform}_client.py"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(client_code)
        
        logger.info(f"Клиент сохранён: {output_path}")
        return output_path
    
    def _is_api_call(self, url: str) -> bool:
        """Определить, является ли URL API-вызовом."""
        api_indicators = [
            "/api/",
            "/graphql",
            "/v1/",
            "/v2/",
            "/v3/",
            "/rest/",
            "ajax",
            "fetch",
            ".json",
        ]
        return any(indicator in url.lower() for indicator in api_indicators)
    
    def _parse_query_string(self, qs_list: List[Dict]) -> Dict[str, str]:
        """Парсинг query string."""
        return {item["name"]: item.get("value", "") for item in qs_list}
    
    def _group_endpoints(self, api_calls: List[Dict]) -> Dict[str, List[Dict]]:
        """Группировка эндпоинтов по базовому URL."""
        grouped = {}
        for call in api_calls:
            # Извлекаем базовый URL (домена + первый путь)
            from urllib.parse import urlparse
            parsed = urlparse(call["url"])
            base = f"{parsed.netloc}{parsed.path.split('/')[1]}" if len(parsed.path.split('/')) > 1 else parsed.netloc
            
            if base not in grouped:
                grouped[base] = []
            grouped[base].append(call)
        
        return grouped
    
    def _build_client_template(self, platform: str) -> str:
        """Генерация шаблона API-клиента."""
        platform_class = platform.title().replace("_", "")
        return f'''"""
Автоматически сгенерированный API клиент для {platform}.
Сгенерировано APIReverser.
"""

from typing import Optional, Dict, Any, List
from src.evolution import TLSClient
from loguru import logger


class {platform_class}Client:
    """Клиент для работы с API {platform}."""
    
    BASE_URL = "https://www.{platform}.com"
    API_URL = "https://api.{platform}.com"
    
    def __init__(self, proxy: Optional[str] = None):
        self.client = TLSClient(browser="chrome_120", proxy=proxy)
        self.auth_headers = {{}}
    
    def authenticate(self, token: str, token_type: str = "bearer"):
        """Установить токен авторизации."""
        if token_type == "bearer":
            self.auth_headers["Authorization"] = "Bearer " + token
        elif token_type == "jwt":
            self.auth_headers["Authorization"] = "Bearer " + token
        elif token_type == "cookie":
            self.client.set_cookies({{"session_token": token}})
        
        logger.info("Авторизация установлена")
    
    def set_csrf_token(self, token: str):
        """Установить CSRF-токен."""
        self.auth_headers["X-CSRF-Token"] = token
        self.auth_headers["X-Requested-With"] = "XMLHttpRequest"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить список проектов/заказов.
        """
        endpoint = "/api/v1/projects"
        params = {{"page": page, "per_page": per_page}}
        if filters:
            params.update(filters)
        
        response = self.client.get(
            self.BASE_URL + endpoint,
            params=params,
            headers=self.auth_headers,
        )
        
        if response.status_code == 200:
            data = response.json()
            logger.info("Получено " + str(len(data)) + " проектов")
            return data
        
        logger.error("Ошибка получения проектов: " + str(response.status_code))
        return []
    
    async def get_project_details(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Получить детали конкретного проекта."""
        endpoint = "/api/v1/projects/" + project_id
        
        response = self.client.get(
            self.BASE_URL + endpoint,
            headers=self.auth_headers,
        )
        
        if response.status_code == 200:
            return response.json()
        
        return None
    
    async def submit_proposal(
        self,
        project_id: str,
        cover_letter: str,
        budget: Optional[float] = None,
    ) -> bool:
        """
        Отправить отклик на проект.
        """
        endpoint = "/api/v1/projects/" + project_id + "/proposals"
        payload = {{"cover_letter": cover_letter}}
        if budget:
            payload["budget"] = budget
        
        response = self.client.post(
            self.BASE_URL + endpoint,
            json=payload,
            headers=self.auth_headers,
        )
        
        if response.status_code in [200, 201]:
            logger.info("Отклик отправлен на проект " + project_id)
            return True
        
        logger.error("Ошибка отправки отклика: " + str(response.status_code))
        return False
    
    def close(self):
        """Закрыть сессию."""
        self.client.close()
'''
