from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_attachments import router
from src.platforms.kwork_buyer.attachment_object_store import LocalBuyerAttachmentObjectStore
from src.platforms.kwork_buyer.attachments import build_attachment_metadata
from src.platforms.kwork_buyer.enrichment import BuyerAttachmentCandidate, BuyerAttachmentDownload


class _SearchService:
    def __init__(self, object_ref: str) -> None:
        self.object_ref = object_ref

    async def get_project(self, run_id: str, project_id: str) -> dict[str, object]:
        assert (run_id, project_id) == ("run-1", "project-1")
        return {
            "attachments": [
                {
                    "attachment_id": "attachment-1",
                    "object_ref": self.object_ref,
                    "filename": "brief.txt",
                    "content_type": "text/plain; charset=utf-8",
                }
            ]
        }


async def _seed(store: LocalBuyerAttachmentObjectStore) -> str:
    content = b"buyer attachment preview"
    return await store(
        BuyerAttachmentCandidate(
            source_index=0,
            remote_url="https://files.kwork.ru/brief.txt",
            resolved_download_url=None,
            filename="brief.txt",
            content_type="text/plain",
            declared_size_bytes=len(content),
            source_observation_id=None,
            attachment_id="attachment-1",
        ),
        BuyerAttachmentDownload(content=content, filename="brief.txt", content_type="text/plain"),
        build_attachment_metadata(content, filename="brief.txt", content_type="text/plain"),
    )


def test_attachment_preview_requires_a_persisted_checksum_object(tmp_path) -> None:
    store = LocalBuyerAttachmentObjectStore(tmp_path / "attachment-store")
    object_ref = asyncio.run(_seed(store))
    app = FastAPI()
    app.state.buyer_search = _SearchService(object_ref)
    app.state.buyer_attachment_store = store
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/kwork/buyer-search/runs/run-1/projects/project-1/attachments/attachment-1/preview")
        missing = client.get("/api/kwork/buyer-search/runs/run-1/projects/project-1/attachments/missing/preview")

    assert response.status_code == 200
    assert response.content == b"buyer attachment preview"
    assert response.headers["content-type"].startswith("text/plain")
    assert "inline" in response.headers["content-disposition"]
    assert missing.status_code == 404
