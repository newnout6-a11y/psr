"""Rubric-scoped Kwork supply scan and LLM market-assistant helpers.

This module deliberately separates seller-side supply analysis from buyer-order
discovery. It collects only the selected rubric's API slices, records what was
actually observed, and lets an LLM interpret raw evidence without fixed topic
keywords or prewritten listing suggestions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from statistics import median
from typing import Any

from loguru import logger

from src.paths import RUNTIME_DIR, ensure_layout
from src.platforms.kwork_market import KworkMarketClient
from src.platforms.kwork_supply import (
    BatchRequest,
    BatchResult,
    BatchState,
    ContractState,
    ProtectionStatus,
    SourceCursor,
    fingerprint_for_cards,
    validate_mobile_page,
)


SUPPLY_CACHE_TTL = 900.0
_SUPPLY_PAGE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SUPPLY_CACHE_LOCK = asyncio.Lock()
_PROTECTION_MARKERS = (
    "captcha",
    "too many requests",
    "rate limit",
    "http 429",
    "status 429",
    "forbidden",
    "http 403",
    "status 403",
    "access denied",
)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, limit: int = 3000) -> str:
    text = unescape(str(value or ""))
    text = " ".join(text.replace("\u00a0", " ").split())
    return text[:limit]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _snapshot_root() -> Path:
    return Path("docs") / "kwork_market_supply_snapshots"


def _context_root() -> Path:
    ensure_layout()
    path = RUNTIME_DIR / "market_assistant_contexts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _coverage_profile(name: str) -> dict[str, int | str]:
    profiles: dict[str, dict[str, int | str]] = {
        "quick": {
            "name": "quick",
            "label": "Быстрый обзор",
            "max_slices": 12,
            "pages_per_slice": 4,
        },
        "balanced": {
            "name": "balanced",
            "label": "Рабочий анализ",
            "max_slices": 24,
            "pages_per_slice": 3,
        },
        "deep": {
            "name": "deep",
            "label": "Глубокий анализ",
            "max_slices": 36,
            "pages_per_slice": 4,
        },
    }
    return dict(profiles.get(name, profiles["balanced"]))


def _supply_proxy_pool() -> tuple[list[str], str]:
    """Read an existing transport pool; this scanner never provisions proxies."""
    configured = os.getenv("KWORK_MARKET_SUPPLY_PROXY_URLS", "").strip()
    source = "KWORK_MARKET_SUPPLY_PROXY_URLS"
    if not configured:
        configured = os.getenv("KWORK_PROXY_LIST", "").strip()
        source = "KWORK_PROXY_LIST"
    urls = [item.strip() for item in configured.split(",") if item.strip()]
    if urls:
        return list(dict.fromkeys(urls)), source

    ports_spec = os.getenv("KWORK_MARKET_SUPPLY_PROXY_PORTS", "").strip()
    if not ports_spec:
        return [], "default_market_transport"
    ports: list[int] = []
    for part in ports_spec.split(","):
        value = part.strip()
        if not value:
            continue
        match = re.fullmatch(r"(\d{1,5})\s*-\s*(\d{1,5})", value)
        if match:
            start, end = (int(match.group(1)), int(match.group(2)))
            if start > end:
                start, end = end, start
            for port in range(max(1, start), min(65535, end) + 1):
                ports.append(port)
                if len(ports) >= 64:
                    break
            if len(ports) >= 64:
                break
            continue
        if value.isdigit():
            ports.append(int(value))
        if len(ports) >= 64:
            break
    urls = [f"http://127.0.0.1:{port}" for port in ports if 1 <= port <= 65535]
    return list(dict.fromkeys(urls)), "KWORK_MARKET_SUPPLY_PROXY_PORTS"


def _flatten_classifiers(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    def child_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for key in ("children", "childs", "classifiers", "items", "subcategories"):
            children.extend(child for child in item.get(key) or [] if isinstance(child, dict))
        return children

    def visit(item: Any, depth: int = 0) -> None:
        if not isinstance(item, dict):
            return
        children = child_rows(item)
        identifier = _as_int(item.get("id") or item.get("classifier_id") or item.get("classifierId"))
        if identifier and identifier not in seen:
            seen.add(identifier)
            result.append(
                {
                    "id": identifier,
                    "name": _clean_text(item.get("name") or item.get("title") or item.get("value") or identifier, 180),
                    "kworks_count": _as_int(item.get("kworks_count") or item.get("kworksCount") or item.get("count")),
                    "depth": depth,
                    "is_leaf": not children,
                }
            )
        for child in children:
            visit(child, depth + 1)

    for row in rows:
        visit(row)
    return result


def _choose_stratified_slices(rows: list[dict[str, Any]], max_slices: int) -> list[dict[str, Any]]:
    """Pick API-provided slices across the count distribution, not by terms."""
    leaves = [row for row in rows if row.get("is_leaf")]
    candidates = leaves or rows
    unique = {int(row["id"]): row for row in candidates if _as_int(row.get("id")) > 0}
    ordered = sorted(unique.values(), key=lambda row: (_as_int(row.get("kworks_count")), _as_int(row.get("id"))))
    if len(ordered) <= max_slices:
        return ordered
    if max_slices <= 1:
        return [ordered[len(ordered) // 2]]
    positions = {round(index * (len(ordered) - 1) / (max_slices - 1)) for index in range(max_slices)}
    return [ordered[index] for index in sorted(positions)]


def _listing_key(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("share_url") or item.get("url") or _stable_json(item)[:120])


def _mobile_reported_page(catalog: dict[str, Any]) -> int | None:
    """Read only an explicit server-reported page; never infer it from input."""

    for key in ("paging", "pagination", "meta"):
        value = catalog.get(key)
        if not isinstance(value, dict):
            continue
        page = _as_int(value.get("page"))
        if page > 0:
            return page
    return None


def _page_fingerprint(cards: list[dict[str, Any]]) -> str:
    return fingerprint_for_cards(cards)


def _public_api_card(value: dict[str, Any]) -> dict[str, Any]:
    """Keep the API evidence available to the LLM without response metadata."""
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}
    return normalized if isinstance(normalized, dict) else {}


def _failure_kind(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}".lower()
    return "protection" if any(marker in detail for marker in _PROTECTION_MARKERS) else "upstream"


def _retry_after_seconds(detail: str) -> float | None:
    match = re.search(r"retry[-_ ]after[^0-9]{0,12}(\d+(?:\.\d+)?)", detail, flags=re.I)
    if not match:
        return None
    return min(300.0, max(0.0, float(match.group(1))))


def _adaptive_backoff_seconds(error_count: int, detail: str = "") -> float:
    """Slow the next wave after a transient failure; protection stops the run."""
    retry_after = _retry_after_seconds(detail)
    if retry_after is not None:
        return retry_after
    return min(30.0, (0.75 * (2 ** min(error_count, 5))) + random.uniform(0.0, 0.5))


def _normalize_listing(item: dict[str, Any], *, slice_info: dict[str, Any], page: int) -> dict[str, Any]:
    worker = item.get("worker") if isinstance(item.get("worker"), dict) else {}
    return {
        "id": item.get("id") or item.get("PID"),
        "title": _clean_text(item.get("title") or item.get("name"), 500),
        "description": _clean_text(
            item.get("description")
            or item.get("short_description")
            or item.get("shortDescription")
            or item.get("preview_description")
            or item.get("gdesc"),
            4000,
        ),
        "price": _as_int(item.get("price") or item.get("priceWithCurrency")),
        "currency": _clean_text(item.get("currency") or "RUB", 16),
        "seller": _clean_text(
            worker.get("username") or worker.get("login") or worker.get("name") or item.get("worker_name"),
            160,
        ),
        "reviews": _as_int(worker.get("reviews") or worker.get("reviews_count") or item.get("reviews")),
        "rating": item.get("rating") or worker.get("rating"),
        "classifier_id": _as_int(item.get("classifier_id") or item.get("classifierId") or slice_info.get("id")),
        "slice_id": slice_info.get("id"),
        "slice_name": slice_info.get("name"),
        "page": page,
        "share_url": _clean_text(item.get("share_url") or item.get("url"), 500),
        "api_card": _public_api_card(item),
    }


def _percent(value: int, total: int) -> float | None:
    return round(value * 100 / total, 1) if total > 0 else None


def _sample_summary(listings: list[dict[str, Any]], *, scanned_scope_total: int | None) -> dict[str, Any]:
    prices = sorted(item["price"] for item in listings if _as_int(item.get("price")) > 0)
    seller_counts = Counter(item["seller"] for item in listings if item.get("seller"))
    repeated_cards = sum(count for count in seller_counts.values() if count > 1)
    repeated_sellers = [
        {"seller": seller, "cards": count}
        for seller, count in seller_counts.most_common()
        if count > 1
    ][:12]
    return {
        "observed_listings": len(listings),
        "unique_sellers": len(seller_counts),
        "reported_selected_scope_total": scanned_scope_total,
        "observed_share_of_scanned_scope_percent": _percent(len(listings), scanned_scope_total or 0),
        "price_sample": {
            "count": len(prices),
            "min": prices[0] if prices else None,
            "median": int(median(prices)) if prices else None,
            "max": prices[-1] if prices else None,
        },
        "seller_repetition_in_observed_cards": {
            "repeated_cards": repeated_cards,
            "repeat_share_percent": _percent(repeated_cards, len(listings)),
            "sellers": repeated_sellers,
        },
    }


def _confidence(*, observed: int, planned_slices: int, completed_slices: int) -> str:
    if observed < 30 or not planned_slices:
        return "low"
    slice_share = completed_slices / planned_slices
    if observed >= 120 and slice_share >= 0.8:
        return "high"
    return "medium"


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _llm_listing_limit() -> int:
    return _env_int("KWORK_MARKET_LLM_MAX_LISTINGS", 5000, low=500, high=5000)


def _llm_evidence_char_limit() -> int:
    return _env_int("KWORK_MARKET_LLM_MAX_EVIDENCE_CHARS", 3_500_000, low=200_000, high=8_000_000)


def _llm_listing_evidence(listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preserve raw cards while respecting the configured provider context budget."""
    included: list[dict[str, Any]] = []
    used_chars = 0
    listing_limit = _llm_listing_limit()
    char_limit = _llm_evidence_char_limit()
    for listing in listings[:listing_limit]:
        encoded = _stable_json(listing)
        if included and used_chars + len(encoded) > char_limit:
            break
        included.append(listing)
        used_chars += len(encoded)
    return included, {
        "raw_listing_count": len(listings),
        "included_listing_count": len(included),
        "listing_limit": listing_limit,
        "evidence_char_limit": char_limit,
        "included_chars": used_chars,
        "truncated": len(included) < len(listings),
    }


