"""Read-only, stable insertion cursor for orchestrator candidate observability."""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.dashboard import router
from src.dashboard import queries


def _client(tmp_path, monkeypatch):
    path = tmp_path / "proposal-candidates.sqlite3"
    monkeypatch.setattr(queries, "PROPOSALS_DB", str(path))
    app = FastAPI()
    app.include_router(router)
    return path, TestClient(app)


def _create_candidates(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE candidates (
                candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL, platform TEXT NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id, platform)
            )"""
        )


def _insert(path, project_id, platform, status="parsed"):
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            "INSERT INTO candidates(project_id, platform, title, status, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, platform, f"Task {project_id}", status, "2026-10-30T12:00:00Z"),
        )
        return cursor.lastrowid


def test_new_candidate_insertions_are_paginated_without_replay_or_reprocessing(tmp_path, monkeypatch):
    path, client = _client(tmp_path, monkeypatch)
    _create_candidates(path)
    first = _insert(path, "remote-1", "kwork")
    second = _insert(path, "remote-2", "freelanceru", "queued")
    with client:
        initial = client.get("/api/dashboard/candidate-discovery")
        assert initial.status_code == 200
        snapshot = initial.json()
        assert snapshot["status"] == "ready"
        assert snapshot["latest_id"] == second
        assert snapshot["next_after_id"] == second
        assert snapshot["items"] == []
        assert snapshot["total"] == 2
        assert snapshot["by_platform"] == [
            {"platform": "freelanceru", "total": 1},
            {"platform": "kwork", "total": 1},
        ]
        assert {row["status"] for row in snapshot["recent"]} == {"parsed", "queued"}

        # Orchestrator updating a pre-existing candidate is NOT a new insert.
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE candidates SET status = 'queued', updated_at = '2026-10-30T13:00:00Z' WHERE candidate_id = ?",
                (first,),
            )
        inserted_ids = [_insert(path, f"remote-{i}", "hh", "parsed") for i in range(3, 6)]
        first_page = client.get(f"/api/dashboard/candidate-discovery?after_id={second}&limit=2")
        assert first_page.status_code == 200
        batch = first_page.json()
        assert [row["candidate_id"] for row in batch["items"]] == inserted_ids[:2]
        assert batch["has_more"] is True
        assert batch["next_after_id"] == inserted_ids[1]
        next_page = client.get(f"/api/dashboard/candidate-discovery?after_id={batch['next_after_id']}&limit=2").json()
        assert [row["candidate_id"] for row in next_page["items"]] == inserted_ids[2:]
        assert next_page["has_more"] is False
        empty = client.get(f"/api/dashboard/candidate-discovery?after_id={inserted_ids[-1]}").json()
        assert empty["items"] == []
        assert empty["next_after_id"] == inserted_ids[-1]
        assert empty["by_platform"][0] == {"platform": "hh", "total": 3}


def test_candidate_discovery_missing_database_and_schema_are_explicit_without_writes(tmp_path, monkeypatch):
    path, client = _client(tmp_path, monkeypatch)
    with client:
        unavailable = client.get("/api/dashboard/candidate-discovery").json()
        assert unavailable["status"] == "unavailable"
        assert unavailable["items"] == []
        assert not path.exists(), "Monitoring must not create ProposalDB"

        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE candidates(candidate_id INTEGER PRIMARY KEY)")
        schema = client.get("/api/dashboard/candidate-discovery").json()
        assert schema["status"] == "unavailable"
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        assert columns == {"candidate_id"}, "Monitoring must not migrate an existing DB"


def test_candidate_discovery_route_validates_cursor_and_limit(tmp_path, monkeypatch):
    path, client = _client(tmp_path, monkeypatch)
    _create_candidates(path)
    with client:
        for url in [
            "/api/dashboard/candidate-discovery?after_id=-1",
            "/api/dashboard/candidate-discovery?limit=0",
            "/api/dashboard/candidate-discovery?limit=201",
        ]:
            assert client.get(url).status_code == 422
