"""Server-side client for the public CatchMail disposable mailbox API."""

from __future__ import annotations

import asyncio
import html
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger


CATCHMAIL_BASE_URL = "https://api.catchmail.io/api/v1"
CATCHMAIL_MIN_REQUEST_INTERVAL = 1.1
_CATCHMAIL_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)
_KWORK_LINK_RE = re.compile(
    r"https?://(?:www\.)?kwork\.ru(?:/[\w\-./?%=&+#~:;,@!$'()*\[\]]*)?",
    re.IGNORECASE,
)


class CatchmailError(RuntimeError):
    """Raised when the CatchMail API cannot be queried or parsed."""


@dataclass(slots=True)
class CatchmailClient:
    """Read one CatchMail inbox and wait for an activation link.

    CatchMail provisions public inboxes lazily: a unique address is enough to
    use the API. Requests are paced locally because the public API allows one
    request per second for an IP address.
    """

    email: str
    base_url: str = CATCHMAIL_BASE_URL
    timeout: float = 20.0
    min_request_interval: float = CATCHMAIL_MIN_REQUEST_INTERVAL
    user_agent: str = "PSR-CatchmailClient/1.0"
    proxy: str = ""
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.email = self.email.strip().lower()
        self.base_url = self.base_url.rstrip("/")
        self.proxy = self.proxy.strip()
        self.min_request_interval = max(CATCHMAIL_MIN_REQUEST_INTERVAL, float(self.min_request_interval))
        if not self.email or "@" not in self.email:
            raise CatchmailError("CatchMail requires a mailbox address")

    @staticmethod
    def generate_address(*, prefix: str = "psr", domain: str = "catchmail.io") -> str:
        """Return a Kwork-compatible, high-entropy CatchMail address."""

        normalized_prefix = re.sub(r"[^a-z0-9-]+", "", prefix.lower()).strip("-") or "psr"
        normalized_prefix = normalized_prefix[:8]
        normalized_domain = domain.strip().lower().rstrip(".")
        if not _CATCHMAIL_DOMAIN_RE.fullmatch(normalized_domain):
            raise CatchmailError("CATCHMAIL_DOMAIN is not a valid domain")
        return f"{normalized_prefix}-{secrets.token_hex(7)}@{normalized_domain}"

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        remaining = self.min_request_interval - (loop.time() - self._last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_at = loop.time()

    async def _get_json(self, path: str, *, params: Mapping[str, Any]) -> dict[str, Any]:
        await self._pace()
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            timeout=self.timeout,
            proxy=self.proxy or None,
            trust_env=False,
        ) as client:
            response = await client.get(url, params=params)

        if response.status_code >= 400:
            raise CatchmailError(
                f"CatchMail GET {path} returned HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatchmailError(f"CatchMail GET {path} returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise CatchmailError(f"CatchMail GET {path} returned an invalid JSON envelope")
        return payload

    async def mailbox(self, *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """Return the current CatchMail mailbox listing."""

        return await self._get_json(
            "mailbox",
            params={"address": self.email, "page": max(1, page), "page_size": max(1, min(page_size, 100))},
        )

    async def message(self, message_id: str) -> dict[str, Any]:
        """Return a full message body by CatchMail message id."""

        clean_id = str(message_id or "").strip()
        if not clean_id:
            raise CatchmailError("CatchMail message is missing an id")
        return await self._get_json(f"message/{clean_id}", params={"mailbox": self.email})

    @staticmethod
    def _message_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_items = payload.get("messages")
        return [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []

    @staticmethod
    def _message_id(message: Mapping[str, Any]) -> str:
        for key in ("id", "message_id", "messageId"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _message_is_after(message: Mapping[str, Any], after: datetime | None) -> bool:
        if after is None:
            return True
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        for key in ("date", "created_at", "createdAt", "received_at", "receivedAt", "timestamp", "time"):
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

    @staticmethod
    def extract_kwork_link(value: Any) -> str | None:
        """Extract an HTTPS Kwork activation-looking URL from arbitrary mail data."""

        if isinstance(value, Mapping):
            for nested in value.values():
                link = CatchmailClient.extract_kwork_link(nested)
                if link:
                    return link
            return None
        if isinstance(value, list):
            for nested in value:
                link = CatchmailClient.extract_kwork_link(nested)
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

    async def wait_for_kwork_link(
        self,
        *,
        timeout: float = 120.0,
        poll_interval: float = 5.0,
        after: datetime | None = None,
        initial_delay: float = 0.0,
    ) -> str | None:
        """Poll mailbox metadata and message bodies until a Kwork link appears."""

        timeout = max(0.0, float(timeout))
        poll_interval = max(CATCHMAIL_MIN_REQUEST_INTERVAL, float(poll_interval))
        deadline = asyncio.get_running_loop().time() + timeout
        attempt = 0

        delay = min(max(0.0, float(initial_delay)), timeout)
        if delay:
            await asyncio.sleep(delay)

        while True:
            attempt += 1
            try:
                listing = await self.mailbox()
                for message in self._message_items(listing):
                    if not self._message_is_after(message, after):
                        continue
                    link = self.extract_kwork_link(message)
                    if link:
                        return link
                    message_id = self._message_id(message)
                    if message_id:
                        detail = await self.message(message_id)
                        link = self.extract_kwork_link(detail)
                        if link:
                            logger.info("CatchMail: Kwork activation link found on attempt {}", attempt)
                            return link
            except CatchmailError:
                raise
            except Exception as exc:
                raise CatchmailError(f"CatchMail polling failed: {type(exc).__name__}: {exc}") from exc

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))
