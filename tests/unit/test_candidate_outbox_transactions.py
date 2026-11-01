"""Transactional candidate notification outbox and replay under real SQLite."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.action import proposal_db
from src.action.proposal_db import ProposalDB


def _project(identifier: str = "a") -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier, platform="freelance_ru", title="Python integration",
        description="API integration", budget=5000, currency="RUB",
        skills=["python"], url=f"https://example.invalid/{identifier}",
        created_at="2026-10-30T12:00:00Z", offers_count=0, client_hired_percent=0,
        search_query="python", platform_data={},
    )


@pytest.fixture
def db(tmp_path):
    return ProposalDB(str(tmp_path / "proposals.db"))


def _candidate(db: ProposalDB, identifier: str = "a") -> int:
    return db.insert_candidate_if_absent(_project(identifier), stage="parsed", status="parsed")[0]


def _outbox_rows(db: ProposalDB):
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM candidate_notification_outbox ORDER BY event_id")]


def test_atomic_status_action_outbox_and_idempotent_transition(db):
    candidate_id = _candidate(db)
    event_id, inserted = db.enqueue_candidate_notification(candidate_id, "queued", reason="ready")
    assert inserted and event_id is not None
    assert db.get_candidate(candidate_id)["status"] == "queued"
    assert db.get_notification_summary()["pending"] == 1

    # Updating the same candidate via the legacy API must not create duplicates.
    db.update_candidate_status(candidate_id, "queued", actor="system", reason="same")
    event_id_2, inserted_again = db.enqueue_candidate_notification(candidate_id, "auto_ready")
    assert event_id_2 == event_id
    assert not inserted_again
    assert len(_outbox_rows(db)) == 1
    journal = db.get_candidate_events_since()["items"]
    assert [row["delivery_status"] for row in journal if row["event_kind"] == "notification"] == ["pending"]
    assert "queued" in [row["candidate_status"] for row in journal if row["event_kind"] == "candidate_status"]
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM candidate_actions WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()[0] == 3


def test_outbox_atomic_rollback_on_journal_failure(db, monkeypatch):
    candidate_id = _candidate(db)

    def fail_journal(*args, **kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(db, "_journal_candidate_event", fail_journal)
    with pytest.raises(RuntimeError, match="disk write"):
        db.update_candidate_status(candidate_id, "queued", reason="ready")
    assert db.get_candidate(candidate_id)["status"] == "parsed"
    assert db.get_notification_summary()["pending"] == 0
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM candidate_actions").fetchone()[0] == 0


def test_legacy_database_migration_preserves_candidate_and_stream_id(tmp_path):
    path = tmp_path / "legacy.db"
    first = ProposalDB(str(path))
    candidate_id = _candidate(first)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE candidate_event_journal")
        conn.execute("DROP TABLE candidate_notification_outbox")
        conn.execute("DELETE FROM runtime_state WHERE state_key = 'candidate_events.stream_id'")
    migrated = ProposalDB(str(path))
    assert migrated.get_candidate(candidate_id)["project_id"] == "a"
    stream_id = migrated.get_candidate_events_since()["stream_id"]
    assert stream_id
    assert ProposalDB(str(path)).get_candidate_events_since()["stream_id"] == stream_id
    migrated.update_candidate_status(candidate_id, "queued")
    assert len(migrated.claim_due_notifications("migrated")) == 1


def test_concurrent_enqueue_and_lease_single_owner(db):
    candidate_id = _candidate(db)
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: db.enqueue_candidate_notification(candidate_id, "queued"), range(16)))
    assert sum(int(inserted) for _, inserted in statuses) == 1
    assert len({event_id for event_id, _ in statuses}) == 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda number: db.claim_due_notifications(f"worker-{number}", limit=1), range(16)))
    events = [event for batch in claimed for event in batch]
    assert len(events) == 1
    assert events[0]["attempts"] == 1
    assert db.get_notification_summary()["leased"] == 1


def test_expired_lease_recovery_and_fencing(db, monkeypatch):
    candidate_id = _candidate(db)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    db.update_candidate_status(candidate_id, "queued")

    first = db.claim_due_notifications("crashed-worker", lease_seconds=10)[0]
    assert db.claim_due_notifications("competitor") == []
    clock[0] += timedelta(seconds=11)
    resumed = db.claim_due_notifications("restarted-worker", lease_seconds=30)[0]
    assert resumed["event_id"] == first["event_id"]
    assert resumed["attempts"] == 2
    assert resumed["lease_token"] != first["lease_token"]
    assert not db.ack_notification(first["event_id"], first["lease_token"], worker_id="crashed-worker")
    assert not db.fail_notification(first["event_id"], first["lease_token"], "stale", worker_id="crashed-worker")
    assert db.ack_notification(resumed["event_id"], resumed["lease_token"], worker_id="restarted-worker")
    assert not db.ack_notification(resumed["event_id"], resumed["lease_token"], worker_id="restarted-worker")
    assert db.get_notification_summary()["delivered"] == 1


def test_retry_backoff_max_attempts_and_replay(db, monkeypatch):
    candidate_id = _candidate(db)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    db.update_candidate_status(candidate_id, "queued")

    first = db.claim_due_notifications("worker", max_attempts=3)[0]
    assert db.fail_notification(first["event_id"], first["lease_token"], "offline", worker_id="worker", max_attempts=3, base_delay_seconds=2)
    assert db.claim_due_notifications("worker", max_attempts=3) == []
    clock[0] += timedelta(seconds=2)
    second = db.claim_due_notifications("worker", max_attempts=3)[0]
    assert second["attempts"] == 2
    assert db.fail_notification(second["event_id"], second["lease_token"], "offline", worker_id="worker", max_attempts=3, base_delay_seconds=2)
    clock[0] += timedelta(seconds=3)
    assert db.claim_due_notifications("worker", max_attempts=3) == []
    clock[0] += timedelta(seconds=1)
    third = db.claim_due_notifications("worker", max_attempts=3)[0]
    assert third["attempts"] == 3
    assert db.fail_notification(third["event_id"], third["lease_token"], "offline", worker_id="worker", max_attempts=3, base_delay_seconds=2)
    clock[0] += timedelta(hours=1)
    assert db.claim_due_notifications("worker", max_attempts=3) == []
    assert db.get_notification_summary()["dead"] == 1

    journal = db.get_candidate_events_since(limit=2)
    stream_id = journal["stream_id"]
    seen = list(journal["items"])
    while len(journal["items"]) == 2:
        journal = db.get_candidate_events_since(after_seq=journal["next_cursor"], limit=2)
        assert journal["stream_id"] == stream_id
        seen.extend(journal["items"])
    assert len({event["seq"] for event in seen}) == len(seen)
    assert [event["delivery_status"] for event in seen if event["delivery_status"]] == [
        "pending", "leased", "retry", "leased", "retry", "leased", "failed"
    ]


def test_cleanup_scrubs_old_payload_but_keeps_idempotency_tombstone(db, monkeypatch):
    candidate_id = _candidate(db)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    db.update_candidate_status(candidate_id, "queued")
    event = db.claim_due_notifications("worker")[0]
    assert db.ack_notification(event["event_id"], event["lease_token"], worker_id="worker")
    clock[0] += timedelta(days=31)
    assert db.cleanup_notification_history(retention_days=30) == 1
    assert _outbox_rows(db)[0]["payload"] is None
    db.update_candidate_status(candidate_id, "auto_ready")
    assert db.get_notification_summary()["delivered"] == 1
    assert db.claim_due_notifications("worker") == []
    assert db.get_candidate_events_since()["stream_id"]

def test_journal_pruning_signals_cursor_gap_and_preserves_generation(db, monkeypatch):
    # Patch the clock before admission so the initial 'parsed' journal row is
    # also old enough to prune; otherwise the test depends on the real date.
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    cid = _candidate(db)
    db.update_candidate_status(cid, "queued")
    old_events = db.get_candidate_events_since()
    stream_id = old_events["stream_id"]
    last_old_seq = old_events["next_cursor"]
    assert last_old_seq >= 2

    clock[0] += timedelta(days=31)
    assert db.cleanup_notification_history(retention_days=30) == 0
    after = db.get_candidate_events_since(after_seq=1)
    assert after["stream_id"] == stream_id
    assert after["reset_required"] is True
    assert after["earliest_seq"] == last_old_seq + 1
    assert after["items"] == []

    db.update_candidate_status(cid, "auto_ready")
    resumed = db.get_candidate_events_since(after_seq=last_old_seq)
    assert resumed["reset_required"] is False
    assert resumed["items"][0]["seq"] > last_old_seq
    assert resumed["items"][0]["candidate_status"] == "auto_ready"


def test_intermediate_status_updates_are_journaled(db):
    cid = _candidate(db)
    db.update_candidate(cid, status="filtered", stage="filtered")
    db.update_candidate(cid, status="scored", stage="scored")
    db.update_candidate(cid, status="vetted", stage="vetted")
    db.update_candidate_status(cid, "queued")
    assert db.claim_candidate_for_sending(cid)
    db.update_candidate_status(cid, "manual_sent")
    kinds = [
        row["candidate_status"]
        for row in db.get_candidate_events_since()["items"]
        if row["event_kind"] == "candidate_status"
    ]
    assert kinds == ["parsed", "filtered", "scored", "vetted", "queued", "sending", "manual_sent"]


def test_direct_requeue_paths_write_status_journal(db):
    cid = _candidate(db)
    db.update_candidate_status(cid, "queued")
    db.snooze_candidate(cid, 1)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE candidates SET snoozed_until = '2000-01-01 00:00:00' WHERE candidate_id = ?",
            (cid,),
        )
    assert db.requeue_due_candidates() == 1
    assert db.claim_candidate_for_sending(cid)
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE candidates SET updated_at = '2000-01-01 00:00:00' WHERE candidate_id = ?",
            (cid,),
        )
    assert db.recover_stale_sending(max_age_minutes=10) == 1
    statuses = [
        row["candidate_status"]
        for row in db.get_candidate_events_since()["items"]
        if row["event_kind"] == "candidate_status"
    ]
    assert statuses[-4:] == ["snoozed", "queued", "sending", "queued"]


def test_cancel_claimed_notification_and_no_reclaim(db):
    candidate_id = _candidate(db)
    db.update_candidate_status(candidate_id, "queued")
    event = db.claim_due_notifications("worker")[0]
    db.update_candidate_status(candidate_id, "manual_sent")
    assert db.cancel_notification(event["event_id"], event["lease_token"], worker_id="worker")
    assert db.claim_due_notifications("other") == []
    assert db.get_notification_summary()["cancelled"] == 1
    assert db.get_candidate_events_since()["items"][-1]["delivery_status"] == "cancelled"


def test_process_death_on_final_lease_exhausts_max_attempts(db, monkeypatch):
    cid = _candidate(db)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    db.update_candidate_status(cid, "queued")
    first = db.claim_due_notifications("failed", lease_seconds=10, max_attempts=1)[0]
    clock[0] += timedelta(seconds=11)
    assert db.claim_due_notifications("new-worker", max_attempts=1) == []
    assert db.get_notification_summary()["dead"] == 1
    assert not db.ack_notification(first["event_id"], first["lease_token"], worker_id="failed")


def test_database_replacement_changes_stream_id(tmp_path):
    first = ProposalDB(str(tmp_path / "one.db")).get_candidate_events_since()["stream_id"]
    second = ProposalDB(str(tmp_path / "two.db")).get_candidate_events_since()["stream_id"]
    assert first != second


def test_default_retry_schedule_survives_a_short_provider_outage(db, monkeypatch):
    """A single lifetime outbox row must not die within a minute of failures."""
    candidate_id = _candidate(db)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(proposal_db, "_outbox_now", lambda: clock[0])
    db.update_candidate_status(candidate_id, "queued")

    delays = []
    for attempt in range(1, proposal_db.OUTBOX_MAX_ATTEMPTS):
        event = db.claim_due_notifications("worker")[0]
        assert event["attempts"] == attempt
        assert db.fail_notification(event["event_id"], event["lease_token"], "offline", worker_id="worker")
        row = _outbox_rows(db)[0]
        assert row["status"] == "pending"
        delay = datetime.fromisoformat(row["available_at"]) - clock[0]
        delays.append(delay.total_seconds())
        # Not claimable a moment before the backoff elapses.
        clock[0] += delay - timedelta(seconds=1)
        assert db.claim_due_notifications("worker") == []
        clock[0] += timedelta(seconds=1)

    assert delays == [30, 60, 120, 240, 480, 900, 900]
    assert sum(delays) >= 30 * 60  # outlasts a half-hour Telegram outage

    last = db.claim_due_notifications("worker")[0]
    assert last["attempts"] == proposal_db.OUTBOX_MAX_ATTEMPTS
    assert db.fail_notification(last["event_id"], last["lease_token"], "offline", worker_id="worker")
    assert _outbox_rows(db)[0]["status"] == "dead"
