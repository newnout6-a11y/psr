import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.action.proposal_image import ProposalImageGenerator
from src.filter.ai_scorer import AIRelevanceScorer, ScoreResult
from src.orchestrator import FreelanceOrchestrator, _limit_payloads_per_platform
from src.parsers.base_parser import ProjectItem
from src.parsers.kwork_api_parser import KworkAPIParser
from src.platforms.kwork import KworkAPIResponseError, KworkService


def _project(project_id: str, *, platform: str = "kwork") -> ProjectItem:
    return ProjectItem(
        id=project_id,
        title=f"Python automation {project_id}",
        description="Need Python API automation script",
        budget=5000,
        currency="RUB",
        skills=["python", "api"],
        url=f"https://kwork.ru/projects/{project_id}",
        platform=platform,
        created_at="2026-06-09 12:00:00",
        offers_count=0,
        client_hired_percent=50,
    )


def test_proposal_generation_limit_is_applied_before_llm_work():
    payloads = [
        {"project": _project("k1", platform="kwork")},
        {"project": _project("k2", platform="kwork")},
        {"project": _project("f1", platform="freelance_ru")},
        {"project": _project("k3", platform="kwork")},
    ]

    selected, limited_out = _limit_payloads_per_platform(payloads, limit_per_platform=1)

    assert [item["project"].id for item in selected] == ["k1", "f1"]
    assert limited_out == 2


class _ExistingCandidateDB:
    def __init__(self, existing):
        self.existing = existing

    def get_candidate_by_project(self, _project_id, _platform):
        return self.existing


def test_auto_skipped_candidate_is_retryable_on_next_cycle():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    orch.db = _ExistingCandidateDB(
        {"status": "skipped", "manual_override": False, "last_actor": "system"}
    )

    assert orch._is_blocked_by_existing_status(_project("retry")) is False


def test_manual_skipped_candidate_remains_blocked():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    orch.db = _ExistingCandidateDB(
        {"status": "skipped", "manual_override": True, "last_actor": "ui"}
    )

    assert orch._is_blocked_by_existing_status(_project("manual-skip")) is True


def test_reprocessed_candidate_clears_stale_generation_fields():
    class CaptureDB:
        def __init__(self):
            self.fields = {}

        def upsert_candidate(self, _project, **fields):
            self.fields = fields
            return 42

    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    orch.db = CaptureDB()
    orch._execution_mode = lambda: "semi_auto"

    candidate_id = orch._stage_candidate(_project("stale"), "parsed", "parsed", dry_run=1)

    assert candidate_id == 42
    assert orch.db.fields["proposal_text"] is None
    assert orch.db.fields["ai_score"] is None
    assert orch.db.fields["vet_score"] is None
    assert orch.db.fields["manual_override"] == 0
    assert orch.db.fields["dry_run"] == 1


def test_kwork_projects_missing_response_with_paging_is_empty_page():
    class FakeAPI:
        async def request(self, *_args, **_kwargs):
            return {"success": True, "paging": {"page": 2}}

    service = KworkService()

    async def fake_get_api():
        return FakeAPI()

    service.get_api = fake_get_api

    projects = asyncio.run(
        service.get_projects(
            categories_ids=[11],
            page=2,
            query="python",
        )
    )

    assert projects == []


def test_wide_kwork_discovery_stops_on_empty_page():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    pages = {
        1: [_project("1"), _project("2")],
        2: [_project("3")],
        3: [_project("4")],
        4: [],
    }

    async def fake_parse(_parser, page, _per_page, query):
        return "kwork", query, page, pages[page], 1, None

    orch._parse_projects = fake_parse
    results, stats = asyncio.run(
        orch._discover_wide_kwork(
            object(),
            ["python"],
            per_page=20,
            max_pages_per_query=50,
            max_projects_per_cycle=500,
            max_parse_seconds=90,
        )
    )

    assert len(results) == 4
    assert stats["raw_found"] == 4
    assert stats["deduped"] == 4
    assert stats["query_stops"]["python"] == "empty_page"


def test_wide_kwork_discovery_stops_on_duplicate_heavy_page():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    duplicate = [_project("1"), _project("2")]

    async def fake_parse(_parser, page, _per_page, query):
        projects = duplicate if page in {1, 2} else [_project("3")]
        return "kwork", query, page, projects, 1, None

    orch._parse_projects = fake_parse
    with patch.dict(os.environ, {"WIDE_DUPLICATE_STOP_RATIO": "0.8", "WIDE_DUPLICATE_STOP_PAGES": "1"}):
        _results, stats = asyncio.run(
            orch._discover_wide_kwork(
                object(),
                ["python"],
                per_page=20,
                max_pages_per_query=50,
                max_projects_per_cycle=500,
                max_parse_seconds=90,
            )
        )

    assert stats["pages_scanned"] == 2
    assert stats["query_stops"]["python"] == "duplicate_heavy"


def test_wide_kwork_discovery_stops_on_project_limit():
    orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
    many = [_project(str(i)) for i in range(30)]

    async def fake_parse(_parser, page, _per_page, query):
        return "kwork", query, page, many, 1, None

    orch._parse_projects = fake_parse
    _results, stats = asyncio.run(
        orch._discover_wide_kwork(
            object(),
            ["python"],
            per_page=20,
            max_pages_per_query=50,
            max_projects_per_cycle=20,
            max_parse_seconds=90,
        )
    )

    assert stats["stop_reason"] == "max_projects"
    assert stats["deduped"] == 30


