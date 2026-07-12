from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.platforms import kwork_market_supply as supply_module
from src.platforms.kwork_market_supply import (
    KworkSupplyScanner,
    MarketAssistant,
    MarketAssistantStore,
    _SUPPLY_PAGE_CACHE,
    _chunk_market_listings,
    _llm_listing_evidence,
    _supply_proxy_pool,
)


class SupplyClient:
    def __init__(self, *, protection_on_slice: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.protection_on_slice = protection_on_slice

    async def close(self) -> None:
        return None

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append({"category_id": category_id, "classifier_id": classifier_id, "page": page})
        if classifier_id is None:
            return {
                "paging": {"page": page},
                "kworks_count": 1_000,
                "classifiers": [
                    {"id": 10, "name": "Slice A", "kworks_count": 600},
                    {"id": 20, "name": "Slice B", "kworks_count": 400},
                ],
                "kworks": [{"id": f"root-{index}"} for index in range(10)],
            }
        if classifier_id == self.protection_on_slice:
            raise RuntimeError("HTTP 429 Too Many Requests")
        cards = [
            {
                "id": f"{classifier_id}-{page}-{index}",
                "title": f"Offer {classifier_id}-{page}-{index}",
                "description": "Evidence from the selected API slice.",
                "price": 1_000 + index,
                "worker": {"username": f"seller-{index % 4}", "reviews_count": index},
                "unmodeled_api_field": "kept for LLM evidence",
            }
            for index in range(10)
        ]
        return {
            "paging": {"page": page},
            "kworks_count": 600 if classifier_id == 10 else 400,
            "kworks": cards,
        }


def test_supply_proxy_pool_accepts_existing_local_port_range(monkeypatch):
    monkeypatch.delenv("KWORK_MARKET_SUPPLY_PROXY_URLS", raising=False)
    monkeypatch.delenv("KWORK_PROXY_LIST", raising=False)
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_PROXY_PORTS", "17990-17999")

    urls, source = _supply_proxy_pool()

    assert source == "KWORK_MARKET_SUPPLY_PROXY_PORTS"
    assert urls == [f"http://127.0.0.1:{port}" for port in range(17990, 18000)]


def test_llm_evidence_uses_more_than_legacy_short_sample(monkeypatch):
    monkeypatch.setenv("KWORK_MARKET_LLM_MAX_LISTINGS", "5000")
    monkeypatch.setenv("KWORK_MARKET_LLM_MAX_EVIDENCE_CHARS", "200000")
    listings = [{"id": index, "title": f"Offer {index}", "api_card": {"field": index}} for index in range(700)]

    included, window = _llm_listing_evidence(listings)

    assert len(included) == 700
    assert window["raw_listing_count"] == 700
    assert window["included_listing_count"] == 700
    assert window["truncated"] is False


def test_full_market_chunking_preserves_every_raw_listing(monkeypatch):
    monkeypatch.setenv("KWORK_MARKET_ASSISTANT_MAP_CHARS", "100000")
    listings = [
        {"id": index, "title": f"Offer {index}", "raw_payload": str(index) * 60_000}
        for index in range(7)
    ]

    chunks = _chunk_market_listings(listings)

    assert len(chunks) == 7
    assert [item for chunk in chunks for item in chunk] == listings


@pytest.mark.asyncio
async def test_supply_scan_uses_existing_configured_transport_pool(monkeypatch):
    _SUPPLY_PAGE_CACHE.clear()
    created: list[SupplyClient] = []

    class PoolClient(SupplyClient):
        def __init__(self, api: Any | None = None, *, proxy_url: str | None = None) -> None:
            super().__init__()
            self.proxy_url = proxy_url
            created.append(self)

    monkeypatch.setattr(supply_module, "KworkMarketClient", PoolClient)
    monkeypatch.delenv("KWORK_MARKET_SUPPLY_PROXY_URLS", raising=False)
    monkeypatch.delenv("KWORK_PROXY_LIST", raising=False)
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_PROXY_PORTS", "17990-17999")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MIN_CARDS", "10")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MAX_REQUESTS", "50")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_WAVE_DELAY_MS", "0")

    result = await KworkSupplyScanner().scan(category_id=38, include_llm=False, write_file=False)

    assert result["transport"] == {
        "client_pool_size": 10,
        "source": "KWORK_MARKET_SUPPLY_PROXY_PORTS",
        "uses_configured_proxy_pool": True,
    }
    assert len({item["transport_slot"] for item in result["requests"]}) > 1
    assert {client.proxy_url for client in created if client.proxy_url} == {
        f"http://127.0.0.1:{port}" for port in range(17990, 18000)
    }


