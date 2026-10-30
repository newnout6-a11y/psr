"""Regression checks for durable discovery admission across retries and runs."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from src.action.proposal_db import ProposalDB


def project(platform: str = "freelance_ru", project_id: str = "42") -> SimpleNamespace:
    return SimpleNamespace(
        id=project_id,
        platform=platform,
        title="Новый заказ",
        description="Описание заказа",
        budget=1000,
        currency="RUB",
        skills=["python"],
        url=f"https://example.invalid/{platform}/{project_id}",
        created_at="2026-10-30T10:00:00Z",
        search_query="python",
        offers_count=0,
        client_hired_percent=0,
        platform_data={},
    )


def test_duplicate_page_does_not_rewind_candidate_progress(tmp_path):
    db = ProposalDB(str(tmp_path / "candidates.db"))
    first_id, inserted = db.insert_candidate_if_absent(project(), stage="parsed", status="parsed")
    assert inserted
    db.update_candidate(first_id, stage="vetted", status="queued", priority=90)
    retry_id, inserted = db.insert_candidate_if_absent(
        project(), stage="parsed", status="parsed", priority=0
    )
    assert not inserted
    assert retry_id == first_id
    candidate = db.get_candidate(first_id)
    assert candidate["stage"] == "vetted"
    assert candidate["status"] == "queued"
    assert candidate["priority"] == 90


def test_same_remote_id_from_different_sites_is_distinct(tmp_path):
    db = ProposalDB(str(tmp_path / "candidates.db"))
    ids = [db.insert_candidate_if_absent(project(platform))[0] for platform in (
        "kwork", "freelance_ru", "hh_ru"
    )]
    assert len(set(ids)) == 3


def test_simultaneous_retry_claims_only_one_new_candidate(tmp_path):
    db = ProposalDB(str(tmp_path / "candidates.db"))

    def admit(_):
        return db.insert_candidate_if_absent(project(), stage="parsed", status="parsed")

    with ThreadPoolExecutor(max_workers=6) as pool:
        admitted = list(pool.map(admit, range(12)))
    assert sum(inserted for _, inserted in admitted) == 1
    assert len({candidate_id for candidate_id, _ in admitted}) == 1


def test_retry_after_restart_preserves_existing_record(tmp_path):
    path = str(tmp_path / "candidates.db")
    candidate_id, inserted = ProposalDB(path).insert_candidate_if_absent(
        project(), stage="parsed", status="parsed"
    )
    assert inserted
    restarted = ProposalDB(path)
    second_id, inserted = restarted.insert_candidate_if_absent(
        project(), stage="parsed", status="parsed"
    )
    assert not inserted
    assert second_id == candidate_id
