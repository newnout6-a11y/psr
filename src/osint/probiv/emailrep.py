"""EmailRep.io — бесплатный API репутации email.

Возвращает огромный объём информации по email:
  - в каких утечках засветился (credentials_leaked)
  - на каких соцсетях зарегистрирован (профили)
  - disposable/free/spam-pattern
  - malicious / suspicious activity
  - возраст домена
Лимит: 1 req/sec для анонимных. С ключом EMAILREP_KEY — выше.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from loguru import logger

from .base import ProbivFinding, ProbivProvider


class EmailRepProvider(ProbivProvider):
    name = "emailrep"
    requires_key = False
    accepts = ("email",)
    BASE = "https://emailrep.io"

    async def lookup(
        self,
        *,
        email: str | None = None,
        **_: Any,
    ) -> list[ProbivFinding]:
        if not email or "@" not in email:
            return []

        headers = {
            "Accept": "application/json",
            "User-Agent": "PSR-OSINT/1.0",
        }
        api_key = os.getenv("EMAILREP_KEY", "").strip()
        if api_key:
            headers["Key"] = api_key

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(f"{self.BASE}/{email}") as r:
                    if r.status == 429:
                        logger.debug(f"EmailRep rate-limited for {email}")
                        return []
                    if r.status != 200:
                        logger.debug(f"EmailRep {email}: HTTP {r.status}")
                        return []
                    data = await r.json()
        except Exception as e:
            logger.debug(f"EmailRep {email}: {e}")
            return []

        return self._parse(email, data)

    @staticmethod
    def _parse(email: str, data: dict) -> list[ProbivFinding]:
        findings: list[ProbivFinding] = []
        details = data.get("details", {}) or {}
        reputation = data.get("reputation", "")
        suspicious = bool(data.get("suspicious"))
        references = int(data.get("references", 0))

        # 1. Главный summary
        summary_parts = []
        if reputation:
            summary_parts.append(f"репутация: {reputation}")
        if references:
            summary_parts.append(f"упоминаний: {references}")
        if details.get("credentials_leaked"):
            summary_parts.append(f"⚠️ учётка в утечках ({details.get('data_breach', 'есть')})")
        if suspicious:
            summary_parts.append("⚠️ suspicious")

        severity = "high" if details.get("credentials_leaked") or suspicious else "info"
        findings.append(
            ProbivFinding(
                source="emailrep",
                kind="risk" if severity != "info" else "account",
                title=f"EmailRep: {email}",
                snippet=" · ".join(summary_parts) or "нет отметок",
                url=f"https://emailrep.io/{email}",
                severity=severity,
                confidence=0.85,
                meta={
                    "reputation": reputation,
                    "suspicious": suspicious,
                    "references": references,
                    "credentials_leaked": details.get("credentials_leaked"),
                    "data_breach": details.get("data_breach"),
                    "malicious_activity": details.get("malicious_activity"),
                    "profiles": details.get("profiles", []),
                    "domain_exists": details.get("domain_exists"),
                    "disposable": details.get("disposable"),
                    "free_provider": details.get("free_provider"),
                    "days_since_domain_creation": details.get("days_since_domain_creation"),
                },
            )
        )

        # 2. Найденные профили (facebook, twitter, github...)
        for platform in details.get("profiles", []) or []:
            findings.append(
                ProbivFinding(
                    source="emailrep",
                    kind="account",
                    title=f"Аккаунт: {platform}",
                    snippet=f"{email} зарегистрирован на {platform}",
                    severity="info",
                    confidence=0.8,
                    meta={"platform": platform, "email": email},
                )
            )

        return findings