@pytest.mark.asyncio
async def test_supply_scanner_serializes_one_transport_slot():
    _SUPPLY_PAGE_CACHE.clear()

    class SlowClient(SupplyClient):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def get_kworks(self, **kwargs: Any) -> dict[str, Any]:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return {"kworks_count": 2, "kworks": [{"id": kwargs["page"], "title": "Offer"}]}
            finally:
                self.active -= 1

    client = SlowClient()
    scanner = KworkSupplyScanner(client=client)
    global_semaphore = asyncio.Semaphore(4)

    await asyncio.gather(
        scanner._collect_page(
            category_id=38,
            slice_info={"id": 10, "name": "Slice"},
            page=1,
            semaphore=global_semaphore,
        ),
        scanner._collect_page(
            category_id=38,
            slice_info={"id": 10, "name": "Slice"},
            page=2,
            semaphore=global_semaphore,
        ),
    )

    assert client.max_active == 1


@pytest.mark.asyncio
async def test_supply_scan_keeps_category_total_separate_from_observed_sample(monkeypatch):
    _SUPPLY_PAGE_CACHE.clear()
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MIN_CARDS", "10")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MAX_REQUESTS", "50")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_WAVE_DELAY_MS", "0")
    client = SupplyClient()
    scanner = KworkSupplyScanner(client=client)

    result = await scanner.scan(
        category_id=38,
        category_name="Site work",
        coverage="balanced",
        include_llm=False,
        write_file=False,
    )

    assert result["scope"]["reported_category_total"] == 1_000
    assert result["sample"]["observed_listings"] == 20
    assert result["coverage"]["minimum_cards_target_met"] is True
    assert result["coverage"]["slice_count_scanned"] == 2
    assert "seller_repetition_in_observed_cards" in result["sample"]
    assert "seller_repetition_in_sample" not in result["sample"]
    assert "concentration" not in str(result["sample"]).lower()
    assert result["raw_listings"][0]["api_card"]["unmodeled_api_field"] == "kept for LLM evidence"
    assert {call["category_id"] for call in client.calls} == {38}
    assert {call["classifier_id"] for call in client.calls if call["classifier_id"]} <= {10, 20}
    assert all(call["page"] == 1 for call in client.calls)
    assert result["coverage"]["mobile_continuation_policy"] == "first_page_only_until_validated_web_source"


@pytest.mark.asyncio
async def test_supply_scan_reports_first_seen_provenance_and_cursor_diagnostics(monkeypatch):
    _SUPPLY_PAGE_CACHE.clear()
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MIN_CARDS", "10")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MAX_REQUESTS", "50")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_WAVE_DELAY_MS", "0")

    result = await KworkSupplyScanner(client=SupplyClient()).scan(
        category_id=38,
        include_llm=False,
        write_file=False,
    )

    first_listing = result["raw_listings"][0]
    first_request = result["requests"][0]
    assert first_listing["first_seen_page"] == 1
    assert first_listing["observation_count"] == 1
    assert first_request["requested_cursor"] == {"kind": "page", "page": 1}
    assert first_request["reported_cursor"] == {"kind": "page", "page": 1}
    assert first_request["contract_state"] == "accepted"
    assert first_request["new_unique"] == 10
    assert first_request["duplicates"] == 0


@pytest.mark.asyncio
async def test_refresh_slice_never_schedules_unvalidated_mobile_pages():
    _SUPPLY_PAGE_CACHE.clear()
    client = SupplyClient()

    result = await KworkSupplyScanner(client=client).refresh_slice(
        category_id=38,
        classifier_id=10,
        classifier_name="Slice A",
        pages=3,
    )

    assert result["status"] == "ok"
    assert result["requested_pages"] == 3
    assert result["pages"] == 1
    assert [call["page"] for call in client.calls] == [1]
    assert result["requests"][0]["contract_state"] == "accepted"


@pytest.mark.asyncio
async def test_supply_scan_stops_after_protection_signal(monkeypatch):
    _SUPPLY_PAGE_CACHE.clear()
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MIN_CARDS", "500")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_MAX_REQUESTS", "50")
    monkeypatch.setenv("KWORK_MARKET_SUPPLY_WAVE_DELAY_MS", "0")
    scanner = KworkSupplyScanner(client=SupplyClient(protection_on_slice=10))

    result = await scanner.scan(category_id=38, include_llm=False, write_file=False)

    assert result["coverage"]["stopped_after_protection_signal"] is True
    assert result["coverage"]["request_errors"] >= 1
    assert any(item.get("failure_kind") == "protection" for item in result["requests"])


