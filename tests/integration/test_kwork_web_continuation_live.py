"""Opt-in live contract gate for Kwork web catalog continuation.

Run explicitly with a valid session only:
```
KWORK_LIVE_WEB_CONTINUATION=1 KWORK_WEB_COOKIES_JSON='{"name":"value"}' \
  python -m pytest tests/integration/test_kwork_web_continuation_live.py -q
```
"""

from __future__ import annotations

import json
import os

import pytest

from src.platforms.kwork_market import KworkMarketClient
from src.platforms.kwork_supply import BatchState, ContractState
from src.platforms.kwork_supply.sources import KworkWebCatalogAdapter


pytestmark = pytest.mark.asyncio


def _live_enabled() -> bool:
    return os.getenv("KWORK_LIVE_WEB_CONTINUATION", "").strip().lower() in {"1", "true", "yes"}


def _live_cookies() -> dict[str, str]:
    raw = os.getenv("KWORK_WEB_COOKIES_JSON", "").strip()
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in payload.items()):
        raise ValueError("KWORK_WEB_COOKIES_JSON must be an object of string cookie values")
    return payload


@pytest.mark.skipif(not _live_enabled(), reason="set KWORK_LIVE_WEB_CONTINUATION=1 to run the live Kwork gate")
async def test_web_catalog_continuation_accepts_three_batches_without_overlap():
    alias = os.getenv("KWORK_LIVE_WEB_ALIAS", "website-repair").strip()
    category_id = int(os.getenv("KWORK_LIVE_WEB_CATEGORY_ID", "38"))
    proxy_url = os.getenv("KWORK_LIVE_WEB_PROXY_URL", "").strip() or None
    adapter = KworkWebCatalogAdapter(
        client=KworkMarketClient(proxy_url=proxy_url),
        cookies=_live_cookies(),
    )
    request = adapter.build_request(alias=alias, category_id=category_id)
    previous = None
    unique_ids: set[str] = set()

    try:
        for _ in range(3):
            result = await adapter.fetch_batch(request)
            verdict = adapter.validate_batch(request, result, previous)

            assert verdict.state is ContractState.ACCEPTED, verdict.reason_codes
            assert result.actual_item_count == len(result.cards)
            assert result.actual_item_count > 0
            unique_ids.update(str(card["id"]) for card in result.cards if card.get("id") is not None)
            previous = (previous or BatchState()).accept(result)
            assert result.next_cursor is not None
            request = adapter.build_request(alias=alias, category_id=category_id, cursor=result.next_cursor)
    finally:
        await adapter.client.close()

    assert len(unique_ids) >= 72
