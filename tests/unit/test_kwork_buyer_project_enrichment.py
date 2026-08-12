from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.platforms.kwork_buyer.enrichment_controller import BuyerAttachmentEnrichmentController
from src.platforms.kwork_buyer.repository import BuyerSearchRepository
from src.platforms.kwork_buyer.service import BuyerSearchService, BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance


async def _service_with_project(
    tmp_path: Path,
    *,
    enrichment_policy: dict[str, Any] | None = None,
) -> tuple[BuyerSearchService, BuyerSearchRepository, str, str]:
    repository = BuyerSearchRepository(tmp_path / "buyer-search.sqlite3")
    await repository.create_run(
        {
            "run_id": "run-1",
            "mode": "manual",
            "name": "Selective enrichment",
            "enrichment_policy": enrichment_policy or {},
        }
    )
    await repository.create_queries(
        "run-1",
        [
            {
                "query_id": "query-1",
                "text": "Telegram integration",
                "normalized_text": "telegram integration",
                "origin": "manual",
                "approved": True,
            }
        ],
    )
    await repository.enqueue_query_task(
        "run-1",
        {"task_id": "task-1", "query_id": "query-1", "source": "mobile_projects", "page": 1},
    )
    task = await repository.lease_query_task("run-1", "worker-1")
    assert task is not None
    await repository.commit_observed_page(
        task_id="task-1",
        worker_id="worker-1",
        attempt_id=task["attempt_id"],
        lease_fence=task["lease_fence"],
        projects=[
            {
                "id": "want-42",
                "title": "Telegram CRM integration",
                "description": "Connect the team CRM to Telegram.",
                "category_id": "12",
                "buyer_username": "acme_buyer",
                "price": 40_000,
                "offers": 2,
                "views": 17,
                "age_seconds": 120,
            }
        ],
    )
    project_id = (await repository.list_run_projects("run-1"))["items"][0]["project_id"]
    service = BuyerSearchService(
        repository,
        export_root=tmp_path / "exports",
        settings=BuyerSearchSettings(enabled=True, attachment_download=False),
    )
    return service, repository, "run-1", project_id


def _capabilities(client: object) -> BuyerReadCapabilities:
    return BuyerReadCapabilities(
        client,
        BuyerReadProvenance(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-1",
            egress_ip="198.51.100.42",
            source="selective_enrichment",
        ),
    )


class _RichReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def fetch_project_detail(self, project_id: str, **_params: Any) -> dict[str, Any]:
        self.calls.append(("project_detail", project_id))
        return {
            "status_code": 200,
            "data": {
                "project": {
                    "id": project_id,
                    "title": "Telegram CRM integration",
                    "description": "Full scope from the mobile project detail.",
                    "buyer": {"username": "acme_buyer", "projects_count": 3},
                    "api_token": "must-not-leak",
                    "files": [
                        {
                            "id": "file-brief",
                            "url": "https://files.example/brief.pdf?token=opaque-token",
                            "name": "brief.pdf",
                            "mime_type": "application/pdf",
                            "size": 1200,
                        }
                    ],
                }
            },
        }

    async def fetch_want_detail(self, want_id: str, **_params: Any) -> dict[str, Any]:
        self.calls.append(("want_detail", want_id))
        return {
            "status_code": 200,
            "data": {"want": {"id": want_id, "views": 31, "offers": 4, "buyer": {"username": "acme_buyer"}}},
        }

    async def fetch_buyer_history(self, buyer_id: str, **_params: Any) -> dict[str, Any]:
        self.calls.append(("buyer_history", buyer_id))
        return {
            "status_code": 200,
            "username": buyer_id,
            "total": 1,
            "items": [{"id": "old-1", "title": "Previous integration", "price": 10_000}],
        }

    async def fetch_web_projects(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("web_state", params))
        return {
            "status_code": 200,
            "paging": {"page": 1, "total": 1},
            "items": [
                {
                    "id": "want-42",
                    "title": "Telegram CRM integration",
                    "files": [
                        {
                            "id": "file-brief-web",
                            "url": "https://files.example/brief.pdf?token=opaque-token",
                            "name": "brief.pdf",
                            "mime_type": "application/pdf",
                            "size": 1200,
                        }
                    ],
                }
            ],
        }


