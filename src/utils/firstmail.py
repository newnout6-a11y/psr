"""Small async REST client for firstmail.ltd mailbox polling.

The provider's public notes describe two read endpoints but do not promise one
stable response envelope.  This client deliberately keeps transport concerns
separate from message/link extraction and accepts the common ``data/items``
envelopes returned by different firstmail deployments.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from src.utils.vpnte_proxy import kwork_http_proxy_url


FIRSTMAIL_BASE_URL = "https://api.firstmail.ltd/v1"
_KWORK_LINK_RE = re.compile(
    r"https?://(?:www\.)?kwork\.ru(?:/[\w\-./?%=&+#~:;,@!$'()*\[\]]*)?",
    re.IGNORECASE,
)


class FirstmailError(RuntimeError):
    """Raised when the firstmail API cannot be queried or parsed."""


@dataclass(slots=True)
class FirstmailClient:
    """Read firstmail messages using mailbox credentials and an optional key."""

    email: str
    password: str
    api_key: str | None = None
    base_url: str = FIRSTMAIL_BASE_URL
    proxy: str | None = None
    timeout: float = 20.0
    user_agent: str = "PSR-FirstmailClient/1.0"

    def __post_init__(self) -> None:
        self.email = self.email.strip()
        self.password = self.password.strip()
        self.api_key = (self.api_key or "").strip() or None
        self.base_url = self.base_url.rstrip("/")
        if self.proxy is None:
            self.proxy = kwork_http_proxy_url(rotate=False)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            # The provider notes mention both spellings.  Sending the second
            # header is harmless and keeps older API gateways compatible.
            headers["X-API-KEY"] = self.api_key
        return headers

    def _credential_params(self, *, legacy: bool = False) -> dict[str, str]:
        if legacy:
            return {"login": self.email, "pass": self.password}
        return {"email": self.email, "password": self.password}

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        retry_legacy_credentials: bool = True,
    ) -> Any:
        request_params = {str(key): value for key, value in (params or {}).items() if value is not None}
        url = f"{self.base_url}/{path.lstrip('/')}"
        logger.debug("Firstmail: GET {} params={}", url, sorted(request_params))
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=self._headers(),
            proxy=self.proxy,
            timeout=self.timeout,
            trust_env=False,
        ) as client:
            response = await client.get(url, params=request_params)
            if response.status_code in {400, 401, 422} and retry_legacy_credentials:
                credentials = self._credential_params(legacy=True)
                current = {key: value for key, value in request_params.items() if key not in {"email", "password", "login", "pass"}}
                current.update(credentials)
                logger.debug("Firstmail: retrying {} with legacy login/pass credentials", path)
                response = await client.get(url, params=current)

        if response.status_code >= 400:
            raise FirstmailError(f"firstmail GET {path} returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise FirstmailError(f"firstmail GET {path} returned non-JSON response") from exc

    async def latest(self) -> Any:
        """Return the latest message envelope from ``/mail/one``."""

        return await self._get_json("mail/one", params=self._credential_params())

    async def messages(self, **filters: Any) -> Any:
        """Return the message list envelope from ``/get/messages``.

        The API accepts many filters.  Unknown values are passed through so
        callers can use provider-side filters without a client release.
        """

        params = self._credential_params()
        for key, value in filters.items():
            if value is not None:
                params[str(key)] = value
        return await self._get_json("get/messages", params=params)

    @classmethod
    def message_items(cls, payload: Any) -> list[dict[str, Any]]:
        """Normalize common firstmail response envelopes into message dicts."""

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("messages", "items", "mails", "emails", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            nested = cls.message_items(data)
            if nested:
                return nested
        response = payload.get("response")
        if isinstance(response, (dict, list)):
            nested = cls.message_items(response)
            if nested:
                return nested
        # /mail/one commonly returns one message directly.
        if any(key in payload for key in ("body", "html", "text", "subject", "from", "sender")):
            return [payload]
        return []

    @staticmethod
    def _message_text(message: Mapping[str, Any]) -> str:
        values: list[str] = []
        for key in ("subject", "body", "html", "text", "content", "message"):
            value = message.get(key)
            if isinstance(value, (str, int, float)):
                values.append(str(value))
        for key in ("data", "payload", "parts"):
            nested = message.get(key)
            if isinstance(nested, Mapping):
                values.append(FirstmailClient._message_text(nested))
            elif isinstance(nested, list):
                values.extend(
                    FirstmailClient._message_text(item)
                    for item in nested
                    if isinstance(item, Mapping)
                )
        raw = html.unescape("\n".join(values))
        if "<" in raw and ">" in raw:
            return BeautifulSoup(raw, "lxml").get_text(" ", strip=True)
        return raw

    @staticmethod
    def extract_kwork_link(value: Any) -> str | None:
        """Extract the first activation-looking HTTPS link hosted by Kwork."""

        if isinstance(value, Mapping):
            for nested in value.values():
                link = FirstmailClient.extract_kwork_link(nested)
                if link:
                    return link
            return None
        if isinstance(value, list):
            for nested in value:
                link = FirstmailClient.extract_kwork_link(nested)
                if link:
                    return link
            return None
        if value is None:
            return None
        text = html.unescape(unquote(str(value)))
        if "<" in text and ">" in text:
            soup = BeautifulSoup(text, "lxml")
            candidates = [str(tag.get("href")) for tag in soup.find_all("a", href=True)]
            candidates.append(soup.get_text(" ", strip=True))
            text = "\n".join(candidates)
        for match in _KWORK_LINK_RE.findall(text):
            candidate = match.rstrip(".,;:)]}>")
            parsed = urlparse(candidate)
            if parsed.scheme == "https" and (parsed.hostname or "").lower().endswith("kwork.ru"):
                if parsed.path not in {"", "/"} or parsed.query:
                    return candidate
        return None

    @classmethod
    def find_kwork_link(cls, payload: Any, *, after: datetime | None = None) -> str | None:
        """Find a Kwork activation URL in a message or response envelope."""

        for message in cls.message_items(payload):
            if after is not None and not cls._message_is_after(message, after):
                continue
            link = cls.extract_kwork_link(message)
            if link:
                return link
            link = cls.extract_kwork_link(cls._message_text(message))
            if link:
                return link
        return None if after is not None else cls.extract_kwork_link(payload)

    @staticmethod
    def _message_is_after(message: Mapping[str, Any], after: datetime) -> bool:
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        for key in ("date", "created_at", "received_at", "timestamp", "time", "internalDate"):
            value = message.get(key)
            if value is None:
                continue
            try:
                if isinstance(value, (int, float)):
                    stamp = datetime.fromtimestamp(float(value), tz=UTC)
                else:
                    stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                    stamp = stamp.astimezone(UTC)
                return stamp >= after.astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                continue
        return True

    async def wait_for_kwork_link(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 5.0,
        max_messages: int = 1000,
        after: datetime | None = None,
    ) -> str | None:
        """Poll firstmail with bounded backoff until a Kwork link appears."""

        timeout = max(0.0, float(timeout))
        poll_interval = max(0.1, float(poll_interval))
        deadline = asyncio.get_running_loop().time() + timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                common = {"limit": min(max_messages, 1000)}
                payload = await self.messages(**common, **{"from": "noreply@kwork.ru"})
                link = self.find_kwork_link(payload, after=after)
                if not link:
                    payload = await self.messages(**common, subject="activation")
                    link = self.find_kwork_link(payload, after=after)
                if not link:
                    payload = await self.messages(**common, subject="registration")
                    link = self.find_kwork_link(payload, after=after)
                if link:
                    logger.info("Firstmail: Kwork activation link found on attempt {}", attempt)
                    return link
                latest = await self.latest()
                link = self.find_kwork_link(latest, after=after)
                if link:
                    logger.info("Firstmail: Kwork activation link found in latest message")
                    return link
            except FirstmailError:
                raise
            except Exception as exc:
                raise FirstmailError(f"firstmail polling failed: {type(exc).__name__}: {exc}") from exc

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval * min(attempt, 4), remaining))