def test_kwork_api_parser_does_not_truncate_to_per_page():
    parser = KworkAPIParser.__new__(KworkAPIParser)
    parser.min_budget = 0
    parser.max_budget = 0
    parser.min_hiring = 0
    parser.max_proposals = 0
    parser._resolve_categories = lambda _query: [11]
    parser._normalize = lambda _raw: [_project(str(i)) for i in range(5)]

    async def fake_get_api():
        return object()

    async def fake_get_projects_with_retry(_api, **_kwargs):
        return list(range(5))

    parser._get_api = fake_get_api
    parser._get_projects_with_retry = fake_get_projects_with_retry

    projects = asyncio.run(parser.get_projects(page=1, per_page=2, filters={"query": "python"}))

    assert len(projects) == 5


def test_fast_full_scoring_batches_all_prefiltered_survivors(monkeypatch):
    scorer = AIRelevanceScorer()
    projects = [_project(str(i)) for i in range(3)]
    seen_batches: list[int] = []

    async def fake_score_batch(batch, threshold):
        seen_batches.append(len(batch))
        return [
            (
                idx,
                ScoreResult(
                    project=project,
                    pre_score=heuristic.pre_score,
                    final_score=heuristic.final_score,
                    threshold=threshold,
                    source="llm",
                    llm_attempted=True,
                    llm_used=True,
                ),
            )
            for idx, project, heuristic in batch
        ]

    monkeypatch.setattr(scorer, "_score_batch_with_llm", fake_score_batch)
    with patch.dict(
        os.environ,
        {"AI_SCORE_MODE": "fast_full", "AI_SCORE_BATCH_SIZE": "2", "AI_SCORE_MAX_CANDIDATES": "3"},
    ):
        asyncio.run(scorer.evaluate_projects(projects, threshold=6))

    assert seen_batches == [2, 1]


def test_deepseek_scoring_uses_smaller_batches(monkeypatch):
    scorer = AIRelevanceScorer()
    projects = [_project(str(i)) for i in range(5)]
    seen_batches: list[int] = []

    async def fake_score_batch(batch, threshold):
        seen_batches.append(len(batch))
        return [
            (
                idx,
                ScoreResult(
                    project=project,
                    pre_score=heuristic.pre_score,
                    final_score=heuristic.final_score,
                    threshold=threshold,
                    source="llm",
                    llm_attempted=True,
                    llm_used=True,
                ),
            )
            for idx, project, heuristic in batch
        ]

    monkeypatch.setattr(scorer, "_score_batch_with_llm", fake_score_batch)
    with patch.dict(
        os.environ,
        {
            "AI_SCORE_MODE": "fast_full",
            "AI_SCORE_BATCH_SIZE": "20",
            "AI_SCORE_DEEPSEEK_BATCH_SIZE": "2",
            "AI_SCORE_MAX_CANDIDATES": "5",
            "SCORING_PROVIDER": "deepseek",
        },
    ):
        asyncio.run(scorer.evaluate_projects(projects, threshold=6))

    assert seen_batches == [2, 2, 1]


def test_proposal_image_stores_artifact_but_does_not_attach_until_confirmed(tmp_path: Path, monkeypatch):
    generator = ProposalImageGenerator(output_dir=tmp_path)

    async def fake_generate(_prompt):
        return b"x" * 2048

    async def fake_validate(_path, _project, _proposal):
        return True, "ok"

    monkeypatch.setattr(generator, "_generate_image_bytes", fake_generate)
    monkeypatch.setattr(generator, "_validate_image", fake_validate)

    with patch.dict(
        os.environ,
        {
            "PROPOSAL_IMAGE_ENABLED": "true",
            "KWORK_IMAGE_ATTACH_CONFIRMED": "false",
            "PROPOSAL_IMAGE_MIN_AI_SCORE": "8",
            "PROPOSAL_IMAGE_MIN_VET_SCORE": "70",
        },
    ):
        asset = asyncio.run(
            generator.maybe_generate(_project("img"), "I can do this.", ai_score=9, vet_score=80)
        )

    assert asset is not None
    assert asset.status == "validated"
    assert asset.attachable is False
    assert Path(asset.path).exists()


def test_proposal_image_validation_fail_is_text_only(tmp_path: Path, monkeypatch):
    generator = ProposalImageGenerator(output_dir=tmp_path)

    async def fake_generate(_prompt):
        return b"x" * 2048

    async def fake_validate(_path, _project, _proposal):
        return False, "bad text"

    monkeypatch.setattr(generator, "_generate_image_bytes", fake_generate)
    monkeypatch.setattr(generator, "_validate_image", fake_validate)

    with patch.dict(
        os.environ,
        {"PROPOSAL_IMAGE_ENABLED": "true", "KWORK_IMAGE_ATTACH_CONFIRMED": "true"},
    ):
        asset = asyncio.run(
            generator.maybe_generate(_project("bad-img"), "I can do this.", ai_score=9, vet_score=80)
        )

    assert asset is not None
    assert asset.status == "validation_failed"
    assert asset.attachable is False
