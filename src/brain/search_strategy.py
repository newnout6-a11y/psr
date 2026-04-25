"""AI-генерация поисковых запросов с опорой на query memory."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from src.action.proposal_db import ProposalDB
from src.paths import PORTFOLIO_FILE


class SearchStrategy:
    """Генерация и ранжирование поисковых запросов по платформам."""

    def __init__(self, portfolio_path: str = str(PORTFOLIO_FILE), db: ProposalDB | None = None):
        self.portfolio = self._load_portfolio(portfolio_path)
        self._profile_summary = self._build_profile()
        self._cached_queries: dict[str, list[str]] = {}
        self.db = db or ProposalDB()

    def _load_portfolio(self, path: str) -> dict[str, Any]:
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

    async def generate_queries(self, platform: str, count: int = 8) -> list[str]:
        cache_key = f"{platform}_{count}_{os.getenv('SEARCH_BRIEF', '')}"
        if cache_key in self._cached_queries:
            return self._cached_queries[cache_key]

        user_brief = os.getenv("SEARCH_BRIEF", "").strip()
        if not user_brief:
            raw = os.getenv("SEARCH_QUERY", "python")
            user_brief = raw.replace("|", ", ")

        preferred_queries = [
            item["query_text"]
            for item in self.db.get_query_memory(platform, limit=max(2, count // 3), preferred_only=True)
            if item.get("query_text")
        ]
        learned_queries = [
            item["query_text"]
            for item in self.db.get_query_memory(platform, limit=count * 2)
            if item.get("query_text") and self._is_positive_memory(item)
        ]
        negative_queries = {
            item["query_text"]
            for item in self.db.get_query_memory(platform, limit=count * 4)
            if item.get("query_text") and self._is_negative_memory(item)
        }

        ai_queries: list[str] = []
        try:
            from src.brain.llm_router import get_llm_router

            router = get_llm_router()
            prompt = self._build_prompt(platform, count, user_brief, preferred_queries, learned_queries, negative_queries)
            response = await router.generate(
                prompt=prompt,
                provider=None,
                task="query_generation",
                temperature=0.55,
                max_tokens=400,
                system_prompt=self._system_prompt(),
            )
            ai_queries = self._parse_queries(response)
            if not ai_queries:
                repair = await router.generate(
                    prompt=(
                        "Преобразуй ответ ниже в валидный JSON-массив строк поисковых запросов. "
                        "Только JSON, без пояснений.\n\n"
                        f"{response}"
                    ),
                    provider=None,
                    task="query_generation",
                    temperature=0.0,
                    max_tokens=250,
                    system_prompt="Ты исправляешь ответы в валидный JSON. Верни только JSON-массив строк.",
                )
                ai_queries = self._parse_queries(repair)
        except Exception as e:
            logger.warning(f"SearchStrategy: AI генерация не удалась: {e}")

        merged = self._merge_queries(
            preferred_queries=preferred_queries,
            learned_queries=learned_queries,
            ai_queries=ai_queries,
            negative_queries=negative_queries,
            platform=platform,
            count=count,
        )

        if not merged:
            merged = self._fallback_queries(platform)

        self._cached_queries[cache_key] = merged
        logger.info(
            f"SearchStrategy[{platform}]: brief='{user_brief[:50]}' -> {merged}"
        )
        return merged

    def _system_prompt(self) -> str:
        return (
            "Ты поисковый стратег для фриланс-бирж. "
            "Генерируй короткие реальные поисковые запросы по профилю разработчика и истории сигналов. "
            "Не объясняй ответ. Возвращай только JSON-массив строк."
        )

    def mark_query_preferred(self, platform: str, query_text: str) -> None:
        self.db.mark_query_preferred(platform, query_text)

    def _build_prompt(
        self,
        platform: str,
        count: int,
        user_brief: str,
        preferred_queries: list[str],
        learned_queries: list[str],
        negative_queries: set[str],
    ) -> str:
        if platform == "kwork":
            platform_ctx = (
                "Kwork.ru — русскоязычная биржа микрозадач и фриланса. "
                "Ищем небольшие заказы на автоматизацию, скрипты, парсеры, ботов, API."
            )
        elif platform == "freelance_ru":
            platform_ctx = (
                "Freelance.ru — русскоязычная биржа фриланса. "
                "Запросы должны быть короткими и реально встречаться в проектных карточках."
            )
        elif platform == "hh_ru":
            platform_ctx = (
                "HH.ru — вакансии. Нужны максимально близкие к разовым задачам запросы, "
                "избегать full-time и корпоративных ролей."
            )
        else:
            platform_ctx = "Биржа фриланса. Короткие, прикладные запросы."

        learned_hint = ", ".join(learned_queries[:4]) if learned_queries else "нет"
        preferred_hint = ", ".join(preferred_queries[:4]) if preferred_queries else "нет"
        negative_hint = ", ".join(sorted(negative_queries)[:6]) if negative_queries else "нет"

        return f"""ОПИСАНИЕ ПОЛЬЗОВАТЕЛЯ:
