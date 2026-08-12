from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from src.platforms.kwork_buyer.export import BuyerExportFormat, BuyerExportSnapshot, build_buyer_export
from src.platforms.kwork_buyer.scoring import BuyerScoreProfile, score_buyer_project


PROJECT = {
    "project_id": "project-1",
    "title": "Build Telegram bot",
    "description": "Need a production-ready Telegram bot with payments and admin tools.",
    "budget_min": 15_000,
    "budget_max": 35_000,
    "offers": 3,
    "views": 14,
    "age_seconds": 900,
    "buyer_hired_percent": 80,
    "attachment_count": 1,
    "matched_queries": ["telegram bot"],
    "attachment_manifest": [{"filename": "brief.pdf", "sha256": "sha256:fixture"}],
    "url": "https://kwork.ru/projects/1",
}


def test_preliminary_score_is_deterministic_and_has_a_versioned_input_hash() -> None:
    profile = BuyerScoreProfile(profile_id="fixture", version="2")
    first = score_buyer_project(PROJECT, profile=profile)
    second = score_buyer_project(dict(PROJECT), profile=profile)

    assert first == second
    assert 0 <= first.total <= 100
    assert first.input_hash.startswith("sha256:")
    assert set(first.breakdown) == {"budget", "competition", "freshness", "buyer_history", "description", "attachments"}


def test_export_formats_preserve_full_description_and_reproducible_manifest() -> None:
    snapshot = BuyerExportSnapshot(
        run_id="run-1",
        format=BuyerExportFormat.JSONL,
        filters={"score_gte": 60},
        selected_project_ids=("project-1",),
        include_attachments=True,
    )
    first = build_buyer_export(snapshot, [PROJECT])
    second = build_buyer_export(snapshot, [dict(PROJECT)])

    assert first.content == second.content
    assert first.manifest == second.manifest
    assert PROJECT["description"].encode("utf-8") in first.content
    assert first.manifest["attachment_count"] == 1


def test_zip_export_contains_dataset_and_manifest() -> None:
    artifact = build_buyer_export(
        BuyerExportSnapshot(run_id="run-zip", format=BuyerExportFormat.ZIP, filters={}),
        [PROJECT],
    )

    with ZipFile(BytesIO(artifact.content)) as archive:
        assert set(archive.namelist()) == {"projects.jsonl", "projects.csv", "manifest.json"}
        assert PROJECT["description"] in archive.read("projects.jsonl").decode("utf-8")
