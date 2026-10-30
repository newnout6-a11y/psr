"""Offline candidate notification delivery and restart regressions."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from src.action.candidate_delivery import deliver_candidate_notifications
from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem


def _admit_queued(db: ProposalDB, platform: str, project_id: str) -> int:
    project = ProjectItem(
        id=project_id,
        platform=platform,
        title=f"Python service {project_id}",
        description="Build a durable API integration",
        url=f"https://example.invalid/{platform}/{project_id}",
        created_at="2026-10-30T10:00:00Z",
        budget=5000,
    )
    candidate_id, _ = db.insert_candidate_if_absent(project, stage="parsed", status="parsed")
    db.update_candidate_status(candidate_id, "queued", actor="system")
    return candidate_id


@pytest.mark.asyncio
async def test_restart_replays_committed_notification_without_new_scan(tmp_path):
    path = str(tmp_path / "proposals.sqlite")
    first_db = ProposalDB(path)
    candidate_id = _admit_queued(first_db, "hh_ru", "crash-before-send")
    # Simulate hard process exit immediately after candidate+outbox commit.
    restarted_db = ProposalDB(path)
    calls: list[int] = []

    async def accepted(candidate):
        calls.append(candidate["candidate_id"])
        return True

    assert await deliver_candidate_notifications(
        restarted_db, accepted, worker_id="after-restart", limit=1,
    ) == {candidate_id}
    assert calls == [candidate_id]
    assert await deliver_candidate_notifications(
        ProposalDB(path), accepted, worker_id="next-cycle", limit=1,
    ) == set()
    assert calls == [candidate_id]
    with sqlite3.connect(path) as connection:
        status = connection.execute(
            "SELECT status FROM candidate_notification_outbox WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()[0]
    assert status == "delivered"  # Provider acceptance, not recipient read/delivery.


@pytest.mark.asyncio
async def test_same_remote_id_on_different_platforms_has_distinct_events(tmp_path):
    db = ProposalDB(str(tmp_path / "proposals.sqlite"))
    expected = {
        _admit_queued(db, "kwork", "shared"),
        _admit_queued(db, "freelance_ru", "shared"),
        _admit_queued(db, "hh_ru", "shared"),
    }
    calls: list[int] = []

    async def send(candidate):
        calls.append(candidate["candidate_id"])
        return True

    assert await deliver_candidate_notifications(db, send, worker_id="one", limit=3) == expected
    assert set(calls) == expected
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_concurrent_dispatchers_do_not_send_same_event_twice(tmp_path):
    path = str(tmp_path / "proposals.sqlite")
    first_db = ProposalDB(path)
    candidate_id = _admit_queued(first_db, "freelance_ru", "once")
    calls: list[int] = []

    async def slow_send(candidate):
        calls.append(candidate["candidate_id"])
        await asyncio.sleep(0.03)
        return True

    outcomes = await asyncio.gather(
        deliver_candidate_notifications(first_db, slow_send, worker_id="worker-one", limit=1),
        deliver_candidate_notifications(ProposalDB(path), slow_send, worker_id="worker-two", limit=1),
    )
    assert outcomes.count({candidate_id}) == 1
    assert outcomes.count(set()) == 1
    assert calls == [candidate_id]


@pytest.mark.asyncio
async def test_provider_rejection_is_not_acknowledged_and_can_be_retried(tmp_path):
    path = str(tmp_path / "proposals.sqlite")
    db = ProposalDB(path)
    candidate_id = _admit_queued(db, "hh_ru", "retry")

    async def reject(_candidate):
        return False

    assert await deliver_candidate_notifications(db, reject, worker_id="rejecter", limit=1) == set()
    with sqlite3.connect(path) as connection:
        status, attempts = connection.execute(
            "SELECT status, attempts FROM candidate_notification_outbox WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    assert status == "pending"
    assert attempts == 1
    assert await deliver_candidate_notifications(db, reject, worker_id="not-yet-due", limit=1) == set()


@pytest.mark.asyncio
async def test_skipped_candidate_cancels_unsent_notification(tmp_path):
    path = str(tmp_path / "proposals.sqlite")
    db = ProposalDB(path)
    candidate_id = _admit_queued(db, "kwork", "user-skipped")
    db.update_candidate_status(candidate_id, "skipped", actor="ui")
    calls: list[int] = []

    async def send(candidate):
        calls.append(candidate["candidate_id"])
        return True

    assert await deliver_candidate_notifications(db, send, worker_id="consumer", limit=1) == set()
    assert calls == []
    with sqlite3.connect(path) as connection:
        status = connection.execute(
            "SELECT status FROM candidate_notification_outbox WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()[0]
    assert status == "cancelled"


@pytest.mark.asyncio
async def test_api_recovery_delivers_without_starting_new_discovery_cycle(tmp_path, monkeypatch):
    from src.api import candidate_notification_runtime as runtime

    db = ProposalDB(str(tmp_path / "proposals.sqlite"))
    candidate_id = _admit_queued(db, "hh_ru", "api-boot")
    sent = asyncio.Event()
    acknowledged = asyncio.Event()
    notifications: list[int] = []
    loop = asyncio.get_running_loop()
    real_ack = db.ack_notification

    def ack_and_signal(*args, **kwargs):
        result = real_ack(*args, **kwargs)
        if result:
            loop.call_soon_threadsafe(acknowledged.set)
        return result

    monkeypatch.setattr(db, "ack_notification", ack_and_signal)

    async def mock_send(candidate):
        notifications.append(candidate["candidate_id"])
        sent.set()
        return True

    monkeypatch.setattr(runtime, "ProposalDB", lambda: db)
    monkeypatch.setattr(
        runtime, "TelegramNotifier",
        lambda db: SimpleNamespace(bot=object(), admin_id="local-mock", notify_candidate=mock_send),
    )
    monkeypatch.setenv("TELEGRAM_TOKEN", "mock-only-not-used-on-network")
    monkeypatch.setenv("ADMIN_CHAT_ID", "mock-only")
    task = asyncio.create_task(runtime.recover_candidate_notifications(interval_seconds=1))
    try:
        await asyncio.wait_for(sent.wait(), timeout=2)
        await asyncio.wait_for(acknowledged.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert notifications == [candidate_id]
    with sqlite3.connect(db.db_path) as connection:
        status = connection.execute(
            "SELECT status FROM candidate_notification_outbox WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()[0]
    assert status == "delivered"


@pytest.mark.asyncio
async def test_api_recovery_without_telegram_leaves_durable_pending(tmp_path, monkeypatch):
    from src.api import candidate_notification_runtime as runtime

    db = ProposalDB(str(tmp_path / "proposals.sqlite"))
    candidate_id = _admit_queued(db, "kwork", "telegram-unconfigured")
    monkeypatch.setattr(runtime, "ProposalDB", lambda: db)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    task = asyncio.create_task(runtime.recover_candidate_notifications(interval_seconds=1))
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with sqlite3.connect(db.db_path) as connection:
        status, attempts = connection.execute(
            "SELECT status, attempts FROM candidate_notification_outbox WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    assert (status, attempts) == ("pending", 0)
