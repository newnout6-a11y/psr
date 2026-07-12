from __future__ import annotations

import gzip
from hashlib import sha256
import json
from pathlib import Path

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore


@pytest.fixture
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "data" / "kwork_market_jobs")


def test_write_raw_creates_atomic_gzip_artifact_with_reference(artifact_store: LocalArtifactStore):
    payload = {"cards": [{"id": "42", "title": "Site repair"}], "reported_cursor": {"exclude_ids": ["1"]}}

    reference = artifact_store.write_raw(
        job_id="job_01",
        operation_id="op_01",
        attempt=2,
        payload=payload,
    )

    target = artifact_store.root / "job_01" / "raw" / "op_01-2.json.gz"
    compressed = target.read_bytes()
    assert target.is_file()
    assert list(target.parent.glob(".*.tmp")) == []
    assert json.loads(gzip.decompress(compressed).decode("utf-8")) == payload
    assert reference == {
        "path": str(target),
        "relative_path": "job_01/raw/op_01-2.json.gz",
        "sha256": sha256(compressed).hexdigest(),
        "byte_size": len(compressed),
        "schema_version": 1,
    }
    assert json.dumps(reference)


def test_write_raw_allows_schema_version_override(artifact_store: LocalArtifactStore):
    reference = artifact_store.write_raw(
        job_id="job_02",
        operation_id="op_02",
        attempt=1,
        payload={"status": "ok"},
        schema_version=3,
    )

    assert reference["schema_version"] == 3


@pytest.mark.parametrize(
    ("job_id", "operation_id", "attempt"),
    [
        ("../job", "op_01", 1),
        ("job_01", "op/01", 1),
        ("job_01", "op_01", 0),
    ],
)
def test_write_raw_rejects_unsafe_layout_segments(
    artifact_store: LocalArtifactStore,
    job_id: str,
    operation_id: str,
    attempt: int,
):
    with pytest.raises(ValueError):
        artifact_store.write_raw(
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            payload={"status": "not-written"},
        )

    assert not artifact_store.root.exists()
