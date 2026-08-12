from __future__ import annotations

from typing import Any

import pytest

from src.platforms.kwork_buyer.enrichment_controller import (
    BuyerAttachmentEnrichmentController,
    BuyerAttachmentEnrichmentControllerError,
)
from src.platforms.kwork_buyer.service import BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance


class _SearchService:
    def __init__(self) -> None:
        self.enrichment_policy: dict[str, Any] = {
            "sources": {
                "project_detail": False,
                "want_detail": False,
                "buyer_history": False,
                "web_state": False,
                "attachment_manifest": False,
            }
        }
        self.project: dict[str, Any] = {
            "project_id": "project-1",
            "remote_project_id": "remote-1",
            "attachments": [
                {
                    "attachment_id": "attachment-1",
                    "remote_url": "https://files.example/brief.txt?token=secret-token",
                    "filename": "brief.txt",
                    "content_type": "text/plain",
                    "state": "discovered",
                }
            ],
        }
        self.attachments: list[dict[str, Any]] = []
        self.derivatives: list[dict[str, Any]] = []
        self.enrichments: list[dict[str, Any]] = []

    async def get_run(self, run_id: str) -> dict[str, Any]:
        assert run_id == "run-1"
        return {"run_id": run_id, "enrichment_policy": dict(self.enrichment_policy)}

    async def get_project(self, run_id: str, project_id: str) -> dict[str, Any]:
        assert (run_id, project_id) == ("run-1", "project-1")
        return dict(self.project)

    async def record_attachment(self, run_id: str, project_id: str, attachment: dict[str, Any]) -> dict[str, Any]:
        assert (run_id, project_id) == ("run-1", "project-1")
        stored = {"attachment_id": "attachment-1", **attachment}
        self.attachments.append(stored)
        return stored

    async def record_project_enrichment(
        self,
        run_id: str,
        project_id: str,
        enrichment: dict[str, Any],
    ) -> dict[str, Any]:
        assert (run_id, project_id) == ("run-1", "project-1")
        stored = {
            "enrichment_id": f"enrichment-{len(self.enrichments) + 1}",
            "state": enrichment.get("state", "completed"),
            **enrichment,
        }
        self.enrichments.append(stored)
        return stored

    async def record_attachment_derivative(
        self,
        run_id: str,
        project_id: str,
        attachment_id: str,
        derivative: dict[str, Any],
    ) -> dict[str, Any]:
        assert (run_id, project_id, attachment_id) == ("run-1", "project-1", "attachment-1")
        stored = {"derivative_id": "derivative-1", "attachment_id": attachment_id, **derivative}
        self.derivatives.append(stored)
        return stored


class _ReadClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def download_attachment(self, attachment_url: str, **_params: object) -> dict[str, object]:
        self.urls.append(attachment_url)
        return {
            "content": b"Scope: Telegram bot\ntoken=discard-me\nDelivery: 7 days",
            "filename": "brief.txt",
            "content_type": "text/plain",
        }


class _ClosableCapabilities(BuyerReadCapabilities):
    def __init__(self, client: object, provenance: BuyerReadProvenance) -> None:
        super().__init__(client, provenance)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _capabilities(client: object, *, account_registration_id: str = "account-1") -> BuyerReadCapabilities:
    return BuyerReadCapabilities(
        client,
        BuyerReadProvenance(
            worker_id="buyer-attachment-worker-1",
            account_registration_id=account_registration_id,
            transport_id="vpnte-1",
            egress_ip="198.51.100.20",
            source="attachment_enrichment",
        ),
    )


@pytest.mark.asyncio
async def test_controller_enriches_durable_manifest_with_account_bound_capabilities() -> None:
    search_service = _SearchService()
    client = _ReadClient()
    calls: list[tuple[str, str, str]] = []

    async def capabilities_factory(run_id: str, project_id: str, account_registration_id: str) -> BuyerReadCapabilities:
        calls.append((run_id, project_id, account_registration_id))
        return _capabilities(client)

    controller = BuyerAttachmentEnrichmentController(
        search_service,
        capabilities_factory=capabilities_factory,
        settings=BuyerSearchSettings(attachment_download=True),
    )

    result = await controller.enrich_project(
        run_id="run-1",
        project_id="project-1",
        account_registration_id="account-1",
    )

    assert calls == [("run-1", "project-1", "account-1")]
    assert client.urls == ["https://files.example/brief.txt?token=secret-token"]
    assert result["parsed_count"] == 1
    assert result["items"][0]["state"] == "parsed"
    assert result["provenance"]["account_registration_id"] == "account-1"
    assert "discard-me" not in str(result)
    assert "secret-token" not in str(result)
    assert search_service.attachments[-1]["state"] == "parsed"
    assert search_service.derivatives[-1]["state"] == "parsed"


@pytest.mark.asyncio
async def test_controller_skips_downloads_when_disabled_and_rejects_mismatched_capability_account() -> None:
    search_service = _SearchService()
    mismatched_capabilities = _ClosableCapabilities(
        _ReadClient(),
        BuyerReadProvenance(
            worker_id="buyer-attachment-worker-1",
            account_registration_id="other-account",
            transport_id="vpnte-1",
            egress_ip="198.51.100.20",
            source="attachment_enrichment",
        ),
    )

    def factory(_run_id: str, _project_id: str, _account_registration_id: str) -> BuyerReadCapabilities:
        return mismatched_capabilities

    matching = BuyerAttachmentEnrichmentController(
        search_service,
        capabilities_factory=lambda *_args: _capabilities(_ReadClient()),
        settings=BuyerSearchSettings(attachment_download=False),
    )
    skipped = await matching.enrich_project(run_id="run-1", project_id="project-1", account_registration_id="account-1")
    assert skipped["attachment_enrichment"]["reason"] == "attachment_download_disabled_by_feature_flag"

    enabled = BuyerAttachmentEnrichmentController(
        search_service,
        capabilities_factory=factory,
        settings=BuyerSearchSettings(attachment_download=True),
    )
    with pytest.raises(BuyerAttachmentEnrichmentControllerError, match="does not match"):
        await enabled.enrich_project(run_id="run-1", project_id="project-1", account_registration_id="account-1")
    assert mismatched_capabilities.close_calls == 1
