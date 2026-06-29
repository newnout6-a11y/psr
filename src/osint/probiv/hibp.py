"""Have I Been Pwned — проверка email в утечках.

Платный API: hibp-api-key, стоимость $3.50/мес.
Эндпоинт: https://haveibeenpwned.com/api/v3/breachedaccount/{email}

Возвращает список breach-ов с именами (Adobe, LinkedIn, ...) и датами.
Наш агрегатор строит риск-сигнал: много свежих утечек → учётка засвечена.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import aiohttp
from loguru import logger

from .base import ProbivFinding, ProbivProvider


class HIBPProvider(ProbivProvider):
    name = "hibp"
    requires_key = True
    accepts = ("email",)
    BASE = "https://haveibeenpwned.com/api/v3"

    def __init__(self) -> None:
        self.api_key = os.getenv("HIBP_API_KEY", "").strip()

    async def lookup(
        self,
        *,
        email: str | None = None,
        **_: Any,
    ) -> list[ProbivFinding]:
        if not email or not self.api_key:
            return []

        headers = {
            "hibp-api-key": self.api_key,
            "User-Agent": "PSR-OSINT/1.0",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(
                    f"{self.BASE}/breachedaccount/{email}",
                    params={"truncateResponse": "false"},
                ) as r:
                    if r.status == 404:
                        return []  # чисто — никаких утечек
                    if r.status == 401:
                        logger.warning("HIBP: invalid api-key")
                        return []
                    if r.status != 200:
                        logger.debug(f"HIBP: HTTP {r.status}")
                        return []
                    breaches = await r.json()
        except Exception as e:
            logger.debug(f"HIBP {email}: {e}")
            return []

        findings: list[ProbivFinding] = []
        now = datetime.now(timezone.utc)
        recent_count = 0
        total_pwn = 0
        for b in breaches or []:
            breach_date = b.get("BreachDate") or ""
            pwn_count = int(b.get("PwnCount", 0) or 0)
            total_pwn += pwn_count
            age_years = 99
            try:
                bd = datetime.strptime(breach_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_years = (now - bd).days / 365
                if age_years <= 2:
                    recent_count += 1
            except ValueError:
                pass

            severity = "medium"
            if pwn_count >= 50_000_000:
                severity = "high"
            if age_years <= 1 and pwn_count >= 10_000_000:
                severity = "critical"

            findings.append(
                ProbivFinding(
                    source="hibp",
                    kind="breach",
                    title=f"{b.get('Name', '?')} ({breach_date[:4] if breach_date else '?'})",
                    snippet=(b.get("Description") or "")[:240],
                    url=b.get("Domain") and f"https://{b['Domain']}" or "",
                    severity=severity,
                    confidence=0.95,
                    meta={
                        "name": b.get("Name"),
                        "breach_date": breach_date,
                        "pwn_count": pwn_count,
                        "data_classes": b.get("DataClasses", []),
                        "is_verified": b.get("IsVerified"),
                        "is_fabricated": b.get("IsFabricated"),
                        "is_sensitive": b.get("IsSensitive"),
                    },
                )
            )

        # Финальный summary
        if findings:
            findings.insert(
                0,
                ProbivFinding(
                    source="hibp",
                    kind="risk",
                    title=f"HIBP: email в {len(findings)} утечках",
                    snippet=f"из них свежих (≤2г): {recent_count}, суммарно пострадавших: {total_pwn:,}",
                    severity="high" if recent_count >= 2 else "medium",
                    confidence=0.95,
                    meta={
                        "total_breaches": len(findings),
                        "recent_breaches": recent_count,
                        "total_affected": total_pwn,
                    },
                ),
            )
        return findings
