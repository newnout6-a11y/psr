"""Smoke-тест на FastAPI conversion endpoints (Phase 3).

Поднимает приложение через `fastapi.testclient.TestClient` (без uvicorn),
монкипатчит `src.dashboard.queries.PROPOSALS_DB` на временную БД, в которой
сидят 3 кандидата, и проверяет каждый эндпоинт `/api/dashboard/conversion/*`.

Запуск:
    python tests/smoke/test_conversion_api.py
"""

from __future__ import annotations

import gc
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.action.proposal_db import ProposalDB  # noqa: E402
from src.parsers.base_parser import ProjectItem  # noqa: E402


def _project(*, project_id: str, platform: str = "kwork") -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title=f"Project {project_id}",
        description="Парсер маркетплейса",
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
    project_id: str,
    client_username: str,
    provider: str,
    status: str = "auto_sent",
    prompt_variant: str | None = None,
) -> int:
    return db.upsert_candidate(
        _project(project_id=project_id),
        stage="vetted",
        status=status,
        client_username=client_username,
        provider=provider,
        sent_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ai_score=8,
        ai_score_source="llm",
        vet_score=80,
        prompt_variant=prompt_variant,
    )


def _seed_db(db_path: Path) -> ProposalDB:
    if db_path.exists():
        db_path.unlink()
    db = ProposalDB(db_path=str(db_path))
    c1 = _seed_candidate(db, project_id="p1", client_username="u1", provider="groq", prompt_variant="control")
    c2 = _seed_candidate(db, project_id="p2", client_username="u2", provider="groq", prompt_variant="warm")
    c3 = _seed_candidate(db, project_id="p3", client_username="u3", provider="google", status="manual_sent")
    db.link_reply_to_candidate(c1, reply_text="Беру!")
    db.set_reply_classification(c1, "interested")
    db.set_candidate_outcome(c1, won=True, revenue=15000)
    db.link_reply_to_candidate(c3, reply_text="Сколько стоит?")
    db.set_reply_classification(c3, "asks_price")
    return db


def _build_test_client(db_path: Path):
    """Клиент, у которого `queries.PROPOSALS_DB` указывает на нашу временную БД.

    Делать import фабрикой — критично: иначе модули dashboard/queries и
    routes/dashboard захватят реальный путь до того, как мы успеем
    подменить его. Поэтому: сначала меняем константу в queries, потом
    импортим server.app. TestClient наследует тот же state.
    """
    from src.dashboard import queries as q

    q.PROPOSALS_DB = str(db_path)  # type: ignore[assignment]

    from fastapi.testclient import TestClient

    from src.api.server import app

    return TestClient(app), q


def test_conversion_endpoints_against_real_db():
    db_path = Path(tempfile.gettempdir()) / "psr_phase3_api_smoke.db"
    db = _seed_db(db_path)

    try:
        client, q = _build_test_client(db_path)
        original_db = q.PROPOSALS_DB
        q.PROPOSALS_DB = str(db_path)  # type: ignore[assignment]

        try:
            # 1. Сводка
            r = client.get("/api/dashboard/conversion/summary?days=30")
            assert r.status_code == 200, r.text
            payload = r.json()
            assert payload["sent"] == 3
            assert payload["replied"] == 2
            assert payload["won"] == 1
            assert payload["revenue"] == 15000
            # reply_rate = 2/3 = 66.67
            assert 60.0 <= payload["reply_rate"] <= 70.0
            # win_rate = 1/3 = 33.33
            assert 30.0 <= payload["win_rate"] <= 40.0

            # 2. По провайдеру — две группы
            r = client.get("/api/dashboard/conversion/by-provider?days=30")
            assert r.status_code == 200, r.text
            providers = {row["provider"] for row in r.json()}
            assert providers == {"groq", "google"}

            # 3. По нише
            r = client.get("/api/dashboard/conversion/by-niche?days=30&limit=5")
            assert r.status_code == 200, r.text
            assert any(row["niche"] == "парсер" for row in r.json())

            # 4. По очереди
            r = client.get("/api/dashboard/conversion/by-queue-position?days=30")
            assert r.status_code == 200, r.text
            assert len(r.json()) >= 1

            # 5. По времени отклика — replies = 2
            r = client.get("/api/dashboard/conversion/by-response-time?days=30")
            assert r.status_code == 200, r.text
            replies_total = sum(row["replies"] for row in r.json())
            assert replies_total == 2

            # 6. По A/B prompt_variant
            r = client.get("/api/dashboard/conversion/by-prompt-variant?days=30")
            assert r.status_code == 200, r.text
            variants = {row["prompt_variant"] for row in r.json()}
            assert {"control", "warm", "default"}.issuperset(variants)

            # 7. Классификация
            r = client.get("/api/dashboard/conversion/classification-breakdown?days=30")
            assert r.status_code == 200, r.text
            classes = {row["classification"] for row in r.json()}
            assert "interested" in classes
            assert "asks_price" in classes

            # 8. Aggregate — один JSON со всеми блоками
            r = client.get("/api/dashboard/conversion?days=30")
            assert r.status_code == 200, r.text
            agg = r.json()
            for key in (
                "summary",
                "by_provider",
                "by_niche",
                "by_queue_position",
                "by_response_time",
                "by_prompt_variant",
                "classification_breakdown",
            ):
                assert key in agg, f"aggregate missing {key}"
            assert agg["summary"]["sent"] == 3

            # 9. Параметры валидации (days вне диапазона)
            r = client.get("/api/dashboard/conversion/summary?days=0")
            assert r.status_code == 422
            r = client.get("/api/dashboard/conversion/summary?days=999")
            assert r.status_code == 422
        finally:
            q.PROPOSALS_DB = original_db  # type: ignore[assignment]
    finally:
        del db
        gc.collect()
        if db_path.exists():
            db_path.unlink()


def test_conversion_endpoints_with_no_db_dont_crash(monkeypatch=None):
    """Если БД нет / нет phase3 колонок — эндпоинты должны вернуть пустые
    структуры, а не 500.
    """
    bogus_path = Path(tempfile.gettempdir()) / "psr_phase3_api_missing.db"
    if bogus_path.exists():
        bogus_path.unlink()

    from src.dashboard import queries as q

    original_db = q.PROPOSALS_DB
    q.PROPOSALS_DB = str(bogus_path)  # type: ignore[assignment]
    try:
        from fastapi.testclient import TestClient

        from src.api.server import app

        client = TestClient(app)

        r = client.get("/api/dashboard/conversion/summary?days=30")
        assert r.status_code == 200
        body = r.json()
        assert body["sent"] == 0
        assert body["reply_rate"] == 0.0

        r = client.get("/api/dashboard/conversion/by-provider?days=30")
        assert r.status_code == 200
        assert r.json() == []

        r = client.get("/api/dashboard/conversion?days=30")
        assert r.status_code == 200
        agg = r.json()
        assert agg["summary"]["sent"] == 0
        assert agg["by_provider"] == []
    finally:
        q.PROPOSALS_DB = original_db  # type: ignore[assignment]


if __name__ == "__main__":
    test_conversion_endpoints_against_real_db()
    print("[ok] conversion endpoints против реальной БД")
    test_conversion_endpoints_with_no_db_dont_crash()
    print("[ok] эндпоинты не падают на отсутствующей БД")
    print("\nВсе проверки conversion API прошли.")
