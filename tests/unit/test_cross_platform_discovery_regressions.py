"""Regression coverage for asynchronous cross-platform discovery.

All network calls are replaced with local fakes; no live site is contacted.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestrator import FreelanceOrchestrator
from src.parsers.freelanceru_parser import FreelanceRuParser
from src.parsers.hh_parser import HHParser


@pytest.mark.asyncio
async def test_hh_list_and_details_are_rate_limited_and_do_not_block_event_loop() -> None:
    parser = HHParser.__new__(HHParser)
    parser.rate_limiter = SimpleNamespace(wait=AsyncMock())
    caller_thread = threading.get_ident()
    request_threads: list[int] = []
    requests: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        request_threads.append(threading.get_ident())
        requests.append((url, kwargs))
        return SimpleNamespace(
            json=lambda: (
                {"items": [{"id": "123", "name": "Backend", "snippet": {}, "alternate_url": "https://hh.ru/vacancy/123"}]}
                if url.endswith("/vacancies")
                else {"id": "123", "name": "Backend", "snippet": {}, "alternate_url": "https://hh.ru/vacancy/123"}
            )
        )

    parser._safe_get = fake_get
    projects = await parser.get_projects(page=2, filters={"query": "python"})
    detail = await parser.get_project_details("123")

    assert projects[0].id == detail.id == "123"
    assert requests[0][1]["params"]["page"] == 1
    assert parser.rate_limiter.wait.await_count == 2
    assert request_threads and all(thread != caller_thread for thread in request_threads)


@pytest.mark.asyncio
async def test_freelance_listing_keeps_throttle_without_blocking_event_loop() -> None:
    parser = FreelanceRuParser.__new__(FreelanceRuParser)
    parser.rate_limiter = SimpleNamespace(wait=AsyncMock())
    caller_thread = threading.get_ident()
    seen_threads: list[int] = []

    def fake_get(url: str, **kwargs):
        seen_threads.append(threading.get_ident())
        assert url == FreelanceRuParser.API_URL
        assert kwargs["params"]["keyword"] == "backend"
        return SimpleNamespace(text="<html><body></body></html>")

    parser._safe_get = fake_get
    assert await parser.get_projects(filters={"query": "backend"}) == []
    parser.rate_limiter.wait.assert_awaited_once()
    assert seen_threads and seen_threads[0] != caller_thread


@pytest.mark.asyncio
async def test_standard_sources_round_robin_with_bounded_inflight_calls() -> None:
    orchestrator = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    started: list[tuple[str, str, int]] = []
    active = 0
    peak = 0
    full = asyncio.Event()
    release = asyncio.Event()

    async def fake_parse(parser, page, per_page, query):
        nonlocal active, peak
        started.append((parser.PLATFORM_NAME, query, page))
        active += 1
        peak = max(peak, active)
        if active == 3:
            full.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return parser.PLATFORM_NAME, query, page, [], 0, None

    orchestrator._parse_projects = fake_parse
    sources = [
        (SimpleNamespace(PLATFORM_NAME="kwork"), ["automation"]),
        (SimpleNamespace(PLATFORM_NAME="freelance_ru"), ["bot", " bot ", "backend"]),
        (SimpleNamespace(PLATFORM_NAME="hh_ru"), ["python"]),
    ]
    collecting = asyncio.create_task(
        orchestrator._collect_standard_pages(sources, per_page=20, pages_to_parse=2, max_concurrency=3)
    )
    try:
        await asyncio.wait_for(full.wait(), timeout=1.0)
        assert started[:3] == [
            ("kwork", "automation", 1),
            ("freelance_ru", "bot", 1),
            ("hh_ru", "python", 1),
        ]
        assert len(started) == 3
    finally:
        release.set()
        result = await collecting

    assert peak == 3
    assert len(result) == 8
    assert sum(item[0] == "kwork" for item in result) == 2
    assert sum(item[0] == "freelance_ru" for item in result) == 4
    assert sum(item[0] == "hh_ru" for item in result) == 2


@pytest.mark.asyncio
async def test_standard_discovery_cancellation_stops_pending_pages() -> None:
    orchestrator = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    entered = asyncio.Event()
    stop = asyncio.Event()
    attempts = 0

    async def fake_parse(parser, page, per_page, query):
        nonlocal attempts
        attempts += 1
        entered.set()
        await stop.wait()
        return parser.PLATFORM_NAME, query, page, [], 0, None

    orchestrator._parse_projects = fake_parse
    collecting = asyncio.create_task(
        orchestrator._collect_standard_pages(
            [(SimpleNamespace(PLATFORM_NAME="hh_ru"), ["backend"])],
            per_page=20,
            pages_to_parse=10,
            max_concurrency=1,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    collecting.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await collecting
    finally:
        stop.set()
    assert attempts == 1
