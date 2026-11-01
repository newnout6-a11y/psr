"""Integration-style tests for incremental discovery without live requests."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.orchestrator import FreelanceOrchestrator
from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem


def project(platform: str, project_id: str) -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title="Python backend automation",
        description="A python API integration project",
        url=f"https://example.invalid/{platform}/{project_id}",
        platform=platform,
        created_at="2026-10-30T10:00:00Z",
        budget=5000,
    )


def fake_orchestrator(monkeypatch):
    """Use the real candidate pipeline and a durable-store-shaped fake."""
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    persisted: dict[tuple[str, str], dict] = {}
    notifications: list[tuple[str, str]] = []
    query_runs: list[tuple[str, str, int]] = []
    next_id = 0

    def stage(item, stage, status, **fields):
        nonlocal next_id
        key = item.platform, item.id
        next_id += 1
        persisted[key] = {"id": next_id, "stage": stage, "status": status, **fields}
        return next_id

    def update_candidate(candidate_id, **fields):
        row = next(row for row in persisted.values() if row["id"] == candidate_id)
        row.update(fields)

    def insert_if_absent(item, **fields):
        key = item.platform, item.id
        if key in persisted:
            return persisted[key]["id"], False
        fields = dict(fields)
        cid = stage(item, fields.pop("stage"), fields.pop("status"), **fields)
        return cid, True

    async def notify(candidate_id, item):
        assert persisted[(item.platform, item.id)]["status"] in {"queued", "auto_ready"}
        notifications.append((item.platform, item.id))

    orch._stage_candidate = stage
    orch._notify_candidate = notify
    orch._record_query_signal = lambda *_args, **_kwargs: None
    orch._write_generated_proposal = lambda *_args: None
    orch._cycle_sent_count = 0
    orch._cycle_sent_per_platform = {}
    orch._execution_mode = lambda: "semi_auto"
    orch.db = SimpleNamespace(
        requeue_due_candidates=lambda: None,
        recover_stale_sending=lambda: None,
        record_query_run=lambda platform, query, count: query_runs.append((platform, query, count)),
        get_candidate_by_project=lambda item_id, platform: persisted.get((platform, item_id)),
        is_client_blacklisted=lambda *_args: False,
        update_candidate=update_candidate,
        insert_candidate_if_absent=insert_if_absent,
        update_candidate_status=lambda cid, status, **kw: update_candidate(cid, status=status),
        is_platform_paused=lambda *_args: False,
    )

    async def pass_projects(items):
        return items, {}

    orch.filter = SimpleNamespace(per_page=20, filter_projects_with_reasons=pass_projects)
    orch.nlp = SimpleNamespace(
        is_honeypot=lambda *_args: False,
        analyze_project=lambda *_args: {"is_spam": False, "is_relevant": True, "tech_stack": []},
    )

    async def evaluate(items, *, threshold):
        return [
            SimpleNamespace(
                project=item, pre_score=9, final_score=9, passed=True,
                fallback_scored=False, source="heuristic", summary="", threshold=threshold,
            )
            for item in items
        ]

    orch.ai_scorer = SimpleNamespace(evaluate_projects=evaluate)
    orch.client_vetter = SimpleNamespace(
        fetch_kwork_client_data=AsyncMock(return_value=None),
        vet_client=AsyncMock(return_value={"passed": True, "score": 90, "reasons": [], "red_flags": []}),
    )
    orch.osint = None
    orch.generator = SimpleNamespace(
        generate=AsyncMock(return_value="I can deliver this Python backend integration safely and promptly."),
        last_generation_meta={"provider": "fake"},
    )
    orch.image_generator = SimpleNamespace(maybe_generate=AsyncMock(return_value=None))
    orch.policy = SimpleNamespace(
        evaluate=lambda *_args: SimpleNamespace(
            status="queued", risk_level="low", priority=10, auto_eligible=False, reason="review"
        )
    )
    orch.notifier = SimpleNamespace(maybe_send_digest=AsyncMock(), notify_skipped=AsyncMock())
    orch.inbox_monitor = SimpleNamespace(check_all=AsyncMock(return_value=[]))
    orch.search_strategy = SimpleNamespace(
        invalidate_cache=lambda: None,
        generate_queries=AsyncMock(side_effect=lambda platform, count: [platform]),
    )
    orch.parsers = [
        SimpleNamespace(PLATFORM_NAME="kwork"),
        SimpleNamespace(PLATFORM_NAME="freelance_ru"),
        SimpleNamespace(PLATFORM_NAME="hh_ru"),
    ]
    monkeypatch.setattr(
        "src.utils.schedule.get_schedule_manager",
        lambda: SimpleNamespace(is_work_time=lambda: True),
    )
    log_db = SimpleNamespace(log_filter=MagicMock(), log_parse=MagicMock(), log_generation=MagicMock())
    monkeypatch.setattr("src.orchestrator.get_log_db", lambda: log_db)
    monkeypatch.setenv("PLATFORMS", "")
    monkeypatch.setenv("DISCOVERY_MODE", "wide")
    monkeypatch.setenv("INCREMENTAL_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("TOP_PROJECTS", "0")
    monkeypatch.setenv("PAGES_TO_PARSE", "2")
    monkeypatch.setenv("MAX_PAGES_PER_QUERY", "3")
    monkeypatch.setenv("MAX_PARSE_CONCURRENCY", "3")
    monkeypatch.setenv("DISCOVERY_PAGE_QUEUE_SIZE", "2")
    return orch, persisted, notifications, query_runs


@pytest.mark.asyncio
async def test_real_candidate_pipeline_persists_and_notifies_before_slow_kwork(monkeypatch, tmp_path, capsys):
    orch, _, notifications, _ = fake_orchestrator(monkeypatch)
    database_file = tmp_path / "candidates.sqlite3"
    orch.db = ProposalDB(str(database_file))
    # Use the real SQLite admission path instead of the fake staging method.
    del orch._stage_candidate
    allow_kwork = asyncio.Event()
    early_done = asyncio.Event()
    start = time.perf_counter()
    timestamps: dict[str, float] = {}

    async def notify(candidate_id, item):
        stored = orch.db.get_candidate_by_project(item.id, item.platform)
        assert stored is not None and stored["candidate_id"] == candidate_id
        assert stored["status"] == "queued"
        notifications.append((item.platform, item.id))
        timestamps[f"{item.platform}_notified"] = time.perf_counter() - start
        if {platform for platform, _ in notifications} >= {"freelance_ru", "hh_ru"}:
            early_done.set()

    orch._notify_candidate = notify

    async def fake_parse(parser, page, per_page, query):
        platform = parser.PLATFORM_NAME
        if platform == "kwork":
            await allow_kwork.wait()
            return platform, query, page, [], 0, None
        await asyncio.sleep(0.012)
        item = project(platform, "same-id")
        return platform, query, page, [item], 12, None

    orch._parse_projects = fake_parse
    running = asyncio.create_task(orch.run_cycle(dry_run=True))
    try:
        await asyncio.wait_for(early_done.wait(), timeout=1.5)
        assert not running.done(), "Kwork discovery remains deliberately blocked"
        assert orch.db.get_candidate_by_project("same-id", "freelance_ru")["status"] == "queued"
        assert orch.db.get_candidate_by_project("same-id", "hh_ru")["status"] == "queued"
        with sqlite3.connect(database_file) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE status = 'queued'"
            ).fetchone()[0] == 2
        assert max(timestamps.values()) < 1.0
    finally:
        allow_kwork.set()
    summary = await running

    assert Counter(notifications) == Counter({("freelance_ru", "same-id"): 1, ("hh_ru", "same-id"): 1})
    assert summary["parsed"] == 2
    assert summary["active"] == 2
    assert summary["queued"] == 2
    assert summary["discovery"]["deduped"] == 2
    assert set(summary["discovery"]["first_persisted_ms"]) == {"freelance_ru", "hh_ru"}
    assert set(summary["discovery"]["first_notifier_call_completed_ms"]) == {"freelance_ru", "hh_ru"}
    print(
        f"early durable+notify: FR={timestamps['freelance_ru_notified']*1000:.1f}ms "
        f"HH={timestamps['hh_ru_notified']*1000:.1f}ms, Kwork still blocked"
    )


@pytest.mark.asyncio
async def test_cap_one_respects_requests_and_global_proposal_limit(monkeypatch):
    orch, persisted, notifications, _ = fake_orchestrator(monkeypatch)
    monkeypatch.setenv("MAX_PARSE_CONCURRENCY", "1")
    monkeypatch.setenv("PAGES_TO_PARSE", "2")
    max_active = 0
    active = 0

    async def fake_parse(parser, page, per_page, query):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.002)
            if parser.PLATFORM_NAME == "kwork":
                return "kwork", query, page, [], 2, None
            return parser.PLATFORM_NAME, query, page, [project(parser.PLATFORM_NAME, f"id-{page}")], 2, None
        finally:
            active -= 1

    orch._parse_projects = fake_parse
    summary = await orch.run_cycle(dry_run=True, limit_per_platform=1)
    assert max_active == 1
    assert summary["parsed"] == 4
    assert summary["proposal_selected"] == 2
    assert summary["proposal_limited"] == 2
    assert len(notifications) == 2
    assert len(persisted) == 4


@pytest.mark.asyncio
async def test_bounded_queue_backpressure_and_no_dropped_pages_on_slow_consumer():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    started = asyncio.Event()
    release = asyncio.Event()
    received: list[int] = []
    fetched = 0

    async def fake_parse(parser, page, per_page, query):
        nonlocal fetched
        fetched += 1
        return parser.PLATFORM_NAME, query, page, [project("hh_ru", f"id-{page}")], 0, None

    async def slow_consumer(item):
        if not received:
            started.set()
            await release.wait()
        received.append(item[2])

    orch._parse_projects = fake_parse
    task = asyncio.create_task(
        orch._consume_incremental_discovery(
            [(SimpleNamespace(PLATFORM_NAME="hh_ru"), ["python"])],
            wide_mode=True,
            per_page=20,
            pages_to_parse=20,
            max_pages_per_query=1,
            max_projects_per_cycle=100,
            max_parse_seconds=30,
            max_concurrency=3,
            on_result=slow_consumer,
            queue_size=1,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1.5)
        await asyncio.sleep(0.02)
        assert fetched <= 3, "one processing page + one queued page + one in-flight producer"
    finally:
        release.set()
    await task
    assert sorted(received) == list(range(1, 21))


@pytest.mark.asyncio
async def test_consumer_failure_cancels_fetchers_and_surfaces_error():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME == "kwork":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await started.wait()
        return parser.PLATFORM_NAME, query, page, [project(parser.PLATFORM_NAME, "x")], 0, None

    async def consumer(_):
        raise RuntimeError("database unavailable")

    orch._parse_projects = fake_parse
    with pytest.raises(ExceptionGroup) as error:
        await orch._consume_incremental_discovery(
            [
                (SimpleNamespace(PLATFORM_NAME="kwork"), ["a"]),
                (SimpleNamespace(PLATFORM_NAME="hh_ru"), ["b"]),
            ],
            wide_mode=True,
            per_page=20,
            pages_to_parse=2,
            max_pages_per_query=4,
            max_projects_per_cycle=100,
            max_parse_seconds=30,
            max_concurrency=2,
            on_result=consumer,
            queue_size=1,
        )
    assert any(isinstance(exc, RuntimeError) for exc in error.value.exceptions)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_duplicate_pages_and_restart_do_not_duplicate_sqlite_notifications(monkeypatch, tmp_path):
    orch, _, notifications, _ = fake_orchestrator(monkeypatch)
    orch.db = ProposalDB(str(tmp_path / "restart.sqlite3"))
    del orch._stage_candidate

    async def notify(candidate_id, item):
        stored = orch.db.get_candidate_by_project(item.id, item.platform)
        assert stored is not None and stored["status"] == "queued"
        notifications.append((item.platform, item.id))

    orch._notify_candidate = notify

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME == "kwork":
            return "kwork", query, page, [], 0, None
        return parser.PLATFORM_NAME, query, page, [project(parser.PLATFORM_NAME, "duplicate")], 0, None

    orch._parse_projects = fake_parse
    first = await orch.run_cycle(dry_run=True)
    second = await orch.run_cycle(dry_run=True)

    assert first["parsed"] == 2
    assert first["discovery"]["raw_found"] == 4
    assert second["parsed"] == 2
    assert second["active"] == 0
    assert Counter(notifications) == Counter(
        {("freelance_ru", "duplicate"): 1, ("hh_ru", "duplicate"): 1}
    )
    with sqlite3.connect(orch.db.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_auto_ready_candidate_is_not_notified_again_on_next_scan(monkeypatch, tmp_path):
    orch, _, notifications, _ = fake_orchestrator(monkeypatch)
    orch.db = ProposalDB(str(tmp_path / "auto_ready.sqlite3"))
    del orch._stage_candidate
    orch.policy.evaluate = lambda *_args: SimpleNamespace(
        status="auto_ready", risk_level="low", priority=10, auto_eligible=True, reason="approved"
    )

    async def notify(candidate_id, item):
        notifications.append((item.platform, item.id))

    orch._notify_candidate = notify

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME == "hh_ru" and page == 1:
            return "hh_ru", query, page, [project("hh_ru", "ready")], 0, None
        return parser.PLATFORM_NAME, query, page, [], 0, None

    orch._parse_projects = fake_parse
    first = await orch.run_cycle(dry_run=True)
    second = await orch.run_cycle(dry_run=True)
    assert first["auto_ready"] == 1
    assert second["auto_ready"] == 0
    assert notifications == [("hh_ru", "ready")]
    assert orch.db.get_candidate_by_project("ready", "hh_ru")["status"] == "auto_ready"


@pytest.mark.asyncio
async def test_atomic_collision_preserves_existing_queued_status(monkeypatch):
    orch, persisted, notifications, _ = fake_orchestrator(monkeypatch)
    existing = project("hh_ru", "winner")
    persisted[("hh_ru", "winner")] = {"id": 44, "stage": "vetted", "status": "queued"}
    # Simulate a concurrent process inserting after the initial SELECT.
    orch.db.get_candidate_by_project = lambda *_args: None
    skipped: list[dict[str, str]] = []
    result = await orch._process_discovery_batch(
        [existing],
        dry_run=True,
        limit_per_platform=5,
        mode="semi_auto",
        log_db=SimpleNamespace(log_filter=MagicMock(), log_generation=MagicMock()),
        skipped_log=skipped,
        cycle_state={},
        on_staged=lambda _project: None,
    )
    assert result["active"] == 0
    assert persisted[("hh_ru", "winner")]["status"] == "queued"
    assert notifications == []
    assert skipped[0]["reason"] == "concurrent candidate admission"


@pytest.mark.asyncio
async def test_partial_commit_recovers_on_next_cycle_after_consumer_crash(monkeypatch, tmp_path):
    orch, _, notifications, _ = fake_orchestrator(monkeypatch)
    orch.db = ProposalDB(str(tmp_path / "crash.sqlite3"))
    del orch._stage_candidate
    fail_once = True

    async def evaluate(items, *, threshold):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("scorer crashed after durable staging")
        return [
            SimpleNamespace(
                project=item, pre_score=9, final_score=9, passed=True,
                fallback_scored=False, source="heuristic", summary="", threshold=threshold,
            )
            for item in items
        ]

    orch.ai_scorer.evaluate_projects = evaluate
    orch._notify_candidate = lambda *_args: _record_notification(notifications, "freelance_ru", "recover")

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME != "freelance_ru" or page > 1:
            return parser.PLATFORM_NAME, query, page, [], 0, None
        return "freelance_ru", query, page, [project("freelance_ru", "recover")], 0, None

    orch._parse_projects = fake_parse
    with pytest.raises(ExceptionGroup):
        await orch.run_cycle(dry_run=True)
    assert orch.db.get_candidate_by_project("recover", "freelance_ru")["status"] == "filtered"

    restored = await orch.run_cycle(dry_run=True)
    assert restored["active"] == 1
    assert orch.db.get_candidate_by_project("recover", "freelance_ru")["status"] == "queued"
    assert notifications == [("freelance_ru", "recover")]


async def _record_notification(notifications, platform, identifier):
    notifications.append((platform, identifier))


@pytest.mark.asyncio
async def test_global_top_projects_retains_original_collect_before_processing(monkeypatch):
    orch, persisted, notifications, _ = fake_orchestrator(monkeypatch)
    monkeypatch.setenv("TOP_PROJECTS", "1")
    gate = asyncio.Event()
    early = asyncio.Event()

    async def fake_parse(parser, page, per_page, query):
        if parser.PLATFORM_NAME == "kwork":
            await gate.wait()
            return "kwork", query, page, [], 0, None
        early.set()
        return parser.PLATFORM_NAME, query, page, [project(parser.PLATFORM_NAME, "p")], 0, None

    orch._parse_projects = fake_parse
    running = asyncio.create_task(orch.run_cycle(dry_run=True))
    try:
        await asyncio.wait_for(early.wait(), timeout=1.5)
        await asyncio.sleep(0)
        assert persisted == {}
        assert notifications == []
    finally:
        gate.set()
    summary = await running
    assert summary["proposal_selected"] == 1
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_measured_first_notification_improves_over_legacy_batch(monkeypatch, capsys):
    async def measure(*, incremental: bool) -> tuple[float, float]:
        orch, _, _, _ = fake_orchestrator(monkeypatch)
        monkeypatch.setenv("INCREMENTAL_DISCOVERY_ENABLED", "true" if incremental else "false")
        first_notified: list[float] = []
        started = time.perf_counter()

        async def notify(candidate_id, item):
            first_notified.append(time.perf_counter() - started)

        async def fake_parse(parser, page, per_page, query):
            if parser.PLATFORM_NAME == "kwork":
                await asyncio.sleep(0.25)
                return "kwork", query, page, [], 250, None
            return parser.PLATFORM_NAME, query, page, [project(parser.PLATFORM_NAME, "fresh")], 0, None

        orch._notify_candidate = notify
        orch._parse_projects = fake_parse
        await orch.run_cycle(dry_run=True)
        return min(first_notified), time.perf_counter() - started

    streamed_first, streamed_total = await measure(incremental=True)
    legacy_first, legacy_total = await measure(incremental=False)
    assert streamed_first < legacy_first
    print(
        f"controlled first notification: incremental={streamed_first*1000:.1f}ms, "
        f"gather-all={legacy_first*1000:.1f}ms; "
        f"complete-cycle incremental={streamed_total*1000:.1f}ms, "
        f"gather-all={legacy_total*1000:.1f}ms"
    )