@pytest.mark.asyncio
async def test_selective_enrichment_persists_raw_normalized_evidence_and_remote_attachment_manifest(tmp_path: Path) -> None:
    service, repository, run_id, project_id = await _service_with_project(tmp_path)
    client = _RichReadClient()
    factories: list[tuple[str, str, str]] = []

    async def factory(run: str, project: str, account: str) -> BuyerReadCapabilities:
        factories.append((run, project, account))
        return _capabilities(client)

    controller = BuyerAttachmentEnrichmentController(
        service,
        capabilities_factory=factory,
        settings=BuyerSearchSettings(enabled=True, attachment_download=False),
    )

    result = await controller.enrich_project(
        run_id=run_id,
        project_id=project_id,
        account_registration_id="account-1",
    )

    assert factories == [(run_id, project_id, "account-1")]
    assert {name for name, _ in client.calls} == {"project_detail", "want_detail", "buyer_history", "web_state"}
    assert result["attachment_enrichment"]["reason"] == "attachment_download_disabled_by_feature_flag"
    assert "opaque-token" not in str(result)
    assert result["detail_enrichment"]["attachment_manifest"] == [
        {
            "attachment_id": "file-brief",
            "filename": "brief.pdf",
            "content_type": "application/pdf",
            "detected_type": None,
            "size_bytes": 1200,
            "sha256": None,
            "state": "discovered",
            "has_remote_url": True,
        }
    ]

    detail = await service.get_project(run_id, project_id)
    assert {entry["kind"] for entry in detail["enrichments"]} == {
        "project_detail",
        "want_detail",
        "buyer_history",
        "web_state",
    }
    assert len(detail["attachments"]) == 1
    assert detail["attachments"][0]["filename"] == "brief.pdf"
    project_evidence = next(entry for entry in detail["enrichments"] if entry["kind"] == "project_detail")
    raw = await repository.get_raw_artifact(project_evidence["raw_artifact_id"])
    assert raw["body"]["data"]["project"]["api_token"] == "[redacted]"
    assert project_evidence["normalized"]["attachments"][0]["has_remote_url"] is True


@pytest.mark.asyncio
async def test_none_policy_prevents_capability_borrow_and_records_selection_decision(tmp_path: Path) -> None:
    service, _repository, run_id, project_id = await _service_with_project(tmp_path, enrichment_policy={"mode": "none"})
    factory_calls = 0

    async def factory(_run: str, _project: str, _account: str) -> BuyerReadCapabilities:
        nonlocal factory_calls
        factory_calls += 1
        return _capabilities(_RichReadClient())

    controller = BuyerAttachmentEnrichmentController(
        service,
        capabilities_factory=factory,
        settings=BuyerSearchSettings(enabled=True, attachment_download=True),
    )

    result = await controller.enrich_project(
        run_id=run_id,
        project_id=project_id,
        account_registration_id="account-1",
    )

    assert factory_calls == 0
    assert result["detail_enrichment"]["selection"]["reason"] == "disabled_by_run_policy"
    detail = await service.get_project(run_id, project_id)
    assert detail["enrichments"][0]["kind"] == "selection"
    assert detail["enrichments"][0]["state"] == "skipped"


class _ProjectOnlyReadClient:
    async def fetch_project_detail(self, project_id: str, **_params: Any) -> dict[str, Any]:
        return {"status_code": 200, "data": {"project": {"id": project_id, "buyer": {"username": "acme_buyer"}}}}


@pytest.mark.asyncio
async def test_unavailable_detail_sources_are_persisted_without_blocking_available_source(tmp_path: Path) -> None:
    service, _repository, run_id, project_id = await _service_with_project(tmp_path)
    controller = BuyerAttachmentEnrichmentController(
        service,
        capabilities_factory=lambda *_args: _capabilities(_ProjectOnlyReadClient()),
        settings=BuyerSearchSettings(enabled=True, attachment_download=False),
    )

    result = await controller.enrich_project(
        run_id=run_id,
        project_id=project_id,
        account_registration_id="account-1",
    )

    outcomes = {item["kind"]: item["state"] for item in result["detail_enrichment"]["sources"]}
    assert outcomes == {
        "project_detail": "completed",
        "want_detail": "unavailable",
        "buyer_history": "unavailable",
        "web_state": "unavailable",
    }
