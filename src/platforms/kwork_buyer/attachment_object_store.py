"""Content-addressed local storage for immutable Buyer attachment originals.

The enrichment pipeline supplies this writer through its narrow
``BuyerAttachmentObjectWriter`` protocol.  Remote URLs, filenames, account
identifiers, and other source metadata are deliberately never used in a local
path: the SHA-256 of the verified body is the entire storage address.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from threading import Lock

from .attachments import BuyerAttachmentMetadata
from .enrichment import BuyerAttachmentCandidate, BuyerAttachmentDownload


class BuyerAttachmentObjectStoreError(RuntimeError):
    """Raised when an immutable attachment object cannot be stored safely."""


class LocalBuyerAttachmentObjectStore:
    """Write verified attachment bytes under a private content-addressed root.

    The returned reference is stable and intentionally opaque to a remote
    attachment source.  Callers that need to serve an object should resolve the
    checksum through this store instead of treating the reference as a path.
    """

    _OBJECT_REF_PREFIX = "buyer-attachment://sha256/"

    def __init__(self, root: str | Path) -> None:
        if isinstance(root, str) and not root.strip():
            raise ValueError("root must not be blank")
        if not isinstance(root, (str, Path)):
            raise TypeError("root must be a path")

        self._root = Path(root).expanduser().resolve(strict=False)
        self._write_lock = Lock()

    @property
    def root(self) -> Path:
        """Return the resolved root that owns every stored object."""

        return self._root

    async def __call__(
        self,
        candidate: BuyerAttachmentCandidate,
        download: BuyerAttachmentDownload,
        metadata: BuyerAttachmentMetadata,
    ) -> str:
        """Persist one verified original and return an opaque checksum reference."""

        payload, digest = _validate_write_inputs(candidate, download, metadata)
        await asyncio.to_thread(self._write_sync, payload, digest)
        return self.object_ref_for_sha256(digest)

    def object_ref_for_sha256(self, digest: str) -> str:
        """Return the durable object reference for a validated content digest."""

        return f"{self._OBJECT_REF_PREFIX}{_validated_digest(digest)}"

    def path_for_sha256(self, digest: str) -> Path:
        """Resolve a content digest to its internal path without source metadata."""

        normalized_digest = _validated_digest(digest)
        target = self._root / normalized_digest[:2] / normalized_digest[2:4] / normalized_digest
        return self._ensure_under_root(target)

    def path_for_object_ref(self, object_ref: str) -> Path:
        """Resolve only an object reference minted by this store.

        Attachment previews must never treat a durable object reference as a
        filesystem path.  Keeping this parser here makes the route-level
        authorization check independent of storage layout.
        """

        if not isinstance(object_ref, str) or not object_ref.startswith(self._OBJECT_REF_PREFIX):
            raise BuyerAttachmentObjectStoreError("attachment object reference is invalid")
        return self.path_for_sha256(object_ref.removeprefix(self._OBJECT_REF_PREFIX))

    async def verified_path_for_object_ref(self, object_ref: str) -> Path:
        """Return a checksum-verified immutable object suitable for preview."""

        return await asyncio.to_thread(self._verified_path_for_object_ref_sync, object_ref)

    def _verified_path_for_object_ref_sync(self, object_ref: str) -> Path:
        target = self.path_for_object_ref(object_ref)
        digest = object_ref.removeprefix(self._OBJECT_REF_PREFIX)
        if not self._existing_object_is_valid(target, digest):
            raise BuyerAttachmentObjectStoreError("attachment object was not found")
        return target

    def _write_sync(self, payload: bytes, digest: str) -> None:
        """Atomically write a single object or verify the existing immutable one."""

        with self._write_lock:
            self._root.mkdir(parents=True, exist_ok=True)
            self._root = self._root.resolve(strict=True)
            if not self._root.is_dir():
                raise BuyerAttachmentObjectStoreError("attachment object root is not a directory")

            target = self.path_for_sha256(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self._ensure_under_root(target)
            if self._existing_object_is_valid(target, digest):
                return

            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{digest}.",
                    suffix=".tmp",
                    dir=target.parent,
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    temporary_file.write(payload)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())

                # A same-digest peer may have completed while this writer was
                # creating its temporary file.  Reuse it after revalidating it.
                if self._existing_object_is_valid(target, digest):
                    return
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _existing_object_is_valid(self, target: Path, digest: str) -> bool:
        """Return whether a preexisting object is the immutable expected body."""

        self._ensure_under_root(target)
        if target.is_symlink():
            raise BuyerAttachmentObjectStoreError("attachment object path must not be a symlink")
        if not target.exists():
            return False
        if not target.is_file():
            raise BuyerAttachmentObjectStoreError("attachment object path is not a file")
        try:
            existing_digest = sha256(target.read_bytes()).hexdigest()
        except OSError as error:
            raise BuyerAttachmentObjectStoreError("could not verify existing attachment object") from error
        if existing_digest != digest:
            raise BuyerAttachmentObjectStoreError("existing attachment object checksum does not match its address")
        return True

    def _ensure_under_root(self, path: Path) -> Path:
        """Reject symlinked or otherwise escaped output paths before writing."""

        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise BuyerAttachmentObjectStoreError("attachment object path escapes configured root") from error
        return path


def _validate_write_inputs(
    candidate: BuyerAttachmentCandidate,
    download: BuyerAttachmentDownload,
    metadata: BuyerAttachmentMetadata,
) -> tuple[bytes, str]:
    if not isinstance(candidate, BuyerAttachmentCandidate):
        raise TypeError("candidate must be BuyerAttachmentCandidate")
    if not isinstance(download, BuyerAttachmentDownload):
        raise TypeError("download must be BuyerAttachmentDownload")
    if not isinstance(metadata, BuyerAttachmentMetadata):
        raise TypeError("metadata must be BuyerAttachmentMetadata")
    if not isinstance(download.content, (bytes, bytearray, memoryview)):
        raise TypeError("download content must be bytes")

    payload = bytes(download.content)
    digest = sha256(payload).hexdigest()
    if metadata.size_bytes != len(payload):
        raise BuyerAttachmentObjectStoreError("attachment metadata size does not match downloaded content")
    if metadata.sha256 != digest:
        raise BuyerAttachmentObjectStoreError("attachment metadata checksum does not match downloaded content")
    return payload, digest


def _validated_digest(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("SHA-256 digest must be a 64-character lowercase hexadecimal string")
    if value != value.lower() or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("SHA-256 digest must be a 64-character lowercase hexadecimal string")
    return value


__all__ = [
    "BuyerAttachmentObjectStoreError",
    "LocalBuyerAttachmentObjectStore",
]
