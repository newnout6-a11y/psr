"""Local raw-response artifacts for durable Kwork market jobs."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from src.paths import DATA_DIR


DEFAULT_ARTIFACT_SCHEMA_VERSION = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_segment(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe path segment")
    return value


def _positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _file_metadata(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    return digest.hexdigest(), byte_size


class LocalArtifactStore:
    """Write gzip JSON artifacts atomically under one local jobs root."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        schema_version: int = DEFAULT_ARTIFACT_SCHEMA_VERSION,
    ) -> None:
        self.root = Path(root) if root is not None else DATA_DIR / "kwork_market_jobs"
        self.schema_version = _positive_int(schema_version, field_name="schema_version")

    def write_raw(
        self,
        *,
        job_id: str,
        operation_id: str,
        attempt: int,
        payload: Any,
        schema_version: int | None = None,
    ) -> dict[str, str | int]:
        safe_job_id = _safe_segment(job_id, field_name="job_id")
        safe_operation_id = _safe_segment(operation_id, field_name="operation_id")
        safe_attempt = _positive_int(attempt, field_name="attempt")
        effective_schema_version = self.schema_version if schema_version is None else _positive_int(
            schema_version,
            field_name="schema_version",
        )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

        raw_dir = self.root / safe_job_id / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / f"{safe_operation_id}-{safe_attempt}.json.gz"
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=raw_dir,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressed:
                    compressed.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        digest, byte_size = _file_metadata(target)
        return {
            "path": str(target),
            "relative_path": target.relative_to(self.root).as_posix(),
            "sha256": digest,
            "byte_size": byte_size,
            "schema_version": effective_schema_version,
        }
