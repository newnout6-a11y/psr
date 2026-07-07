"""Kwork market/category intelligence helpers.

This module intentionally uses Kwork JSON/API endpoints instead of browser
automation. Authenticated demand calls are optional and degrade gracefully when
Session Hub cookies are not available.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from html import unescape
from typing import Any

import httpx
from loguru import logger

KWORK_WEB_BASE_URL = "https://kwork.ru"
KWORK_CDN_BASE_URL = "https://cdn-edge.kwork.ru"
STATE_MARKER = "window.stateData="
ATTRIBUTE_FILTER_RE = re.compile(r"^(attribute\[\d+\](?:\[\])?)$")
MARKET_METRICS_CACHE_TTL = 120.0
COMPETITOR_DETAIL_CACHE_TTL = 900.0
_MARKET_METRICS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKET_METRICS_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_MARKET_CACHE_LOCK = asyncio.Lock()
_COMPETITOR_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


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


def _clean_text(value: Any, limit: int = 0) -> str:
    text = str(value or "")
    if "<" in text or "&" in text:
        try:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


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

            self._api = Kwork(login="", password="", timeout=30, retry_max_attempts=2)
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
        data = await api.request("post", endpoint, **params)
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
            timeout=20.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.get(f"/{endpoint}", params=params)
            response.raise_for_status()
            return response.json() if response.content else {}

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
                    "description": "",
                    "instruction": "",
                    "service_size": "",
                    "practice_context": "",
                    "detail_status": "pending" if share_url else "missing_url",
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
        targets = [item for item in competitors[: max(0, limit)] if item.get("share_url")]
        if not targets:
            return competitors

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 PSR-KworkMarket/1.0",
        }
        results: list[dict[str, Any] | Exception] = []
        detail_delay = _float_env("KWORK_COMPETITOR_DETAIL_DELAY", 0.8)
        async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True, trust_env=False) as client:
            for index, item in enumerate(targets):
                cache_key = str(item.get("share_url") or item.get("id") or "")
                cached = _cache_get(_COMPETITOR_DETAIL_CACHE, cache_key, COMPETITOR_DETAIL_CACHE_TTL)
                if cached is not None:
                    results.append(cached)
                    continue
                try:
                    result = await self.fetch_competitor_detail(client, item)
                    _cache_set(_COMPETITOR_DETAIL_CACHE, cache_key, result)
                    results.append(result)
                except Exception as exc:
                    results.append(exc)
                if detail_delay > 0 and index < len(targets) - 1:
                    await asyncio.sleep(detail_delay)

        by_id: dict[Any, dict[str, Any]] = {}
        for item, result in zip(targets, results):
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

    async def fetch_competitor_detail(
        self,
        client: httpx.AsyncClient,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.get(str(competitor["share_url"]))
        response.raise_for_status()
        html = response.text
        state = _extract_state_data(html)
        kwork = state.get("kwork") if isinstance(state.get("kwork"), dict) else {}

        title = _clean_text(kwork.get("gtitle") or kwork.get("title") or competitor.get("title"), 220)
        description = _clean_text(kwork.get("gdesc") or _meta_content(html, "og:description"), 1400)
        instruction = _clean_text(kwork.get("ginst"), 420)
        service_size = _clean_text(kwork.get("gwork"), 260)
        image_url = competitor.get("image_url") or _cdn_photo_url(kwork.get("photo") or kwork.get("photoFile"))
        price = kwork.get("minVolumePrice") or kwork.get("displayedPrice") or competitor.get("price")
        worker = _clean_text(kwork.get("username") or competitor.get("worker"), 80)

        practice_parts = [
            title,
            f"Цена: {price} ₽" if price else "",
            f"Продавец: {worker}" if worker else "",
            f"Объем: {service_size}" if service_size else "",
            f"Описание: {description}" if description else "",
            f"Что просит у клиента: {instruction}" if instruction else "",
        ]
        return {
            "title": title or competitor.get("title"),
            "price": price or competitor.get("price"),
            "worker": worker or competitor.get("worker"),
            "image_url": image_url,
            "description": description,
            "instruction": instruction,
            "service_size": service_size,
            "queue_count": _as_int(kwork.get("queueCount")),
            "work_time_seconds": _as_int(kwork.get("avgWorkTime")),
            "practice_context": " | ".join(part for part in practice_parts if part),
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
        include_demand: bool = True,
        include_competitor_details: bool = True,
        competitor_detail_limit: int = 2,
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
        include_demand: bool = True,
        include_competitor_details: bool = True,
        competitor_detail_limit: int = 2,
        attribute_filters: dict[str, Any] | None = None,
        attribute_controls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        filter_scope = describe_attribute_filter_scope(attribute_filters, controls=attribute_controls)
        effective_classifier_ids = selected_classifier_ids_from_attributes(
            attribute_filters,
            controls=attribute_controls,
            fallback_classifier_id=classifier_id,
        )
        effective_classifier_ids = effective_classifier_ids[: _market_max_fanout()]
        filter_scope["effective_classifier_ids"] = effective_classifier_ids
        catalog = await self.get_kworks(
            category_id=category_id,
            classifier_id=classifier_id,
            page=page,
            attribute_filters=filter_scope["params"],
            attribute_controls=attribute_controls,
        )
        kworks = _safe_list(catalog.get("kworks"))
        classifiers = self.summarize_classifiers(_safe_list(catalog.get("classifiers")))
        competitors = self.summarize_competitors(kworks)
        if include_competitor_details:
            competitors = await self.enrich_competitors(competitors, limit=competitor_detail_limit)
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
        }

        if include_demand and category_id:
            metrics["demand"] = await self.get_demand_snapshot(
                category_id,
                filters=filter_scope["params"],
                scope=filter_scope,
                classifier_ids=effective_classifier_ids,
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
                    "sample": projects[:5],
                    "meta": meta,
                    "scope": scope or describe_attribute_filter_scope(filters),
                    "filter_params": request_filters,
                }
            return {
                "status": "needs_cookies",
                "label": "нужны куки",
                "wants_count": 0,
                "sample_count": 0,
                "detail": "Kwork не вернул заказы без авторизованных cookies Session Hub.",
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
