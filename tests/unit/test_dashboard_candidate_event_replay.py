"""Durable dashboard REST replay of the independent orchestrator event journal."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.dashboard import router
from src.dashboard import queries


def _client(tmp_path, monkeypatch):
    database = tmp_path / "candidate-events.sqlite3"
    monkeypatch.setattr(queries, "PROPOSALS_DB", str(database))
    app = FastAPI()
    app.include_router(router)
    return database, TestClient(app)


def _schema(database, *, stream_id="stream-one"):
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE runtime_state(state_key TEXT PRIMARY KEY, state_value TEXT);
            CREATE TABLE candidates (
                candidate_id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                project_id TEXT NOT NULL
            );
            CREATE TABLE candidate_notification_outbox (
                event_id INTEGER PRIMARY KEY,
                candidate_id INTEGER,
                status TEXT
            );
            CREATE TABLE candidate_event_journal (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id TEXT NOT NULL,
                candidate_id INTEGER NOT NULL,
                event_id INTEGER,
                event_kind TEXT NOT NULL,
                candidate_status TEXT,
                delivery_status TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO runtime_state VALUES ('candidate_events.stream_id', ?)",
            (stream_id,),
        )
        conn.execute("INSERT INTO candidates VALUES (1, 'kwork', 'remote-1')")
        conn.execute("INSERT INTO candidates VALUES (2, 'hh_ru', 'remote-2')")
        conn.execute("INSERT INTO candidate_notification_outbox VALUES (1, 1, 'pending')")
        conn.execute("INSERT INTO candidate_notification_outbox VALUES (2, 2, 'delivered')")


def _event(database, *, candidate_id=1, kind="candidate_status", status="parsed", delivery=None):
    with sqlite3.connect(database) as conn:
        return conn.execute(
            """INSERT INTO candidate_event_journal
                (stream_id,candidate_id,event_id,event_kind,candidate_status,delivery_status,detail,created_at)
                VALUES ((SELECT state_value FROM runtime_state WHERE state_key = 'candidate_events.stream_id'),
                ?, 1, ?, ?, ?, '', '2026-10-30T13:00:00+00:00')""",
            (candidate_id, kind, status, delivery),
        ).lastrowid


def test_initial_snapshot_is_not_replayed_as_new_and_is_read_only(tmp_path, monkeypatch):
    database, client = _client(tmp_path, monkeypatch)
    _schema(database)
    seq = _event(database)
    with client:
        response = client.get("/api/dashboard/candidate-events")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["stream_id"] == "stream-one"
        assert data["latest_seq"] == seq
        assert data["next_after_seq"] == seq
        assert data["items"] == []
        assert data["recent"] == [{
            "seq": seq,
            "candidate_id": 1,
            "platform": "kwork",
            "project_id": "remote-1",
            "event_type": "candidate.status_changed",
            "candidate_status": "parsed",
            "delivery_status": None,
            "occurred_at": "2026-10-30T13:00:00+00:00",
        }]
        assert data["outbox_status_counts"] == {"pending": 1, "delivered": 1}


def test_replay_sees_status_and_delivery_transitions_in_order(tmp_path, monkeypatch):
    database, client = _client(tmp_path, monkeypatch)
    _schema(database)
    initial = _event(database)
    committed = [
        _event(database, status="queued"),
        _event(database, kind="notification", status="queued", delivery="pending"),
        _event(database, kind="notification", status="queued", delivery="leased"),
        _event(database, kind="notification", status="queued", delivery="provider_accepted"),
    ]
    with client:
        first_page = client.get(
            f"/api/dashboard/candidate-events?after_seq={initial}&stream_id=stream-one&limit=2"
        ).json()
        assert first_page["status"] == "ready"
        assert [item["seq"] for item in first_page["items"]] == committed[:2]
        assert [item["event_type"] for item in first_page["items"]] == [
            "candidate.status_changed", "notification.pending"
        ]
        assert first_page["has_more"] is True
        second_page = client.get(
            f"/api/dashboard/candidate-events?after_seq={first_page['next_after_seq']}&stream_id=stream-one&limit=2"
        ).json()
        assert [item["event_type"] for item in second_page["items"]] == [
            "notification.leased", "notification.provider_accepted"
        ]
        assert second_page["has_more"] is False
        assert second_page["next_after_seq"] == committed[-1]
        assert client.get(
            f"/api/dashboard/candidate-events?after_seq={committed[-1]}&stream_id=stream-one"
        ).json()["items"] == []


def test_reset_required_for_database_replacement_advanced_cursor_and_history_gap(tmp_path, monkeypatch):
    database, client = _client(tmp_path, monkeypatch)
    _schema(database)
    _event(database)
    _event(database)
    _event(database)
    with client:
        changed = client.get(
            "/api/dashboard/candidate-events?after_seq=1&stream_id=old-stream"
        ).json()
        assert changed["status"] == "reset_required"
        assert changed["stream_id"] == "stream-one"
        assert changed["items"] == []
        assert client.get(
            "/api/dashboard/candidate-events?after_seq=500&stream_id=stream-one"
        ).json()["status"] == "reset_required"
        with sqlite3.connect(database) as conn:
            conn.execute("DELETE FROM candidate_event_journal WHERE seq = 2")
        gap = client.get(
            "/api/dashboard/candidate-events?after_seq=1&stream_id=stream-one"
        ).json()
        assert gap["status"] == "reset_required"
        assert gap["items"] == []


def test_missing_database_or_journal_never_creates_storage(tmp_path, monkeypatch):
    database, client = _client(tmp_path, monkeypatch)
    with client:
        missing = client.get("/api/dashboard/candidate-events").json()
        assert missing["status"] == "unavailable"
        assert not database.exists()
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE candidates (candidate_id INTEGER)")
        missing_schema = client.get("/api/dashboard/candidate-events").json()
        assert missing_schema["status"] == "unavailable"
        with sqlite3.connect(database) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"candidates"}


def test_candidate_event_route_rejects_invalid_cursor_and_page_size(tmp_path, monkeypatch):
    database, client = _client(tmp_path, monkeypatch)
    _schema(database)
    with client:
        for query in (
            "after_seq=-1", "limit=0", "limit=201", "stream_id=",
        ):
            assert client.get(f"/api/dashboard/candidate-events?{query}").status_code == 422


def test_route_replays_actual_atomic_candidate_and_outbox_transitions(tmp_path, monkeypatch):
    from src.action.proposal_db import ProposalDB

    database, client = _client(tmp_path, monkeypatch)
    db = ProposalDB(str(database))
    project = SimpleNamespace(
        id="persisted", platform="freelance_ru", title="Real persisted test",
        description="local", budget=100, currency="RUB", skills=[],
        url="https://example.invalid/task", created_at="2026-10-30 13:00:00",
        offers_count=0, client_hired_percent=0, search_query="automation",
        platform_data={},
    )
    candidate_id, inserted = db.insert_candidate_if_absent(
        project, stage="parsed", status="parsed",
    )
    assert inserted
    with client:
        baseline = client.get("/api/dashboard/candidate-events").json()
        assert baseline["status"] == "ready"
        assert baseline["recent"][-1]["candidate_status"] == "parsed"
        seq = baseline["next_after_seq"]
        db.update_candidate_status(candidate_id, "queued", actor="system")
        claimed = db.claim_due_notifications("test-worker", limit=1)
        assert len(claimed) == 1
        assert db.ack_notification(
            int(claimed[0]["event_id"]), str(claimed[0]["lease_token"]), worker_id="test-worker",
        )
        response = client.get(
            f"/api/dashboard/candidate-events?after_seq={seq}&stream_id={baseline['stream_id']}"
        )
        assert response.status_code == 200
        actual = response.json()
        assert actual["outbox_status_counts"]["delivered"] == 1
        assert [(item["event_type"], item["candidate_status"], item["delivery_status"])
                for item in actual["items"]] == [
            ("candidate.status_changed", "queued", None),
            ("notification.pending", "queued", "pending"),
            ("notification.leased", "queued", "leased"),
            ("notification.provider_accepted", "queued", "provider_accepted"),
        ]
        assert {item["platform"] for item in actual["items"]} == {"freelance_ru"}


def test_actual_failed_delivery_is_not_reported_as_provider_accepted(tmp_path, monkeypatch):
    from src.action.proposal_db import ProposalDB

    database, client = _client(tmp_path, monkeypatch)
    db = ProposalDB(str(database))
    project = SimpleNamespace(
        id="failed-notification", platform="hh_ru", title="Local failure",
        description="local", budget=None, currency="RUB", skills=[],
        url="https://example.invalid/failure", created_at="2026-10-30 13:00:00",
        offers_count=0, client_hired_percent=0, search_query="qa", platform_data={},
    )
    candidate_id, _ = db.insert_candidate_if_absent(project, stage="parsed", status="parsed")
    db.update_candidate_status(candidate_id, "auto_ready")
    with client:
        baseline = client.get("/api/dashboard/candidate-events").json()
        claimed = db.claim_due_notifications("fail-worker", limit=1)
        assert len(claimed) == 1
        assert db.fail_notification(
            int(claimed[0]["event_id"]),
            str(claimed[0]["lease_token"]),
            "simulated timeout",
            worker_id="fail-worker",
            max_attempts=1,
        )
        replay = client.get(
            f"/api/dashboard/candidate-events?after_seq={baseline['next_after_seq']}"
            f"&stream_id={baseline['stream_id']}"
        ).json()
        assert replay["status"] == "ready"
        assert replay["outbox_status_counts"]["dead"] == 1
        assert [item["event_type"] for item in replay["items"]] == [
            "notification.leased", "notification.failed",
        ]
        assert all(item["event_type"] != "notification.provider_accepted" for item in replay["items"])
