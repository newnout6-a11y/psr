"""The desktop conversion endpoint uses persisted candidates, not fabricated data."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.dashboard import router
from src.dashboard import queries


def _timestamp(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE candidates (
            candidate_id INTEGER PRIMARY KEY, status TEXT, sent_at TEXT,
            updated_at TEXT, replied_at TEXT, reply_classification TEXT,
            won INTEGER, revenue REAL, currency TEXT, provider TEXT,
            search_query TEXT, offers_count INTEGER, prompt_variant TEXT
        );
        CREATE TABLE earnings (candidate_id INTEGER, status TEXT, created_at TEXT);
        """)
        db.executemany(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "completed", _timestamp(2), _timestamp(), _timestamp(1), "interested", 1, 12000, "RUB", "groq", "python", 3, "A"),
                (2, "auto_sent", _timestamp(3), _timestamp(), None, None, 0, None, "RUB", "groq", "python", 0, "B"),
                (3, "manual_sent", _timestamp(50), _timestamp(), _timestamp(1), "rejected", 0, 0, "RUB", "google", "site", 5, "B"),
                (4, "draft", _timestamp(2), _timestamp(), None, None, 0, None, "RUB", "google", "site", 0, "A"),
                (5, "hired", _timestamp(2), _timestamp(), _timestamp(1), "interested", 1, 900, "USD", "google", "site", 8, "A"),
                (6, "declined", _timestamp(2), _timestamp(), None, None, 0, None, "RUB", "google", "site", 8, "A"),
            ],
        )
        db.execute("INSERT INTO earnings VALUES (NULL, 'paid', ?)", (_timestamp(),))


def test_conversion_endpoint_uses_sent_cohort_and_persisted_breakdowns(tmp_path, monkeypatch):
    db_path = tmp_path / "conversion.sqlite3"
    _seed(db_path)
    monkeypatch.setattr(queries, "PROPOSALS_DB", str(db_path))
    monkeypatch.setattr(queries, "_ensure_proposals_schema", lambda: None)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/dashboard/conversion?days=30")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["summary"] == {"sent": 4, "replied": 2, "won": 2, "revenue": 12000.0, "reply_rate": 50.0, "win_rate": 50.0}
        assert sum(row["sent"] for row in data["by_provider"]) == 4
        assert sum(row["sent"] for row in data["by_niche"]) == 4
        assert sum(row["sent"] for row in data["by_queue_position"]) == 4
        assert sum(row["sent"] for row in data["by_prompt_variant"]) == 4
        assert sum(row["replies"] for row in data["classification_breakdown"]) == 2
        assert sum(row["replies"] for row in data["by_response_time"]) == 2
        assert client.get("/api/dashboard/conversion?days=91").status_code == 422


def test_conversion_endpoint_distinguishes_missing_schema_from_empty_cohort(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(queries, "PROPOSALS_DB", str(db_path))
    monkeypatch.setattr(queries, "_ensure_proposals_schema", lambda: None)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/dashboard/conversion").json()["status"] == "unavailable"
        _seed(db_path)
        empty = client.get("/api/dashboard/conversion?days=1").json()
        assert empty["status"] == "ready"
        assert empty["summary"]["sent"] == 0