«{user_brief}»

ПРОФИЛЬ РАЗРАБОТЧИКА:
{self._profile_summary}

ПЛАТФОРМА:
{platform_ctx}

УСПЕШНЫЕ ЗАПРОСЫ ИЗ ИСТОРИИ:
{learned_hint}

ПРИОРИТЕТНЫЕ ЗАПРОСЫ ОТ ОПЕРАТОРА:
{preferred_hint}

НЕУДАЧНЫЕ / МУСОРНЫЕ ЗАПРОСЫ:
{negative_hint}

ПРАВИЛА:
- Верни ровно {count} коротких запросов
- 1-3 слова на запрос
- Русский язык для русских платформ
- Избегай вакансий в штат, больших агентских и enterprise-проектов
- Не повторяй запросы из списка неудачных
- Можно опираться на успешные запросы, но не дублировать их полностью больше 2 раз

Ответ строго JSON-массивом строк:
["запрос1", "запрос2"]
"""

    def _parse_queries(self, response: str) -> list[str]:
        json_match = re.search(r"\[.*\]", response, re.DOTALL)
        if not json_match:
            return []
        try:
            queries = json.loads(json_match.group())
        except json.JSONDecodeError:
            return []
        if not isinstance(queries, list):
            return []
        return [self._normalize_query(str(item)) for item in queries if self._normalize_query(str(item))]

    def _merge_queries(
        self,
        *,
        preferred_queries: list[str],
        learned_queries: list[str],
        ai_queries: list[str],
        negative_queries: set[str],
        platform: str,
        count: int,
    ) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add_many(values: list[str]) -> None:
            for value in values:
                normalized = self._normalize_query(value)
                if not normalized or normalized in negative_queries or normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)

        add_many(preferred_queries)
        add_many(learned_queries)
        add_many(ai_queries)
        add_many(self._fallback_queries(platform))
        return out[:count]

    def _normalize_query(self, value: str) -> str:
        text = re.sub(r"\s+", " ", value.strip().lower())
        text = re.sub(r"[^0-9a-zа-яё+\- ]", "", text, flags=re.IGNORECASE)
        return text[:50].strip()

    def _is_positive_memory(self, item: dict[str, Any]) -> bool:
        return (
            item.get("user_preferred", 0)
            or item.get("responded_count", 0) > 0
            or item.get("sent_count", 0) > 0
            or item.get("shortlisted_count", 0) >= 2
        )

    def _is_negative_memory(self, item: dict[str, Any]) -> bool:
        return (
            item.get("runs", 0) >= 3
            and item.get("shortlisted_count", 0) == 0
            and item.get("sent_count", 0) == 0
            and not item.get("user_preferred", 0)
        )

    def _fallback_queries(self, platform: str) -> list[str]:
        if platform == "kwork":
            return [
                "python",
                "telegram бот",
                "парсер",
                "api",
                "автоматизация",
                "скрипт",
                "flask",
                "django",
            ]
        if platform == "freelance_ru":
            return [
                "python",
                "бот",
                "парсер",
                "api",
                "автоматизация",
                "скрипт",
            ]
        if platform == "hh_ru":
            return [
                "python",
                "автоматизация",
                "парсер",
                "telegram бот",
                "api",
                "backend",
            ]
        return ["python", "automation", "bot", "api", "script"]
