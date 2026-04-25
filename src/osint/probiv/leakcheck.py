"""LeakCheck.io — база утечек с публичным API.

Платный (от ~$3/мес). Работает с email, phone, username, hash.
Эндпоинт: https://leakcheck.io/api/public?key=KEY&check=EMAIL
Возвращает источники утечек, даты и (по тарифу) поля типа пароля/хеша.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from loguru import logger

from .base import ProbivFinding, ProbivProvider


class LeakCheckProvider(ProbivProvider):
    name = "leakcheck"
    requires_key = True
    accepts = ("email", "phone", "username")
    BASE = "https://leakcheck.io/api/v2"

    def __init__(self) -> None:
        self.api_key = os.getenv("LEAKCHECK_API_KEY", "").strip()

    async def lookup(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        username: str | None = None,
        **_: Any,
    ) -> list[ProbivFinding]:
        if not self.api_key:
            return []
        query = email or phone or username
        if not query:
            return []

        params = {"check": query}
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "PSR-OSINT/1.0",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(self.BASE, params=params) as r:
                    if r.status == 404:
                        return []
                    if r.status == 401:
                        logger.warning("LeakCheck: invalid api-key")
                        return []
                    if r.status != 200:
                        logger.debug(f"LeakCheck: HTTP {r.status}")
                        return []
                    data = await r.json()
        except Exception as e:
            logger.debug(f"LeakCheck {query}: {e}")
            return []

        if not data.get("success"):
            return []

        results = data.get("result") or []
        if not results:
            return []

        findings: list[ProbivFinding] = []

        # Агрегированный сигнал
        sources = sorted({r.get("source", {}).get("name", "?") for r in results})
        findings.append(
            ProbivFinding(
                source="leakcheck",
                kind="risk",
                title=f"LeakCheck: {len(results)} записей в утечках",
                snippet=f"источники: {', '.join(sources[:6])}" + (" ..." if len(sources) > 6 else ""),
                severity="high" if len(results) >= 3 else "medium",
                confidence=0.9,
                meta={"count": len(results), "sources": sources, "query": query},
            )
        )

        # Детали (первые N)
        for r in results[:20]:
            src = r.get("source", {}) or {}
            name = src.get("name", "?")
            date = src.get("breach_date", "") or ""
            line = r.get("line") or []
            fields = ", ".join(line) if isinstance(line, list) else str(line)[:120]
            findings.append(
                ProbivFinding(
                    source="leakcheck",
                    kind="breach",
                    title=f"{name} ({date[:4]})" if date else name,
                    snippet=f"поля: {fields}" if fields else "",
                    severity="medium",
                    confidence=0.9,
                    meta={"source": name, "date": date, "fields": line, "query": query},
                )
            )

        return findings
