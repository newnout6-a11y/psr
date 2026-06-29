"""Habr OSINT провайдер. Поиск пользователя по нику через публичный URL."""

from __future__ import annotations

import re
from typing import Any

import aiohttp
from loguru import logger

from ..base import OSINTFinding, OSINTProvider


class HabrProvider(OSINTProvider):
    """Проверяем есть ли аккаунт на habr.com/ru/users/<username>.

    Парсим мини-инфу: имя, специализация, карму, рейтинг.
    """

    name = "habr"
    BASE_URL = "https://habr.com/ru/users"

    async def search(self, query: str, **ctx: Any) -> list[OSINTFinding]:
        username = (query or "").strip().lstrip("@")
        if not username or len(username) < 2:
            return []

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        headers = {"User-Agent": "Mozilla/5.0 PSR-OSINT/1.0"}
        url = f"{self.BASE_URL}/{username}/"

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(url, allow_redirects=False) as r:
                    if r.status == 404:
                        return []
                    if r.status >= 400:
                        logger.debug(f"Habr {username}: HTTP {r.status}")
                        return []
                    html = await r.text()
        except Exception as e:
            logger.debug(f"Habr {username}: {e}")
            return []

        fullname = self._extract(html, r'<h1[^>]*class="[^"]*tm-user-card__nickname[^"]*"[^>]*>([^<]+)</h1>')
        specialization = self._extract(html, r'class="[^"]*tm-user-card__specialization[^"]*"[^>]*>([^<]+)<')
        # Карма и рейтинг — формат Habr динамический, делаем бест-эффорт
        karma = self._extract(html, r'"karma":\s*"?([-\d.]+)"?') or self._extract(
            html, r"Карма</div>\s*<div[^>]*>([-\d.]+)"
        )
        rating = self._extract(html, r'"rating":\s*"?([-\d.]+)"?')

        snippet = " · ".join(
            p for p in (specialization, f"karma={karma}" if karma else "", f"rating={rating}" if rating else "") if p
        )

        return [
            OSINTFinding(
                source=self.name,
                kind="profile",
                title=f"Habr / @{username}",
                url=url,
                snippet=snippet or "профиль существует",
                confidence=0.75,
                meta={
                    "username": username,
                    "fullname": fullname,
                    "specialization": specialization,
                    "karma": karma,
                    "rating": rating,
                },
            )
        ]

    @staticmethod
    def _extract(html: str, pattern: str) -> str:
        m = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            return ""
        return re.sub(r"\s+", " ", m.group(1)).strip()
