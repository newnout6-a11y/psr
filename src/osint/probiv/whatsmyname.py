"""WhatsMyName — поиск username на 600+ сайтах.

Использует публичный JSON-список от https://github.com/WebBreacher/WhatsMyName
(такой же стандарт использует Sherlock, Maigret).

Алгоритм: для каждого сайта подставляем username в URL-шаблон,
делаем HEAD/GET, проверяем:
  - HTTP status (нужный код)
  - наличие/отсутствие 'e_string' в теле ответа

Это тяжёлая операция (600 сайтов × HTTP запрос), поэтому:
  - делаем параллельно в пулах по 30
  - таймаут на сайт = 5 сек
  - проверяем только 'популярные' категории (по умолчанию) — быстро
  - можно расширить до всех (OSINT_WMN_FULL=true)

Список сайтов кэшируется локально в data/reference/wmn_sites.json (обновляется раз в неделю).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import aiohttp
from loguru import logger

from .base import ProbivFinding, ProbivProvider
from src.paths import WMN_SITES_FILE


# Официальный репо WhatsMyName
_WMN_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json"
_CACHE_FILE = WMN_SITES_FILE
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # неделя

# Популярные категории для быстрого режима (по умолчанию)
_POPULAR_CATEGORIES = {
    "social",
    "social networking",
    "video",
    "images",
    "blog",
    "coding",
    "dev",
    "gaming",
    "messaging",
    "news",
    "tech",
    "russian",
    "finance",
    "shopping",
}


class WhatsMyNameProvider(ProbivProvider):
    name = "whatsmyname"
    requires_key = False
    accepts = ("username",)
    timeout_sec = 45.0

    def __init__(
        self,
        concurrency: int = 30,
        per_site_timeout: float = 5.0,
        full_scan: bool | None = None,
    ) -> None:
        self.concurrency = concurrency
        self.per_site_timeout = per_site_timeout
        self.full_scan = full_scan if full_scan is not None else os.getenv("OSINT_WMN_FULL", "false").lower() == "true"

    # ---------- список сайтов ----------

    async def _load_sites(self) -> list[dict]:
        if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_TTL_SECONDS:
            try:
                with _CACHE_FILE.open("r", encoding="utf-8") as f:
                    return json.load(f).get("sites", [])
            except Exception:
                pass

        logger.info("WhatsMyName: обновляем список сайтов...")
        headers = {"User-Agent": "PSR-OSINT/1.0"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(_WMN_URL) as r:
                    if r.status != 200:
                        logger.warning(f"WhatsMyName: HTTP {r.status} при загрузке списка")
                        return []
                    data = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"WhatsMyName: не удалось загрузить список: {e}")
            # попытка взять устаревший кэш
            if _CACHE_FILE.exists():
                with _CACHE_FILE.open("r", encoding="utf-8") as f:
                    return json.load(f).get("sites", [])
            return []

        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _CACHE_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"WhatsMyName: не удалось сохранить кэш: {e}")

        return data.get("sites", [])

    # ---------- lookup ----------

    async def lookup(
        self,
        *,
        username: str | None = None,
        **_: Any,
    ) -> list[ProbivFinding]:
        username = (username or "").strip().lstrip("@")
        if not username or len(username) < 3:
            return []

        sites = await self._load_sites()
        if not sites:
            return []

        if not self.full_scan:
            sites = [s for s in sites if (s.get("cat") or "").lower() in _POPULAR_CATEGORIES]
        logger.debug(f"WhatsMyName: проверяем {username!r} на {len(sites)} сайтах")

        sem = asyncio.Semaphore(self.concurrency)
        timeout = aiohttp.ClientTimeout(total=self.per_site_timeout)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            tasks = [self._probe(session, sem, site, username) for site in sites]
            results = await asyncio.gather(*tasks, return_exceptions=False)

        findings = [f for f in results if f is not None]
        logger.debug(f"WhatsMyName: найдено аккаунтов для {username!r}: {len(findings)}")
        return findings

    @staticmethod
    def _format_url(template: str, username: str) -> str:
        return template.replace("{account}", username)

    async def _probe(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        site: dict,
        username: str,
    ) -> ProbivFinding | None:
        check_url = self._format_url(site.get("uri_check", ""), username)
        if not check_url:
            return None

        expected_status = int(site.get("e_code", 200))
        expected_string = site.get("e_string") or ""
        m_string = site.get("m_string") or ""
        async with sem:
            try:
                async with session.get(check_url, allow_redirects=True) as r:
                    if r.status != expected_status:
                        return None
                    # Если указана e_string — она должна присутствовать
                    if expected_string:
                        body = await r.text(errors="ignore")
                        if expected_string not in body:
                            return None
                        # И отсутствовать негативный маркер
                        if m_string and m_string in body:
                            return None
                    profile_url = self._format_url(site.get("uri_pretty") or site.get("uri_check", ""), username)
                    return ProbivFinding(
                        source="whatsmyname",
                        kind="account",
                        title=f"{site.get('name', '?')} / @{username}",
                        snippet=(site.get("cat") or "").strip(),
                        url=profile_url,
                        severity="info",
                        confidence=0.8,
                        meta={"site": site.get("name"), "category": site.get("cat")},
                    )
            except asyncio.TimeoutError:
                return None
            except Exception:
                return None