def _market_assistant_map_char_limit() -> int:
    return _env_int("KWORK_MARKET_ASSISTANT_MAP_CHARS", 525_000, low=100_000, high=700_000)


def _market_assistant_map_concurrency() -> int:
    return _env_int("KWORK_MARKET_ASSISTANT_MAP_CONCURRENCY", 3, low=1, high=4)


def _market_assistant_map_attempts() -> int:
    return _env_int("KWORK_MARKET_ASSISTANT_MAP_ATTEMPTS", 3, low=1, high=5)


def _market_assistant_llm_timeout() -> int:
    return _env_int("KWORK_MARKET_ASSISTANT_LLM_TIMEOUT_SECONDS", 180, low=60, high=600)


def _market_assistant_map_model() -> str:
    return os.getenv("KWORK_MARKET_ASSISTANT_MAP_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"


def _market_assistant_reduce_model() -> str:
    return os.getenv("KWORK_MARKET_ASSISTANT_REDUCE_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra"


def _market_assistant_map_reasoning() -> str:
    return os.getenv("KWORK_MARKET_ASSISTANT_MAP_REASONING", "high").strip() or "high"


def _market_assistant_reduce_reasoning() -> str:
    return os.getenv("KWORK_MARKET_ASSISTANT_REDUCE_REASONING", "high").strip() or "high"


def _market_assistant_analysis_cache_root() -> Path:
    path = _context_root() / "analysis_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _market_assistant_chunk_cache_path(question: str, chunk: list[dict[str, Any]]) -> Path:
    digest = hashlib.sha256()
    digest.update(b"market-assistant-map-v2\0")
    digest.update(_market_assistant_map_model().encode("utf-8"))
    digest.update(b"\0")
    digest.update(_market_assistant_map_reasoning().encode("utf-8"))
    digest.update(b"\0")
    digest.update(question.encode("utf-8"))
    digest.update(b"\0")
    digest.update(_stable_json(chunk).encode("utf-8"))
    return _market_assistant_analysis_cache_root() / f"{digest.hexdigest()}.json"


def _chunk_market_listings(listings: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the complete evidence set without dropping or rewriting any listing."""
    char_limit = _market_assistant_map_char_limit()
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for listing in listings:
        encoded_chars = len(_stable_json(listing))
        if current and current_chars + encoded_chars > char_limit:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(listing)
        current_chars += encoded_chars
    if current:
        chunks.append(current)
    return chunks


def _market_listing_reference(listing: dict[str, Any]) -> dict[str, Any]:
    source = listing.get("api_card") if isinstance(listing.get("api_card"), dict) else listing
    return {
        "id": source.get("id") or source.get("listing_id") or _listing_key(listing),
        "title": _clean_text(source.get("gtitle") or source.get("title") or source.get("name"), 500),
        "seller": _clean_text(source.get("userName") or source.get("seller_name"), 180),
        "price": source.get("price"),
        "url": _clean_text(source.get("url") or source.get("link"), 700),
    }


def _complete_market_listings(snapshot: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    listings: list[dict[str, Any]] = [
        item for item in snapshot.get("raw_listings") or [] if isinstance(item, dict)
    ]
    for refresh in payload.get("refreshes") or []:
        if not isinstance(refresh, dict):
            continue
        listings.extend(item for item in refresh.get("raw_listings") or [] if isinstance(item, dict))

    unique: dict[str, dict[str, Any]] = {}
    for index, listing in enumerate(listings):
        key = _listing_key(listing) or f"row:{index}"
        unique.setdefault(key, listing)
    return list(unique.values())


def _market_assistant_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "market_overview",
            "description": "Return scope, coverage, metrics, and the exact number of locally stored market listings.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": True,
        },
        {
            "type": "function",
            "name": "read_market_page",
            "description": "Read exact raw JSON listings from the local market file by offset. Use small pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["offset", "limit"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "search_market_listings",
            "description": "Search all stored raw listings and return references. Fetch an exact match with get_market_listing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query", "limit"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "get_market_listing",
            "description": "Return one exact raw JSON listing by its id or listing key.",
            "parameters": {
                "type": "object",
                "properties": {"listing_id": {"type": "string"}},
                "required": ["listing_id"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "analyze_full_market",
            "description": "Analyze every stored raw listing in bounded chunks, then synthesize one complete answer.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


class MarketAssistantStore:
    """Durable local context store. Cookies and provider credentials never enter it."""

    @staticmethod
    def path(context_id: str) -> Path:
        safe = "".join(ch for ch in context_id if ch.isalnum() or ch in {"-", "_"})
        if not safe:
            raise ValueError("Invalid market assistant context id")
        return _context_root() / f"{safe}.json"

    @classmethod
    def create(cls, snapshot: dict[str, Any]) -> str:
        context_id = uuid.uuid4().hex
        payload = {
            "context_id": context_id,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "snapshot": snapshot,
            "messages": [],
        }
        cls.path(context_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return context_id

    @classmethod
    def load(cls, context_id: str) -> dict[str, Any]:
        path = cls.path(context_id)
        if not path.exists():
            raise FileNotFoundError("Market analysis context was not found. Run a supply analysis again.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Market analysis context is invalid")
        return payload

    @classmethod
    def save(cls, context_id: str, payload: dict[str, Any]) -> None:
        payload["updated_at"] = _utc_now()
        cls.path(context_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def upsert(cls, context_id: str, snapshot: dict[str, Any]) -> str:
        """Create or refresh a deterministic context for a durable market job."""

        path = cls.path(context_id)
        if path.exists():
            payload = cls.load(context_id)
            payload["snapshot"] = snapshot
            cls.save(context_id, payload)
            return context_id
        payload = {
            "context_id": context_id,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "snapshot": snapshot,
            "messages": [],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return context_id

    @classmethod
    def replace_snapshot(cls, context_id: str, snapshot: dict[str, Any]) -> None:
        payload = cls.load(context_id)
        payload["snapshot"] = snapshot
        cls.save(context_id, payload)


class KworkSupplyScanner:
    """Collect a bounded, rubric-only sample of supply through existing API calls."""

    def __init__(self, client: KworkMarketClient | None = None) -> None:
        self.client = client or KworkMarketClient()
        self._owns_client = client is None
        self._transport_clients: list[KworkMarketClient] = [self.client]
        self._transport_locks: list[asyncio.Semaphore] = [asyncio.Semaphore(1)]
        self._transport_index = 0
        self._transport_source = "provided_client" if client is not None else "default_market_transport"

    async def close(self) -> None:
        if not self._owns_client:
            return
        closed: set[int] = set()
        for client in self._transport_clients:
            if id(client) in closed:
                continue
            closed.add(id(client))
            await client.close()

    async def _configure_transport(self) -> None:
        if not self._owns_client or len(self._transport_clients) > 1:
            return
        proxy_urls, source = _supply_proxy_pool()
        if not proxy_urls:
            return
        await self.client.close()
        self._transport_clients = [KworkMarketClient(proxy_url=proxy_url) for proxy_url in proxy_urls]
        self._transport_locks = [asyncio.Semaphore(1) for _ in self._transport_clients]
        self.client = self._transport_clients[0]
        self._transport_index = 0
        self._transport_source = source

    def _next_transport_client(self) -> tuple[KworkMarketClient, int, asyncio.Semaphore]:
        slot = self._transport_index % len(self._transport_clients)
        self._transport_index += 1
        return self._transport_clients[slot], slot, self._transport_locks[slot]

    async def _cached_page(
        self,
        *,
        category_id: int,
        classifier_id: int | None,
        page: int,
        client: KworkMarketClient,
        semaphore: asyncio.Semaphore,
        transport_lock: asyncio.Semaphore,
        force_refresh: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        key = _stable_json({"category_id": category_id, "classifier_id": classifier_id, "page": page})
        now = time.monotonic()
        if not force_refresh:
            async with _SUPPLY_CACHE_LOCK:
                cached = _SUPPLY_PAGE_CACHE.get(key)
                if cached and now - cached[0] <= SUPPLY_CACHE_TTL:
                    return copy.deepcopy(cached[1]), True
        async with semaphore:
            async with transport_lock:
                catalog = await client.get_kworks(
                    category_id=category_id,
                    classifier_id=classifier_id,
                    page=page,
                )
        async with _SUPPLY_CACHE_LOCK:
            _SUPPLY_PAGE_CACHE[key] = (time.monotonic(), copy.deepcopy(catalog))
        return catalog, False

    async def _collect_page(
        self,
        *,
        category_id: int,
        slice_info: dict[str, Any],
        page: int,
        semaphore: asyncio.Semaphore,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        request_started = time.monotonic()
        client, transport_slot, transport_lock = self._next_transport_client()
        try:
            catalog, cached = await self._cached_page(
                category_id=category_id,
                classifier_id=_as_int(slice_info.get("id")) or None,
                page=page,
                client=client,
                semaphore=semaphore,
                transport_lock=transport_lock,
                force_refresh=force_refresh,
            )
            cards = [item for item in catalog.get("kworks") or [] if isinstance(item, dict)]
            normalized_cards = [_normalize_listing(item, slice_info=slice_info, page=page) for item in cards]
            reported_page = _mobile_reported_page(catalog)
            return {
                "source": "mobile_kworks",
                "slice": slice_info,
                "page": page,
                "status": "ok" if cards else "empty",
                "cached": cached,
                "transport_slot": transport_slot,
                "declared_count": _as_int(catalog.get("kworks_count") or catalog.get("count")),
                "requested_cursor": {"kind": "page", "page": page},
                "reported_cursor": {"kind": "page", "page": reported_page} if reported_page else None,
                "page_fingerprint": _page_fingerprint(normalized_cards),
                "cards": normalized_cards,
                "timing_ms": int((time.monotonic() - request_started) * 1000),
            }
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            return {
                "source": "mobile_kworks",
                "slice": slice_info,
                "page": page,
                "status": "error",
                "failure_kind": _failure_kind(exc),
                "transport_slot": transport_slot,
                "requested_cursor": {"kind": "page", "page": page},
                "reported_cursor": None,
                "detail": detail,
                "retry_after_seconds": _retry_after_seconds(detail),
                "timing_ms": int((time.monotonic() - request_started) * 1000),
            }

    async def scan(
        self,
        *,
        category_id: int,
        category_name: str = "",
        classifier_id: int | None = None,
        classifier_name: str = "",
        coverage: str = "balanced",
        include_llm: bool = True,
        write_file: bool = True,
    ) -> dict[str, Any]:
        if category_id <= 0:
            raise ValueError("category_id is required")
        await self._configure_transport()
        profile = _coverage_profile(coverage)
        max_requests = _env_int("KWORK_MARKET_SUPPLY_MAX_REQUESTS", 72, low=50, high=720)
        concurrency = _env_int("KWORK_MARKET_SUPPLY_CONCURRENCY", 2, low=1, high=4)
        minimum_cards = _env_int("KWORK_MARKET_SUPPLY_MIN_CARDS", 60, low=1, high=10_000)
        wave_delay_ms = _env_int("KWORK_MARKET_SUPPLY_WAVE_DELAY_MS", 750, low=0, high=10000)
        requested_pages_per_slice = min(
            int(profile["pages_per_slice"]),
            _env_int("KWORK_MARKET_SUPPLY_MAX_PAGES", 3, low=1, high=8),
        )
        # Mobile `/kworks` has no accepted continuation contract yet. It remains
        # a first-page source until the web adapter proves continuation instead.
        pages_per_slice = 1
        per_slice_safety_limit = 1
        max_slices = min(int(profile["max_slices"]), max(1, max_requests // pages_per_slice))
        started = time.monotonic()
        semaphore = asyncio.Semaphore(concurrency)

        root_client, _root_transport_slot, root_transport_lock = self._next_transport_client()
        root_catalog, root_cached = await self._cached_page(
            category_id=category_id,
            classifier_id=None,
            page=1,
            client=root_client,
            semaphore=semaphore,
            transport_lock=root_transport_lock,
        )
        available_slices = _flatten_classifiers(list(root_catalog.get("classifiers") or []))
        category_total = _as_int(root_catalog.get("kworks_count") or root_catalog.get("count"))
        if classifier_id:
            selected = next((item for item in available_slices if item["id"] == classifier_id), None)
            slices = [
                selected
                or {
                    "id": classifier_id,
                    "name": classifier_name or f"Classifier {classifier_id}",
                    "kworks_count": _as_int(root_catalog.get("kworks_count")),
                    "depth": 0,
                }
            ]
            selected_scope_total = _as_int(slices[0].get("kworks_count")) or category_total
        elif available_slices:
            slices = _choose_stratified_slices(available_slices, max_slices)
            selected_scope_total = sum(_as_int(item.get("kworks_count")) for item in slices) or None
        else:
            slices = [
                {
                    "id": None,
                    "name": category_name or f"Category {category_id}",
                    "kworks_count": category_total,
                    "depth": 0,
                    "is_leaf": True,
                }
            ]
            selected_scope_total = category_total or None

        expected_page_size = max(
            1,
            len([item for item in root_catalog.get("kworks") or [] if isinstance(item, dict)])
            or _env_int("KWORK_MARKET_SUPPLY_EXPECTED_PAGE_SIZE", 10, low=1, high=100),
        )
        estimated_requests_for_minimum_cards = math.ceil(minimum_cards / expected_page_size)
        minimum_pages_per_slice = max(1, math.ceil(minimum_cards / expected_page_size / max(1, len(slices))))
        target_pages_per_slice = min(per_slice_safety_limit, max(pages_per_slice, minimum_pages_per_slice))
        slice_page_limits: dict[int, int] = {}
        for slice_info in slices:
            slice_id = _as_int(slice_info.get("id"))
            declared = _as_int(slice_info.get("kworks_count"))
            declared_pages = math.ceil(declared / expected_page_size) if declared else target_pages_per_slice
            slice_page_limits[slice_id] = max(1, min(target_pages_per_slice, declared_pages))

        potential_jobs: list[tuple[dict[str, Any], int]] = []
        # Round-robin pages across dynamic API slices. This keeps a partial run
        # representative of the selected rubric instead of exhausting one slice.
        for page in range(1, max(slice_page_limits.values(), default=1) + 1):
            for slice_info in slices:
                if page > slice_page_limits[_as_int(slice_info.get("id"))]:
                    continue
                if len(potential_jobs) >= max_requests:
                    break
                potential_jobs.append((slice_info, page))
            if len(potential_jobs) >= max_requests:
                break

        listings_by_id: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        requested_jobs: list[tuple[dict[str, Any], int]] = []
        exhausted_slices: set[int] = set()
        mobile_batch_states: dict[int, BatchState] = {}
        request_errors = 0
        contract_violations = 0
        stopped_after_target = False
        aborted_after_upstream_errors = False
        stopped_after_protection_signal = False
        backoff_seconds_total = 0.0

        # Request in small concurrent waves. This permits an early stop at the
        # requested card target and prevents a failing upstream from triggering
        # the whole plan at once.
        for page in range(1, max(slice_page_limits.values(), default=1) + 1):
            page_jobs = [
                job
                for job in potential_jobs
                if job[1] == page and _as_int(job[0].get("id")) not in exhausted_slices
            ]
            for offset in range(0, len(page_jobs), concurrency):
                batch = page_jobs[offset : offset + concurrency]
                requested_jobs.extend(batch)
                batch_results = await asyncio.gather(
                    *(
                        self._collect_page(
                            category_id=category_id,
                            slice_info=slice_info,
                            page=job_page,
                            semaphore=semaphore,
                        )
                        for slice_info, job_page in batch
                    )
                )
                results.extend(batch_results)
                for result in batch_results:
                    if result.get("status") == "error":
                        request_errors += 1
                        continue

                    slice_id = _as_int(result.get("slice", {}).get("id"))
                    requested_page = _as_int(result.get("page"))
                    reported_cursor = result.get("reported_cursor") or {}
                    reported_page = _as_int(reported_cursor.get("page")) if isinstance(reported_cursor, dict) else 0
                    request = BatchRequest(
                        source="mobile_kworks",
                        shard_key=f"category:{category_id}:slice:{slice_id}",
                        cursor=SourceCursor.page_cursor(requested_page),
                    )
                    response = BatchResult(
                        source="mobile_kworks",
                        requested_cursor=request.cursor,
                        reported_cursor=SourceCursor.page_cursor(reported_page) if reported_page else None,
                        cards=tuple(result.get("cards") or []),
                        actual_item_count=len(result.get("cards") or []),
                        fingerprint=result.get("page_fingerprint"),
                        source_total=result.get("declared_count") or None,
                        protection_status=ProtectionStatus.OK,
                        timing_ms=_as_int(result.get("timing_ms")) or None,
                    )
                    previous = mobile_batch_states.get(slice_id)
                    verdict = validate_mobile_page(request, response, previous)
                    result["contract_state"] = verdict.state.value
                    result["contract_reason_codes"] = list(verdict.reason_codes)
                    result["page_fingerprint"] = response.fingerprint

                    cards = list(result.get("cards") or [])
                    batch_keys: set[str] = set()
                    global_new_unique = 0
                    for listing in cards:
                        key = _listing_key(listing)
                        if key in listings_by_id or key in batch_keys:
                            continue
                        batch_keys.add(key)
                        global_new_unique += 1
                    result["received_count"] = len(cards)
                    result["new_unique"] = global_new_unique
                    result["duplicates"] = len(cards) - global_new_unique
                    result["duplicate_rate"] = round(result["duplicates"] / len(cards), 4) if cards else 0.0

                    if verdict.novelty is not None:
                        result["source_new_unique"] = verdict.novelty.new_unique
                        result["source_duplicates"] = verdict.novelty.duplicate_count

                    if verdict.state is ContractState.CONTRACT_VIOLATION:
                        result["status"] = "contract_violation"
                        result["failure_kind"] = "contract_violation"
                        contract_violations += 1
                        exhausted_slices.add(slice_id)
                        continue
                    if verdict.state is ContractState.BLOCKED:
                        result["status"] = "blocked"
                        result["failure_kind"] = "protection"
                        stopped_after_protection_signal = True
                        exhausted_slices.add(slice_id)
                        continue
                    if verdict.state is ContractState.EXHAUSTED:
                        exhausted_slices.add(slice_id)
                        continue

                    mobile_batch_states[slice_id] = (previous or BatchState()).accept(response)
                    for listing in cards:
                        key = _listing_key(listing)
                        existing = listings_by_id.get(key)
                        if existing is None:
                            listing["first_seen_slice_id"] = slice_id or None
                            listing["first_seen_page"] = requested_page
                            listing["first_seen_requested_cursor"] = result["requested_cursor"]
                            listing["first_seen_reported_cursor"] = result["reported_cursor"]
                            listing["observation_count"] = 1
                            listings_by_id[key] = listing
                            continue
                        existing["observation_count"] = _as_int(existing.get("observation_count"), 1) + 1
                        existing["duplicate_observation_count"] = _as_int(existing.get("duplicate_observation_count")) + 1
                if any(result.get("failure_kind") == "protection" for result in batch_results):
                    stopped_after_protection_signal = True
                    break
                if request_errors >= 3:
                    aborted_after_upstream_errors = True
                    break
                if len(listings_by_id) >= minimum_cards:
                    stopped_after_target = True
                    break
                error_details = [str(result.get("detail") or "") for result in batch_results if result.get("status") == "error"]
                delay_seconds = (
                    _adaptive_backoff_seconds(request_errors, error_details[-1])
                    if error_details
                    else wave_delay_ms / 1000
                )
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                    backoff_seconds_total += delay_seconds
            if stopped_after_target or aborted_after_upstream_errors or stopped_after_protection_signal:
                break

        listings = list(listings_by_id.values())
        completed_slices = {
            _as_int(result.get("slice", {}).get("id"))
            for result in results
            if result.get("status") in {"ok", "empty"}
        }
        sample = _sample_summary(listings, scanned_scope_total=selected_scope_total)
        slice_rows: list[dict[str, Any]] = []
        for slice_info in slices:
            slice_id = _as_int(slice_info.get("id"))
            observed = [item for item in listings if _as_int(item.get("slice_id")) == slice_id]
            slice_rows.append(
                {
                    **slice_info,
                    "observed_cards": len(observed),
                    "observed_unique_sellers": len({item.get("seller") for item in observed if item.get("seller")}),
                    "pages_requested": sum(1 for item, _ in requested_jobs if _as_int(item.get("id")) == slice_id),
                    "pages_completed": sum(
                        1
                        for result in results
                        if _as_int(result.get("slice", {}).get("id")) == slice_id and result.get("status") in {"ok", "empty"}
                    ),
                }
            )
        snapshot: dict[str, Any] = {
            "source": "psr.kwork_supply_scan",
            "generated_at": _utc_now(),
            "transport": {
                "client_pool_size": len(self._transport_clients),
                "source": self._transport_source,
                "uses_configured_proxy_pool": self._transport_source != "default_market_transport",
            },
            "scope": {
                "category_id": category_id,
                "category_name": category_name,
                "classifier_id": classifier_id,
                "classifier_name": classifier_name,
                "reported_category_total": category_total or None,
                "reported_selected_slices_total": selected_scope_total,
            },
            "coverage": {
                "profile": profile,
                "concurrency": concurrency,
                "inter_wave_delay_ms": wave_delay_ms,
                "max_requests": max_requests,
                "requested_pages_per_slice": requested_pages_per_slice,
                "mobile_continuation_policy": "first_page_only_until_validated_web_source",
                "estimated_requests_for_minimum_cards": estimated_requests_for_minimum_cards,
                "request_budget_can_reach_minimum_target": max_requests >= estimated_requests_for_minimum_cards,
                "minimum_cards_target": minimum_cards,
                "expected_cards_per_page": expected_page_size,
                "requests_planned": len(potential_jobs),
                "requests_started": len(requested_jobs),
                "requests_completed": sum(1 for result in results if result.get("status") in {"ok", "empty"}),
                "request_errors": request_errors,
                "contract_violations": contract_violations,
                "backoff_seconds_total": round(backoff_seconds_total, 2),
                "slice_count_available": len(available_slices),
                "slice_count_scanned": len(slices),
                "confidence": _confidence(observed=len(listings), planned_slices=len(slices), completed_slices=len(completed_slices)),
                "root_page_cached": root_cached,
                "aborted_after_upstream_errors": aborted_after_upstream_errors,
                "stopped_after_protection_signal": stopped_after_protection_signal,
                "minimum_cards_target_met": len(listings) >= minimum_cards,
                "stopped_after_minimum_cards": stopped_after_target,
                "observed_listing_count": len(listings),
                "scanned_slices_share_of_category_percent": _percent(selected_scope_total or 0, category_total),
            },
            "slices": slice_rows,
            "sample": sample,
            "raw_listings": listings,
            "requests": [
                {
                    "slice_id": result.get("slice", {}).get("id"),
                    "slice_name": result.get("slice", {}).get("name"),
                    "page": result.get("page"),
                    "requested_cursor": result.get("requested_cursor"),
                    "reported_cursor": result.get("reported_cursor"),
                    "page_fingerprint": result.get("page_fingerprint"),
                    "status": result.get("status"),
                    "contract_state": result.get("contract_state"),
                    "contract_reason_codes": result.get("contract_reason_codes") or [],
                    "cached": result.get("cached", False),
                    "transport_slot": result.get("transport_slot"),
                    "declared_count": result.get("declared_count"),
                    "card_count": len(result.get("cards") or []),
                    "received_count": result.get("received_count"),
                    "new_unique": result.get("new_unique"),
                    "duplicates": result.get("duplicates"),
                    "duplicate_rate": result.get("duplicate_rate"),
                    "source_new_unique": result.get("source_new_unique"),
                    "source_duplicates": result.get("source_duplicates"),
                    "timing_ms": result.get("timing_ms"),
                    "detail": result.get("detail"),
                    "failure_kind": result.get("failure_kind"),
                    "retry_after_seconds": result.get("retry_after_seconds"),
                }
                for result in results
            ],
            "timings_ms": {"total": int((time.monotonic() - started) * 1000)},
        }
        if include_llm:
            snapshot["analysis"] = await self._analyze(snapshot)
        else:
            snapshot["analysis"] = {"status": "disabled"}
        context_id = MarketAssistantStore.create(snapshot)
        snapshot["assistant_context_id"] = context_id
        if write_file:
            root = _snapshot_root()
            root.mkdir(parents=True, exist_ok=True)
            filename = datetime.now(UTC).strftime("kwork_supply_%Y%m%dT%H%M%SZ.json")
            path = root / filename
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot["file_path"] = str(path)
        MarketAssistantStore.replace_snapshot(context_id, snapshot)
        return snapshot

    async def refresh_slice(
        self,
        *,
        category_id: int,
        classifier_id: int,
        classifier_name: str,
        pages: int,
    ) -> dict[str, Any]:
        """Refresh one verified mobile baseline for the chat assistant."""
        if category_id <= 0 or classifier_id <= 0:
            raise ValueError("A category and classifier are required for a narrow refresh")
        await self._configure_transport()
        requested_pages = max(1, min(pages, 3))
        pages = 1
        concurrency = 1
        semaphore = asyncio.Semaphore(concurrency)
        slice_info = {"id": classifier_id, "name": classifier_name or f"Classifier {classifier_id}"}
        started = time.monotonic()
        result = await self._collect_page(
            category_id=category_id,
            slice_info=slice_info,
            page=1,
            semaphore=semaphore,
            force_refresh=True,
        )
        results = [result]
        listings_by_id: dict[str, dict[str, Any]] = {}
        if result.get("status") != "error":
            reported_cursor = result.get("reported_cursor") or {}
            reported_page = _as_int(reported_cursor.get("page")) if isinstance(reported_cursor, dict) else 0
            request = BatchRequest(
                source="mobile_kworks",
                shard_key=f"category:{category_id}:slice:{classifier_id}",
                cursor=SourceCursor.page_cursor(1),
            )
            response = BatchResult(
                source="mobile_kworks",
                requested_cursor=request.cursor,
                reported_cursor=SourceCursor.page_cursor(reported_page) if reported_page else None,
                cards=tuple(result.get("cards") or []),
                actual_item_count=len(result.get("cards") or []),
                fingerprint=result.get("page_fingerprint"),
                source_total=result.get("declared_count") or None,
                protection_status=ProtectionStatus.OK,
                timing_ms=_as_int(result.get("timing_ms")) or None,
            )
            verdict = validate_mobile_page(request, response)
            result["contract_state"] = verdict.state.value
            result["contract_reason_codes"] = list(verdict.reason_codes)
            result["page_fingerprint"] = response.fingerprint
            if verdict.state is ContractState.ACCEPTED:
                for listing in result.get("cards") or []:
                    key = _listing_key(listing)
                    if key in listings_by_id:
                        listings_by_id[key]["observation_count"] = _as_int(
                            listings_by_id[key].get("observation_count"), 1
                        ) + 1
                        continue
                    listing["first_seen_slice_id"] = classifier_id
                    listing["first_seen_page"] = 1
                    listing["first_seen_requested_cursor"] = result["requested_cursor"]
                    listing["first_seen_reported_cursor"] = result["reported_cursor"]
                    listing["observation_count"] = 1
                    listings_by_id[key] = listing
            elif verdict.state is ContractState.CONTRACT_VIOLATION:
                result["status"] = "contract_violation"
                result["failure_kind"] = "contract_violation"
        return {
            "status": "ok" if any(result.get("status") == "ok" for result in results) else "unavailable",
            "refreshed_at": _utc_now(),
            "scope": {"category_id": category_id, "classifier_id": classifier_id, "classifier_name": slice_info["name"]},
            "pages": pages,
            "requested_pages": requested_pages,
            "mobile_continuation_policy": "first_page_only_until_validated_web_source",
            "raw_listings": list(listings_by_id.values()),
            "requests": [
                {
                    "page": result.get("page"),
                    "requested_cursor": result.get("requested_cursor"),
                    "reported_cursor": result.get("reported_cursor"),
                    "page_fingerprint": result.get("page_fingerprint"),
                    "status": result.get("status"),
                    "contract_state": result.get("contract_state"),
                    "contract_reason_codes": result.get("contract_reason_codes") or [],
                    "cached": result.get("cached", False),
                    "transport_slot": result.get("transport_slot"),
                    "card_count": len(result.get("cards") or []),
                    "detail": result.get("detail"),
                }
                for result in results
            ],
            "timings_ms": {"total": int((time.monotonic() - started) * 1000)},
        }

    async def _analyze(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raw_listings = [item for item in snapshot.get("raw_listings") or [] if isinstance(item, dict)]
        llm_listings, evidence_window = _llm_listing_evidence(raw_listings)
        evidence = {
            "scope": snapshot["scope"],
            "coverage": snapshot["coverage"],
            "slices": snapshot["slices"],
            "sample": snapshot["sample"],
            "evidence_window": evidence_window,
            "raw_listings": llm_listings,
        }
        system_prompt = (
            "Ты аналитик предложения услуг на Kwork. Анализируй только предоставленные "
            "данные выбранной рубрики, включая исходные API-карточки. Не добавляй темы, "
            "технологии или советы, которых нет в доказательствах. Не выдавай выборку за весь "
            "рынок: всегда указывай размер и границы покрытия, а также уверенность вывода. "
            "Верни только JSON с полями summary, supply_shape, niches, listing_directions, "
            "risks, questions_to_validate. niches и listing_directions должны быть массивами "
            "объектов с полями name/direction, evidence, why, confidence."
        )
        prompt = "Сформируй практический анализ предложения услуг и варианты позиционирования.\n\n" + json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":")
        )
        try:
            from src.brain.llm_router import get_llm_router

            response = await get_llm_router().generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=5000,
                task="market_analysis",
            )
            parsed = json.loads(_strip_json_fence(response))
            if not isinstance(parsed, dict):
                raise ValueError("LLM response is not a JSON object")
            return {"status": "ok", "provider_output": parsed, "evidence_window": evidence_window}
        except Exception as exc:
            logger.warning(f"Kwork supply analysis LLM unavailable: {type(exc).__name__}: {exc}")
            return {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}", "evidence_window": evidence_window}


class MarketAssistant:
    """LLM conversation over one saved supply scan with a narrow refresh tool."""

    @staticmethod
    async def _generate(*, system_prompt: str, prompt: str) -> dict[str, Any]:
        from src.brain.llm_router import get_llm_router

        raw = await get_llm_router().generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.25,
            max_tokens=3000,
            task="market_assistant",
        )
        result = json.loads(_strip_json_fence(raw))
        if not isinstance(result, dict):
            raise ValueError("LLM response is not a JSON object")
        return result

    @staticmethod
    async def _generate_with_tools(
        *,
        system_prompt: str,
        prompt: str,
        tool_handler: Any,
    ) -> dict[str, Any]:
        from src.brain.llm_router import get_llm_router

        raw = await get_llm_router().generate_with_tools(
            prompt=prompt,
            system_prompt=system_prompt,
            tools=_market_assistant_tools(),
            tool_handler=tool_handler,
            provider="openai",
            temperature=0.25,
            max_tokens=3000,
            task="market_assistant",
            max_steps=10,
        )
        result = json.loads(_strip_json_fence(raw))
        if not isinstance(result, dict):
            raise ValueError("LLM response is not a JSON object")
        return result

    async def _analyze_full_market(
        self,
        *,
        question: str,
        listings: list[dict[str, Any]],
        overview: dict[str, Any],
    ) -> dict[str, Any]:
        if not listings:
            return {
                "status": "ok",
                "answer": "В сохранённой базе нет карточек для анализа.",
                "listing_count": 0,
                "chunk_count": 0,
            }

        from src.brain.llm_router import get_llm_router

        router = get_llm_router()
        client = router.providers.get("openai")
        if client is None or not hasattr(client, "generate"):
            raise ValueError("OpenAI Responses provider is unavailable for full-market analysis")
        chunks = _chunk_market_listings(listings)
        semaphore = asyncio.Semaphore(_market_assistant_map_concurrency())

        async def generate_openai(*, reasoning_effort: str, **kwargs: Any) -> str:
            last_error: Exception | None = None
            task = str(kwargs.pop("task"))
            model = (
                _market_assistant_map_model()
                if task == "market_assistant_map"
                else _market_assistant_reduce_model()
            )
            for attempt in range(_market_assistant_map_attempts()):
                try:
                    logger.info(
                        f"Kwork full-market LLM: task={task} model={model} "
                        f"attempt={attempt + 1}/{_market_assistant_map_attempts()}"
                    )
                    return await client.generate(
                        model=model,
                        reasoning_effort=reasoning_effort,
                        request_timeout=float(_market_assistant_llm_timeout()),
                        **kwargs,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Kwork full-market LLM step failed "
                        f"(attempt {attempt + 1}/{_market_assistant_map_attempts()}): {type(exc).__name__}: {exc}"
                    )
                    if attempt + 1 < _market_assistant_map_attempts():
                        await asyncio.sleep(min(2 ** attempt, 4))
            raise ValueError(f"Full-market LLM step failed after retries: {last_error}")

        async def analyze_chunk(index: int, chunk: list[dict[str, Any]]) -> dict[str, Any]:
            cache_path = _market_assistant_chunk_cache_path(question, chunk)
            if cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cached = None
                if isinstance(cached, dict):
                    return cached

            evidence = {
                "chunk_index": index + 1,
                "chunk_count": len(chunks),
                "listing_count": len(chunk),
                "raw_listings": chunk,
            }
            async with semaphore:
                raw = await generate_openai(
                    prompt=(
                        f"Вопрос пользователя: {question}\n\n"
                        "Проанализируй каждую карточку в этом фрагменте и верни только JSON с полями "
                        "facts, patterns, evidence_listing_ids, caveats. Не делай выводов о данных вне фрагмента.\n\n"
                        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
                    ),
                    system_prompt=(
                        "Ты выполняешь map-этап полного анализа Kwork. Используй все переданные сырые карточки, "
                        "не пропускай записи и не подменяй наблюдения общими знаниями."
                    ),
                    temperature=0.1,
                    max_tokens=1000,
                    task="market_assistant_map",
                    reasoning_effort=_market_assistant_map_reasoning(),
                )
            try:
                parsed = json.loads(_strip_json_fence(raw))
            except json.JSONDecodeError:
                parsed = {"analysis": raw}
            if not isinstance(parsed, dict):
                parsed = {"analysis": str(parsed)}
            result = {
                "chunk_index": index + 1,
                "listing_count": len(chunk),
                "analysis": parsed,
            }
            temp_path = cache_path.with_name(f"{cache_path.stem}.{index}.tmp")
            temp_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(cache_path)
            return result

        analyses = await asyncio.gather(
            *(analyze_chunk(index, chunk) for index, chunk in enumerate(chunks))
        )
        synthesis_input = {
            "question": question,
            "overview": overview,
            "coverage": {
                "listing_count": len(listings),
                "chunk_count": len(chunks),
                "all_chunks_completed": len(analyses) == len(chunks),
            },
            "chunk_analyses": analyses,
        }
        synthesis = await generate_openai(
            prompt=(
                "Собери единый ответ на вопрос по результатам всех фрагментов. Верни только JSON вида "
                "{answer:string}. Не теряй противоречия и обязательно укажи границы покрытия.\n\n"
                + json.dumps(synthesis_input, ensure_ascii=False, separators=(",", ":"))
            ),
            system_prompt=(
                "Ты выполняешь reduce-этап полного анализа Kwork. Все фрагменты уже обработаны; "
                "синтезируй вывод только из них и из метрик обзора."
            ),
            temperature=0.15,
            max_tokens=2500,
            task="market_assistant_reduce",
            reasoning_effort=_market_assistant_reduce_reasoning(),
        )
        try:
            parsed_synthesis = json.loads(_strip_json_fence(synthesis))
        except json.JSONDecodeError:
            parsed_synthesis = {"answer": synthesis}
        answer = parsed_synthesis.get("answer") if isinstance(parsed_synthesis, dict) else synthesis
        return {
            "status": "ok",
            "answer": _clean_text(answer, 20_000),
            "listing_count": len(listings),
            "chunk_count": len(chunks),
            "all_chunks_completed": len(analyses) == len(chunks),
        }

    async def _handle_market_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        question: str,
        listings: list[dict[str, Any]],
        overview: dict[str, Any],
    ) -> Any:
        if name == "market_overview":
            return overview
        if name == "read_market_page":
            offset = max(0, _as_int(arguments.get("offset")))
            limit = max(1, min(_as_int(arguments.get("limit"), 1), 3))
            return {
                "offset": offset,
                "limit": limit,
                "total": len(listings),
                "next_offset": offset + limit if offset + limit < len(listings) else None,
                "raw_listings": listings[offset : offset + limit],
            }
        if name == "search_market_listings":
            query = _clean_text(arguments.get("query"), 300).casefold()
            limit = max(1, min(_as_int(arguments.get("limit"), 10), 20))
            matches = [listing for listing in listings if query and query in _stable_json(listing).casefold()]
            return {
                "query": query,
                "match_count": len(matches),
                "items": [_market_listing_reference(listing) for listing in matches[:limit]],
            }
        if name == "get_market_listing":
            listing_id = _clean_text(arguments.get("listing_id"), 300)
            for listing in listings:
                reference = _market_listing_reference(listing)
                if listing_id in {str(reference.get("id") or ""), _listing_key(listing)}:
                    return {"found": True, "raw_listing": listing}
            return {"found": False, "listing_id": listing_id}
        if name == "analyze_full_market":
            requested_question = _clean_text(arguments.get("question"), 8000) or question
            return await self._analyze_full_market(
                question=requested_question,
                listings=listings,
                overview=overview,
            )
        return {"error": f"Unknown market tool: {name}"}

    async def ask(self, context_id: str, message: str) -> dict[str, Any]:
        payload = MarketAssistantStore.load(context_id)
        snapshot = payload.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            raise ValueError("Market analysis context has no snapshot")
        question = _clean_text(message, 8000)
        if not question:
            raise ValueError("Question is required")
        history = [item for item in payload.get("messages") or [] if isinstance(item, dict)][-8:]
        recent_refreshes = [
            {
                "status": item.get("status"),
                "refreshed_at": item.get("refreshed_at"),
                "scope": item.get("scope"),
                "observed_listing_count": len(item.get("raw_listings") or []),
            }
            for item in [item for item in payload.get("refreshes") or [] if isinstance(item, dict)][-6:]
        ]
        allowed_slice_ids = [
            _as_int(item.get("id"))
            for item in snapshot.get("slices") or []
            if isinstance(item, dict) and _as_int(item.get("id")) > 0
        ]
        raw_listings = _complete_market_listings(snapshot, payload)
        context = {
            "scope": snapshot.get("scope"),
            "coverage": snapshot.get("coverage"),
            "slices": snapshot.get("slices"),
            "sample": snapshot.get("sample"),
            "analysis": snapshot.get("analysis"),
            "market_database": {
                "storage": "local",
                "listing_count": len(raw_listings),
                "access": "Responses function tools",
                "full_market_tool": "analyze_full_market",
            },
            "allowed_refresh_classifier_ids": allowed_slice_ids,
            "history": history,
            "recent_refreshes": recent_refreshes,
        }
        system_prompt = (
            "Ты Market Assistant для одной выбранной Kwork-рубрики. Отвечай только на основе "
            "контекста и не выдумывай популярные темы. Честно отмечай границы выборки. "
            "Если для ответа нужен свежий срез, верни JSON-поле refresh с classifier_id из "
            "allowed_refresh_classifier_ids и pages от 1 до 3. Иначе refresh=null. "
            "Верни только JSON: {answer:string, refresh:null|{classifier_id:number,pages:number,reason:string}}."
        )
        prompt = f"Вопрос пользователя: {question}\n\nКонтекст рынка:\n{json.dumps(context, ensure_ascii=False)}"
        overview = {
            "scope": snapshot.get("scope"),
            "coverage": snapshot.get("coverage"),
            "slices": snapshot.get("slices"),
            "sample": snapshot.get("sample"),
            "analysis": snapshot.get("analysis"),
            "listing_count": len(raw_listings),
        }
        system_prompt += (
            " Полная база находится локально и доступна через инструменты. Не проси вставить её в prompt. "
            "На приветствие или обычную реплику отвечай без инструментов. Для любого вывода о рынке целиком, "
            "нишах, спросе, предложении, конкуренции или позиционировании обязательно вызови analyze_full_market: "
            "он обработает каждую сохранённую карточку. Для точечного поиска используй search_market_listings и "
            "get_market_listing. Никогда не утверждай, что проанализировал всю базу, если analyze_full_market не вызван."
        )
        prompt = (
            f"Вопрос пользователя: {question}\n\n"
            "Лёгкий контекст и описание локальной базы:\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )

        async def tool_handler(name: str, arguments: dict[str, Any]) -> Any:
            return await self._handle_market_tool(
                name=name,
                arguments=arguments,
                question=question,
                listings=raw_listings,
                overview=overview,
            )

        try:
            result = await self._generate_with_tools(
                system_prompt=system_prompt,
                prompt=prompt,
                tool_handler=tool_handler,
            )
            if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
                raise ValueError("LLM response has no answer")
        except Exception as exc:
            result = {
                "answer": "Анализатор сейчас недоступен. Повторите вопрос после проверки LLM-провайдера.",
                "refresh": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        refresh = result.get("refresh")
        if isinstance(refresh, dict):
            classifier_id = _as_int(refresh.get("classifier_id"))
            pages = _as_int(refresh.get("pages"), 1)
            if classifier_id not in allowed_slice_ids or not 1 <= pages <= 3:
                result["refresh"] = None
                result["refresh_rejected"] = "Запрошенное обновление выходит за пределы выбранного скана рубрики."
            else:
                slice_info = next(
                    (
                        item
                        for item in snapshot.get("slices") or []
                        if isinstance(item, dict) and _as_int(item.get("id")) == classifier_id
                    ),
                    {},
                )
                scanner = KworkSupplyScanner()
                refresh_result: dict[str, Any]
                try:
                    refresh_result = await scanner.refresh_slice(
                        category_id=_as_int(snapshot.get("scope", {}).get("category_id")),
                        classifier_id=classifier_id,
                        classifier_name=_clean_text(slice_info.get("name"), 180),
                        pages=pages,
                    )
                except Exception as exc:
                    refresh_result = {
                        "status": "unavailable",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "raw_listings": [],
                    }
                finally:
                    await scanner.close()

                refresh_summary = {
                    "classifier_id": classifier_id,
                    "classifier_name": _clean_text(slice_info.get("name"), 180),
                    "pages": pages,
                    "reason": _clean_text(refresh.get("reason"), 500),
                    "status": refresh_result.get("status"),
                    "refreshed_at": refresh_result.get("refreshed_at"),
                    "observed_listing_count": len(refresh_result.get("raw_listings") or []),
                }
                result["refresh"] = refresh_summary
                payload.setdefault("refreshes", []).append(refresh_result)
                payload["refreshes"] = payload["refreshes"][-12:]

                if refresh_result.get("status") == "ok":
                    follow_up_listings = _complete_market_listings(snapshot, payload)
                    follow_up_overview = {**overview, "listing_count": len(follow_up_listings)}

                    async def follow_up_tool_handler(name: str, arguments: dict[str, Any]) -> Any:
                        return await self._handle_market_tool(
                            name=name,
                            arguments=arguments,
                            question=question,
                            listings=follow_up_listings,
                            overview=follow_up_overview,
                        )

                    follow_up_context = {
                        "question": question,
                        "existing_context": context,
                        "fresh_slice": refresh_summary,
                        "market_database": {"listing_count": len(follow_up_listings), "access": "tools"},
                    }
                    follow_up_prompt = (
                        "Ответь на вопрос пользователя с учетом свежего узкого среза. "
                        "Не запрашивай дополнительные данные и не выдавай наблюдения за весь рынок.\n\n"
                        + json.dumps(follow_up_context, ensure_ascii=False)
                    )
                    follow_up_prompt += (
                        "\n\nПолные свежие карточки доступны через инструменты. Для общего вывода вызови "
                        "analyze_full_market; для точечного ответа используй поиск и чтение карточки."
                    )
                    try:
                        follow_up = await self._generate_with_tools(
                            system_prompt=(
                                "Ты Market Assistant для одной Kwork-рубрики. Используй только "
                                "предоставленные данные; указывай границы выборки. Верни только JSON "
                                "вида {answer:string}."
                            ),
                            prompt=follow_up_prompt,
                            tool_handler=follow_up_tool_handler,
                        )
                        if isinstance(follow_up.get("answer"), str) and follow_up["answer"].strip():
                            result["answer"] = follow_up["answer"].strip()
                    except Exception as exc:
                        result["refresh_follow_up_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["refresh"] = None
        payload.setdefault("messages", []).extend(
            [
                {"role": "user", "content": question, "at": _utc_now()},
                {"role": "assistant", "content": result.get("answer"), "at": _utc_now(), "refresh": result.get("refresh")},
            ]
        )
        MarketAssistantStore.save(context_id, payload)
        return {"context_id": context_id, **result}
