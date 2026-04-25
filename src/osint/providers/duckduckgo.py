"""DuckDuckGo OSINT провайдер. Поиск по открытым источникам через html.duckduckgo.com.

Google блокирует скрейпинг, Bing требует ключ. DDG отдаёт чистый HTML без JS
и без агрессивной защиты. Этого достаточно для OSINT-упоминаний по нику.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from loguru import logger

from ..base import OSINTFinding, OSINTProvider


class DuckDuckGoProvider(OSINTProvider):
    """Поиск по нику/имени в открытых источниках."""

    name = "duckduckgo"
    BASE_URL = "https://html.duckduckgo.com/html/"
    timeout_sec = 12.0

    # Полезные сайты для кросс-сверки
    OSINT_DOMAINS = (
        "linkedin.com", "vk.com", "facebook.com", "twitter.com", "t.me", "telegram.me",
        "github.com", "habr.com", "stackoverflow.com", "medium.com", "dev.to",
        "upwork.com", "freelancer.com", "kwork.ru", "fl.ru", "workzilla.com",
    )

    async def search(self, query: str, **ctx: Any) -> list[OSINTFinding]:
        q = (query or "").strip()
        if not q:
            return []

        # Обогатим запрос контекстом (имя, город) если передан
        context = ctx.get("context", "")
        full_query = f'"{q}" {context}'.strip() if context else q

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ru,en;q=0.7",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.post(self.BASE_URL, data={"q": full_query}) as r:
                    if r.status != 200:
                        logger.debug(f"DDG status {r.status}")
                        return []
                    html = await r.text()
        except Exception as e:
            logger.debug(f"DDG request failed: {e}")
            return []

        return self._parse(html, q)[:8]

    @staticmethod
    def _clean_url(raw: str) -> str:
        """DDG оборачивает ссылки: /l/?uddg=<encoded>..."""
        if raw.startswith("//"):
            raw = "https:" + raw
        if "duckduckgo.com/l/" in raw:
            qs = parse_qs(urlparse(raw).query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        return raw

    def _parse(self, html: str, query: str) -> list[OSINTFinding]:
        findings: list[OSINTFinding] = []
        # HTML DDG: каждый результат внутри <div class="result ...">
        # Ищем ссылку + заголовок + сниппет.
        blocks = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            html,
            flags=re.DOTALL,
        )
        q_lower = query.lower()
        for href, title_html, snippet_html in blocks:
            url = self._clean_url(href)
            title = unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
            snippet = unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()
            domain = urlparse(url).netloc.lower()

            # Сколько раз наш ник встречается
            hits = (title + " " + snippet).lower().count(q_lower)
            is_relevant_domain = any(d in domain for d in self.OSINT_DOMAINS)
            if hits == 0 and not is_relevant_domain:
                continue

            confidence = 0.6 if is_relevant_domain else 0.3
            if hits >= 2:
                confidence = min(0.9, confidence + 0.2)

            findings.append(
                OSINTFinding(
                    source=self.name,
                    kind="mention",
                    title=title[:120],
                    url=url,
                    snippet=snippet[:240],
                    confidence=confidence,
                    meta={"domain": domain, "hits": hits},
                )
            )
        return findings
