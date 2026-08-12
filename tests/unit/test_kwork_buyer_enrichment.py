from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
from typing import Any

import pytest

from src.platforms.kwork_buyer.enrichment import (
    BuyerAttachmentEnrichmentPipeline,
    BuyerAttachmentEnrichmentPolicy,
    BuyerAttachmentEnrichmentState,
    plan_buyer_attachment_enrichment,
)
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance


class FakeAttachmentRepository:
    def __init__(self) -> None:
        self.attachments: list[dict[str, Any]] = []
        self.derivatives: list[dict[str, Any]] = []
        self._attachment_ids_by_remote_url: dict[str, str] = {}

    async def upsert_attachment(
        self,
        run_id: str,
        project_id: str,
        attachment: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {"run_id": run_id, "project_id": project_id, **attachment}
        remote_url = str(payload.get("remote_url") or "")
        attachment_id = str(payload.get("attachment_id") or self._attachment_ids_by_remote_url.get(remote_url) or "")
        if not attachment_id:
            attachment_id = f"attachment_{len(self._attachment_ids_by_remote_url) + 1}"
        if remote_url:
            self._attachment_ids_by_remote_url[remote_url] = attachment_id
        payload["attachment_id"] = attachment_id
        self.attachments.append(payload)
        return dict(payload)

    async def record_attachment_derivative(self, attachment_id: str, derivative: dict[str, Any]) -> dict[str, Any]:
        payload = {"attachment_id": attachment_id, **derivative}
        self.derivatives.append(payload)
        return dict(payload)


class FakeDownloadClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def download_attachment(self, attachment_url: str, **_params: object) -> object:
        self.calls.append(attachment_url)
        return self.responses[attachment_url]


def _capabilities(client: object) -> BuyerReadCapabilities:
    return BuyerReadCapabilities(
        client,
        BuyerReadProvenance(
            worker_id="buyer-worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-1",
            egress_ip="198.51.100.5",
            source="attachment_enrichment",
        ),
    )


@pytest.mark.asyncio
async def test_pipeline_persists_bounded_parsed_attachment_and_honest_manifest() -> None:
    url = "https://files.example/brief.txt?token=secret-token"
    client = FakeDownloadClient(
        {
            url: {
                "content": b"Scope: Telegram bot\ntoken=very-secret\nDelivery: 7 days",
                "filename": "brief.txt",
                "content_type": "text/plain; charset=utf-8",
                "resolved_download_url": "https://cdn.example/brief.txt?token=secret-token",
            }
        }
    )
    repository = FakeAttachmentRepository()
    stored: list[tuple[str, bytes]] = []

    async def write_original(candidate, download, metadata) -> str:
        stored.append((candidate.filename, download.content))
        return f"memory://attachments/{metadata.sha256}"

    pipeline = BuyerAttachmentEnrichmentPipeline(
        repository,
        _capabilities(client),
        policy=BuyerAttachmentEnrichmentPolicy(
            max_attachments_per_project=3,
            max_bytes_per_attachment=128,
            max_total_bytes_per_project=256,
        ),
        original_writer=write_original,
    )

    result = await pipeline.enrich(
        run_id="run-1",
        project_id="project-1",
        attachments=(
            {
                "url": url,
                "filename": "brief.txt",
                "content_type": "text/plain",
                "source_observation_id": "observation-1",
            },
            {
                "url": "https://files.example/program.exe",
                "filename": "program.exe",
                "content_type": "application/octet-stream",
            },
            {
                "url": "https://files.example/large.txt",
                "filename": "large.txt",
                "content_type": "text/plain",
                "size_bytes": 129,
            },
        ),
    )

    assert client.calls == [url]
    assert stored == [("brief.txt", b"Scope: Telegram bot\ntoken=very-secret\nDelivery: 7 days")]
    assert [item.state for item in result.items] == [
        BuyerAttachmentEnrichmentState.PARSED,
        BuyerAttachmentEnrichmentState.UNSUPPORTED,
        BuyerAttachmentEnrichmentState.SKIPPED,
    ]
    assert result.items[1].reason == "unsupported_type"
    assert result.items[2].reason == "attachment_byte_limit"
    assert "very-secret" not in result.context.text
    assert "secret-token" not in str(result.context_manifest)
    assert result.context_manifest["context_hash"] == result.context.context_hash
    assert len(result.context_manifest["attachments"]) == 3
    assert result.provenance["account_registration_id"] == "account-1"

    parsed = repository.attachments[-3]
    assert parsed["state"] == "parsed"
    assert parsed["sha256"] == sha256(stored[0][1]).hexdigest()
    assert parsed["object_ref"].startswith("memory://attachments/")
    assert repository.derivatives[0]["state"] == "parsed"
    assert "very-secret" not in repository.derivatives[0]["extracted_text"]
    assert repository.derivatives[1]["state"] == "unsupported"


@pytest.mark.asyncio
async def test_pipeline_uses_optional_vision_gateway_for_image_attachments() -> None:
    from PIL import Image

    image_stream = BytesIO()
    Image.new("RGB", (8, 6), color="white").save(image_stream, format="PNG")
    url = "https://files.example/brief.png"
    client = FakeDownloadClient(
        {
            url: {
                "content": image_stream.getvalue(),
                "filename": "brief.png",
                "content_type": "image/png",
            }
        }
    )
    repository = FakeAttachmentRepository()

    class _VisionGateway:
        calls = 0

        async def analyze_image(self, *, content: bytes, metadata: object) -> str:
            self.calls += 1
            assert content == image_stream.getvalue()
            assert metadata.filename == "brief.png"  # type: ignore[attr-defined]
            return "OCR scope: CRM dashboard, delivery in 5 days"

    vision = _VisionGateway()
    result = await BuyerAttachmentEnrichmentPipeline(
        repository,
        _capabilities(client),
        vision_gateway=vision,  # type: ignore[arg-type]
    ).enrich(
        run_id="run-vision",
        project_id="project-vision",
        attachments=({"url": url, "filename": "brief.png", "content_type": "image/png"},),
    )

    assert vision.calls == 1
    assert result.items[0].parse_result is not None
    assert result.items[0].parse_result.parser == "image_vision"
    assert "OCR scope" in result.context.text
    assert all(item.reason != "vision_not_configured" for item in result.items[0].parse_result.omissions)
    assert repository.derivatives[0]["parser_name"] == "image_vision"


@pytest.mark.asyncio
async def test_pipeline_bounds_custom_reader_timeout_and_records_terminal_failure() -> None:
    repository = FakeAttachmentRepository()

    async def never_return(_request):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    pipeline = BuyerAttachmentEnrichmentPipeline(
        repository,
        _capabilities(FakeDownloadClient({})),
        policy=BuyerAttachmentEnrichmentPolicy(download_timeout_seconds=0.01),
        attachment_reader=never_return,
    )

    result = await pipeline.enrich(
        run_id="run-1",
        project_id="project-1",
        attachments=({"url": "https://files.example/brief.txt", "filename": "brief.txt"},),
    )

    assert result.items[0].state is BuyerAttachmentEnrichmentState.FAILED
    assert result.items[0].reason == "download_timeout"
    assert repository.attachments[-1]["state"] == "failed"
    assert repository.attachments[-1]["error"] == "download_timeout"
    assert repository.derivatives == []


@pytest.mark.asyncio
async def test_pipeline_enforces_total_byte_budget_and_reuses_checksum_parse_result() -> None:
    first_url = "https://files.example/first.txt"
    second_url = "https://files.example/second.txt"
    duplicate_url = "https://files.example/duplicate.txt"
    body = b"same"
    client = FakeDownloadClient(
        {
            first_url: body,
            second_url: b"more",
            duplicate_url: body,
        }
    )
    repository = FakeAttachmentRepository()
    pipeline = BuyerAttachmentEnrichmentPipeline(
        repository,
        _capabilities(client),
        policy=BuyerAttachmentEnrichmentPolicy(
            max_attachments_per_project=3,
            max_bytes_per_attachment=10,
            max_total_bytes_per_project=8,
        ),
    )

    result = await pipeline.enrich(
        run_id="run-1",
        project_id="project-1",
        attachments=(
            {"url": first_url, "filename": "first.txt"},
            {"url": duplicate_url, "filename": "duplicate.txt"},
            {"url": second_url, "filename": "second.txt"},
        ),
    )

    assert client.calls == [first_url, duplicate_url, second_url]
    assert [item.state for item in result.items] == [
        BuyerAttachmentEnrichmentState.PARSED,
        BuyerAttachmentEnrichmentState.PARSED,
        BuyerAttachmentEnrichmentState.TOO_LARGE,
    ]
    assert result.items[1].checksum_deduplicated is True
    assert result.items[2].reason == "project_byte_limit"
    assert result.bytes_downloaded == 12
    assert repository.derivatives[-1]["state"] == "too_large"


def test_attachment_selection_is_source_ordered_and_bounded_before_network_reads() -> None:
    decisions = plan_buyer_attachment_enrichment(
        (
            {"url": "https://files.example/first.txt", "filename": "first.txt", "size_bytes": 5},
            {"url": "https://files.example/second.txt", "filename": "second.txt", "size_bytes": 5},
            {"url": "https://files.example/program.exe", "filename": "program.exe"},
        ),
        policy=BuyerAttachmentEnrichmentPolicy(
            max_attachments_per_project=1,
            max_bytes_per_attachment=10,
            max_total_bytes_per_project=10,
        ),
    )

    assert [(decision.selected, decision.reason) for decision in decisions] == [
        (True, None),
        (False, "attachment_limit"),
        (False, "attachment_limit"),
    ]
