"""GitHub OSINT провайдер. Использует публичный REST API v3."""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from loguru import logger

from ..base import OSINTFinding, OSINTProvider


class GitHubProvider(OSINTProvider):
    """Поиск пользователя и его активности на GitHub.

    Публичный API: 60 req/hour без токена, 5000 с токеном.
    Пишет GITHUB_TOKEN в .env — используем (опционально).
    """

    name = "github"
    BASE_URL = "https://api.github.com"

    async def search(self, query: str, **ctx: Any) -> list[OSINTFinding]:
        username = (query or "").strip()
        if not username or len(username) < 2:
            return []

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "PSR-OSINT/1.0"}
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        findings: list[OSINTFinding] = []

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            try:
                # 1. Пробуем как username напрямую
                async with s.get(f"{self.BASE_URL}/users/{username}") as r:
                    if r.status == 200:
                        user = await r.json()
                        findings.append(self._user_to_finding(user, confidence=0.9))
                    elif r.status != 404:
                        logger.debug(f"GitHub /users/{username} → {r.status}")
            except Exception as e:
                logger.debug(f"GitHub user lookup failed: {e}")

            # 2. Поиск по users (более широкий)
            try:
                async with s.get(
                    f"{self.BASE_URL}/search/users",
                    params={"q": username, "per_page": 3},
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in data.get("items", [])[:3]:
                            login = item.get("login", "")
                            if any(f.meta.get("login") == login for f in findings):
                                continue
                            conf = 0.8 if login.lower() == username.lower() else 0.4
                            findings.append(
                                OSINTFinding(
                                    source=self.name,
                                    kind="profile",
                                    title=f"@{login}",
                                    url=item.get("html_url", ""),
                                    confidence=conf,
                                    meta={"login": login, "score": item.get("score")},
                                )
                            )
            except Exception as e:
                logger.debug(f"GitHub user search failed: {e}")

        return findings

    @staticmethod
    def _user_to_finding(user: dict[str, Any], confidence: float) -> OSINTFinding:
        login = user.get("login", "")
        bio = (user.get("bio") or "").strip()
        name = user.get("name") or ""
        snippet_parts = []
        if name:
            snippet_parts.append(name)
        if user.get("company"):
            snippet_parts.append(user["company"])
        if user.get("location"):
            snippet_parts.append(user["location"])
        if bio:
            snippet_parts.append(bio[:120])

        return OSINTFinding(
            source="github",
            kind="profile",
            title=f"@{login}",
            url=user.get("html_url", ""),
            snippet=" · ".join(snippet_parts),
            confidence=confidence,
            meta={
                "login": login,
                "public_repos": user.get("public_repos", 0),
                "followers": user.get("followers", 0),
                "created_at": user.get("created_at"),
                "company": user.get("company"),
                "blog": user.get("blog"),
            },
        )
