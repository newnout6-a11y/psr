"""Phase 3 feedback loop — smoke tests.

Проверяют:
- миграцию схемы candidates (новые колонки добавились идемпотентно);
- ProposalDB.find_candidate_for_reply / link_reply_to_candidate;
- src.utils.reply_linker.link_inbox_response;
- src.brain.reply_classifier.classify_reply_heuristic;
- ProposalDB.set_reply_classification и set_candidate_outcome;
- dashboard queries: conversion_summary / by_provider / by_niche.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.action.proposal_db import ProposalDB  # noqa: E402
from src.brain.reply_classifier import (  # noqa: E402
    ALLOWED_LABELS,
    classify_reply_heuristic,
)
from src.parsers.base_parser import ProjectItem  # noqa: E402
from src.utils.reply_linker import link_inbox_response  # noqa: E402


def _project(*, project_id: str = "p1", platform: str = "kwork") -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title="Парсер маркетплейса",
        description="Нужен парсер с обходом капчи",
        budget=8000,
        currency="RUB",
        skills=["Python"],
        url=f"https://kwork.ru/projects/{project_id}",
        platform=platform,
        created_at="2026-04-21 12:00:00",
        offers_count=4,
        client_hired_percent=70,
        search_query="парсер",
    )


def _seed_candidate(
    db: ProposalDB,
    *,
    project_id: str = "p1",
    platform: str = "kwork",
    client_username: str = "alice",
    status: str = "auto_sent",
    provider: str = "groq",
    sent_at: str | None = None,
) -> int:
    candidate_id = db.upsert_candidate(
        _project(project_id=project_id, platform=platform),
        stage="vetted",
        status=status,
        client_username=client_username,
        provider=provider,
        sent_at=sent_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ai_score=8,
        ai_score_source="llm",
        vet_score=80,
    )
    return candidate_id


def _make_db() -> tuple[ProposalDB, Path]:
    db_path = Path(tempfile.gettempdir()) / "psr_phase3_smoke.db"
    if db_path.exists():
        db_path.unlink()
    return ProposalDB(db_path=str(db_path)), db_path


def test_schema_migration_adds_phase3_columns():
    db, db_path = _make_db()
    try:
        with sqlite3.connect(db.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
        for required in [
            "client_username",
            "replied_at",
            "reply_text",
            "reply_classification",
            "reply_classified_at",
            "won",
            "revenue",
            "prompt_variant",
        ]:
            assert required in cols, f"missing column: {required}"

        # Идемпотентность: повторное создание ProposalDB не должно падать
        db2 = ProposalDB(db_path=str(db_path))
        assert db2 is not None
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_link_reply_finds_candidate_and_records_reply():
    db, db_path = _make_db()
    try:
        candidate_id = _seed_candidate(db, client_username="alice", project_id="p1")

        # 1. Сначала ищем по точному совпадению (platform, project_id, username).
        match = db.find_candidate_for_reply("kwork", "alice", project_id="p1")
        assert match is not None
        assert match["candidate_id"] == candidate_id

        # 2. Линкуем входящий — должно записаться replied_at + reply_text.
        ok = db.link_reply_to_candidate(
            candidate_id,
            reply_text="Сколько будет стоить?",
        )
        assert ok is True

        cand = db.get_candidate(candidate_id)
        assert cand is not None
        assert cand["replied_at"]
        assert "стоить" in cand["reply_text"]

        # 3. Повторный линк (та же реплика) НЕ должен перетереть — overwrite=False.
        ok2 = db.link_reply_to_candidate(
            candidate_id,
            reply_text="Другой текст",
        )
        assert ok2 is False
        cand2 = db.get_candidate(candidate_id)
        assert "стоить" in cand2["reply_text"]
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_link_reply_falls_back_to_username_only():
    db, db_path = _make_db()
    try:
        candidate_id = _seed_candidate(
            db,
            client_username="bob",
            project_id="p2",
            sent_at=(datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )

        # У kwork-инбокса часто нет project_id. Линкер должен найти кандидата
        # только по (platform, username).
        match = db.find_candidate_for_reply("kwork", "bob", project_id=None)
        assert match is not None
        assert match["candidate_id"] == candidate_id

        # И не находит, если username unknown.
        assert db.find_candidate_for_reply("kwork", "ghost") is None
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_link_inbox_response_end_to_end():
    db, db_path = _make_db()
    try:
        candidate_id = _seed_candidate(db, client_username="carol", project_id="p3")

        result = link_inbox_response(
            db,
            {
                "platform": "kwork",
                "username": "carol",
                "project_id": "",
                "project_title": "Чат с @carol",
                "message": "Подходит, давайте обсудим сроки",
                "timestamp": "2026-05-03T10:00:00",
            },
        )

        assert result.linked is True
        assert result.candidate_id == candidate_id
        assert result.project_id == "p3"

        # Empty payload должен вернуть unlinked, не падать.
        empty = link_inbox_response(db, {"platform": "kwork", "username": "", "message": ""})
        assert empty.linked is False
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_classify_reply_heuristic_buckets():
    cases = {
        "Сколько будет стоить и какой у вас прайс?": "asks_price",
        "Покажите портфолио, пожалуйста": "asks_portfolio",
        "К сожалению, не подходит, уже нашёл": "rejected",
        "Можно дешевле? Бюджет ограничен": "negotiating",
        "Давайте обсудим, мне интересно": "interested",
    }
    for text, expected in cases.items():
        got = classify_reply_heuristic(text)
        assert got == expected, f"для текста {text!r} ожидали {expected}, получили {got}"
        assert got in ALLOWED_LABELS

    # Полностью нейтральная фраза — эвристика честно говорит «не знаю».
    assert classify_reply_heuristic("Hello") is None


def test_set_reply_classification_and_outcome():
    db, db_path = _make_db()
    try:
        candidate_id = _seed_candidate(db, client_username="dave", project_id="p4")
        db.link_reply_to_candidate(candidate_id, reply_text="Сколько стоит?")
        db.set_reply_classification(candidate_id, "asks_price")
        db.set_candidate_outcome(candidate_id, won=True, revenue=12500)

        cand = db.get_candidate(candidate_id)
        assert cand["reply_classification"] == "asks_price"
        assert cand["reply_classified_at"]
        assert cand["won"] is True
        assert abs(cand["revenue"] - 12500) < 1e-6

        # outcome без полей — no-op, не падает.
        db.set_candidate_outcome(candidate_id)
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_dashboard_conversion_queries_against_real_db(tmp_path=None):
    db, db_path = _make_db()
    try:
        # Засеять три кандидата: один auto_sent + replied + won, второй auto_sent
        # без реплая, третий manual_sent с реплаем но без win.
        c1 = _seed_candidate(db, client_username="u1", project_id="p1", provider="groq")
        c2 = _seed_candidate(db, client_username="u2", project_id="p2", provider="groq")
        c3 = _seed_candidate(db, client_username="u3", project_id="p3", provider="google", status="manual_sent")

        db.link_reply_to_candidate(c1, reply_text="Беру!")
        db.set_candidate_outcome(c1, won=True, revenue=15000)
        db.link_reply_to_candidate(c3, reply_text="Сколько стоит?")
        db.set_reply_classification(c3, "asks_price")

        # Импортим только сейчас — модуль читает PROPOSALS_DB через src.paths.
        # Но в тесте мы создаём свою БД, поэтому используем queries._read_sql
        # напрямую через monkeypatch путей.
        from src.dashboard import queries as q

        original_db = q.PROPOSALS_DB
        try:
            q.PROPOSALS_DB = str(db_path)  # type: ignore[assignment]

            summary = q.conversion_summary(days=30)
            assert summary["sent"] == 3
            assert summary["replied"] == 2
            assert summary["won"] == 1
            assert summary["revenue"] == 15000

            providers = q.conversion_by_provider(days=30)
            assert not providers.empty
            assert set(providers["provider"]) == {"groq", "google"}

            niches = q.conversion_by_niche(days=30)
            assert not niches.empty

            cls = q.reply_classification_breakdown(days=30)
            assert not cls.empty
            assert "asks_price" in set(cls["classification"])
        finally:
            q.PROPOSALS_DB = original_db  # type: ignore[assignment]
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    test_schema_migration_adds_phase3_columns()
    print("[ok] schema migration adds phase3 columns")
    test_link_reply_finds_candidate_and_records_reply()
    print("[ok] link reply finds candidate + records first touch only")
    test_link_reply_falls_back_to_username_only()
    print("[ok] link reply falls back to username-only")
    test_link_inbox_response_end_to_end()
    print("[ok] link_inbox_response e2e")
    test_classify_reply_heuristic_buckets()
    print("[ok] reply classifier heuristic buckets")
    test_set_reply_classification_and_outcome()
    print("[ok] reply classification + outcome persistence")
    test_dashboard_conversion_queries_against_real_db()
    print("[ok] dashboard conversion queries")

    # Дополнительно прогоняем async-классификатор как smoke
    from src.brain.reply_classifier import classify_reply

    async def _async_smoke() -> None:
        # При недоступном LLM эвристика для известного шаблона должна сработать.
        label = await classify_reply("Покажите портфолио, пожалуйста")
        assert label == "asks_portfolio"

    asyncio.run(_async_smoke())
    print("[ok] async classify_reply (heuristic-only path)")
    print("\nВсе проверки Phase 3 прошли.")
