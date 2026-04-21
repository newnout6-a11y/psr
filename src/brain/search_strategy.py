"""
AI-генерация поисковых запросов для парсинга фриланс-платформ.
Пользователь описывает что ищет своими словами → AI генерирует
конкретные поисковые запросы под каждую платформу.
"""

import os
import json
import re
from typing import List, Dict, Any
from loguru import logger
from src.paths import PORTFOLIO_FILE


class SearchStrategy:
    """Генерация поисковых запросов через AI из пользовательского описания."""

    def __init__(self, portfolio_path: str = str(PORTFOLIO_FILE)):
        self.portfolio = self._load_portfolio(portfolio_path)
        self._profile_summary = self._build_profile()
        self._cached_queries: Dict[str, List[str]] = {}

    def _load_portfolio(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _build_profile(self) -> str:
        dev = self.portfolio.get("developer", {})
        skills = dev.get("skills", [])
        highlights = self.portfolio.get("portfolio_highlights", [])
        return (
            f"Навыки: {', '.join(skills)}. "
            f"Опыт: {dev.get('experience_years', 3)} лет. "
            f"Примеры: {'; '.join(highlights[:3]) if highlights else 'веб, автоматизация'}."
        )

    async def generate_queries(self, platform: str, count: int = 8) -> List[str]:
        """Генерация поисковых запросов из описания пользователя."""
        cache_key = f"{platform}_{count}"
        if cache_key in self._cached_queries:
            return self._cached_queries[cache_key]

        # Описание от пользователя (своими словами)
        user_brief = os.getenv("SEARCH_BRIEF", "").strip()
        if not user_brief:
            # Fallback на SEARCH_QUERY если нет описания
            raw = os.getenv("SEARCH_QUERY", "python")
            user_brief = raw.replace("|", ", ")

        try:
            from src.brain.llm_router import get_llm_router
            router = get_llm_router()

            prompt = self._build_prompt(platform, count, user_brief)
            response = await router.generate(
                prompt=prompt,
                provider=None,
                temperature=0.8,
                max_tokens=400,
            )
            queries = self._parse_queries(response)

            if queries:
                self._cached_queries[cache_key] = queries
                logger.info(f"SearchStrategy: из описания «{user_brief[:50]}» → {len(queries)} запросов для {platform}: {queries}")
                return queries
        except Exception as e:
            logger.warning(f"SearchStrategy: AI генерация не удалась: {e}")

        # Fallback
        fallback = self._fallback_queries(platform)
        self._cached_queries[cache_key] = fallback
        return fallback

    def _build_prompt(self, platform: str, count: int, user_brief: str) -> str:
        if platform == "kwork":
            platform_ctx = (
                "Kwork.ru — русскоязычная биржа микрозадач и фриланса. "
                "Категории: скрипты (11), веб-разработка (79). "
                "Поиск по названию и описанию проектов. "
                "Бюджет 500-10000 руб. Заказы на русском."
            )
        elif platform == "freelance_ru":
            platform_ctx = "Freelance.ru — русскоязычная биржа фриланса. Поиск по ключевым словам (?keyword=...). Бюджет обычно в рублях."
        elif platform == "hh_ru":
            platform_ctx = "HH.ru — русскоязычная площадка вакансий. Нужны короткие запросы по стеку и автоматизации, без full-time корпоративных ролей."
        else:
            platform_ctx = "Международная биржа фриланса. Проекты на английском."

        return f"""Ты — поисковый движок для фриланс-биржи. Пользователь описал что ищет своими словами.
Твоя задача — превратить это описание в конкретные поисковые запросы для парсинга.

ОПИСАНИЕ ПОЛЬЗОВАТЕЛЯ:
«{user_brief}»

ПРОФИЛЬ РАЗРАБОТЧИКА:
{self._profile_summary}

ПЛАТФОРМА: {platform_ctx}

ПРАВИЛА ГЕНЕРАЦИИ ЗАПРОСОВ:
- Каждый запрос — 1-3 слова, конкретный, для поиска на бирже
- Запросы должны находить РЕАЛЬНЫЕ фриланс-заказы, не вакансии
- Бюджет до 10000 руб — не крупные проекты, не на постоянку
- Исключить: full-time, долгосрочные, корпоративные позиции
- Разнообразие: разные формулировки, синонимы, смежные темы
- Запросы на языке платформы (русский для Kwork/Freelance.ru/HH.ru)

Сгенерируй ровно {count} запросов в формате JSON массива строк:
["запрос1", "запрос2", ...]

Только JSON, ничего больше!"""

    def _parse_queries(self, response: str) -> List[str]:
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if not json_match:
            return []
        try:
            queries = json.loads(json_match.group())
            if isinstance(queries, list):
                return [str(q).strip() for q in queries if str(q).strip()]
        except json.JSONDecodeError:
            pass
        return []

    def _fallback_queries(self, platform: str) -> List[str]:
        if platform == "kwork":
            return [
                "python", "telegram бот", "парсер", "api",
                "автоматизация", "скрипт", "flask", "django",
            ]
        elif platform == "freelance_ru":
            return [
                "python", "бот", "парсер", "api",
                "автоматизация", "скрипт",
            ]
        elif platform == "hh_ru":
            return [
                "python", "автоматизация", "парсер",
                "telegram бот", "api", "backend",
            ]
        return ["python", "automation", "bot", "api", "script"]