@pytest.mark.asyncio
async def test_market_assistant_runs_only_validated_narrow_refresh(monkeypatch):
    snapshot = {
        "scope": {"category_id": 38, "category_name": "Site work"},
        "coverage": {"observed_listing_count": 500},
        "slices": [{"id": 10, "name": "Slice A"}],
        "sample": {},
        "analysis": {"status": "ok"},
        "raw_listings": [],
    }
    context_id = MarketAssistantStore.create(snapshot)
    calls: list[dict[str, Any]] = []

    async def fake_generate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            return {
                "answer": "Need fresh data.",
                "refresh": {"classifier_id": 10, "pages": 2, "reason": "Need a current narrow sample."},
            }
        return {"answer": "Fresh answer based on the narrow sample."}

    async def fake_refresh(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["category_id"] == 38
        assert kwargs["classifier_id"] == 10
        assert kwargs["pages"] == 2
        return {
            "status": "ok",
            "refreshed_at": "2026-07-10T00:00:00Z",
            "raw_listings": [{"id": "fresh-1", "title": "Fresh evidence"}],
        }

    async def fake_close(self) -> None:
        return None

    monkeypatch.setattr(MarketAssistant, "_generate", staticmethod(fake_generate))
    monkeypatch.setattr(MarketAssistant, "_generate_with_tools", staticmethod(fake_generate))
    monkeypatch.setattr(KworkSupplyScanner, "refresh_slice", fake_refresh)
    monkeypatch.setattr(KworkSupplyScanner, "close", fake_close)

    try:
        result = await MarketAssistant().ask(context_id, "What changed?")
        follow_up = await MarketAssistant().ask(context_id, "What is still true?")
        stored = MarketAssistantStore.load(context_id)
    finally:
        path = MarketAssistantStore.path(context_id)
        if path.exists():
            path.unlink()

    assert result["answer"] == "Fresh answer based on the narrow sample."
    assert result["refresh"]["classifier_id"] == 10
    assert result["refresh"]["observed_listing_count"] == 1
    assert follow_up["answer"] == "Fresh answer based on the narrow sample."
    assert len(calls) == 3
    search = await calls[-1]["tool_handler"]("search_market_listings", {"query": "fresh-1", "limit": 10})
    assert search["match_count"] == 1
    assert stored["refreshes"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_full_market_analysis_processes_all_chunks(monkeypatch, tmp_path):
    monkeypatch.setenv("KWORK_MARKET_ASSISTANT_MAP_CHARS", "100000")
    monkeypatch.setenv("KWORK_MARKET_ASSISTANT_MAP_CONCURRENCY", "2")
    monkeypatch.setattr(supply_module, "_market_assistant_analysis_cache_root", lambda: tmp_path)
    listings = [
        {"id": index, "title": f"Offer {index}", "raw_payload": str(index) * 60_000}
        for index in range(4)
    ]
    mapped_prompts: list[str] = []

    class Client:
        async def generate(self, **kwargs: Any) -> str:
            if "map-этап" in kwargs["system_prompt"]:
                assert kwargs["model"] == "gpt-5.6-luna"
                assert kwargs["reasoning_effort"] == "high"
                mapped_prompts.append(kwargs["prompt"])
                return '{"facts":[],"patterns":[],"evidence_listing_ids":[],"caveats":[]}'
            assert "reduce-этап" in kwargs["system_prompt"]
            assert kwargs["model"] == "gpt-5.6-terra"
            assert kwargs["reasoning_effort"] == "high"
            return '{"answer":"Complete market answer"}'

    class Router:
        providers = {"openai": Client()}

        @staticmethod
        def _select_model(provider: str, task: str, explicit_model: str | None) -> str:
            assert provider == "openai"
            return "test-model"

    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: Router())

    result = await MarketAssistant()._analyze_full_market(
        question="Analyze everything",
        listings=listings,
        overview={"listing_count": 4},
    )

    mapped = "\n".join(mapped_prompts)
    assert all(f"Offer {index}" in mapped for index in range(4))
    assert result["listing_count"] == 4
    assert result["chunk_count"] == 4
    assert result["all_chunks_completed"] is True
    assert result["answer"] == "Complete market answer"

    first_map_count = len(mapped_prompts)
    await MarketAssistant()._analyze_full_market(
        question="Analyze everything",
        listings=listings,
        overview={"listing_count": 4},
    )
    assert len(mapped_prompts) == first_map_count
