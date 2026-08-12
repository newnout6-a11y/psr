"""Private, checksum-verified attachment preview routes for Buyer Search."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from src.platforms.kwork_buyer.attachment_object_store import (
    BuyerAttachmentObjectStoreError,
    LocalBuyerAttachmentObjectStore,
)


router = APIRouter(prefix="/api/kwork/buyer-search", tags=["kwork-buyer-attachments"])


def _service(app: Any) -> Any:
    service = getattr(app.state, "buyer_search", None)
    if service is None or not callable(getattr(service, "get_project", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer Search is unavailable",
        )
    return service


def _store(app: Any) -> LocalBuyerAttachmentObjectStore:
    store = getattr(app.state, "buyer_attachment_store", None)
    if not isinstance(store, LocalBuyerAttachmentObjectStore):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer attachment previews are unavailable",
        )
    return store


@router.get("/runs/{run_id}/projects/{project_id}/attachments/{attachment_id}/preview")
async def preview_attachment(
    run_id: str,
    project_id: str,
    attachment_id: str,
    request: Request,
) -> FileResponse:
    """Serve a persisted original only after run/project ownership and checksum verification."""

    project = await _service(request.app).get_project(run_id, project_id)
    attachments = project.get("attachments") if isinstance(project, dict) else None
    attachment = next(
        (
            item
            for item in attachments or ()
            if isinstance(item, dict) and str(item.get("attachment_id") or "") == attachment_id
        ),
        None,
    )
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer attachment was not found")

    object_ref = attachment.get("object_ref")
    if not isinstance(object_ref, str) or not object_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer attachment original is unavailable")
    try:
        path = await _store(request.app).verified_path_for_object_ref(object_ref)
    except (BuyerAttachmentObjectStoreError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Buyer attachment original is unavailable") from exc

    filename = _safe_filename(attachment.get("filename"), path)
    media_type = _safe_media_type(attachment.get("content_type"))
    return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline")


def _safe_filename(value: object, path: Path) -> str:
    candidate = Path(str(value or path.name)).name.strip()
    return candidate[:255] or "attachment"


def _safe_media_type(value: object) -> str:
    candidate = str(value or "application/octet-stream").split(";", 1)[0].strip().casefold()
    if not candidate or "/" not in candidate or any(character.isspace() for character in candidate):
        return "application/octet-stream"
    return candidate


__all__ = ["router"]
