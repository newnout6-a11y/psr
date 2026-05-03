"""Kwork-profile OSINT: парсит публичную страницу заказчика.

Стратегия: Kwork — SPA, и весь контент страницы /user/<username> рендерится
из `window.stateData` JSON-payload. Сначала пробуем выдрать его через общий
парсер `KworkStateDataParser` — это стабильнее, чем регулярки по верстке,
и даёт типизированные поля (рейтинг, число завершённых заказов, отзывы).

Если StateData отсутствует (например, страница вернула пустой каркас для
анонимного клиента) — возвращаем пустой список. Никакой регулярной
эвристики поверх такой страницы быть не должно: она генерирует ложные
"отзывы" с произвольной тональностью.

Если StateData есть, но без отзывов в payload (бывает на отдельных
профилях) — отдаём только profile-finding. Лучше отсутствие данных,
чем мусор.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

import aiohttp
from loguru import logger

from src.platforms.kwork import KworkStateDataParser

from ..base import OSINTFinding, OSINTProvider


_REVIEW_KEY_CANDIDATES = ("reviews", "feedbacks", "comments", "lastReviews")
_USER_DATA_KEY_CANDIDATES = ("userData", "user", "profile", "userProfile")


def _clean_html(text: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _normalize_polarity(value: Any) -> bool | None:
    """Возвращает True/False/None.
    None означает «полярность неизвестна» — не считаем такой отзыв ни плюсом,
    ни минусом.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value > 0:
            return True
        if value < 0:
            return False
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"positive", "good", "+", "1", "true", "thumb_up", "thumbsup"}:
            return True
        if v in {"negative", "bad", "-", "0", "false", "thumb_down", "thumbsdown"}:
            return False
    return None


class KworkProfileProvider(OSINTProvider):
    """Скрейп /user/<username>/ — забор отзывов и био из window.stateData."""

    name = "kwork_profile"
    BASE_URL = "https://kwork.ru/user"
    timeout_sec = 12.0

    async def search(self, query: str, **ctx: Any) -> list[OSINTFinding]:
        username = (query or "").strip().lstrip("@")
        if not username:
            return []

        url = f"{self.BASE_URL}/{username}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ru,en;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(url, allow_redirects=True) as r:
                    if r.status == 404:
                        return []
                    if r.status != 200:
                        logger.debug(f"Kwork profile {username}: HTTP {r.status}")
                        return []
                    html = await r.text()
        except Exception as e:
            logger.debug(f"Kwork profile {username}: {e}")
            return []

        # Структурированный путь: вытаскиваем JSON и парсим типизированно.
        state = KworkStateDataParser.extract(html)
        if state is not None:
            findings = self._from_state_data(state, username, url)
            if findings:
                return findings
            logger.debug(
                f"Kwork profile {username}: stateData без отзывов/био — "
                f"возвращаем пустой результат, не генерируем мусор"
            )
            return []

        # StateData не нашли — это типично для анонимной загрузки SPA
        # (каркас без хитов API). Раньше код тут включал хрупкий regex,
        # который чаще всего эмитил отзывы с неверной тональностью —
        # отказываемся от этой эвристики.
        logger.debug(
            f"Kwork profile {username}: stateData отсутствует "
            f"(анонимный SPA?) — отзывы недоступны"
        )
        return []

    # ---------------- StateData → findings ----------------

    def _from_state_data(
        self, state: dict[str, Any], username: str, url: str
    ) -> list[OSINTFinding]:
        findings: list[OSINTFinding] = []

        user_payload = self._pick_user_payload(state)
        bio = self._extract_bio(user_payload)
        if bio:
            findings.append(
                OSINTFinding(
                    source=self.name,
                    kind="profile",
                    title=f"Kwork / {username}",
                    url=url,
                    snippet=bio[:300],
                    confidence=0.9,
                    meta={"username": username},
                )
            )

        for index, review in enumerate(self._iter_reviews(state, user_payload)[:15]):
            text = _clean_html(
                review.get("text")
                or review.get("comment")
                or review.get("message")
                or ""
            )
            if len(text) < 20:
                continue
            polarity = _normalize_polarity(
                review.get("positive")
                if "positive" in review
                else review.get("type") or review.get("status") or review.get("kind")
            )

            if polarity is True:
                kind, mark = "review", "+"
            elif polarity is False:
                kind, mark = "review", "−"
            else:
                # Полярность не извлеклась — НЕ создаём отзыв, чтобы не
                # отравлять reputation_score. Сохраняем как комментарий
                # (информационный, в скоринге не учитывается).
                kind, mark = "comment", "?"

            meta: dict[str, Any] = {"index": index}
            if polarity is not None:
                meta["positive"] = polarity

            findings.append(
                OSINTFinding(
                    source=self.name,
                    kind=kind,
                    title=f"Отзыв #{index + 1} ({mark})",
                    url=url,
                    snippet=text[:400],
                    confidence=0.85,
                    meta=meta,
                )
            )

        return findings

    @staticmethod
    def _pick_user_payload(state: dict[str, Any]) -> dict[str, Any]:
        for key in _USER_DATA_KEY_CANDIDATES:
            value = state.get(key)
            if isinstance(value, dict) and value:
                return value
        return {}

    @staticmethod
    def _extract_bio(user_payload: dict[str, Any]) -> str:
        if not user_payload:
            return ""
        for key in ("description", "about", "bio", "user_about", "userAbout"):
            value = user_payload.get(key)
            if isinstance(value, str) and len(value.strip()) > 15:
                return _clean_html(value)
        data = user_payload.get("data")
        if isinstance(data, dict):
            for key in ("description", "about", "bio"):
                value = data.get(key)
                if isinstance(value, str) and len(value.strip()) > 15:
                    return _clean_html(value)
        return ""

    @staticmethod
    def _iter_reviews(
        state: dict[str, Any], user_payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Проверяем top-level state, потом userPayload, потом userPayload.data
        for source in (state, user_payload, user_payload.get("data") or {}):
            if not isinstance(source, dict):
                continue
            for key in _REVIEW_KEY_CANDIDATES:
                value = source.get(key)
                if isinstance(value, list) and value:
                    return [r for r in value if isinstance(r, dict)]
                if isinstance(value, dict):
                    inner = value.get("data") or value.get("items")
                    if isinstance(inner, list) and inner:
                        return [r for r in inner if isinstance(r, dict)]
        return []
