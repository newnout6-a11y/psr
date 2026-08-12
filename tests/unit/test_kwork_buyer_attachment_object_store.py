from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest

from src.platforms.kwork_buyer.attachment_object_store import (
    BuyerAttachmentObjectStoreError,
    LocalBuyerAttachmentObjectStore,
)
from src.platforms.kwork_buyer.attachments import build_attachment_metadata
from src.platforms.kwork_buyer.enrichment import BuyerAttachmentCandidate, BuyerAttachmentDownload


def _candidate() -> BuyerAttachmentCandidate:
    return BuyerAttachmentCandidate(
        source_index=0,
        remote_url="https://files.example/brief.txt?token=very-secret-token",
        resolved_download_url="https://cdn.example/private/brief.txt?sig=also-secret",
        filename="brief.txt",
        content_type="text/plain",
        declared_size_bytes=None,
        source_observation_id="observation-1",
        attachment_id="attachment-1",
    )


@pytest.mark.asyncio
async def test_store_writes_and_reuses_content_addressed_object_without_source_tokens(tmp_path) -> None:
    content = b"private buyer brief"
    metadata = build_attachment_metadata(content, filename="brief.txt", content_type="text/plain")
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")
    download = BuyerAttachmentDownload(content=content, filename="brief.txt", content_type="text/plain")

    first_ref = await store(_candidate(), download, metadata)
    second_ref = await store(_candidate(), download, metadata)

    digest = sha256(content).hexdigest()
    target = store.path_for_sha256(digest)
    assert first_ref == second_ref == f"buyer-attachment://sha256/{digest}"
    assert target == store.root / digest[:2] / digest[2:4] / digest
    assert target.read_bytes() == content
    assert "very-secret-token" not in str(target)
    assert "also-secret" not in str(target)
    assert "brief.txt" not in str(target)


@pytest.mark.asyncio
async def test_store_concurrently_reuses_one_atomic_content_address(tmp_path) -> None:
    content = b"same immutable object"
    metadata = build_attachment_metadata(content, filename="brief.txt", content_type="text/plain")
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")
    download = BuyerAttachmentDownload(content=content, filename="brief.txt", content_type="text/plain")

    refs = await asyncio.gather(*(store(_candidate(), download, metadata) for _ in range(8)))

    target = store.path_for_sha256(metadata.sha256)
    assert set(refs) == {f"buyer-attachment://sha256/{metadata.sha256}"}
    assert target.read_bytes() == content
    assert list(target.parent.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_store_resolves_only_its_checksum_verified_object_reference(tmp_path) -> None:
    content = b"previewable immutable object"
    metadata = build_attachment_metadata(content, filename="brief.txt", content_type="text/plain")
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")
    object_ref = await store(
        _candidate(),
        BuyerAttachmentDownload(content=content, filename="brief.txt", content_type="text/plain"),
        metadata,
    )

    assert await store.verified_path_for_object_ref(object_ref) == store.path_for_sha256(metadata.sha256)
    with pytest.raises(BuyerAttachmentObjectStoreError, match="reference"):
        await store.verified_path_for_object_ref("C:/private/brief.txt")


@pytest.mark.asyncio
async def test_store_rejects_metadata_mismatch_without_creating_an_object(tmp_path) -> None:
    stored_content = b"expected body"
    mismatched_content = b"expected bodY"
    metadata = build_attachment_metadata(stored_content, filename="brief.txt", content_type="text/plain")
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")

    with pytest.raises(BuyerAttachmentObjectStoreError, match="checksum"):
        await store(
            _candidate(),
            BuyerAttachmentDownload(content=mismatched_content, filename="brief.txt", content_type="text/plain"),
            metadata,
        )

    assert not store.root.exists()


@pytest.mark.asyncio
async def test_store_fails_closed_for_a_corrupt_preexisting_content_address(tmp_path) -> None:
    content = b"expected body"
    metadata = build_attachment_metadata(content, filename="brief.txt", content_type="text/plain")
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")
    target = store.path_for_sha256(metadata.sha256)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt body")

    with pytest.raises(BuyerAttachmentObjectStoreError, match="checksum"):
        await store(
            _candidate(),
            BuyerAttachmentDownload(content=content, filename="brief.txt", content_type="text/plain"),
            metadata,
        )

    assert target.read_bytes() == b"corrupt body"


def test_store_rejects_invalid_digest_without_constructing_a_path(tmp_path) -> None:
    store = LocalBuyerAttachmentObjectStore(tmp_path / "buyer-attachments")

    with pytest.raises(ValueError, match="SHA-256"):
        store.path_for_sha256("../remote-token")
