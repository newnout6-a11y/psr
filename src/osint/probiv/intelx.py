"""Intelligence X (intelx.io) — поиск по утечкам, darknet, paste-sites, документам.

Работает с любой строкой: email, phone, username, домен, телеграм-ник, хеш.

ДВА РАЗНЫХ ХОСТА:
  - free tier   → https://free.intelx.io     (по умолчанию)
  - paid/corp   → https://2.intelx.io         (нужно поставить в INTELX_BASE_URL)

API flow (два шага):
  1) POST /intelligent/search     — создаём поиск, получаем id (СЪЕДАЕТ КРЕДИТ)
  2) GET  /intelligent/search/result?id=  — опрашиваем результаты (бесплатно)

Кредиты free-tier: 50 /intelligent/search в месяц. Экономим:
  - не дублируем одинаковые term (агрегатор уже кэширует результат)
  - maxresults=25 (больше не нужно для триажа)
  - taget buckets: только релевантные для пробива (leaks+darknet+pastes+dumpster)

Status коды в ответе:
  0  — поиск ещё выполняется (есть результаты, можно опрашивать дальше)
  1  — поиск завершён (нет новых результатов)
  2  — search id не найден / истёк
  3  — результатов не найдено (пусто)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp
from loguru import logger

from .base import ProbivFinding, ProbivProvider


# Buckets, доступные на free-tier и полезные для пробива.
# Полный список: https://intelx.io/integrations → API → Buckets.
_DEFAULT_BUCKETS = [
    "leaks.public",  # публичные утечки — главное
    "leaks.public.general",
    "darknet.tor",  # .onion
    "darknet.i2p",
    "pastes",  # pastebin-подобные
    "dumpster",  # все file-drops
    "whois",  # whois-записи
    "usenet",  # newsgroups
]


class IntelXProvider(ProbivProvider):
    name = "intelx"
    requires_key = True
    accepts = ("email", "phone", "username", "telegram")
    timeout_sec = 25.0

    def __init__(self) -> None:
        self.api_key = os.getenv("INTELX_API_KEY", "").strip()
        # Free-tier использует ОТДЕЛЬНЫЙ хост. По умолчанию берём free,
        # платные аккаунты должны явно переопределить INTELX_BASE_URL=https://2.intelx.io
        self.base = os.getenv("INTELX_BASE_URL", "https://free.intelx.io").rstrip("/")
        buckets_env = os.getenv("INTELX_BUCKETS", "").strip()
        self.buckets = [b.strip() for b in buckets_env.split(",") if b.strip()] if buckets_env else _DEFAULT_BUCKETS
        try:
            self.max_results = int(os.getenv("INTELX_MAX_RESULTS", "25"))
        except ValueError:
            self.max_results = 25

    async def lookup(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        username: str | None = None,
        telegram: str | None = None,
        **_: Any,
    ) -> list[ProbivFinding]:
        if not self.api_key:
            return []
        term = email or phone or telegram or username
        if not term:
            return []

        headers = {
            "x-key": self.api_key,
            "User-Agent": "PSR-OSINT/1.0",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                # 1. Создаём поиск (расходует 1 search-credit)
                search_body = {
                    "term": term,
                    "buckets": self.buckets,
                    "lookuplevel": 0,
                    "maxresults": self.max_results,
                    "timeout": 5,  # сек на стороне IntelX
                    "datefrom": "",
                    "dateto": "",
                    "sort": 4,  # по дате, свежие первыми
                    "media": 0,  # любые типы файлов
                    "terminate": [],
                }
                async with s.post(f"{self.base}/intelligent/search", json=search_body) as r:
                    if r.status == 402:
                        logger.warning("IntelX: закончились search-кредиты")
                        return []
                    if r.status != 200:
                        body = (await r.text())[:200]
                        logger.debug(f"IntelX search start HTTP {r.status}: {body}")
                        return []
                    start = await r.json()
                search_id = start.get("id")
                if not search_id:
                    return []

                # 2. Пулинг: status=0 ещё работает, 1=готово, 2=exp, 3=пусто
                results: list[dict] = []
                for attempt in range(4):
                    await asyncio.sleep(1.0 * (attempt + 1))
                    async with s.get(
                        f"{self.base}/intelligent/search/result",
                        params={"id": search_id, "limit": self.max_results},
                    ) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                    records = data.get("records") or []
                    results.extend(records)
                    status = data.get("status")
                    if status in (1, 2, 3):
                        break
        except Exception as e:
            logger.debug(f"IntelX {term}: {e}")
            return []

        if not results:
            return []

        findings: list[ProbivFinding] = [
            ProbivFinding(
                source="intelx",
                kind="risk",
                title=f"IntelX: {len(results)} записей по '{term[:40]}'",
                snippet="запись/ссылка в даркнете, утечке или документах",
                severity="medium" if len(results) < 10 else "high",
                confidence=0.85,
                meta={"count": len(results), "term": term},
            )
        ]
        for rec in results[:15]:
            findings.append(
                ProbivFinding(
                    source="intelx",
                    kind="leak",
                    title=rec.get("name") or rec.get("systemid", "?"),
                    snippet=(rec.get("description") or rec.get("bucket") or "")[:240],
                    url=f"https://intelx.io/?did={rec.get('systemid')}" if rec.get("systemid") else "",
                    severity="medium",
                    confidence=0.8,
                    meta={
                        "bucket": rec.get("bucket"),
                        "date": rec.get("date"),
                        "size": rec.get("size"),
                        "type": rec.get("type"),
                    },
                )
            )
        return findings
