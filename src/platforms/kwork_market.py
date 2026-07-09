"""Kwork market/category intelligence helpers.

This module intentionally uses Kwork JSON/API endpoints instead of browser
automation. Authenticated demand calls are optional and degrade gracefully when
Session Hub cookies are not available.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from src.utils.vpnte_proxy import kwork_http_proxy_url

KWORK_WEB_BASE_URL = "https://kwork.ru"
KWORK_CDN_BASE_URL = "https://cdn-edge.kwork.ru"
STATE_MARKER = "window.stateData="
ATTRIBUTE_FILTER_RE = re.compile(r"^(attribute\[\d+\](?:\[\])?)$")
CATALOG_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,120}$")
MARKET_METRICS_CACHE_TTL = 120.0
COMPETITOR_DETAIL_CACHE_TTL = 900.0
SELLER_DETAIL_CACHE_TTL = 900.0
_MARKET_METRICS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKET_METRICS_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_MARKET_CACHE_LOCK = asyncio.Lock()
_COMPETITOR_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SELLER_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
BUYER_REASONABLE_BUDGET_MAX = 150000
BUYER_DEFAULT_PRICE_TO = 5000

DEFAULT_MARKET_INTELLIGENCE_SEEDS: list[dict[str, Any]] = [
    {"name": "Marketplace design", "category_id": 286, "classifier_id": 1433413},
    {"name": "Links", "category_id": 59},
    {"name": "Ready databases", "category_id": 113, "classifier_id": 1117},
    {"name": "Logos", "category_id": 25, "classifier_id": 401928},
    {"name": "Marketplaces", "category_id": 112, "classifier_id": 1357},
    {"name": "Telegram", "category_id": 46, "classifier_id": 281},
    {"name": "AI logos/infographics", "category_id": 306, "classifier_id": 4200156},
    {"name": "Programming broad", "category_id": 41},
]
DEFAULT_MARKET_INTELLIGENCE_QUERIES = ["telegram", "ai", "seo", "python", "bot", "logo"]
DEFAULT_BUYER_SCOUT_PROBES: list[dict[str, Any]] = [
    {"name": "telegram_bot_low_offer", "categories": "all", "query": "С‚РµР»РµРіСЂР°Рј Р±РѕС‚", "kworks_filter_to": 5},
    {"name": "telegram_low_offer", "categories": "all", "query": "telegram", "kworks_filter_to": 5},
    {
        "name": "telegram_budget30_low_offer",
        "categories": "all",
        "query": "telegram",
        "price_from": 30000,
        "kworks_filter_to": 5,
    },
    {"name": "automation_low_offer", "categories": "all", "query": "Р°РІС‚РѕРјР°С‚РёР·Р°С†РёСЏ", "kworks_filter_to": 5},
    {"name": "wordpress_low_offer", "categories": "all", "query": "wordpress", "kworks_filter_to": 5},
    {"name": "programming_low_offer", "categories": "41", "query": "", "kworks_filter_to": 5},
    {"name": "website_maintenance_low_offer", "categories": "all", "query": "РґРѕСЂР°Р±РѕС‚РєР° СЃР°Р№С‚Р°", "kworks_filter_to": 10},
    {"name": "telegram_bot_zero_offer", "categories": "all", "query": "С‚РµР»РµРіСЂР°Рј Р±РѕС‚", "kworks_filter_to": 0},
    {"name": "site_zero_offer", "categories": "all", "query": "СЃР°Р№С‚", "kworks_filter_to": 0},
]
DEFAULT_BUYER_SCOUT_PROBES = [
    {"name": "telegram_low_offer", "categories": "all", "query": "telegram", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "telegram_zero_offer", "categories": "all", "query": "telegram", "kworks_filter_to": 0, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "site_zero_offer", "categories": "all", "query": "site", "kworks_filter_to": 0, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "python_low_offer", "categories": "all", "query": "python", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "ai_low_offer", "categories": "all", "query": "ai", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "chatgpt_low_offer", "categories": "all", "query": "chatgpt", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "wordpress_low_offer", "categories": "all", "query": "wordpress", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "seo_low_offer", "categories": "all", "query": "seo", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "design_low_offer", "categories": "all", "query": "design", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "landing_low_offer", "categories": "all", "query": "landing", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "automation_low_offer", "categories": "all", "query": "automation", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "programming_low_offer", "categories": "41", "query": "", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
]

DEFAULT_BUYER_SCOUT_PROBES = [
    {"name": "programming_low_offer", "categories": "41", "query": "", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "broad_low_offer", "categories": "85", "query": "", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "video_low_offer", "categories": "78", "query": "", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "category80_low_offer", "categories": "80", "query": "", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {
        "name": "telegram_bot_ru_low_offer",
        "categories": "all",
        "query": "\u0442\u0435\u043b\u0435\u0433\u0440\u0430\u043c \u0431\u043e\u0442",
        "kworks_filter_to": 5,
        "price_to": BUYER_DEFAULT_PRICE_TO,
    },
    {"name": "telegram_low_offer", "categories": "all", "query": "telegram", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "python_low_offer", "categories": "all", "query": "python", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "ai_low_offer", "categories": "all", "query": "ai", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {
        "name": "automation_ru_low_offer",
        "categories": "all",
        "query": "\u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0437\u0430\u0446\u0438\u044f",
        "kworks_filter_to": 5,
        "price_to": BUYER_DEFAULT_PRICE_TO,
    },
    {
        "name": "parser_ru_low_offer",
        "categories": "all",
        "query": "\u043f\u0430\u0440\u0441\u0435\u0440",
        "kworks_filter_to": 5,
        "price_to": BUYER_DEFAULT_PRICE_TO,
    },
    {"name": "wordpress_low_offer", "categories": "all", "query": "wordpress", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "landing_low_offer", "categories": "all", "query": "landing", "kworks_filter_to": 5, "price_to": BUYER_DEFAULT_PRICE_TO},
    {"name": "telegram_zero_offer", "categories": "all", "query": "telegram", "kworks_filter_to": 0, "price_to": BUYER_DEFAULT_PRICE_TO},
]

DEFAULT_WEB_CATALOG_ALIASES = [
    "programming",
    "design",
    "seo",
    "promotion",
    "links",
    "script-programming",
    "website-development",
    "smm",
    "logo",
    "traffic",
    "imagegeneration",
    "information-bases",
]

MARKET_TERM_STOPWORDS = {
    "РґР»СЏ",
    "РёР»Рё",
    "РїРѕРґ",
    "РїСЂРё",
    "РІР°С€",
    "РІР°С€Р°",
    "РІР°С€Рµ",
    "РІР°С€РµРіРѕ",
    "РІР°С€РµР№",
    "РІР°С€Рё",
    "СЃР°Р№С‚",
    "СЃР°Р№С‚Р°",
    "СЃР°Р№С‚РѕРІ",
    "СЃРґРµР»Р°СЋ",
    "СЃРѕР·РґР°Рј",
    "СЂР°Р·СЂР°Р±РѕС‚Р°СЋ",
    "РґРѕСЂР°Р±РѕС‚РєР°",
    "РЅР°СЃС‚СЂРѕР№РєР°",
    "РєРІРѕСЂРє",
    "kwork",
    "СЌС‚Рѕ",
    "РјРѕР№",
    "РІР°Рј",
    "РІР°СЃ",
    "С‡С‚Рѕ",
    "РєР°Рє",
    "Р±СѓРґРµС‚",
    "РјРѕР¶РЅРѕ",
}


def _seed_discovery_timeout() -> float:
    try:
        return max(1.0, float(os.getenv("KWORK_MARKET_SEED_DISCOVERY_TIMEOUT", "6") or "6"))
    except (TypeError, ValueError):
        return 6.0


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _market_api_timeout() -> float:
    try:
        return max(2.0, float(os.getenv("KWORK_MARKET_API_TIMEOUT", "8") or "8"))
    except (TypeError, ValueError):
        return 8.0


def _market_http_proxy_url(*, rotate: bool = False) -> str | None:
    if not _env_truthy("KWORK_MARKET_USE_PROXY", False):
        return None
    try:
        return kwork_http_proxy_url(rotate=rotate)
    except Exception as exc:
        if _env_truthy("KWORK_MARKET_PROXY_STRICT", False):
            raise
        logger.warning(f"KworkMarket: proxy unavailable, using direct market API: {exc}")
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace("\xa0", " ").replace(" ", "").replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else default
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"", "0", "false", "no", "none", "null", "off", "РЅРµС‚"}:
        return False
    if text in {"1", "true", "yes", "on", "РґР°"}:
        return True
    return bool(text)


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _cache_get(cache: dict[str, tuple[float, dict[str, Any]]], key: str, ttl: float) -> dict[str, Any] | None:
    item = cache.get(key)
    if not item:
        return None
    created, value = item
    if time.monotonic() - created > ttl:
        cache.pop(key, None)
        return None
    return copy.deepcopy(value)


def _cache_set(cache: dict[str, tuple[float, dict[str, Any]]], key: str, value: dict[str, Any]) -> None:
    cache[key] = (time.monotonic(), copy.deepcopy(value))


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _snapshot_filename_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _extract_catalog_seed_items(value: Any, *, path: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for index, child in enumerate(value):
            items.extend(_extract_catalog_seed_items(child, path=f"{path}[{index}]"))
        return items
    if not isinstance(value, dict):
        return items
    if value.get("category_id") or value.get("classifier_id"):
        items.append(
            {
                "name": value.get("name") or value.get("title") or path,
                "category_id": _as_int(value.get("category_id")),
                "classifier_id": _as_int(value.get("classifier_id")),
                "kworks_count": _as_int(value.get("kworks_count")),
                "source_path": path,
            }
        )
    for key, child in value.items():
        if isinstance(child, (dict, list)):
            child_path = f"{path}.{key}" if path else str(key)
            items.extend(_extract_catalog_seed_items(child, path=child_path))
    return items


def _extract_taxonomy_seed_items(
    value: Any,
    *,
    rubric_id: int = 0,
    parent_category_id: int = 0,
    path: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for index, child in enumerate(value):
            items.extend(
                _extract_taxonomy_seed_items(
                    child,
                    rubric_id=rubric_id,
                    parent_category_id=parent_category_id,
                    path=f"{path}[{index}]",
                )
            )
        return items
    if not isinstance(value, dict):
        return items

    category_id = _as_int(value.get("id") or value.get("category_id") or value.get("categoryId"))
    current_parent_id = _as_int(
        value.get("parent_id") or value.get("parentId") or value.get("parent_category_id"),
        default=parent_category_id,
    )
    if category_id:
        items.append(
            {
                "name": value.get("name") or value.get("title") or str(category_id),
                "category_id": category_id,
                "classifier_id": _as_int(value.get("classifier_id") or value.get("classifierId")) or None,
                "kworks_count": _as_int(value.get("kworks_count") or value.get("count")),
                "rubric_id": rubric_id or _as_int(value.get("rubric_id") or value.get("rubricId")),
                "parent_category_id": current_parent_id or None,
                "source_path": path,
            }
        )

    for key in ("children", "subcategories", "categories", "items"):
        child_value = value.get(key)
        if isinstance(child_value, (dict, list)):
            child_path = f"{path}.{key}" if path else key
            items.extend(
                _extract_taxonomy_seed_items(
                    child_value,
                    rubric_id=rubric_id,
                    parent_category_id=category_id or current_parent_id,
                    path=child_path,
                )
            )
    return items


def _normalize_market_seed(seed: dict[str, Any]) -> dict[str, Any] | None:
    category_id = _as_int(seed.get("category_id") or seed.get("categoryId"))
    classifier_id = _as_int(seed.get("classifier_id") or seed.get("classifierId"))
    if not category_id and not classifier_id:
        return None
    name = str(seed.get("name") or seed.get("title") or classifier_id or category_id)
    result: dict[str, Any] = {"name": name, "category_id": category_id or None}
    if classifier_id:
        result["classifier_id"] = classifier_id
    if seed.get("kworks_count") is not None:
        result["seed_kworks_count"] = _as_int(seed.get("kworks_count"))
    if seed.get("rubric_id") is not None:
        result["rubric_id"] = _as_int(seed.get("rubric_id"))
    if seed.get("parent_category_id") is not None:
        result["parent_category_id"] = _as_int(seed.get("parent_category_id"))
    if seed.get("source") is not None:
        result["source"] = seed.get("source")
    return result


def _compact_market_snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    aggregate = snapshot.get("aggregate") if isinstance(snapshot.get("aggregate"), dict) else {}
    config = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
    query_demand = snapshot.get("query_demand") if isinstance(snapshot.get("query_demand"), dict) else {}
    demand_counts = {
        str(query): _as_int(value.get("wants_count"))
        for query, value in query_demand.items()
        if isinstance(value, dict) and value.get("wants_count") is not None
    }
    return {
        "generated_at": snapshot.get("generated_at"),
        "file_path": snapshot.get("file_path"),
        "source": snapshot.get("source"),
        "config": {
            "max_seeds": config.get("max_seeds"),
            "pages": config.get("pages"),
            "include_demand": config.get("include_demand"),
            "include_seller_details": config.get("include_seller_details"),
        },
        "aggregate": {
            "seed_count": aggregate.get("seed_count"),
            "cards_seen": aggregate.get("cards_seen"),
            "unique_sellers_seen": aggregate.get("unique_sellers_seen"),
            "seller_profiles_collected": aggregate.get("seller_profiles_collected"),
        },
        "top_opportunities": _safe_list(aggregate.get("top_opportunities"))[:10],
        "top_sellers": _safe_list(aggregate.get("top_sellers"))[:20],
        "query_demand_counts": demand_counts,
        "timings_ms": snapshot.get("timings_ms"),
    }


def _market_max_fanout() -> int:
    try:
        return max(1, int(os.getenv("KWORK_MARKET_MAX_FANOUT", "2")))
    except (TypeError, ValueError):
        return 2


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _repair_text_encoding(text: str) -> str:
    bad_sequences = (
        "\u0420\u040e",
        "\u0420\u00a0",
        "\u0420\u0454",
        "\u0420\u00b0",
        "\u0420\u00b5",
        "\u0421\u0402",
        "\u0421\u201a",
        "\u0421\u0403",
        "\u0421\u0152",
        "\u0432\u0402",
        "\u0412\xa0",
        "\u0420\u0403",
        "\u0421\u2018",
        "\u00d0",
        "\u00d1",
        "\u00f0",
        "\u00f2",
        "\u00e5",
        "\u00eb",
        "\u00e8",
        "\u00e0",
        "\u00ea",
    )
    if not text:
        return text

    def _bad_score(value: str) -> int:
        high_latin = sum(1 for char in value if "\u00c0" <= char <= "\u00ff")
        return high_latin + sum(value.count(marker) * 4 for marker in bad_sequences)

    def _cyrillic_score(value: str) -> int:
        return sum(1 for char in value if "\u0400" <= char <= "\u04ff")

    current = text
    for _ in range(3):
        current_bad_score = _bad_score(current)
        current_cyrillic_score = _cyrillic_score(current)
        if current_bad_score <= 0:
            break
        best = current
        best_score = 0
        for source_encoding, target_encoding in (("latin1", "utf-8"), ("cp1251", "utf-8"), ("latin1", "cp1251")):
            try:
                candidate = current.encode(source_encoding).decode(target_encoding)
            except UnicodeError:
                continue
            candidate_bad_score = _bad_score(candidate)
            candidate_cyrillic_score = _cyrillic_score(candidate)
            if candidate_cyrillic_score <= 0:
                continue
            score = ((current_bad_score - candidate_bad_score) * 3) + (
                (candidate_cyrillic_score - current_cyrillic_score) * 2
            )
            if score > best_score and candidate != current:
                best = candidate
                best_score = score
        if best == current:
            break
        current = best
    return current


def _clean_text(value: Any, limit: int = 0) -> str:
    if isinstance(value, dict):
        for key in (
            "volume_service_in_kwork",
            "unit_and_quantity",
            "service_size",
            "description",
            "title",
            "name",
            "label",
            "text",
            "value",
        ):
            nested = value.get(key)
            if nested not in (None, "", [], {}):
                return _clean_text(nested, limit)
        return ""
    if isinstance(value, (list, tuple, set)):
        text = " ".join(_clean_text(item) for item in value if item not in (None, "", [], {}))
    else:
        text = str(value or "")
    text = _repair_text_encoding(text)
    if "volume_type_id" in text or "base_volume" in text or "volume_types" in text:
        match = re.search(r"""["']volume_service_in_kwork["']\s*:\s*["']([^"']*)["']""", text)
        if match:
            text = match.group(1)
        else:
            return ""
        if not text.strip():
            return ""
    text = _repair_text_encoding(unescape(text))
    if "<" in text or "&" in text:
        try:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = _repair_text_encoding(unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return int(ordered[middle])
    return int(round((ordered[middle - 1] + ordered[middle]) / 2))


def _market_terms(*values: Any, limit: int = 12) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for value in values:
        text = _clean_text(value, 2000).lower()
        text = re.sub(r"[^a-zР°-СЏС‘0-9\s-]+", " ", text)
        for token in text.split():
            if len(token) < 3 or token in MARKET_TERM_STOPWORDS:
                continue
            counter[token] += 1
    return [{"term": term, "count": count} for term, count in counter.most_common(limit)]


def build_market_insights(
    competitors: list[dict[str, Any]],
    *,
    kworks_count: int | None = None,
    classifiers: list[dict[str, Any]] | None = None,
    demand: dict[str, Any] | None = None,
    sample_limit: int = 10,
) -> dict[str, Any]:
    sample = [item for item in competitors[: max(1, sample_limit)] if isinstance(item, dict)]
    prices = [_as_int(item.get("price")) for item in sample]
    prices = [item for item in prices if item > 0]
    reviews = [_as_int(item.get("reviews")) for item in sample if item.get("reviews") not in (None, "")]
    seller_counts: Counter[str] = Counter(_clean_text(item.get("worker"), 80) or "unknown" for item in sample)
    repeated_sellers = [
        {"seller": seller, "cards": count}
        for seller, count in seller_counts.most_common(8)
        if seller != "unknown" and count > 1
    ]
    title_terms = _market_terms(*(item.get("title") for item in sample), limit=10)
    body_terms = _market_terms(
        *[item.get("description") for item in sample],
        *[item.get("service_size") for item in sample],
        limit=10,
    )
    top_classifiers = [
        {
            "id": item.get("id"),
            "name": _clean_text(item.get("name"), 120),
            "kworks_count": _as_int(item.get("kworks_count")),
        }
        for item in (classifiers or [])[:8]
        if isinstance(item, dict)
    ]
    price_summary = {
        "min": min(prices) if prices else None,
        "median": _median_int(prices),
        "max": max(prices) if prices else None,
        "sample_size": len(prices),
    }
    reviews_100_plus = sum(1 for item in reviews if item >= 100)
    trust_summary = {
        "reviews_100_plus": reviews_100_plus,
        "sample_size": len(reviews),
        "threshold": 100,
    }
    repeated_cards = sum(item["cards"] for item in repeated_sellers)
    concentration = {
        "repeated_sellers": repeated_sellers,
        "repeated_cards": repeated_cards,
        "sample_size": len(sample),
        "repeat_share": round(repeated_cards / len(sample), 2) if sample else 0,
    }
    bullets: list[str] = []
    if kworks_count is not None:
        bullets.append(f"Р’ РІС‹Р±СЂР°РЅРЅРѕРј СЃСЂРµР·Рµ РЅР°Р№РґРµРЅРѕ {kworks_count} РєРІРѕСЂРєРѕРІ.")
    if prices:
        bullets.append(
            f"Р¦РµРЅР° РІ С‚РѕРїРµ: РѕС‚ {min(prices)} в‚Ѕ РґРѕ {max(prices)} в‚Ѕ, РјРµРґРёР°РЅР° {price_summary['median']} в‚Ѕ."
        )
    if reviews:
        bullets.append(
            f"РџРѕСЂРѕРі РґРѕРІРµСЂРёСЏ РІС‹СЃРѕРєРёР№: {reviews_100_plus}/{len(reviews)} РєР°СЂС‚РѕС‡РµРє РІ РІС‹Р±РѕСЂРєРµ РёРјРµСЋС‚ 100+ РѕС‚Р·С‹РІРѕРІ."
        )
    if repeated_sellers:
        sellers_text = ", ".join(f"{item['seller']} ({item['cards']})" for item in repeated_sellers[:4])
        bullets.append(f"Р•СЃС‚СЊ РєРѕРЅС†РµРЅС‚СЂР°С†РёСЏ РІС‹РґР°С‡Рё: РїРѕРІС‚РѕСЂСЏСЋС‚СЃСЏ РїСЂРѕРґР°РІС†С‹ {sellers_text}.")
    if title_terms:
        bullets.append("Р§Р°СЃС‚С‹Рµ С‚РµРјС‹ РІ Р·Р°РіРѕР»РѕРІРєР°С…: " + ", ".join(item["term"] for item in title_terms[:6]) + ".")
    if body_terms:
        bullets.append("Р§Р°СЃС‚С‹Рµ С‚РµРјС‹ РІ РѕРїРёСЃР°РЅРёСЏС…: " + ", ".join(item["term"] for item in body_terms[:6]) + ".")
    if demand and demand.get("status") not in (None, "skipped"):
        if demand.get("status") == "ok":
            bullets.append(
                f"РЎРїСЂРѕСЃ РїРѕ Р·Р°РєР°Р·Р°Рј: {demand.get('wants_count', 0)} Р·Р°РєР°Р·РѕРІ, РїСЂРёРјРµСЂРѕРІ {demand.get('sample_count', 0)}."
            )
        elif demand.get("status") == "timeout":
            bullets.append("РЎРїСЂРѕСЃ РїРѕ Р·Р°РєР°Р·Р°Рј РЅРµ СѓСЃРїРµР» РѕС‚РІРµС‚РёС‚СЊ Р±С‹СЃС‚СЂРѕ; СЌРєСЂР°РЅ РїРѕРєР°Р·Р°Р» РєРѕРЅРєСѓСЂРµРЅС‚РѕРІ Р±РµР· РѕР¶РёРґР°РЅРёСЏ Р±РёСЂР¶Рё.")

    recommendations: list[str] = []
    if reviews and reviews_100_plus >= max(1, len(reviews) // 2):
        recommendations.append("РЈРїР°РєСѓР№ РґРѕРІРµСЂРёРµ: РєРµР№СЃС‹, РіР°СЂР°РЅС‚РёСЏ, РїРѕРЅСЏС‚РЅС‹Р№ РѕР±СЉС‘Рј СЂР°Р±РѕС‚ Рё СЃРёР»СЊРЅР°СЏ РѕР±Р»РѕР¶РєР° РІР°Р¶РЅРµРµ РѕР±С‰РµР№ С„СЂР°Р·С‹.")
    if price_summary["median"]:
        recommendations.append(f"Р‘Р°Р·РѕРІСѓСЋ С†РµРЅСѓ Р»СѓС‡С€Рµ РґРµСЂР¶Р°С‚СЊ РѕРєРѕР»Рѕ РјРµРґРёР°РЅС‹ СЃСЂРµР·Р°: РїСЂРёРјРµСЂРЅРѕ {price_summary['median']} в‚Ѕ.")
    if title_terms:
        recommendations.append(
            "Р’ Р·Р°РіРѕР»РѕРІРєРµ СЃС‚РѕРёС‚ СЏРІРЅРѕ РЅР°Р·РІР°С‚СЊ С‚РµС…РЅРѕР»РѕРіРёСЋ/С‚РёРї СѓСЃР»СѓРіРё: " + ", ".join(item["term"] for item in title_terms[:4]) + "."
        )
    if repeated_sellers:
        recommendations.append("РќРµ РєРѕРїРёСЂСѓР№ С‚РѕРї С†РµР»РёРєРѕРј: РїРѕРІС‚РѕСЂСЏСЋС‰РёРµСЃСЏ РїСЂРѕРґР°РІС†С‹ Р·Р°РЅРёРјР°СЋС‚ РјРµСЃС‚Р° Р·Р° СЃС‡С‘С‚ РґРѕРІРµСЂРёСЏ, РёС‰Рё Р±РѕР»РµРµ СѓР·РєРёР№ СЃСЂРµР·.")
    if top_classifiers:
        narrow = [item for item in top_classifiers if item.get("kworks_count") and item["kworks_count"] < (kworks_count or 0)]
        if narrow:
            recommendations.append(
                "РџСЂРѕРІРµСЂСЊ Р±РѕР»РµРµ СѓР·РєРёРµ СЃСЂРµР·С‹: "
                + ", ".join(f"{item['name']} ({item['kworks_count']})" for item in narrow[:3])
                + "."
            )

    return {
        "sample_size": len(sample),
        "kworks_count": kworks_count,
        "price": price_summary,
        "trust": trust_summary,
        "concentration": concentration,
        "title_terms": title_terms,
        "description_terms": body_terms,
        "top_classifiers": top_classifiers,
        "bullets": bullets,
        "recommendations": recommendations,
    }


def _value_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def normalize_attribute_filters(selection: dict[str, Any] | None) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for key, value in (selection or {}).items():
        name = str(key or "")
        if not ATTRIBUTE_FILTER_RE.match(name):
            continue
        values: list[str] = []
        for item in _value_list(value):
            if item in (None, ""):
                continue
            item_text = str(item).strip()
            if item_text:
                values.append(item_text)
        if not values:
            continue
        filters[name] = values if name.endswith("[]") or len(values) > 1 else values[0]
    return filters


def describe_attribute_filter_scope(
    selection: dict[str, Any] | None,
    *,
    controls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    filters = normalize_attribute_filters(selection)
    by_name = {str(control.get("name")): control for control in controls or [] if isinstance(control, dict)}
    selected: list[dict[str, Any]] = []
    for name, value in filters.items():
        values = _value_list(value)
        control = by_name.get(name) or {}
        options = control.get("options") if isinstance(control.get("options"), list) else []
        labels: list[str] = []
        for raw in values:
            option = next(
                (
                    item
                    for item in options
                    if isinstance(item, dict) and str(item.get("id") or item.get("value") or "") == str(raw)
                ),
                None,
            )
            labels.append(str(option.get("label") or raw) if option else str(raw))
        selected.append(
            {
                "name": name,
                "question": control.get("question") or control.get("label") or "",
                "values": values,
                "labels": labels,
            }
        )
    return {"params": filters, "selected": selected, "count": len(selected)}


def selected_classifier_ids_from_attributes(
    selection: dict[str, Any] | None,
    *,
    controls: list[dict[str, Any]] | None = None,
    fallback_classifier_id: int | None = None,
) -> list[int]:
    groups: list[list[int]] = []
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        name = str(control.get("name") or "")
        if name not in (selection or {}):
            continue
        options = control.get("options") if isinstance(control.get("options"), list) else []
        option_ids = {_as_int(option.get("id") or option.get("value")) for option in options if isinstance(option, dict)}
        option_ids.discard(0)
        if not option_ids:
            continue
        ids: list[int] = []
        for raw in _value_list((selection or {}).get(name)):
            option_id = _as_int(raw)
            if option_id and option_id in option_ids and option_id not in ids:
                ids.append(option_id)
        if ids:
            groups.append(ids)
    if groups:
        return groups[-1]

    fallback_groups: list[list[int]] = []
    for value in normalize_attribute_filters(selection).values():
        ids = [_as_int(item) for item in _value_list(value)]
        clean = [item for item in ids if item]
        if clean:
            fallback_groups.append(clean)
    if fallback_groups:
        return fallback_groups[-1]
    return [int(fallback_classifier_id)] if fallback_classifier_id else []


def _absolute_kwork_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{KWORK_WEB_BASE_URL}{url}"
    return f"{KWORK_WEB_BASE_URL}/{url.lstrip('/')}"


def _extract_kwork_id(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return _as_int(text)
    match = re.search(r"(?:^|[/_-])(\d{3,})(?:[/_-]|$)", text)
    return _as_int(match.group(1)) if match else 0


def _clean_catalog_alias(value: Any) -> str:
    alias = str(value or "").strip().strip("/")
    alias = alias.split("?", 1)[0].split("#", 1)[0]
    if not CATALOG_ALIAS_RE.match(alias):
        raise ValueError("invalid Kwork catalog alias")
    return alias


def _cdn_photo_url(value: Any) -> str:
    photo = str(value or "").strip()
    if not photo:
        return ""
    if photo.startswith("http://") or photo.startswith("https://"):
        return photo
    return f"{KWORK_CDN_BASE_URL}/pics/t3/{photo.lstrip('/')}"


def _extract_json_object_after_marker(html: str, marker: str = STATE_MARKER) -> str | None:
    start = html.find(marker)
    if start < 0:
        return None
    index = html.find("{", start + len(marker))
    if index < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    quote = ""
    for pos in range(index, len(html)):
        char = html[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[index : pos + 1]
    return None


def _extract_state_data(html: str) -> dict[str, Any]:
    raw = _extract_json_object_after_marker(html)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _meta_content(html: str, key: str) -> str:
    pattern = (
        r"<meta[^>]+(?:name|property)=[\"']"
        + re.escape(key)
        + r"[\"'][^>]+content=[\"']([^\"']+)[\"'][^>]*>"
    )
    match = re.search(pattern, html, flags=re.I)
    if match:
        return _clean_text(match.group(1))
    pattern = (
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:name|property)=[\"']"
        + re.escape(key)
        + r"[\"'][^>]*>"
    )
    match = re.search(pattern, html, flags=re.I)
    return _clean_text(match.group(1)) if match else ""


class KworkMarketClient:
    """Read Kwork category, classifier and competition data through API calls."""

    def __init__(self, api: Any | None = None) -> None:
        self._api = api
        self._owns_api = api is None

    async def _get_api(self) -> Any:
        if self._api is None:
            from kwork import Kwork

            self._api = Kwork(
                login="",
                password="",
                timeout=_market_api_timeout(),
                retry_max_attempts=1,
                proxy=_market_http_proxy_url(rotate=False),
            )
        return self._api

    async def close(self) -> None:
        if self._owns_api and self._api is not None:
            close = getattr(self._api, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass
            self._api = None

    async def request(self, endpoint: str, **params: Any) -> dict[str, Any]:
        api = await self._get_api()
        request_params = dict(params)
        if getattr(api, "_psr_pacing_patched", False) or getattr(api.__class__, "_psr_pacing_patched", False):
            request_params["_psr_skip_pacing"] = True
        data = await api.request("post", endpoint, **request_params)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def normalize_category(item: dict[str, Any]) -> dict[str, Any]:
        children: list[dict[str, Any]] = []
        for key in ("subcategories", "categories", "childs", "children"):
            for child in _safe_list(item.get(key)):
                if isinstance(child, dict):
                    children.append(KworkMarketClient.normalize_category(child))

        return {
            "id": _as_int(item.get("id")),
            "name": item.get("name") or item.get("title") or item.get("seo") or str(item.get("id") or ""),
            "parent_id": item.get("parent_id") or item.get("parentId"),
            "alias": item.get("alias") or item.get("seo"),
            "kworks_count": _as_int(item.get("kworks_count") or item.get("kworksCount")),
            "raw": item,
            "children": children,
        }

    async def get_categories_tree(self) -> dict[str, Any]:
        data = await self.request("categories")
        response = data.get("response")
        categories = [self.normalize_category(item) for item in _safe_list(response) if isinstance(item, dict)]
        return {"categories": categories, "raw": data}

    async def get_category_attributes(self, category_id: int) -> dict[str, Any]:
        data = await self.request("categoryAttributes", category_id=category_id)
        response = data.get("response")
        attributes = _safe_list(response)
        flat = self.flatten_attributes(attributes)
        return {"category_id": category_id, "attributes": attributes, "flat": flat, "raw": data}

    @classmethod
    def flatten_attributes(
        cls,
        attributes: list[Any],
        *,
        parent_path: tuple[str, ...] = (),
        parent_ids: tuple[int, ...] = (),
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in attributes:
            if not isinstance(item, dict):
                continue
            attr_id = _as_int(item.get("id") or item.get("attribute_id"))
            name = str(item.get("name") or item.get("title") or attr_id)
            path = (*parent_path, name)
            ids = (*parent_ids, attr_id) if attr_id else parent_ids
            children = _safe_list(item.get("children") or item.get("childs") or item.get("values"))
            result.append(
                {
                    "id": attr_id,
                    "name": name,
                    "path": " > ".join(path),
                    "path_ids": list(ids),
                    "required": bool(item.get("required")),
                    "allow_multiple": bool(item.get("allow_multiple")),
                    "allow_custom": bool(item.get("allow_custom")),
                    "percent_usage": item.get("percent_usage"),
                    "kworks_count": _as_int(item.get("kworks_count")),
                    "orders_inprogress_limit": item.get("orders_inprogress_limit"),
                    "has_children": bool(children),
                    "raw": item,
                }
            )
            result.extend(cls.flatten_attributes(children, parent_path=path, parent_ids=ids))
        return result

    async def get_price_rules(self, category_id: int, attribute_id: int | None = None) -> dict[str, Any]:
        endpoint = "attributegetprices" if attribute_id else "categorygetprices"
        params: dict[str, Any] = {"categoryId": category_id, "lang": "ru"}
        if attribute_id:
            params["attributeId"] = attribute_id

        async with httpx.AsyncClient(
            base_url=f"{KWORK_WEB_BASE_URL}/api/freeprice",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{KWORK_WEB_BASE_URL}/new",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkMarket/1.0",
            },
            timeout=_market_api_timeout(),
            follow_redirects=True,
            proxy=_market_http_proxy_url(rotate=False),
            trust_env=False,
        ) as client:
            response = await client.get(f"/{endpoint}", params=params)
            response.raise_for_status()
            return response.json() if response.content else {}

    @staticmethod
    def summarize_price_rules(data: dict[str, Any]) -> dict[str, Any]:
        response = data.get("response")
        root = response if isinstance(response, dict) else data if isinstance(data, dict) else {}
        prices = root.get("prices") if isinstance(root.get("prices"), dict) else root
        if not isinstance(prices, dict):
            prices = {}
        gradation = (
            prices.get("typicalPriceGradation")
            or prices.get("typical_price_gradation")
            or prices.get("priceGradation")
            or prices.get("gradations")
        )
        return {
            "status": "ok" if prices else "empty",
            "min_price": _as_int(prices.get("minPrice") or prices.get("min_price")),
            "max_price": _as_int(prices.get("maxPrice") or prices.get("max_price")),
            "typical_price": _as_int(prices.get("typicalPrice") or prices.get("typical_price")),
            "typical_price_gradation": gradation if isinstance(gradation, list) else [],
            "raw_keys": sorted(root.keys()),
        }

    @staticmethod
    def summarize_web_catalog_state(alias: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        state = data.get("stateData") if isinstance(data.get("stateData"), dict) else {}
        view = state.get("viewData") if isinstance(state.get("viewData"), dict) else {}
        filters = view.get("filters") if isinstance(view.get("filters"), dict) else {}
        kworks = view.get("kworks")
        active_cat = filters.get("activeCat") if isinstance(filters.get("activeCat"), dict) else {}

        kworks_summary: dict[str, Any] = {"type": type(kworks).__name__}
        if isinstance(kworks, list):
            kworks_summary["count"] = len(kworks)
            if kworks and isinstance(kworks[0], dict):
                kworks_summary["first_keys"] = sorted(kworks[0].keys())[:40]
                kworks_summary["first_id"] = kworks[0].get("id")
                kworks_summary["first_title"] = _clean_text(kworks[0].get("title"), 180)
        elif isinstance(kworks, dict):
            kworks_summary["keys"] = sorted(kworks.keys())[:60]
            list_counts: dict[str, int] = {}
            dict_keys: dict[str, list[str]] = {}
            scalar_values: dict[str, Any] = {}
            for key, value in kworks.items():
                if isinstance(value, list):
                    list_counts[key] = len(value)
                elif isinstance(value, dict):
                    dict_keys[key] = sorted(value.keys())[:30]
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    scalar_values[key] = _clean_text(value, 180) if isinstance(value, str) else value
            if list_counts:
                kworks_summary["list_counts"] = list_counts
            if dict_keys:
                kworks_summary["dict_keys"] = dict_keys
            if scalar_values:
                kworks_summary["scalar_values"] = scalar_values

        return {
            "alias": alias,
            "success": bool(payload.get("success")),
            "state_keys": sorted(state.keys())[:40],
            "view_keys": sorted(view.keys())[:40],
            "filters": {
                "keys": sorted(filters.keys())[:80],
                "active_category_id": _as_int(filters.get("activeCategoryId") or filters.get("categoryId")),
                "kworks_count": _as_int(filters.get("kworksCount")),
                "selected_attribute_ids": filters.get("selectedAttributesIds"),
                "selected_subattribute_ids": filters.get("selectedSubattributesIds"),
                "price_filter_bounds": filters.get("priceFilterBounds"),
                "price_limits": filters.get("priceLimits"),
                "active_category": {
                    key: active_cat.get(key)
                    for key in ("id", "name", "alias", "parent_id", "parentId", "kworks_count")
                    if key in active_cat
                },
            },
            "kworks": kworks_summary,
        }

    async def get_web_catalog_filters(
        self,
        alias: str,
        *,
        page: int = 1,
        page_size: int = 10,
        filters: dict[str, Any] | None = None,
        include_raw: bool = False,
        cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch Kwork's same-origin web catalog state for one canonical category alias."""
        clean_alias = _clean_catalog_alias(alias)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 10), 50))
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        for key, value in (filters or {}).items():
            if value is not None and value != "":
                params[str(key)] = value

        endpoint = f"/catalog_kworks_filters/{clean_alias}"
        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Referer": f"{KWORK_WEB_BASE_URL}/categories/{clean_alias}",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkMarket/1.0",
            },
            timeout=_market_api_timeout(),
            follow_redirects=True,
            cookies=cookies or None,
            proxy=_market_http_proxy_url(rotate=False),
            trust_env=False,
        ) as client:
            response = await client.post(endpoint, data=params)

        content_type = response.headers.get("content-type", "")
        result: dict[str, Any] = {
            "alias": clean_alias,
            "endpoint": endpoint,
            "url": f"{KWORK_WEB_BASE_URL}{endpoint}",
            "request_params": params,
            "status_code": response.status_code,
            "content_type": content_type,
            "bytes": len(response.content),
            "protection_status": "blocked" if response.status_code == 403 else "ok",
        }
        if response.status_code != 200:
            result["success"] = False
            result["protection_status"] = "blocked" if response.status_code == 403 else "http_error"
            result["body_prefix"] = _clean_text(response.text, 240)
            return result

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            result.update(
                {
                    "success": False,
                    "protection_status": "parse_error",
                    "body_prefix": _clean_text(response.text, 240),
                }
            )
            return result

        payload = payload if isinstance(payload, dict) else {}
        result.update(self.summarize_web_catalog_state(clean_alias, payload))
        if not result.get("success") and result.get("protection_status") == "ok":
            result["protection_status"] = "empty_or_invalid_alias"
        if include_raw:
            result["raw"] = payload
        return result

    async def get_web_catalog_alias_snapshot(
        self,
        aliases: list[str] | None = None,
        *,
        page: int = 1,
        page_size: int = 10,
        delay_seconds: float = 2.0,
        include_raw: bool = False,
        cookies: dict[str, str] | None = None,
        write_file: bool = False,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Collect a throttled browser-catalog snapshot for selected canonical aliases."""
        started = time.monotonic()
        selected = aliases or DEFAULT_WEB_CATALOG_ALIASES
        clean_aliases: list[str] = []
        seen: set[str] = set()
        errors: list[dict[str, Any]] = []
        for alias in selected:
            try:
                clean = _clean_catalog_alias(alias)
            except ValueError as exc:
                errors.append({"alias": str(alias), "status": "invalid_alias", "detail": str(exc)})
                continue
            if clean not in seen:
                seen.add(clean)
                clean_aliases.append(clean)
            if len(clean_aliases) >= 30:
                break

        delay_seconds = max(0.0, min(float(delay_seconds or 0), 20.0))
        results: list[dict[str, Any]] = []
        for index, alias in enumerate(clean_aliases):
            if index and delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                results.append(
                    await self.get_web_catalog_filters(
                        alias,
                        page=page,
                        page_size=page_size,
                        include_raw=include_raw,
                        cookies=cookies,
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "alias": alias,
                        "success": False,
                        "protection_status": "exception",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )

        status_counts: dict[str, int] = {}
        for item in results:
            status = str(item.get("protection_status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        snapshot: dict[str, Any] = {
            "generated_at": _utc_timestamp(),
            "source": "psr.kwork_web_catalog_alias_snapshot",
            "config": {
                "aliases": clean_aliases,
                "page": max(1, int(page or 1)),
                "page_size": max(1, min(int(page_size or 10), 50)),
                "delay_seconds": delay_seconds,
                "include_raw": include_raw,
                "cookie_count": len(cookies or {}),
            },
            "results": results,
            "errors": errors,
            "aggregate": {
                "alias_count": len(clean_aliases),
                "ok_count": status_counts.get("ok", 0),
                "blocked_count": status_counts.get("blocked", 0),
                "empty_or_invalid_alias_count": status_counts.get("empty_or_invalid_alias", 0),
                "status_counts": sorted(status_counts.items(), key=lambda item: item[0]),
            },
            "timings_ms": {"total": int((time.monotonic() - started) * 1000)},
        }

        if write_file:
            root = Path(output_dir) if output_dir else Path("docs") / "kwork_web_catalog_snapshots"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"kwork_web_catalog_alias_snapshot_{_snapshot_filename_timestamp()}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            snapshot["file_path"] = str(path)
        return snapshot

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
        attribute_filters: dict[str, Any] | None = None,
        attribute_controls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        effective_classifier_ids = selected_classifier_ids_from_attributes(
            attribute_filters,
            controls=attribute_controls,
            fallback_classifier_id=classifier_id,
        )
        effective_classifier_ids = effective_classifier_ids[: _market_max_fanout()]

        normalized_filters = normalize_attribute_filters(attribute_filters)
        requests: list[dict[str, Any]] = []
        if effective_classifier_ids:
            requests = [{"page": page, "classifierId": item, **normalized_filters} for item in effective_classifier_ids]
        elif category_id:
            requests = [{"page": page, "categoryId": category_id, **normalized_filters}]
        else:
            raise ValueError("category_id or classifier_id is required")

        catalogs: list[dict[str, Any]] = []
        for params in requests:
            data = await self.request("kworks", **params)
            response = data.get("response")
            if isinstance(response, dict):
                response["_request_params"] = params
                catalogs.append(response)
            elif isinstance(response, list):
                catalogs.append({"kworks": response, "kworks_count": len(response), "_request_params": params})
        if len(catalogs) == 1:
            catalogs[0]["_filter_requests"] = requests
            return catalogs[0]
        return self.merge_kwork_catalogs(catalogs, requests=requests)

    @staticmethod
    def merge_kwork_catalogs(catalogs: list[dict[str, Any]], *, requests: list[dict[str, Any]]) -> dict[str, Any]:
        merged_kworks: list[Any] = []
        seen_kworks: set[str] = set()
        merged_classifiers: dict[int, dict[str, Any]] = {}
        total_count = 0
        raw_keys: set[str] = set()
        for catalog in catalogs:
            raw_keys.update(catalog.keys())
            total_count += _as_int(catalog.get("kworks_count") or catalog.get("count"))
            for classifier in _safe_list(catalog.get("classifiers")):
                if isinstance(classifier, dict):
                    classifier_id = _as_int(classifier.get("id"))
                    if classifier_id and classifier_id not in merged_classifiers:
                        merged_classifiers[classifier_id] = classifier
            for item in _safe_list(catalog.get("kworks")):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or item.get("PID") or item.get("share_url") or item.get("url") or len(merged_kworks))
                if key in seen_kworks:
                    continue
                seen_kworks.add(key)
                merged_kworks.append(item)
        return {
            "kworks": merged_kworks,
            "kworks_count": total_count or len(merged_kworks),
            "classifiers": list(merged_classifiers.values()),
            "_filter_requests": requests,
            "_merged_catalogs": len(catalogs),
            "_raw_keys": sorted(raw_keys),
        }

    async def get_kworks_legacy(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page}
        if classifier_id:
            params["classifierId"] = classifier_id
        elif category_id:
            params["categoryId"] = category_id
        else:
            raise ValueError("category_id or classifier_id is required")
        data = await self.request("kworks", **params)
        response = data.get("response")
        if isinstance(response, dict):
            return response
        if isinstance(response, list):
            return {"kworks": response, "kworks_count": len(response)}
        return {}

    @staticmethod
    def summarize_competitors(kworks: list[Any], limit: int = 12) -> list[dict[str, Any]]:
        competitors: list[dict[str, Any]] = []
        for item in kworks[:limit]:
            if not isinstance(item, dict):
                continue
            worker = item.get("worker") if isinstance(item.get("worker"), dict) else {}
            cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
            share_url = _absolute_kwork_url(item.get("share_url") or item.get("url"))
            summary_description = _clean_text(
                item.get("description")
                or item.get("short_description")
                or item.get("shortDescription")
                or item.get("preview_description")
                or item.get("gdesc"),
                520,
            )
            summary_service_size = _clean_text(
                item.get("service_size") or item.get("unit_and_quantity") or item.get("volume") or item.get("gwork"),
                260,
            )
            competitors.append(
                {
                    "id": item.get("id") or item.get("PID"),
                    "title": item.get("title") or item.get("name"),
                    "price": item.get("price") or item.get("priceWithCurrency"),
                    "classifier_id": item.get("classifier_id") or item.get("classifierId"),
                    "image_url": item.get("image_url") or cover.get("tablet") or cover.get("phone") or item.get("photo"),
                    "share_url": share_url,
                    "worker": worker.get("username") or item.get("username"),
                    "worker_avatar": worker.get("profilepicture"),
                    "seller_level": worker.get("level_description"),
                    "rating": worker.get("rating") or item.get("rating"),
                    "reviews": worker.get("reviews_count") or item.get("reviews_count"),
                    "is_best": bool(item.get("is_best")),
                    "description": summary_description,
                    "instruction": "",
                    "service_size": summary_service_size,
                    "practice_context": summary_description,
                    "detail_status": "summary" if summary_description else "pending" if share_url else "missing_url",
                    "raw": item,
                }
            )
        return competitors

    async def enrich_competitors(
        self,
        competitors: list[dict[str, Any]],
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        targets = [item for item in competitors[: max(0, limit)] if item.get("id") or item.get("share_url")]
        if not targets:
            return competitors

        results: list[dict[str, Any] | Exception] = []
        detail_delay = _float_env("KWORK_COMPETITOR_DETAIL_DELAY", 0.15)
        started = time.monotonic()
        cache_hits = 0
        for index, item in enumerate(targets):
            cache_key = str(item.get("id") or item.get("share_url") or "")
            cached = _cache_get(_COMPETITOR_DETAIL_CACHE, cache_key, COMPETITOR_DETAIL_CACHE_TTL)
            if cached is not None:
                cache_hits += 1
                results.append(cached)
                continue
            try:
                result = await self.fetch_competitor_detail(None, item)
                _cache_set(_COMPETITOR_DETAIL_CACHE, cache_key, result)
                results.append(result)
            except Exception as exc:
                results.append(exc)
            if detail_delay > 0 and index < len(targets) - 1:
                await asyncio.sleep(detail_delay)

        errors = sum(1 for item in results if isinstance(item, Exception))
        logger.info(
            "KworkMarket: competitor API details targets={} cache_hits={} errors={} delay_s={} elapsed_ms={}",
            len(targets),
            cache_hits,
            errors,
            detail_delay,
            int((time.monotonic() - started) * 1000),
        )

        by_id: dict[Any, dict[str, Any]] = {}
        for item, result in zip(targets, results, strict=False):
            if isinstance(result, Exception):
                by_id[item.get("id")] = {
                    "detail_status": "error",
                    "detail_error": f"{type(result).__name__}: {result}",
                }
            else:
                by_id[item.get("id")] = result

        enriched: list[dict[str, Any]] = []
        for item in competitors:
            extra = by_id.get(item.get("id"))
            enriched.append({**item, **extra} if extra else item)
        return enriched

    @staticmethod
    def _response_dict(data: dict[str, Any]) -> dict[str, Any]:
        response = data.get("response")
        if isinstance(response, dict):
            return response
        return data if isinstance(data, dict) else {}

    @staticmethod
    def summarize_seller_profile(username: str, profile: dict[str, Any]) -> dict[str, Any]:
        user = profile.get("user") if isinstance(profile.get("user"), dict) else profile
        stats = profile.get("stats") if isinstance(profile.get("stats"), dict) else {}
        portfolio = _safe_list(profile.get("portfolio_list") or profile.get("portfolio"))
        skills = _safe_list(profile.get("skills"))
        reviews = profile.get("reviews") if isinstance(profile.get("reviews"), dict) else {}
        return {
            "id": user.get("id") or profile.get("user_id") or profile.get("id"),
            "username": user.get("username") or profile.get("username") or username,
            "display_name": user.get("fullname") or user.get("name") or profile.get("fullname") or profile.get("name"),
            "level": user.get("level_description") or user.get("level") or profile.get("level_description"),
            "rating": user.get("rating") or profile.get("rating") or stats.get("rating"),
            "reviews_count": (
                user.get("reviews_count")
                or profile.get("reviews_count")
                or stats.get("reviews_count")
                or reviews.get("count")
            ),
            "completed_orders": stats.get("completed_orders") or profile.get("completed_orders") or user.get("orders_done"),
            "active_kworks_count": stats.get("active_kworks_count") or profile.get("active_kworks_count"),
            "portfolio_count": len(portfolio),
            "skills": [
                item.get("name") if isinstance(item, dict) else str(item)
                for item in skills[:12]
                if item
            ],
            "raw_keys": sorted(profile.keys()),
        }

    def summarize_seller_categories(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        response = data.get("response")
        items = response.get("categories") if isinstance(response, dict) else response
        result: list[dict[str, Any]] = []
        for item in _safe_list(items):
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "id": _as_int(item.get("id") or item.get("category_id") or item.get("categoryId")),
                    "name": item.get("name") or item.get("title") or item.get("category_name"),
                    "kworks_count": _as_int(item.get("kworks_count") or item.get("count")),
                    "raw": item,
                }
            )
        return result

    @staticmethod
    def _first_response_list(data: dict[str, Any], *keys: str) -> list[Any]:
        response = data.get("response")
        if isinstance(response, list):
            return response
        containers = [response, data]
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in keys:
                value = container.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _response_paging(data: dict[str, Any]) -> dict[str, Any]:
        response = data.get("response")
        paging = response.get("paging") if isinstance(response, dict) else None
        if not isinstance(paging, dict):
            paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
        return paging

    @staticmethod
    def summarize_seller_portfolio(data: dict[str, Any], limit: int = 6) -> dict[str, Any]:
        items = KworkMarketClient._first_response_list(data, "portfolio", "portfolio_list", "items", "data")
        paging = KworkMarketClient._response_paging(data)
        summary_items: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            summary_items.append(
                {
                    "id": item.get("id"),
                    "title": _clean_text(item.get("title") or item.get("name"), 220),
                    "category_id": _as_int(item.get("category_id") or item.get("categoryId")),
                    "category_name": item.get("category_name") or item.get("category"),
                    "type": item.get("type"),
                    "views": _as_int(item.get("views")),
                    "views_dirty": _as_int(item.get("views_dirty")),
                    "comments_count": _as_int(item.get("comments_count")),
                    "media_counts": {
                        "images": len(_safe_list(item.get("images"))),
                        "videos": len(_safe_list(item.get("videos"))),
                        "audios": len(_safe_list(item.get("audios"))),
                        "pdf": len(_safe_list(item.get("pdf"))),
                    },
                    "thumbnail_url": item.get("photo") or item.get("thumbnail") or item.get("image"),
                    "raw_keys": sorted(item.keys()),
                }
            )
        total = _as_int(paging.get("total"), default=len(items))
        return {
            "total": total,
            "pages": _as_int(paging.get("pages")),
            "sample_count": len(summary_items),
            "items": summary_items,
        }

    @staticmethod
    def summarize_seller_reviews(data: dict[str, Any], limit: int = 6) -> dict[str, Any]:
        items = KworkMarketClient._first_response_list(data, "reviews", "items", "data")
        paging = KworkMarketClient._response_paging(data)
        summary_items: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            kwork = item.get("kwork") if isinstance(item.get("kwork"), dict) else {}
            summary_items.append(
                {
                    "id": item.get("id") or item.get("review_display_id"),
                    "review_display_id": item.get("review_display_id"),
                    "time_added": item.get("time_added") or item.get("date_create"),
                    "text": _clean_text(item.get("text") or item.get("comment"), 420),
                    "good": _as_int(item.get("good")),
                    "bad": _as_int(item.get("bad")),
                    "auto_mode": item.get("auto_mode"),
                    "kwork_id": kwork.get("id") or item.get("kwork_id"),
                    "kwork_title": _clean_text(kwork.get("title") or item.get("kwork_title"), 220),
                    "has_answer": bool(item.get("answer")),
                    "raw_keys": sorted(item.keys()),
                }
            )
        total = _as_int(paging.get("total"), default=len(items))
        return {
            "total": total,
            "pages": _as_int(paging.get("pages")),
            "sample_count": len(summary_items),
            "items": summary_items,
        }

    @staticmethod
    def summarize_competitor_extra(data: dict[str, Any], limit: int = 4) -> dict[str, Any]:
        response = data.get("response")
        extra = response if isinstance(response, dict) else data if isinstance(data, dict) else {}

        def summarize_related(key: str) -> dict[str, Any]:
            items = _safe_list(extra.get(key))
            related: list[dict[str, Any]] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                worker = item.get("worker") if isinstance(item.get("worker"), dict) else {}
                cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
                related.append(
                    {
                        "id": item.get("id") or item.get("PID"),
                        "title": _clean_text(item.get("title") or item.get("name"), 220),
                        "price": item.get("price") or item.get("priceWithCurrency"),
                        "share_url": _absolute_kwork_url(item.get("share_url") or item.get("url")),
                        "worker": worker.get("username") or item.get("username"),
                        "rating": worker.get("rating") or item.get("rating"),
                        "reviews": worker.get("reviews_count") or item.get("reviews_count"),
                        "image_url": item.get("image_url") or cover.get("tablet") or cover.get("phone") or item.get("photo"),
                    }
                )
            return {"total": len(items), "items": related}

        ratings = extra.get("kwork_ratings") if isinstance(extra.get("kwork_ratings"), dict) else {}
        last_reviews = _safe_list(extra.get("last_reviews"))
        return {
            "review_summary": {
                "reviews_count": _as_int(extra.get("reviews_count")),
                "good_reviews": _as_int(extra.get("goodReviews")),
                "bad_reviews": _as_int(extra.get("badReviews")),
                "ratings": ratings,
                "last_reviews_count": len(last_reviews),
            },
            "faq_count": _as_int(extra.get("frequently_asked_questions_count")),
            "related_kworks": {
                "recommended": summarize_related("recommended_kworks"),
                "similar": summarize_related("similar_kworks"),
                "other": summarize_related("other_kworks"),
            },
            "extra_raw_keys": sorted(extra.keys()),
        }

    async def fetch_seller_intelligence(
        self,
        username: str,
        *,
        sample_kworks_limit: int = 6,
        include_portfolio: bool = True,
        include_reviews: bool = True,
    ) -> dict[str, Any]:
        username = str(username or "").strip()
        if not username:
            raise ValueError("seller username is missing")

        profile_data = await self.request("userByUsername", username=username)
        profile = self._response_dict(profile_data)
        summary = self.summarize_seller_profile(username, profile)
        user_id = _as_int(summary.get("id"))

        categories: list[dict[str, Any]] = []
        kworks: list[dict[str, Any]] = []
        portfolio: dict[str, Any] = {"status": "skipped"}
        reviews: dict[str, Any] = {"status": "skipped"}
        errors: list[str] = []
        if user_id:
            try:
                categories = self.summarize_seller_categories(
                    await self.request("kworksCategoriesList", user_id=user_id)
                )
            except Exception as exc:
                errors.append(f"kworksCategoriesList: {type(exc).__name__}: {exc}")
            try:
                user_kworks_data = await self.request("userKworks", user_id=user_id, page=1)
                response = user_kworks_data.get("response")
                if isinstance(response, list):
                    kwork_items = response
                else:
                    raw_kworks = self._response_dict(user_kworks_data)
                    kwork_items = (
                        raw_kworks.get("kworks")
                        if isinstance(raw_kworks.get("kworks"), list)
                        else raw_kworks.get("items")
                    )
                    if kwork_items is None and isinstance(raw_kworks.get("data"), list):
                        kwork_items = raw_kworks.get("data")
                kworks = self.summarize_competitors(_safe_list(kwork_items), limit=sample_kworks_limit)
            except Exception as exc:
                errors.append(f"userKworks: {type(exc).__name__}: {exc}")
            if include_portfolio:
                try:
                    portfolio = {
                        "status": "ok",
                        **self.summarize_seller_portfolio(
                            await self.request("portfolioList", user_id=user_id, category_id="all", page=1)
                        ),
                    }
                except Exception as exc:
                    portfolio = {"status": "error", "total": 0, "sample_count": 0, "items": []}
                    errors.append(f"portfolioList: {type(exc).__name__}: {exc}")
            if include_reviews:
                review_sets: dict[str, Any] = {}
                for review_type in ("all", "negative"):
                    try:
                        review_sets[review_type] = {
                            "status": "ok",
                            **self.summarize_seller_reviews(
                                await self.request("userReviews", user_id=user_id, type=review_type, page=1)
                            ),
                        }
                    except Exception as exc:
                        review_sets[review_type] = {"status": "error", "total": 0, "sample_count": 0, "items": []}
                        errors.append(f"userReviews[{review_type}]: {type(exc).__name__}: {exc}")
                reviews = {
                    "status": "ok" if all(item.get("status") == "ok" for item in review_sets.values()) else "partial",
                    **review_sets,
                }
        else:
            errors.append("user id missing; category and inventory endpoints skipped")

        return {
            **summary,
            "categories": categories,
            "sample_kworks": kworks,
            "sample_kworks_count": len(kworks),
            "portfolio": portfolio,
            "reviews": reviews,
            "status": "ok" if not errors else "partial",
            "errors": errors,
        }

    async def enrich_sellers_from_competitors(
        self,
        competitors: list[dict[str, Any]],
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        seller_counts: dict[str, int] = {}
        for item in competitors:
            username = str(item.get("worker") or "").strip()
            if username:
                seller_counts[username] = seller_counts.get(username, 0) + 1
        usernames = [
            username
            for username, _count in sorted(seller_counts.items(), key=lambda item: item[1], reverse=True)[: max(0, limit)]
        ]
        if not usernames:
            return []

        seller_delay = _float_env("KWORK_SELLER_DETAIL_DELAY", 0.4)
        results: list[dict[str, Any]] = []
        for index, username in enumerate(usernames):
            cache_key = username.lower()
            cached = _cache_get(_SELLER_DETAIL_CACHE, cache_key, SELLER_DETAIL_CACHE_TTL)
            if cached is not None:
                result = cached
            else:
                try:
                    result = await self.fetch_seller_intelligence(username)
                    _cache_set(_SELLER_DETAIL_CACHE, cache_key, result)
                except Exception as exc:
                    result = {
                        "username": username,
                        "status": "error",
                        "errors": [f"{type(exc).__name__}: {exc}"],
                        "categories": [],
                        "sample_kworks": [],
                    }
            result = {**result, "seen_cards": seller_counts.get(username, 0)}
            results.append(result)
            if seller_delay > 0 and index < len(usernames) - 1:
                await asyncio.sleep(seller_delay)
        return results

    async def fetch_competitor_detail(
        self,
        client: httpx.AsyncClient | None,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        kwork_id = _extract_kwork_id(competitor.get("id") or competitor.get("share_url") or competitor.get("url"))
        if not kwork_id:
            raise ValueError("competitor kwork id is missing")
        data = await self.request("getKworkDetails", id=kwork_id)
        response = data.get("response")
        kwork = response if isinstance(response, dict) else {}
        short_user = kwork.get("short_user_info") if isinstance(kwork.get("short_user_info"), dict) else {}

        title = _clean_text(
            kwork.get("kwork_title")
            or kwork.get("title")
            or kwork.get("gtitle")
            or competitor.get("title"),
            220,
        )
        description = _clean_text(
            kwork.get("kwork_description") or kwork.get("description") or kwork.get("gdesc"),
            1400,
        )
        instruction = _clean_text(
            kwork.get("kwork_instructions") or kwork.get("instructions") or kwork.get("ginst"),
            420,
        )
        service_size = _clean_text(kwork.get("unit_and_quantity") or kwork.get("volume") or kwork.get("gwork"), 260)
        image_url = competitor.get("image_url") or _cdn_photo_url(
            kwork.get("image_url") or kwork.get("photo") or kwork.get("photoFile")
        )
        price = (
            kwork.get("default_kwork_price")
            or kwork.get("minVolumePrice")
            or kwork.get("displayedPrice")
            or competitor.get("price")
        )
        worker = _clean_text(short_user.get("username") or kwork.get("username") or competitor.get("worker"), 80)
        extra: dict[str, Any] = {"status": "skipped"}
        try:
            extra = {"status": "ok", **self.summarize_competitor_extra(await self.request("getKworkDetailsExtra", id=kwork_id))}
        except Exception as exc:
            extra = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

        practice_parts = [
            title,
            f"Р¦РµРЅР°: {price} в‚Ѕ" if price else "",
            f"РџСЂРѕРґР°РІРµС†: {worker}" if worker else "",
            f"РћР±СЉРµРј: {service_size}" if service_size else "",
            f"РћРїРёСЃР°РЅРёРµ: {description}" if description else "",
            f"Р§С‚Рѕ РїСЂРѕСЃРёС‚ Сѓ РєР»РёРµРЅС‚Р°: {instruction}" if instruction else "",
        ]
        return {
            "title": title or competitor.get("title"),
            "price": price or competitor.get("price"),
            "worker": worker or competitor.get("worker"),
            "image_url": image_url,
            "description": description,
            "instruction": instruction,
            "service_size": service_size,
            "queue_count": _as_int(kwork.get("orders_in_queue_count") or kwork.get("queueCount")),
            "work_time_seconds": _as_int(kwork.get("term") or kwork.get("avgWorkTime")),
            "practice_context": " | ".join(part for part in practice_parts if part),
            "extra": extra,
            "detail_status": "ok" if description else "no_description",
        }

    @staticmethod
    def summarize_classifiers(classifiers: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in classifiers:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "id": _as_int(item.get("id")),
                    "name": item.get("name") or item.get("title") or str(item.get("id") or ""),
                    "kworks_count": _as_int(item.get("kworks_count") or item.get("count")),
                    "raw": item,
                }
            )
        return result

    @staticmethod
    def build_practice_context(competitors: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
        context: list[dict[str, Any]] = []
        for item in competitors[:limit]:
            if not item.get("title") and not item.get("description"):
                continue
            context.append(
                {
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "worker": item.get("worker"),
                    "rating": item.get("rating"),
                    "reviews": item.get("reviews"),
                    "description": item.get("description"),
                    "service_size": item.get("service_size"),
                    "instruction": item.get("instruction"),
                    "url": item.get("share_url"),
                }
            )
        return context

    async def get_market_metrics(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
        include_demand: bool = False,
        include_competitor_details: bool = False,
        competitor_detail_limit: int = 6,
        attribute_filters: dict[str, Any] | None = None,
        attribute_controls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self._owns_api:
            return await self._get_market_metrics_uncached(
                category_id=category_id,
                classifier_id=classifier_id,
                page=page,
                include_demand=include_demand,
                include_competitor_details=include_competitor_details,
                competitor_detail_limit=competitor_detail_limit,
                attribute_filters=attribute_filters,
                attribute_controls=attribute_controls,
            )

        cache_key = _stable_json(
            {
                "category_id": category_id,
                "classifier_id": classifier_id,
                "page": page,
                "include_demand": include_demand,
                "include_competitor_details": include_competitor_details,
                "competitor_detail_limit": competitor_detail_limit,
                "attribute_filters": normalize_attribute_filters(attribute_filters),
                "attribute_controls": attribute_controls or [],
            }
        )
        cached = _cache_get(_MARKET_METRICS_CACHE, cache_key, MARKET_METRICS_CACHE_TTL)
        if cached is not None:
            cached["cache_status"] = "hit"
            return cached

        async with _MARKET_CACHE_LOCK:
            cached = _cache_get(_MARKET_METRICS_CACHE, cache_key, MARKET_METRICS_CACHE_TTL)
            if cached is not None:
                cached["cache_status"] = "hit"
                return cached
            task = _MARKET_METRICS_INFLIGHT.get(cache_key)
            if task is None:
                task = asyncio.create_task(
                    self._get_market_metrics_uncached(
                        category_id=category_id,
                        classifier_id=classifier_id,
                        page=page,
                        include_demand=include_demand,
                        include_competitor_details=include_competitor_details,
                        competitor_detail_limit=competitor_detail_limit,
                        attribute_filters=attribute_filters,
                        attribute_controls=attribute_controls,
                    )
                )
                _MARKET_METRICS_INFLIGHT[cache_key] = task

        try:
            result = await task
        finally:
            async with _MARKET_CACHE_LOCK:
                if _MARKET_METRICS_INFLIGHT.get(cache_key) is task and task.done():
                    _MARKET_METRICS_INFLIGHT.pop(cache_key, None)

        if not task.cancelled() and task.exception() is None:
            result["cache_status"] = "miss"
            _cache_set(_MARKET_METRICS_CACHE, cache_key, result)
        return result

    async def _get_market_metrics_uncached(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
        include_demand: bool = False,
        include_competitor_details: bool = False,
        competitor_detail_limit: int = 6,
        attribute_filters: dict[str, Any] | None = None,
        attribute_controls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        timings_ms: dict[str, int] = {}
        filter_scope = describe_attribute_filter_scope(attribute_filters, controls=attribute_controls)
        effective_classifier_ids = selected_classifier_ids_from_attributes(
            attribute_filters,
            controls=attribute_controls,
            fallback_classifier_id=classifier_id,
        )
        effective_classifier_ids = effective_classifier_ids[: _market_max_fanout()]
        filter_scope["effective_classifier_ids"] = effective_classifier_ids
        kworks_started = time.monotonic()
        try:
            catalog = await self.get_kworks(
                category_id=category_id,
                classifier_id=classifier_id,
                page=page,
                attribute_filters=filter_scope["params"],
                attribute_controls=attribute_controls,
            )
        except Exception as exc:
            timings_ms["kworks"] = int((time.monotonic() - kworks_started) * 1000)
            timings_ms["total"] = int((time.monotonic() - started) * 1000)
            detail = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, TimeoutError) or "TimeoutError" in detail or "timed out" in detail.lower():
                logger.warning(
                    "KworkMarket: metrics kworks timeout category={} classifier={} page={} detail={}",
                    category_id,
                    classifier_id,
                    page,
                    detail,
                )
                return {
                    "category_id": category_id,
                    "classifier_id": classifier_id,
                    "page": page,
                    "kworks_count": 0,
                    "classifiers": [],
                    "competitors": [],
                    "practice_context": self.build_practice_context([]),
                    "raw_keys": [],
                    "filter_scope": filter_scope,
                    "filter_requests": [],
                    "demand": {"status": "skipped"},
                    "market_insights": build_market_insights([], kworks_count=0, classifiers=[]),
                    "status": "timeout",
                    "detail": "Kwork /kworks timed out; refresh or rotate VPN/proxy.",
                    "timings_ms": timings_ms,
                }
            raise
        timings_ms["kworks"] = int((time.monotonic() - kworks_started) * 1000)
        kworks = _safe_list(catalog.get("kworks"))
        classifiers = self.summarize_classifiers(_safe_list(catalog.get("classifiers")))
        competitors = self.summarize_competitors(kworks)
        if include_competitor_details:
            details_started = time.monotonic()
            competitors = await self.enrich_competitors(competitors, limit=competitor_detail_limit)
            timings_ms["competitor_details"] = int((time.monotonic() - details_started) * 1000)
        else:
            for competitor in competitors:
                if not competitor.get("description") and competitor.get("detail_status") == "pending":
                    competitor["detail_status"] = "details_off"
        metrics: dict[str, Any] = {
            "category_id": category_id,
            "classifier_id": classifier_id,
            "page": page,
            "kworks_count": _as_int(catalog.get("kworks_count") or catalog.get("count")),
            "classifiers": classifiers,
            "competitors": competitors,
            "practice_context": self.build_practice_context(competitors),
            "raw_keys": sorted(catalog.keys()),
            "filter_scope": filter_scope,
            "filter_requests": catalog.get("_filter_requests", []),
            "demand": {"status": "skipped"},
            "timings_ms": timings_ms,
        }

        if include_demand and category_id:
            demand_started = time.monotonic()
            demand_timeout = _float_env("KWORK_MARKET_DEMAND_TIMEOUT", 8.0)
            try:
                metrics["demand"] = await asyncio.wait_for(
                    self.get_demand_snapshot(
                        category_id,
                        filters=filter_scope["params"],
                        scope=filter_scope,
                        classifier_ids=effective_classifier_ids,
                    ),
                    timeout=max(1.0, demand_timeout),
                )
            except (TimeoutError, asyncio.TimeoutError):
                metrics["demand"] = {
                    "status": "timeout",
                    "detail": f"Kwork demand lookup exceeded {demand_timeout:g}s and was skipped for speed.",
                }
            timings_ms["demand"] = int((time.monotonic() - demand_started) * 1000)
        metrics["market_insights"] = build_market_insights(
            competitors,
            kworks_count=metrics["kworks_count"],
            classifiers=classifiers,
            demand=metrics.get("demand"),
        )
        timings_ms["total"] = int((time.monotonic() - started) * 1000)
        logger.info(
            "KworkMarket: metrics category={} classifier={} demand={} details={} limit={} kworks={} competitors={} timings_ms={}",
            category_id,
            classifier_id,
            include_demand,
            include_competitor_details,
            competitor_detail_limit,
            metrics["kworks_count"],
            len(competitors),
            timings_ms,
        )
        return metrics

    async def get_demand_snapshot(
        self,
        category_id: int,
        *,
        filters: dict[str, Any] | None = None,
        scope: dict[str, Any] | None = None,
        classifier_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        clean_filters = normalize_attribute_filters(filters)
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            request_filters = dict(clean_filters)
            if classifier_ids:
                request_filters["classifierId"] = classifier_ids[0]
                request_filters["attr"] = classifier_ids[0]
            count = await service.get_wants_count(categories=str(category_id), **request_filters)
            projects, meta = await service.get_raw_projects(categories=str(category_id), page=1, **request_filters)
            if count or projects:
                return {
                    "status": "ok",
                    "wants_count": count,
                    "sample_count": len(projects),
                    "sample": [self._summarize_project(item) for item in projects[:5] if isinstance(item, dict)],
                    "meta": meta,
                    "scope": scope or describe_attribute_filter_scope(filters),
                    "filter_params": request_filters,
                }
            return {
                "status": "needs_cookies",
                "label": "РЅСѓР¶РЅС‹ РєСѓРєРё",
                "wants_count": 0,
                "sample_count": 0,
                "detail": "Kwork РЅРµ РІРµСЂРЅСѓР» Р·Р°РєР°Р·С‹ Р±РµР· Р°РІС‚РѕСЂРёР·РѕРІР°РЅРЅС‹С… cookies Session Hub.",
                "scope": scope or describe_attribute_filter_scope(filters),
                "filter_params": request_filters,
            }
        except Exception as exc:
            logger.debug(f"KworkMarket: demand snapshot failed: {exc}")
            return {
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "scope": scope or describe_attribute_filter_scope(filters),
                "filter_params": clean_filters,
            }

    async def get_catalog_taxonomy_seeds(self, *, limit: int = 48) -> list[dict[str, Any]]:
        rubrics_data = await self.request("catalogRubrics")
        rubric_items = self._first_response_list(rubrics_data, "rubrics", "items", "data", "categories")
        seeds: list[dict[str, Any]] = []
        for rubric in rubric_items[:20]:
            if not isinstance(rubric, dict):
                continue
            rubric_id = _as_int(rubric.get("id") or rubric.get("rubric_id") or rubric.get("rubricId"))
            if not rubric_id:
                continue
            categories_data = await self.request("catalogCategories", rubricId=rubric_id)
            category_items = self._first_response_list(categories_data, "categories", "items", "data")
            for item in _extract_taxonomy_seed_items(category_items, rubric_id=rubric_id):
                item["source"] = "catalog_taxonomy"
                seeds.append(item)
            if len(seeds) >= limit:
                break

        clean = [item for item in (_normalize_market_seed(seed) for seed in seeds) if item]
        clean.sort(key=lambda item: _as_int(item.get("seed_kworks_count")), reverse=True)
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int | None]] = set()
        for item in clean:
            key = (item.get("category_id"), item.get("classifier_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        return deduped

    async def get_catalog_market_seeds(self, *, limit: int = 24, include_taxonomy: bool = True) -> list[dict[str, Any]]:
        data = await self.request("catalogMainv2")
        response = data.get("response")
        main_items = _extract_catalog_seed_items(response)
        for item in main_items:
            item["source"] = "catalog_mainv2"
        seeds = [_normalize_market_seed(item) for item in main_items]
        clean = [item for item in seeds if item]
        if include_taxonomy:
            try:
                clean.extend(await self.get_catalog_taxonomy_seeds(limit=max(limit * 3, 24)))
            except Exception as exc:
                logger.debug(f"KworkMarket: catalog taxonomy seed discovery failed: {exc}")
        clean.sort(key=lambda item: _as_int(item.get("seed_kworks_count")), reverse=True)
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int | None]] = set()
        for item in clean:
            key = (item.get("category_id"), item.get("classifier_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        return deduped

    @staticmethod
    def build_market_rankings(supply: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rankings: list[dict[str, Any]] = []
        for item in supply:
            seed = item.get("seed") if isinstance(item.get("seed"), dict) else {}
            cards = [card for card in _safe_list(item.get("cards")) if isinstance(card, dict)]
            seller_counts: dict[str, int] = {}
            prices: list[float] = []
            for card in cards:
                worker = str(card.get("worker") or "").strip()
                if worker:
                    seller_counts[worker] = seller_counts.get(worker, 0) + 1
                price = _as_float(card.get("price"))
                if price > 0:
                    prices.append(price)

            demand = item.get("demand") if isinstance(item.get("demand"), dict) else {}
            demand_status = str(demand.get("status") or "unknown")
            wants_count = _as_int(demand.get("wants_count"), -1) if demand.get("wants_count") is not None else -1
            demand_sample = [row for row in _safe_list(demand.get("sample")) if isinstance(row, dict)]
            demand_offers = [
                _as_int(row.get("offers"), -1)
                for row in demand_sample
                if row.get("offers") is not None and _as_int(row.get("offers"), -1) >= 0
            ]
            demand_prices = [
                _as_float(row.get("price") or row.get("possible_price_limit"))
                for row in demand_sample
                if _as_float(row.get("price") or row.get("possible_price_limit")) > 0
            ]
            low_offer_count = sum(1 for offers in demand_offers if offers <= 2)
            portfolio_required_count = sum(1 for row in demand_sample if _as_bool(row.get("user_need_portfolio")))
            kwork_required_count = sum(1 for row in demand_sample if _as_bool(row.get("user_need_kwork")))
            higher_price_allowed_count = sum(1 for row in demand_sample if _as_bool(row.get("allow_higher_price")))
            price_rules = item.get("price_rules") if isinstance(item.get("price_rules"), dict) else {}
            price_rule_min = _as_int(price_rules.get("min_price"))
            price_rule_max = _as_int(price_rules.get("max_price"))
            price_rule_typical = _as_int(price_rules.get("typical_price"))
            supply_count = _as_int(item.get("kworks_count"))
            sample_count = len(cards)
            max_seller_cards = max(seller_counts.values(), default=0)
            demand_per_1000 = round((wants_count * 1000) / max(supply_count, 1), 3) if wants_count >= 0 else None
            opportunity_score = (
                round((math.log1p(wants_count) * 1000) / (math.log1p(max(supply_count, 1)) + 1), 2)
                if wants_count >= 0
                else None
            )
            rankings.append(
                {
                    "seed_name": seed.get("name"),
                    "category_id": seed.get("category_id"),
                    "classifier_id": seed.get("classifier_id"),
                    "supply_kworks_count": supply_count,
                    "demand_wants_count": wants_count if wants_count >= 0 else None,
                    "demand_status": demand_status,
                    "demand_per_1000_kworks": demand_per_1000,
                    "opportunity_score": opportunity_score,
                    "sample_cards": sample_count,
                    "sample_unique_sellers": len(seller_counts),
                    "seller_concentration": round(max_seller_cards / sample_count, 3) if sample_count else 0,
                    "sample_price_min": int(min(prices)) if prices else None,
                    "sample_price_max": int(max(prices)) if prices else None,
                    "sample_price_avg": round(sum(prices) / len(prices), 2) if prices else None,
                    "price_rule_min": price_rule_min or None,
                    "price_rule_max": price_rule_max or None,
                    "price_rule_typical": price_rule_typical or None,
                    "demand_sample_offers_min": min(demand_offers) if demand_offers else None,
                    "demand_sample_low_offer_count": low_offer_count,
                    "demand_sample_budget_avg": round(sum(demand_prices) / len(demand_prices), 2)
                    if demand_prices
                    else None,
                    "demand_sample_portfolio_required_count": portfolio_required_count,
                    "demand_sample_kwork_required_count": kwork_required_count,
                    "demand_sample_higher_price_allowed_count": higher_price_allowed_count,
                    "top_sellers": sorted(seller_counts.items(), key=lambda entry: entry[1], reverse=True)[:5],
                    "signals": {
                        "demand_level": (
                            "skipped"
                            if wants_count < 0
                            else "high"
                            if wants_count >= 50
                            else "medium"
                            if wants_count >= 15
                            else "low"
                            if wants_count > 0
                            else "none"
                        ),
                        "supply_level": (
                            "saturated"
                            if supply_count >= 50000
                            else "competitive"
                            if supply_count >= 10000
                            else "moderate"
                            if supply_count >= 1000
                            else "niche"
                        ),
                        "low_offer_demand": bool(demand_offers and low_offer_count >= max(1, len(demand_offers) // 2)),
                        "portfolio_heavy_demand": bool(
                            demand_sample and portfolio_required_count >= max(1, len(demand_sample) // 2)
                        ),
                        "higher_price_allowed": bool(higher_price_allowed_count),
                    },
                }
            )
        return sorted(
            rankings,
            key=lambda row: (
                row.get("opportunity_score") is not None,
                row.get("opportunity_score") or 0,
                row.get("demand_wants_count") or 0,
            ),
            reverse=True,
        )

    def get_market_intelligence_history(
        self,
        *,
        limit: int = 50,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        root = Path(output_dir) if output_dir else Path("docs") / "kwork_market_snapshots"
        latest_path = root / "latest.json"
        index_path = root / "index.jsonl"
        latest: dict[str, Any] | None = None
        if latest_path.exists():
            try:
                value = json.loads(latest_path.read_text(encoding="utf-8"))
                latest = value if isinstance(value, dict) else None
            except (OSError, json.JSONDecodeError):
                latest = None

        entries: list[dict[str, Any]] = []
        if index_path.exists():
            try:
                lines = index_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[-max(1, limit) :]:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    entries.append(value)

        return {
            "root": str(root),
            "latest_path": str(latest_path),
            "index_path": str(index_path),
            "latest": latest,
            "entries": entries,
            "entry_count": len(entries),
            "exists": latest_path.exists() or index_path.exists(),
        }

    @staticmethod
    def _summarize_project(item: dict[str, Any]) -> dict[str, Any]:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        user_data = user.get("data") if isinstance(user.get("data"), dict) else {}
        platform_data = item.get("platform_data") if isinstance(item.get("platform_data"), dict) else {}
        price = (
            item.get("price")
            or item.get("budget")
            or item.get("priceLimit")
            or item.get("price_limit")
            or item.get("possible_price")
            or item.get("possiblePriceLimit")
            or item.get("possible_price_limit")
            or platform_data.get("possible_price_limit")
        )
        possible_price_limit = (
            item.get("possible_price_limit")
            or item.get("possiblePriceLimit")
            or item.get("possible_price")
            or platform_data.get("possible_price_limit")
        )
        offers = item.get("offers")
        if offers is None:
            offers = item.get("offers_count")
        if offers is None:
            offers = item.get("kwork_count")
        if offers is None:
            offers = item.get("kworks_count")
        user_hired_percent = (
            item.get("user_hired_percent")
            or item.get("wants_hired_percent")
            or user_data.get("wants_hired_percent")
            or user_data.get("order_done_repeat_persent")
        )
        buyer_id = item.get("user_id") or item.get("userId") or user.get("id") or user_data.get("id")
        buyer_username = _clean_text(
            item.get("username") or item.get("user_name") or item.get("userName") or user.get("username"),
            80,
        )
        profile_picture = (
            item.get("profile_picture")
            or item.get("profilePicture")
            or user.get("profile_picture")
            or user.get("profilePicture")
            or user_data.get("profile_picture")
        )
        user_projects_count = item.get("user_projects_count") or item.get("userProjectsCount")
        user_active_projects_count = item.get("user_active_projects_count") or item.get("userActiveProjectsCount")
        has_offer = item.get("has_offer") if "has_offer" in item else item.get("hasOffer")
        is_viewed = item.get("is_viewed") if "is_viewed" in item else item.get("isViewed")
        return {
            "id": item.get("id") or item.get("want_id"),
            "title": _clean_text(item.get("title") or item.get("name"), 220),
            "category_id": _as_int(item.get("category_id") or item.get("categoryId")),
            "parent_category_id": _as_int(item.get("parent_category_id") or item.get("parentCategoryId")),
            "price": price,
            "possible_price_limit": possible_price_limit,
            "offers": _as_int(offers),
            "time_left": item.get("time_left") or item.get("timeLeft"),
            "max_days": _as_int(item.get("max_days") or platform_data.get("max_days")),
            "views": item.get("views") or item.get("views_dirty") or platform_data.get("views_dirty"),
            "orders": item.get("orders"),
            "date_create": item.get("date_create") or item.get("dateCreate") or item.get("created_at"),
            "date_expire": item.get("date_expire") or item.get("dateExpire") or platform_data.get("date_expire"),
            "user_hired_percent": _as_int(user_hired_percent),
            "user_id": _as_int(buyer_id),
            "username": buyer_username,
            "user_projects_count": _as_int(user_projects_count),
            "user_active_projects_count": _as_int(user_active_projects_count),
            "profile_picture": profile_picture,
            "has_offer": _as_bool(has_offer),
            "is_viewed": _as_bool(is_viewed),
            "buyer_projects_url": f"https://kwork.ru/projects/list/{buyer_username}" if buyer_username else "",
            "user_need_kwork": _as_bool(item.get("userNeedKwork") or item.get("user_need_kwork")),
            "user_need_portfolio": _as_bool(
                item.get("userNeedPortfolio")
                or item.get("user_need_portfolio")
                or platform_data.get("user_need_portfolio")
            ),
            "allow_higher_price": _as_bool(
                item.get("allowHigherPrice")
                or item.get("allow_higher_price")
                or platform_data.get("allow_higher_price")
            ),
            "is_higher_price": _as_bool(item.get("isHigherPrice") or item.get("is_higher_price")),
            "user": {
                "id": _as_int(buyer_id),
                "username": buyer_username,
                "rating": user.get("rating"),
                "rating_count": user.get("rating_count"),
                "projects_count": _as_int(user_projects_count),
                "active_projects_count": _as_int(user_active_projects_count),
            }
            if user or buyer_id or buyer_username
            else {},
        }

    @staticmethod
    def summarize_want_detail(data: dict[str, Any]) -> dict[str, Any]:
        response = data.get("response")
        if isinstance(response, list) and response and isinstance(response[0], dict):
            want = response[0]
        elif isinstance(response, dict):
            want = response
        else:
            want = data if isinstance(data, dict) else {}
        if isinstance(want.get("want"), dict):
            want = want["want"]
        views_history = want.get("views_history") or want.get("viewsHistory")
        orders = want.get("orders") if want.get("orders") is not None else want.get("orders_count")
        buyer_username = _clean_text(want.get("username") or want.get("user_name") or want.get("userName"), 80)
        return {
            "status": "ok" if want else "empty",
            "id": want.get("id") or want.get("want_id"),
            "title": _clean_text(want.get("title") or want.get("name"), 220),
            "description": _clean_text(want.get("description"), 700),
            "views": _as_int(want.get("views") or want.get("views_dirty")),
            "orders": _as_int(orders),
            "views_history_count": len(_safe_list(views_history)),
            "views_history": _safe_list(views_history)[:14],
            "date_create": want.get("date_create") or want.get("dateCreate"),
            "date_expire": want.get("date_expire") or want.get("dateExpire"),
            "user_id": _as_int(want.get("user_id") or want.get("userId")),
            "username": buyer_username,
            "user_projects_count": _as_int(want.get("user_projects_count") or want.get("userProjectsCount")),
            "user_active_projects_count": _as_int(
                want.get("user_active_projects_count") or want.get("userActiveProjectsCount")
            ),
            "user_hired_percent": _as_int(want.get("user_hired_percent") or want.get("wants_hired_percent")),
            "buyer_projects_url": f"https://kwork.ru/projects/list/{buyer_username}" if buyer_username else "",
            "raw_keys": sorted(want.keys()),
        }

    async def fetch_want_detail(self, want_id: Any) -> dict[str, Any]:
        want_id_int = _as_int(want_id)
        if not want_id_int:
            return {"status": "skipped", "detail": "missing want id"}
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            get_token_api = getattr(service, "get_token_api", None)
            api = await get_token_api() if get_token_api else await service.get_api()
            if not api:
                return {"status": "error", "detail": "Kwork API is unavailable"}
            data = await api.request("post", "want", use_token=True, id=want_id_int)
            return self.summarize_want_detail(data if isinstance(data, dict) else {})
        except Exception as exc:
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def score_buyer_project(
        project: dict[str, Any],
        *,
        budget_max: int = BUYER_REASONABLE_BUDGET_MAX,
    ) -> dict[str, Any]:
        offers = _as_int(project.get("offers"), default=-1)
        price = _as_int(project.get("price"))
        possible_price_limit = _as_int(project.get("possible_price_limit"))
        user_hired_percent = _as_int(project.get("user_hired_percent"))
        description = str(project.get("description") or "")
        score = 0.0
        reasons: list[str] = []

        if offers == 0:
            score += 45
            reasons.append("0 offers")
        elif 0 < offers <= 2:
            score += 38
            reasons.append("1-2 offers")
        elif offers <= 5:
            score += 28
            reasons.append("<=5 offers")
        elif offers <= 10:
            score += 16
            reasons.append("<=10 offers")
        elif offers > 25:
            score -= 18
            reasons.append("crowded offers")

        budget_basis = max(price or 0, possible_price_limit if project.get("allow_higher_price") else 0)
        budget_max = max(0, _as_int(budget_max, default=BUYER_REASONABLE_BUDGET_MAX))
        if budget_max and budget_basis > budget_max:
            score -= 35
            reasons.append("above budget cap")
        elif budget_basis >= 30000:
            score += 28
            reasons.append("high budget")
        elif budget_basis >= 10000:
            score += 18
            reasons.append("good budget")
        elif budget_basis >= 3000:
            score += 8
            reasons.append("ok budget")
        else:
            score -= 6
            reasons.append("low budget")

        if project.get("allow_higher_price"):
            score += 10
            reasons.append("higher price allowed")
        if user_hired_percent >= 70:
            score += 10
            reasons.append("buyer hires often")
        elif 30 <= user_hired_percent < 70:
            score += 5
            reasons.append("buyer has hiring history")
        if project.get("user_need_portfolio"):
            score += 4
            reasons.append("portfolio requested")
        if len(description) >= 180:
            score += 6
            reasons.append("detailed brief")
        if project.get("already_work"):
            score -= 40
            reasons.append("already_work")

        return {"score": round(score, 2), "score_reasons": reasons[:8]}

    @staticmethod
    def build_buyer_market_signals(
        ranked: list[dict[str, Any]],
        probes: list[dict[str, Any]],
        *,
        budget_max: int = BUYER_REASONABLE_BUDGET_MAX,
    ) -> list[dict[str, Any]]:
        def budget(project: dict[str, Any]) -> int:
            direct = _as_int(project.get("price"))
            possible = _as_int(project.get("possible_price_limit"))
            return max(direct, possible if project.get("allow_higher_price") else 0)

        def history_total(project: dict[str, Any]) -> int:
            history = project.get("buyer_history") if isinstance(project.get("buyer_history"), dict) else {}
            return max(
                _as_int(project.get("user_projects_count")),
                _as_int(history.get("total")),
                _as_int(history.get("projects_count")),
            )

        def compact_project(project: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": project.get("id"),
                "title": _clean_text(project.get("title"), 140),
                "offers": _as_int(project.get("offers")),
                "budget": budget(project),
                "username": _clean_text(project.get("username"), 80),
                "score": project.get("score"),
            }

        low_offer = [project for project in ranked if 0 <= _as_int(project.get("offers"), default=999) <= 5]
        zero_offer = [project for project in ranked if _as_int(project.get("offers"), default=999) == 0]
        budget_max = max(0, _as_int(budget_max, default=BUYER_REASONABLE_BUDGET_MAX))
        budget_fit = sorted(
            [project for project in low_offer if not budget_max or budget(project) <= budget_max],
            key=lambda project: (project.get("score") or 0, budget(project)),
            reverse=True,
        )
        high_budget = sorted(
            [project for project in low_offer if budget(project) >= 10000 and (not budget_max or budget(project) <= budget_max)],
            key=lambda project: (budget(project), project.get("score") or 0),
            reverse=True,
        )
        repeat_buyers = sorted(
            [project for project in ranked if history_total(project) >= 3],
            key=lambda project: (history_total(project), project.get("score") or 0),
            reverse=True,
        )
        proven_buyers = sorted(
            [project for project in ranked if _as_int(project.get("user_hired_percent")) >= 30],
            key=lambda project: (_as_int(project.get("user_hired_percent")), project.get("score") or 0),
            reverse=True,
        )
        probe_leaders = sorted(
            [
                {
                    "name": _clean_text(probe.get("name") or probe.get("query") or "probe", 120),
                    "query": _clean_text(probe.get("query"), 120),
                    "count": _as_int(probe.get("count")),
                    "sample_count": _as_int(probe.get("sample_count")),
                    "filters": probe.get("filters") if isinstance(probe.get("filters"), dict) else {},
                }
                for probe in probes
                if probe.get("status") == "ok"
            ],
            key=lambda probe: (probe["count"], probe["sample_count"]),
            reverse=True,
        )

        signals: list[dict[str, Any]] = []
        if zero_offer:
            signals.append(
                {
                    "kind": "zero_offer",
                    "label": "Р›РѕС‚С‹ Р±РµР· РѕС‚РєР»РёРєРѕРІ",
                    "value": len(zero_offer),
                    "detail": "РџРµСЂРІС‹Рµ С†РµР»Рё РґР»СЏ Р±С‹СЃС‚СЂРѕРіРѕ РѕС‚РІРµС‚Р°: РєРѕРЅРєСѓСЂРµРЅС†РёСЏ РµС‰С‘ РЅРµ РЅР°Р±РµР¶Р°Р»Р°.",
                    "projects": [compact_project(project) for project in zero_offer[:4]],
                }
            )
        if high_budget:
            signals.append(
                {
                    "kind": "high_budget_low_offer",
                    "label": "Р”РµРЅСЊРіРё Рё РјР°Р»Рѕ РѕС‚РєР»РёРєРѕРІ",
                    "value": len(high_budget),
                    "detail": "Р‘СЋРґР¶РµС‚ РѕС‚ 10 000 в‚Ѕ Рё РЅРµ Р±РѕР»СЊС€Рµ 5 РѕС‚РєР»РёРєРѕРІ.",
                    "projects": [compact_project(project) for project in high_budget[:4]],
                }
            )
        if budget_fit:
            signals.append(
                {
                    "kind": "budget_fit_low_offer",
                    "label": "Р РµР°Р»РёСЃС‚РёС‡РЅС‹Р№ Р±СЋРґР¶РµС‚",
                    "value": len(budget_fit),
                    "detail": f"Р›РѕС‚С‹ РІ Р»РёРјРёС‚Рµ РґРѕ {budget_max} в‚Ѕ Рё СЃ РјР°Р»С‹Рј С‡РёСЃР»РѕРј РѕС‚РєР»РёРєРѕРІ.",
                    "projects": [compact_project(project) for project in budget_fit[:4]],
                }
            )
        if repeat_buyers:
            signals.append(
                {
                    "kind": "repeat_buyers",
                    "label": "РџРѕРєСѓРїР°С‚РµР»Рё СЃ РёСЃС‚РѕСЂРёРµР№",
                    "value": len(repeat_buyers),
                    "detail": "РЈ РїРѕРєСѓРїР°С‚РµР»СЏ СѓР¶Рµ РµСЃС‚СЊ РЅРµСЃРєРѕР»СЊРєРѕ Р»РѕС‚РѕРІ, СЌС‚Рѕ Р»СѓС‡С€Рµ СЂР°Р·РѕРІРѕРіРѕ СЃР»СѓС‡Р°Р№РЅРѕРіРѕ Р·Р°РїСЂРѕСЃР°.",
                    "projects": [compact_project(project) for project in repeat_buyers[:4]],
                }
            )
        if proven_buyers:
            signals.append(
                {
                    "kind": "proven_buyers",
                    "label": "РџРѕРєСѓРїР°С‚РµР»Рё РЅР°РЅРёРјР°СЋС‚",
                    "value": len(proven_buyers),
                    "detail": "Р’ РєР°СЂС‚РѕС‡РєРµ РїРѕРєСѓРїР°С‚РµР»СЏ РµСЃС‚СЊ Р·Р°РјРµС‚РЅС‹Р№ РїСЂРѕС†РµРЅС‚ РЅР°Р№РјР°.",
                    "projects": [compact_project(project) for project in proven_buyers[:4]],
                }
            )
        if probe_leaders:
            signals.append(
                {
                    "kind": "probe_leaders",
                    "label": "Р Р°Р±РѕС‡РёРµ РїРѕРёСЃРєРѕРІС‹Рµ РѕРєРЅР°",
                    "value": len(probe_leaders),
                    "detail": "Р—Р°РїСЂРѕСЃС‹ Рё С„РёР»СЊС‚СЂС‹, РіРґРµ СЃРµР№С‡Р°СЃ РЅР°С…РѕРґРёС‚СЃСЏ Р±РѕР»СЊС€Рµ РІСЃРµРіРѕ Р¶РёРІС‹С… Р»РѕС‚РѕРІ.",
                    "probes": probe_leaders[:5],
                }
            )
        for signal in signals:
            signal["label"] = _clean_text(signal.get("label"))
            signal["detail"] = _clean_text(signal.get("detail"))
        return signals

    @staticmethod
    def build_buyer_summary(
        ranked: list[dict[str, Any]],
        probes: list[dict[str, Any]],
        query_suggestions: list[dict[str, Any]] | None = None,
        *,
        budget_max: int = BUYER_DEFAULT_PRICE_TO,
    ) -> dict[str, Any]:
        def project_budget(project: dict[str, Any]) -> int:
            direct = _as_int(project.get("price"))
            possible = _as_int(project.get("possible_price_limit"))
            return max(direct, possible if project.get("allow_higher_price") else 0)

        budget_cap = max(0, _as_int(budget_max, default=BUYER_DEFAULT_PRICE_TO))
        low_offer = [item for item in ranked if 0 <= _as_int(item.get("offers"), default=999) <= 5]
        zero_offer = [item for item in ranked if _as_int(item.get("offers"), default=999) == 0]
        budget_fit = [item for item in low_offer if not budget_cap or project_budget(item) <= budget_cap]
        proven = [item for item in ranked if _as_int(item.get("user_hired_percent")) >= 30]
        best_windows = sorted(
            [
                {
                    "name": _clean_text(probe.get("name") or probe.get("query") or "probe", 120),
                    "query": _clean_text(probe.get("query"), 120),
                    "categories": str(probe.get("categories") or "all"),
                    "count": _as_int(probe.get("count")),
                    "sample_count": _as_int(probe.get("sample_count")),
                    "source": _clean_text((probe.get("meta") or {}).get("source"), 80)
                    if isinstance(probe.get("meta"), dict)
                    else "",
                    "filters": probe.get("filters") if isinstance(probe.get("filters"), dict) else {},
                    "pages_sampled": len(probe.get("pages") or []) if isinstance(probe.get("pages"), list) else 0,
                }
                for probe in probes
                if isinstance(probe, dict) and probe.get("status") == "ok"
            ],
            key=lambda item: (item["count"], item["sample_count"]),
            reverse=True,
        )[:6]
        best_projects = [
            {
                "id": project.get("id"),
                "title": _clean_text(project.get("title"), 140),
                "budget": project_budget(project),
                "offers": _as_int(project.get("offers")),
                "score": project.get("score"),
                "matched_probe": _clean_text(project.get("matched_probe"), 120),
                "user_hired_percent": _as_int(project.get("user_hired_percent")),
            }
            for project in ranked[:6]
        ]
        next_actions: list[str] = []
        if zero_offer:
            next_actions.append(f"Reply first to {len(zero_offer)} zero-offer lots before they become crowded.")
        if budget_fit:
            next_actions.append(f"Keep budget cap at {budget_cap} RUB for this account and use {len(budget_fit)} low-offer fits.")
        if best_windows:
            next_actions.append(f"Reuse the strongest window: {best_windows[0]['name']} ({best_windows[0]['count']} lots).")
        if proven:
            next_actions.append(f"Prioritize {len(proven)} buyers with visible hiring history.")
        if query_suggestions:
            next_actions.append("Use keyword hints to add more probes when current windows dry up.")
        if not next_actions:
            next_actions.append("No strong buyer window was found; widen category probes before enabling heavy details.")

        return {
            "budget_max": budget_cap,
            "unique_projects": len(ranked),
            "zero_offer_count": len(zero_offer),
            "low_offer_count": len(low_offer),
            "budget_fit_count": len(budget_fit),
            "proven_buyer_count": len(proven),
            "best_windows": best_windows,
            "best_projects": best_projects,
            "next_actions": next_actions[:5],
        }

    @staticmethod
    def _buyer_probe_filters(probe: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "price_from",
            "price_to",
            "hiring_from",
            "kworks_filter_from",
            "kworks_filter_to",
            "classifierId",
            "attr",
        }
        return {key: value for key, value in probe.items() if key in allowed and value not in (None, "")}

    async def fetch_want_search_suggestions(
        self,
        query: Any,
        *,
        limit: int = 8,
        cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        clean_query = _clean_text(query, 120)
        limit = max(1, min(_as_int(limit, default=8), 20))
        if len(clean_query) < 2:
            return {"status": "skipped", "query": clean_query, "suggestions": []}
        try:
            from src.platforms.kwork import get_kwork_service

            if cookies is None:
                service = get_kwork_service()
                fetch_cookies = getattr(service, "_fetch_session_hub_cookies", None)
                cookies = await fetch_cookies() if fetch_cookies else {}
            if not cookies:
                return {"status": "skipped", "query": clean_query, "detail": "missing Session Hub cookies", "suggestions": []}

            async with httpx.AsyncClient(
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Origin": KWORK_WEB_BASE_URL,
                    "Referer": f"{KWORK_WEB_BASE_URL}/projects",
                    "User-Agent": "Mozilla/5.0 PSR-KworkWantSuggest/1.0",
                    "X-Requested-With": "XMLHttpRequest",
                },
                cookies=cookies,
                timeout=_market_api_timeout(),
                follow_redirects=True,
                proxy=_market_http_proxy_url(rotate=False),
                trust_env=False,
            ) as client:
                response = await client.post(f"{KWORK_WEB_BASE_URL}/want-search/suggest", data={"query": clean_query})

            data = response.json() if "json" in response.headers.get("content-type", "").lower() else {}
            root = data.get("data") if isinstance(data, dict) else {}
            raw_suggestions = root.get("suggestions") if isinstance(root, dict) else []
            suggestions: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in _safe_list(raw_suggestions):
                if isinstance(item, dict):
                    suggestion = _clean_text(item.get("suggestion"), 120)
                    excerpt = _clean_text(item.get("excerpt"), 160)
                else:
                    suggestion = _clean_text(item, 120)
                    excerpt = suggestion
                key = suggestion.casefold()
                if not suggestion or key in seen:
                    continue
                seen.add(key)
                suggestions.append({"suggestion": suggestion, "excerpt": excerpt or suggestion})
                if len(suggestions) >= limit:
                    break
            return {
                "status": "ok" if response.status_code == 200 else "http_error",
                "query": clean_query,
                "status_code": response.status_code,
                "suggestions": suggestions,
            }
        except Exception as exc:
            return {
                "status": "error",
                "query": clean_query,
                "detail": f"{type(exc).__name__}: {exc}",
                "suggestions": [],
            }

    async def build_buyer_query_suggestions(
        self,
        probes: list[dict[str, Any]],
        *,
        max_queries: int = 5,
        suggestion_limit: int = 6,
    ) -> list[dict[str, Any]]:
        unique_queries: list[str] = []
        for probe in probes:
            query = _clean_text(probe.get("query"), 120)
            if len(query) < 2 or query.casefold() in {item.casefold() for item in unique_queries}:
                continue
            unique_queries.append(query)
            if len(unique_queries) >= max(1, min(max_queries, 10)):
                break
        results: list[dict[str, Any]] = []
        cookies: dict[str, str] | None = None
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            fetch_cookies = getattr(service, "_fetch_session_hub_cookies", None)
            cookies = await fetch_cookies() if fetch_cookies else {}
        except Exception:
            cookies = None
        for query in unique_queries:
            result = await self.fetch_want_search_suggestions(query, limit=suggestion_limit, cookies=cookies)
            results.append(result)
        return results

    async def fetch_project_detail(self, project_id: Any) -> dict[str, Any]:
        project_id_int = _as_int(project_id)
        if not project_id_int:
            return {"status": "skipped", "detail": "missing project id"}
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            get_project_details_raw = getattr(service, "get_project_details_raw", None)
            data = await get_project_details_raw(project_id_int) if get_project_details_raw else None
            if not isinstance(data, dict):
                get_token_api = getattr(service, "get_token_api", None)
                api = await get_token_api() if get_token_api else await service.get_api()
                if not api:
                    return {"status": "error", "detail": "Kwork API is unavailable"}
                raw = await api.request("post", "project", use_token=True, id=project_id_int)
                data = raw.get("response") if isinstance(raw, dict) else None
            if not isinstance(data, dict):
                return {"status": "empty", "id": project_id_int}
            summary = self._summarize_project(data)
            summary["status"] = "ok"
            summary["raw_keys"] = sorted(data.keys())
            return summary
        except Exception as exc:
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}", "id": project_id_int}

    @staticmethod
    def summarize_history_project(project: Any) -> dict[str, Any]:
        platform_data = project.platform_data if isinstance(getattr(project, "platform_data", None), dict) else {}
        user = platform_data.get("user") if isinstance(platform_data.get("user"), dict) else {}
        return {
            "id": getattr(project, "id", None),
            "title": _clean_text(getattr(project, "title", ""), 220),
            "description": _clean_text(getattr(project, "description", ""), 700),
            "price": getattr(project, "budget", None),
            "possible_price_limit": platform_data.get("possible_price_limit"),
            "offers": _as_int(getattr(project, "offers_count", None)),
            "date_create": getattr(project, "created_at", ""),
            "category_id": _as_int(platform_data.get("category_id")),
            "classification_id": _as_int(platform_data.get("classification_id")),
            "views": platform_data.get("views_dirty"),
            "user_id": _as_int(getattr(project, "client_user_id", None)),
            "username": user.get("username"),
            "user_hired_percent": _as_int(getattr(project, "client_hired_percent", None)),
            "user_projects_count": _as_int(platform_data.get("user_projects_count")),
            "user_active_projects_count": _as_int(platform_data.get("user_active_projects_count")),
            "url": getattr(project, "url", ""),
        }

    async def fetch_buyer_history(
        self,
        username: Any,
        *,
        page: int = 1,
        limit: int = 8,
        cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        clean_username = str(username or "").strip().strip("/")
        if not clean_username:
            return {"status": "skipped", "detail": "missing buyer username", "projects": []}
        page = max(1, min(_as_int(page, default=1), 20))
        limit = max(1, min(_as_int(limit, default=8), 50))
        try:
            from src.platforms.kwork import KworkStateDataParser, get_kwork_service

            if cookies is None:
                service = get_kwork_service()
                fetch_cookies = getattr(service, "_fetch_session_hub_cookies", None)
                cookies = await fetch_cookies() if fetch_cookies else {}
            url = f"{KWORK_WEB_BASE_URL}/projects/list/{clean_username}"
            params = {"page": page} if page > 1 else {}
            async with httpx.AsyncClient(
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "User-Agent": "Mozilla/5.0 PSR-KworkBuyerHistory/1.0",
                },
                cookies=cookies or None,
                timeout=_market_api_timeout(),
                follow_redirects=True,
                proxy=_market_http_proxy_url(rotate=False),
                trust_env=False,
            ) as client:
                response = await client.get(url, params=params)

            state = KworkStateDataParser.extract(response.text) or {}
            wants_root = state.get("wants") if isinstance(state.get("wants"), dict) else {}
            projects = KworkStateDataParser.projects_from_state(state)
            summaries = [self.summarize_history_project(project) for project in projects[:limit]]
            return {
                "status": "ok" if response.status_code == 200 else "http_error",
                "username": clean_username,
                "url": str(response.url),
                "status_code": response.status_code,
                "projects_count": len(projects),
                "returned_count": len(summaries),
                "total": _as_int(wants_root.get("total") or state.get("wantsCount") or len(projects)),
                "active_count": _as_int(state.get("newWantsCount") or wants_root.get("active_count")),
                "projects": summaries,
            }
        except Exception as exc:
            return {
                "status": "error",
                "username": clean_username,
                "detail": f"{type(exc).__name__}: {exc}",
                "projects": [],
            }

    async def get_buyer_scout(
        self,
        *,
        probes: list[dict[str, Any]] | None = None,
        max_probes: int = 10,
        page: int = 1,
        project_page_limit: int = 1,
        per_probe_limit: int = 12,
        top_limit: int = 20,
        include_project_details: bool = True,
        include_want_details: bool = True,
        include_buyer_history: bool = False,
        detail_limit: int = 8,
        buyer_history_limit: int = 6,
        budget_max: int | None = BUYER_DEFAULT_PRICE_TO,
        include_query_suggestions: bool = True,
        query_suggestion_limit: int = 5,
        write_file: bool = False,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Collect ranked buyer lots from the Kwork exchange APIs."""
        started = time.monotonic()
        selected_probes = probes if probes is not None else DEFAULT_BUYER_SCOUT_PROBES
        selected_probes = [dict(item) for item in selected_probes if isinstance(item, dict)][: max(1, min(max_probes, 30))]
        page = max(1, min(page, 5))
        project_page_limit = max(1, min(_as_int(project_page_limit, default=1), 3))
        per_probe_limit = max(1, min(per_probe_limit, 50))
        top_limit = max(1, min(top_limit, 100))
        detail_limit = max(0, min(detail_limit, top_limit, 20))
        buyer_history_limit = max(0, min(buyer_history_limit, top_limit, 20))
        budget_cap = max(0, min(_as_int(budget_max, default=BUYER_DEFAULT_PRICE_TO), BUYER_REASONABLE_BUDGET_MAX))
        query_suggestion_limit = max(0, min(_as_int(query_suggestion_limit, default=5), 20))
        if budget_cap:
            for probe in selected_probes:
                probe.setdefault("price_to", budget_cap)

        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
        except Exception as exc:
            return {
                "generated_at": _utc_timestamp(),
                "source": "psr.kwork_buyer_scout",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "probes": [],
                "top": [],
            }

        probe_results: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        endpoint_errors: list[dict[str, Any]] = []
        project_request_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        slow_empty_streak = 0
        for probe in selected_probes:
            probe_started = time.monotonic()
            name = str(probe.get("name") or probe.get("query") or probe.get("categories") or "probe")
            categories = str(probe.get("categories") or "all")
            query = str(probe.get("query") or "")
            filters = self._buyer_probe_filters(probe)
            row: dict[str, Any] = {"name": name, "categories": categories, "query": query, "filters": filters}
            token_required_stop = False
            try:
                projects: list[dict[str, Any]] = []
                first_meta: dict[str, Any] = {}
                page_rows: list[dict[str, Any]] = []
                max_probe_items = per_probe_limit * project_page_limit
                for page_offset in range(project_page_limit):
                    current_page = page + page_offset
                    cache_key = _stable_json(
                        {
                            "categories": categories,
                            "page": current_page,
                            "query": query,
                            "filters": filters,
                        }
                    )
                    cache_hit = cache_key in project_request_cache
                    if cache_hit:
                        page_projects, page_meta = project_request_cache[cache_key]
                        page_meta = copy.deepcopy(page_meta)
                        page_meta["cache_hit"] = True
                    else:
                        page_projects, page_meta = await service.get_raw_projects(
                            categories=categories,
                            page=current_page,
                            query=query,
                            **filters,
                        )
                        project_request_cache[cache_key] = (list(page_projects), copy.deepcopy(page_meta))
                    if not first_meta:
                        first_meta = page_meta if isinstance(page_meta, dict) else {}
                    page_paging = (
                        page_meta.get("paging") if isinstance(page_meta, dict) and isinstance(page_meta.get("paging"), dict) else {}
                    )
                    page_rows.append(
                        {
                            "page": current_page,
                            "count": _as_int(page_paging.get("total") or page_meta.get("total") or len(page_projects), default=len(page_projects))
                            if isinstance(page_meta, dict)
                            else len(page_projects),
                            "sample_count": len(page_projects),
                            "cache_hit": cache_hit,
                            "source": page_meta.get("source") if isinstance(page_meta, dict) else None,
                        }
                    )
                    projects.extend([item for item in page_projects if isinstance(item, dict)])
                    if isinstance(page_meta, dict) and page_meta.get("token_required"):
                        token_required_stop = True
                        break
                    total_available = _as_int(page_paging.get("total") or page_meta.get("total"), default=0) if isinstance(page_meta, dict) else 0
                    if not page_projects or (total_available and len(projects) >= total_available) or len(projects) >= max_probe_items:
                        break
                projects = projects[:max_probe_items]
                meta = first_meta
                paging = meta.get("paging") if isinstance(meta, dict) and isinstance(meta.get("paging"), dict) else {}
                count = _as_int(
                    paging.get("total")
                    or paging.get("count")
                    or meta.get("total")
                    or meta.get("count")
                    or len(projects),
                    default=len(projects),
                )
                summarized_projects = [self._summarize_project(item) for item in projects if isinstance(item, dict)]
                sample = summarized_projects[:per_probe_limit]
                for project in summarized_projects:
                    project_id = str(project.get("id") or "").strip()
                    if not project_id:
                        continue
                    scored = {**project, **self.score_buyer_project(project, budget_max=budget_cap), "matched_probe": name}
                    existing = seen.get(project_id)
                    if existing is None or scored.get("score", 0) > existing.get("score", 0):
                        seen[project_id] = scored
                row.update(
                    {
                        "status": "ok" if count or projects else "empty",
                        "count": count,
                        "sample_count": len(projects),
                        "sample": sample,
                        "meta": meta,
                        "pages": page_rows,
                    }
                )
                if token_required_stop or (isinstance(meta, dict) and meta.get("token_required")):
                    row["status"] = "skipped"
                    token_required_stop = True
                    endpoint_errors.append(
                        {
                            "probe": name,
                            "detail": _clean_text(meta.get("detail") or "token-mode auth required for project API", 240),
                        }
                    )
            except Exception as exc:
                error = {"probe": name, "detail": f"{type(exc).__name__}: {exc}"}
                endpoint_errors.append(error)
                row.update({"status": "error", **error})
            row["timings_ms"] = {"total": int((time.monotonic() - probe_started) * 1000)}
            probe_results.append(row)
            if token_required_stop:
                break
            if row.get("status") == "empty" and row["timings_ms"]["total"] > 3000:
                slow_empty_streak += 1
            else:
                slow_empty_streak = 0
            if slow_empty_streak >= 2:
                endpoint_errors.append(
                    {
                        "probe": name,
                        "detail": "stopped buyer project fan-out after repeated slow empty responses",
                    }
                )
                break

        ranked = sorted(seen.values(), key=lambda item: item.get("score", 0), reverse=True)[:top_limit]
        buyer_history_cache: dict[str, dict[str, Any]] = {}
        for project in ranked[:detail_limit]:
            if include_project_details:
                project["project_detail"] = await self.fetch_project_detail(project.get("id"))
            if include_want_details:
                project["want_detail"] = await self.fetch_want_detail(project.get("id"))
        if include_buyer_history and buyer_history_limit > 0:
            for project in ranked[:buyer_history_limit]:
                username = _clean_text(
                    project.get("username")
                    or (project.get("project_detail") or {}).get("username")
                    or (project.get("want_detail") or {}).get("username"),
                    80,
                )
                if not username:
                    continue
                if username not in buyer_history_cache:
                    buyer_history_cache[username] = await self.fetch_buyer_history(username, limit=6)
                project["buyer_history"] = buyer_history_cache[username]
        query_suggestions = (
            await self.build_buyer_query_suggestions(selected_probes, max_queries=5, suggestion_limit=query_suggestion_limit)
            if include_query_suggestions and query_suggestion_limit > 0
            else []
        )
        buyer_summary = self.build_buyer_summary(
            ranked,
            probe_results,
            query_suggestions,
            budget_max=budget_cap,
        )

        snapshot: dict[str, Any] = {
            "generated_at": _utc_timestamp(),
            "source": "psr.kwork_buyer_scout",
            "status": "ok" if probe_results and any(item.get("status") == "ok" for item in probe_results) else "empty",
            "config": {
                "max_probes": max_probes,
                "page": page,
                "project_page_limit": project_page_limit,
                "per_probe_limit": per_probe_limit,
                "top_limit": top_limit,
                "include_project_details": include_project_details,
                "include_want_details": include_want_details,
                "include_buyer_history": include_buyer_history,
                "detail_limit": detail_limit,
                "buyer_history_limit": buyer_history_limit,
                "budget_max": budget_cap,
                "include_query_suggestions": include_query_suggestions,
                "query_suggestion_limit": query_suggestion_limit,
            },
            "probes": probe_results,
            "top": ranked,
            "aggregate": {
                "probe_count": len(probe_results),
                "unique_projects": len(seen),
                "zero_offer_count": sum(1 for item in seen.values() if item.get("offers") == 0),
                "low_offer_count": sum(1 for item in seen.values() if 0 <= (item.get("offers") or 999) <= 5),
                "top_score": ranked[0].get("score") if ranked else None,
                "market_signals": self.build_buyer_market_signals(ranked, probe_results, budget_max=budget_cap),
                "query_suggestions": query_suggestions,
                "buyer_summary": buyer_summary,
            },
            "endpoint_errors": endpoint_errors,
            "timings_ms": {"total": int((time.monotonic() - started) * 1000)},
        }

        if write_file:
            root = Path(output_dir) if output_dir else Path("docs")
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"kwork_buyer_scout_{_snapshot_filename_timestamp()}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            snapshot["file_path"] = str(path)
        return snapshot

    @staticmethod
    def summarize_account_context(data: dict[str, Any]) -> dict[str, Any]:
        actor_response = data.get("actor")
        actor = actor_response.get("response") if isinstance(actor_response, dict) else actor_response
        actor = actor if isinstance(actor, dict) else {}
        info_response = data.get("getActorInfo")
        actor_info = info_response.get("response") if isinstance(info_response, dict) else info_response
        actor_info = actor_info if isinstance(actor_info, dict) else {}
        statuses_response = data.get("kworksStatusList")
        statuses = statuses_response.get("response") if isinstance(statuses_response, dict) else statuses_response
        offers_response = data.get("offers")
        offers_root = offers_response.get("response") if isinstance(offers_response, dict) else offers_response
        offers = _safe_list(offers_root if isinstance(offers_root, list) else offers_root.get("offers") if isinstance(offers_root, dict) else [])

        worker = actor.get("worker") if isinstance(actor.get("worker"), dict) else actor
        status_items = _safe_list(statuses)
        status_counts: dict[str, int] = {}
        for item in status_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("status") or item.get("name") or item.get("type") or item.get("id") or "unknown")
            status_counts[key] = _as_int(item.get("count") or item.get("kworks_count") or item.get("total"))

        return {
            "status": "ok" if actor or actor_info or status_items or offers else "empty",
            "username": worker.get("username") or actor_info.get("username"),
            "worker_id": worker.get("id") or actor_info.get("id"),
            "level": worker.get("level_description") or worker.get("level"),
            "rating": worker.get("rating") or actor_info.get("rating"),
            "reviews_count": worker.get("reviews_count") or actor_info.get("reviews_count"),
            "active_kworks_count": _as_int(actor.get("active_kworks_count") or actor_info.get("active_kworks_count")),
            "statuses": status_counts,
            "offers_count": len(offers),
            "connects": offers_response.get("connects") if isinstance(offers_response, dict) else None,
            "raw_sections": sorted(data.keys()),
        }

    async def fetch_account_context(self) -> dict[str, Any]:
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            get_token_api = getattr(service, "get_token_api", None)
            api = await get_token_api() if get_token_api else await service.get_api()
            if not api:
                return {"status": "error", "detail": "Kwork API is unavailable"}
            result: dict[str, Any] = {}
            for endpoint in ("actor", "getActorInfo", "kworksStatusList", "offers"):
                try:
                    result[endpoint] = await api.request("post", endpoint, use_token=True)
                except Exception as exc:
                    result[endpoint] = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            summary = self.summarize_account_context(result)
            errors = {
                key: value.get("error")
                for key, value in result.items()
                if isinstance(value, dict) and value.get("error")
            }
            if errors:
                summary["errors"] = errors
                summary["status"] = "partial" if summary.get("status") == "ok" else "error"
            return summary
        except Exception as exc:
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    async def _get_projects_snapshot(
        self,
        *,
        categories: str,
        page: int = 1,
        query: str = "",
        limit: int = 5,
        include_want_details: bool = False,
        want_detail_limit: int = 2,
    ) -> dict[str, Any]:
        try:
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            wants_count = await service.get_wants_count(categories=categories, query=query)
            projects, meta = await service.get_raw_projects(categories=categories, page=page, query=query)
            sample = [self._summarize_project(item) for item in projects[:limit] if isinstance(item, dict)]
            if include_want_details and sample:
                for project in sample[: max(0, want_detail_limit)]:
                    project["want_detail"] = await self.fetch_want_detail(project.get("id"))
            return {
                "status": "ok" if wants_count or projects else "empty",
                "wants_count": wants_count,
                "sample_count": len(projects),
                "sample": sample,
                "meta": meta,
                "categories": categories,
                "query": query,
                "want_detail_limit": max(0, want_detail_limit) if include_want_details else 0,
            }
        except Exception as exc:
            return {
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "categories": categories,
                "query": query,
            }

    async def get_market_intelligence_snapshot(
        self,
        *,
        seeds: list[dict[str, Any]] | None = None,
        max_seeds: int = 8,
        pages: int = 1,
        include_demand: bool = True,
        demand_queries: list[str] | None = None,
        include_competitor_details: bool = False,
        competitor_detail_limit: int = 0,
        include_seller_details: bool = False,
        seller_detail_limit: int = 8,
        include_want_details: bool = False,
        want_detail_limit: int = 2,
        include_price_rules: bool = False,
        include_account_context: bool = False,
        write_file: bool = False,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Collect a repeatable whole-market supply/demand snapshot."""
        started = time.monotonic()
        max_seeds = max(1, min(max_seeds, 30))
        pages = max(1, min(pages, 3))

        resolved_seeds: list[dict[str, Any]]
        if seeds:
            resolved_seeds = [item for item in (_normalize_market_seed(seed) for seed in seeds) if item]
        else:
            try:
                resolved_seeds = await asyncio.wait_for(
                    self.get_catalog_market_seeds(limit=max_seeds, include_taxonomy=False),
                    timeout=_seed_discovery_timeout(),
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                logger.debug(f"KworkMarket: catalog seed discovery timed out: {exc}")
                resolved_seeds = [copy.deepcopy(item) for item in DEFAULT_MARKET_INTELLIGENCE_SEEDS]
            except Exception as exc:
                logger.debug(f"KworkMarket: catalog seed discovery failed: {exc}")
                resolved_seeds = [copy.deepcopy(item) for item in DEFAULT_MARKET_INTELLIGENCE_SEEDS]
        resolved_seeds = resolved_seeds[:max_seeds]

        supply: list[dict[str, Any]] = []
        seller_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        all_cards: list[dict[str, Any]] = []
        cards_seen = 0
        for seed in resolved_seeds:
            seed_started = time.monotonic()
            seed_cards: list[dict[str, Any]] = []
            kworks_count = 0
            classifiers: list[dict[str, Any]] = []
            for page in range(1, pages + 1):
                catalog = await self.get_kworks(
                    category_id=_as_int(seed.get("category_id")) or None,
                    classifier_id=_as_int(seed.get("classifier_id")) or None,
                    page=page,
                )
                if page == 1:
                    kworks_count = _as_int(catalog.get("kworks_count") or catalog.get("count"))
                    classifiers = self.summarize_classifiers(_safe_list(catalog.get("classifiers")))[:12]
                competitors = self.summarize_competitors(_safe_list(catalog.get("kworks")), limit=12)
                if include_competitor_details and competitor_detail_limit > 0:
                    competitors = await self.enrich_competitors(competitors, limit=competitor_detail_limit)
                seed_cards.extend(competitors)

            for card in seed_cards:
                all_cards.append(card)
                cards_seen += 1
                worker = str(card.get("worker") or "")
                if worker:
                    seller_counts[worker] = seller_counts.get(worker, 0) + 1
                category_id = card.get("raw", {}).get("category_id") if isinstance(card.get("raw"), dict) else None
                if category_id:
                    key = str(category_id)
                    category_counts[key] = category_counts.get(key, 0) + 1

            demand = {"status": "skipped"}
            category_id = _as_int(seed.get("category_id"))
            if include_demand and category_id:
                demand = await self._get_projects_snapshot(
                    categories=str(category_id),
                    limit=5,
                    include_want_details=include_want_details,
                    want_detail_limit=want_detail_limit,
                )

            price_rules = {"status": "skipped"}
            if include_price_rules and category_id:
                try:
                    price_rules = self.summarize_price_rules(await self.get_price_rules(category_id))
                except Exception as exc:
                    price_rules = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

            supply.append(
                {
                    "seed": seed,
                    "kworks_count": kworks_count,
                    "classifiers": classifiers,
                    "cards": seed_cards[:12],
                    "demand": demand,
                    "price_rules": price_rules,
                    "timings_ms": {"total": int((time.monotonic() - seed_started) * 1000)},
                }
            )

        query_demand: dict[str, Any] = {}
        if include_demand:
            for query in (demand_queries if demand_queries is not None else DEFAULT_MARKET_INTELLIGENCE_QUERIES):
                query = str(query or "").strip()
                if not query:
                    continue
                query_demand[query] = await self._get_projects_snapshot(
                    categories="all",
                    query=query,
                    limit=5,
                    include_want_details=include_want_details,
                    want_detail_limit=want_detail_limit,
                )

        seller_intelligence: list[dict[str, Any]] = []
        if include_seller_details and seller_detail_limit > 0:
            seller_intelligence = await self.enrich_sellers_from_competitors(
                all_cards,
                limit=max(1, min(seller_detail_limit, 20)),
            )

        account_context = {"status": "skipped"}
        if include_account_context:
            account_context = await self.fetch_account_context()

        market_rankings = self.build_market_rankings(supply)

        snapshot: dict[str, Any] = {
            "generated_at": _utc_timestamp(),
            "source": "psr.kwork_market_intelligence_snapshot",
            "config": {
                "max_seeds": max_seeds,
                "pages": pages,
                "include_demand": include_demand,
                "include_competitor_details": include_competitor_details,
                "competitor_detail_limit": competitor_detail_limit,
                "include_seller_details": include_seller_details,
                "seller_detail_limit": seller_detail_limit,
                "include_want_details": include_want_details,
                "want_detail_limit": want_detail_limit,
                "include_price_rules": include_price_rules,
                "include_account_context": include_account_context,
            },
            "seeds": resolved_seeds,
            "supply": supply,
            "query_demand": query_demand,
            "seller_intelligence": seller_intelligence,
            "account_context": account_context,
            "market_rankings": market_rankings,
            "aggregate": {
                "seed_count": len(resolved_seeds),
                "cards_seen": cards_seen,
                "unique_sellers_seen": len(seller_counts),
                "seller_profiles_collected": len(seller_intelligence),
                "top_opportunities": market_rankings[:10],
                "top_sellers": sorted(seller_counts.items(), key=lambda item: item[1], reverse=True)[:20],
                "category_counts": sorted(category_counts.items(), key=lambda item: item[1], reverse=True)[:20],
            },
            "timings_ms": {"total": int((time.monotonic() - started) * 1000)},
        }

        if write_file:
            root = Path(output_dir) if output_dir else Path("docs") / "kwork_market_snapshots"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"kwork_market_intelligence_{_snapshot_filename_timestamp()}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            snapshot["file_path"] = str(path)
            summary = _compact_market_snapshot_summary(snapshot)
            latest_path = root / "latest.json"
            index_path = root / "index.jsonl"
            latest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            with index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
            snapshot["latest_path"] = str(latest_path)
            snapshot["index_path"] = str(index_path)
        return snapshot
