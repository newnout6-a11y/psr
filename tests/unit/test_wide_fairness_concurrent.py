"""Cross-platform fairness in the wide discovery phase.

Use controlled sources and events to prove first-result ordering without
contacting Kwork, Freelance.ru, HH.ru or any persistent database.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.orchestrator import FreelanceOrchestrator
from src.parsers.base_parser import ProjectItem


def _project(platform: str, project_id: str) -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title=f"Job {project_id}",
        description="Need Python",
        url=f"https://example.invalid/{platform}/{project_id}",
        platform=platform,
        created_at="2026-10-30T08:00:00Z",
    )


def _sources():
    return [
        (SimpleNamespace(PLATFORM_NAME="kwork"), ["automation"]),
        (SimpleNamespace(PLATFORM_NAME="freelance_ru"), ["python"]),
        (SimpleNamespace(PLATFORM_NAME="hh_ru"), ["backend"]),
    ]


async def _collect(orch, *, sources=None, max_concurrency=3):
    return await orch._collect_discovery_pages(
        sources or _sources(),
        wide_mode=True,
        per_page=20,
        pages_to_parse=2,
        max_pages_per_query=3,
        max_projects_per_cycle=50,
        max_parse_seconds=30,
        max_concurrency=max_concurrency,
    )


@pytest.mark.asyncio
async def test_wide_kwork_does_not_delay_first_freelance_and_hh_results(capsys) -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    slow_kwork = asyncio.Event()
    others_complete = asyncio.Event()
    timestamps: dict[str, float] = {}
    active = 0
    peak = 0
    completed_others = 0
    started_at = time.perf_counter()

    async def fake_parse(parser, page, per_page, query):
        nonlocal active, peak, completed_others
        platform = parser.PLATFORM_NAME
        active += 1
        peak = max(peak, active)
        timestamps.setdefault(f"{platform}_start", time.perf_counter() - started_at)
        try:
            if platform == "kwork":
                if page == 1:
                    await slow_kwork.wait()
                    return platform, query, page, [_project(platform, "k1")], 0, None
                return platform, query, page, [], 0, None
            await asyncio.sleep(0.02)
            completed_others += 1
            timestamps.setdefault(f"{platform}_done", time.perf_counter() - started_at)
            if completed_others == 2:
                others_complete.set()
            return platform, query, page, [_project(platform, f"{page}")], 20, None
        finally:
            active -= 1

    orch._parse_projects = fake_parse
    discovery = asyncio.create_task(_collect(orch))
    try:
        await asyncio.wait_for(others_complete.wait(), timeout=1.5)
        assert not discovery.done(), "Kwork's slow first request is still pending"
        assert peak == 3
        assert timestamps["freelance_ru_done"] < 0.5
        assert timestamps["hh_ru_done"] < 0.5
        assert "kwork_start" in timestamps
    finally:
        slow_kwork.set()
    wide, wide_stats, standard = await discovery
    assert [row[0] for row in wide] == ["kwork", "kwork"]
    assert wide_stats["kwork"]["query_stops"]["automation"] == "empty_page"
    assert {row[0] for row in standard} == {"freelance_ru", "hh_ru"}
    assert len(standard) == 4
    print(
        f"wide fairness: FR first={timestamps['freelance_ru_done']*1000:.1f}ms, "
        f"HH first={timestamps['hh_ru_done']*1000:.1f}ms, "
        f"total={(time.perf_counter()-started_at)*1000:.1f}ms, peak={peak}, "
        f"standard_results={len(standard)}"
    )


@pytest.mark.asyncio
async def test_wide_global_limit_one_interleaves_sources_before_kwork_finishes() -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    calls: list[tuple[str, int]] = []
    active = 0
    peak = 0

    async def fake_parse(parser, page, per_page, query):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        platform = parser.PLATFORM_NAME
        calls.append((platform, page))
        try:
            await asyncio.sleep(0)
            if platform == "kwork" and page < 3:
                return platform, query, page, [_project(platform, f"k{page}")], 0, None
            if platform == "kwork":
                return platform, query, page, [], 0, None
            return platform, query, page, [_project(platform, str(page))], 0, None
        finally:
            active -= 1

    orch._parse_projects = fake_parse
    wide, stats, standard = await _collect(orch, max_concurrency=1)
    assert peak == 1
    assert len(wide) == 3
    assert len(standard) == 4
    assert stats["kwork"]["query_stops"]["automation"] == "empty_page"
    assert calls.index(("freelance_ru", 1)) < calls.index(("kwork", 3))
    assert calls.index(("hh_ru", 1)) < calls.index(("kwork", 3))


@pytest.mark.asyncio
async def test_wide_source_page_error_does_not_discard_other_platforms() -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)

    async def fake_parse(parser, page, per_page, query):
        platform = parser.PLATFORM_NAME
        if platform == "kwork":
            return platform, query, page, [], 1, RuntimeError("source failure")
        return platform, query, page, [_project(platform, str(page))], 1, None

    orch._parse_projects = fake_parse
    wide, stats, standard = await _collect(orch)
    assert len(wide) == 1
    assert isinstance(wide[0][-1], RuntimeError)
    assert stats["kwork"]["query_stops"]["automation"] == "error:RuntimeError"
    assert len(standard) == 4


@pytest.mark.asyncio
async def test_cancel_wide_discovery_cancels_both_collectors_and_unscheduled_pages() -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    waiting = asyncio.Event()
    entered: set[str] = set()
    cancelled: set[str] = set()
    calls: list[tuple[str, int]] = []

    async def fake_parse(parser, page, per_page, query):
        platform = parser.PLATFORM_NAME
        calls.append((platform, page))
        entered.add(platform)
        if len(entered) == 3:
            waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(platform)
            raise

    orch._parse_projects = fake_parse
    task = asyncio.create_task(_collect(orch))
    await asyncio.wait_for(waiting.wait(), timeout=1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == {"kwork", "freelance_ru", "hh_ru"}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_unexpected_wide_failure_cancels_inflight_standard_work() -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME == "kwork":
            await started.wait()
            raise RuntimeError("unexpected orchestration error")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    orch._parse_projects = fake_parse
    with pytest.raises(ExceptionGroup) as failure:
        await _collect(orch)
    assert any(isinstance(exc, RuntimeError) and str(exc) == "unexpected orchestration error"
               for exc in failure.value.exceptions)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_standard_worker_failure_cleans_up_all_other_source_tasks() -> None:
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    started = asyncio.Event()
    cancelled: set[str] = set()

    async def fake_parse(parser, page, per_page, query):
        platform = parser.PLATFORM_NAME
        if platform == "freelance_ru":
            await started.wait()
            raise RuntimeError("unexpected standard failure")
        if platform == "hh_ru":
            started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(platform)
            raise

    orch._parse_projects = fake_parse
    with pytest.raises(ExceptionGroup) as failure:
        await _collect(orch)
    assert any(isinstance(exc, RuntimeError) and str(exc) == "unexpected standard failure"
               for exc in failure.value.exceptions)
    assert cancelled == {"kwork", "hh_ru"}
