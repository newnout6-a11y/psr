"""Kwork-profile OSINT: парсит публичную страницу заказчика.

API даёт завершённые заказы/рейтинг, но сами ТЕКСТЫ отзывов лежат только на
странице /user/<username>. Забираем их и кормим LLM для sentiment-анализа.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

import aiohttp
from loguru import logger

from ..base import OSINTFinding, OSINTProvider


class KworkProfileProvider(OSINTProvider):
    """Скрейп /user/<username>/ — забор текстовых отзывов и био."""

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

        findings: list[OSINTFinding] = []

        # Блок профиля
        bio = self._extract_bio(html)
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

        # Отзывы — ищем типичные блоки reviews
        reviews = self._extract_reviews(html)
        for i, (text, is_positive) in enumerate(reviews[:15]):
            findings.append(
                OSINTFinding(
                    source=self.name,
                    kind="review",
                    title=f"Отзыв #{i + 1} ({'+' if is_positive else '−'})",
                    url=url,
                    snippet=text[:400],
                    confidence=0.85,
                    meta={"positive": is_positive, "index": i},
                )
            )

        return findings

    # ------- HTML-извлечение (best-effort, выдержит мелкие правки верстки) -------

    @staticmethod
    def _clean(text: str) -> str:
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        return re.sub(r"\s+", " ", text).strip()

    def _extract_bio(self, html: str) -> str:
        # class=...description... или первый большой блок в user-card
        patterns = [
            r'class="[^"]*user-card__description[^"]*"[^>]*>(.*?)</',
            r'class="[^"]*user-about[^"]*"[^>]*>(.*?)</div>',
            r'<meta\s+name="description"\s+content="([^"]+)"',
        ]
        for p in patterns:
            m = re.search(p, html, flags=re.DOTALL | re.IGNORECASE)
            if m:
                bio = self._clean(m.group(1))
                if len(bio) > 15:
                    return bio
        return ""

    def _extract_reviews(self, html: str) -> list[tuple[str, bool]]:
        """Грубо вытаскиваем отзывы с индикатором тональности."""
        results: list[tuple[str, bool]] = []

        # Паттерн: блок с классом review + текст внутри
        for m in re.finditer(
            r'<div[^>]*class="[^"]*(?:review|feedback)[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            block = m.group(1)
            # Тональность: ищем пометки positive/negative/thumb
            is_positive = bool(re.search(r"(positive|thumb[_-]?up|rating[_-]?good|\+)", block, re.IGNORECASE))
            is_negative = bool(re.search(r"(negative|thumb[_-]?down|rating[_-]?bad|\-[^\d])", block, re.IGNORECASE))
            text = self._clean(block)
            if len(text) < 20:
                continue
            # Отбросим дубли (Kwork рендерит похожие блоки)
            if any(text == existing for existing, _ in results):
                continue
            results.append((text, is_positive and not is_negative))

        return results
