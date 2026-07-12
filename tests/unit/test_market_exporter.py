from __future__ import annotations

from decimal import Decimal
import gzip
import json
from pathlib import Path

import pytest

from src.platforms.kwork_supply import (
    LocalArtifactStore,
    MarketExportError,
    MarketSnapshotExporter,
    export_market_snapshot,
)


def _job() -> dict[str, object]:
    return {
        "job_id": "job_export",
        "state": "completed",
        "scope": {"category_id": 38, "canonical_alias": "website-repair"},
    }


def _listings() -> list[dict[str, object]]:
    return [
        {"job_id": "job_export", "listing_id": 10, "listing_key": "kwork:10", "price": Decimal("1200.50")},
        {"job_id": "job_export", "listing_id": 2, "listing_key": "kwork:2", "price": 500},
    ]


def _observations() -> list[dict[str, object]]:
    return [
        {
            "job_id": "job_export",
            "observation_id": 5,
            "operation_id": "op_b",
            "response_position": 1,
            "listing_id": 10,
            "raw_response_ref": "job_export/raw/op_b-1.json.gz",
        },
        {
            "job_id": "job_export",
            "observation_id": 3,
            "operation_id": "op_a",
            "response_position": 0,
            "listing_id": 2,
            "raw_response_ref": "job_export/raw/op_a-1.json.gz",
        },
    ]


def _read_jsonl_gzip(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_export_writes_deterministic_layout_and_keeps_event_payloads_out_of_summary(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", schema_version=3)
    exporter = MarketSnapshotExporter(store)

    manifest = exporter.export(
        job=_job(),
        listings=_listings(),
        observations=_observations(),
        metrics={"price_distribution": {"p50": Decimal("850.25")}},
        summary={"profile": "working"},
        events=[
            {"event_type": "job.metrics", "payload": {"raw_response": "private raw payload"}},
            {"type": "job.metrics", "payload": {"html": "also private"}},
            {"type": "warning", "payload": {"detail": "public warning"}},
        ],
    )

    exports_dir = tmp_path / "artifacts" / "job_export" / "exports"
    assert manifest["schema_version"] == 3
    assert manifest["listings"]["relative_path"] == "job_export/exports/listings.jsonl.gz"
    assert manifest["observations"]["relative_path"] == "job_export/exports/observations.jsonl.gz"
    assert manifest["summary"]["relative_path"] == "job_export/exports/summary.json"
    assert all((exports_dir / name).exists() for name in ("summary.json", "listings.jsonl.gz", "observations.jsonl.gz"))

    summary_text = (exports_dir / "summary.json").read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    assert summary["counts"] == {"event_count": 3, "observation_count": 2, "observed_listing_count": 2}
    assert summary["event_counts"] == {"job.metrics": 2, "warning": 1}
    assert summary["metrics"]["price_distribution"]["p50"] == "850.25"
    assert "private raw payload" not in summary_text
    assert "also private" not in summary_text
    assert "payload" not in summary_text
    assert "path" not in summary["files"]["listings"]

    assert [row["listing_id"] for row in _read_jsonl_gzip(exports_dir / "listings.jsonl.gz")] == [2, 10]
    assert [row["price"] for row in _read_jsonl_gzip(exports_dir / "listings.jsonl.gz")] == [500, "1200.50"]
    assert [row["observation_id"] for row in _read_jsonl_gzip(exports_dir / "observations.jsonl.gz")] == [3, 5]


def test_export_is_byte_deterministic_independent_of_input_record_order(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")

    first = export_market_snapshot(
        store,
        job=_job(),
        listings=_listings(),
        observations=_observations(),
        metrics={"a": 1, "b": Decimal("2.50")},
        events=[{"type": "warning"}, {"type": "job.metrics"}],
    )
    exports_dir = tmp_path / "artifacts" / "job_export" / "exports"
    first_bytes = {name: (exports_dir / name).read_bytes() for name in ("summary.json", "listings.jsonl.gz", "observations.jsonl.gz")}

    second = export_market_snapshot(
        store,
        job=dict(reversed(tuple(_job().items()))),
        listings=list(reversed(_listings())),
        observations=list(reversed(_observations())),
        metrics={"b": Decimal("2.50"), "a": 1},
        events=[{"type": "job.metrics"}, {"type": "warning"}],
    )
    second_bytes = {name: (exports_dir / name).read_bytes() for name in first_bytes}

    assert first_bytes == second_bytes
    assert first["summary"]["sha256"] == second["summary"]["sha256"]
    assert first["listings"]["sha256"] == second["listings"]["sha256"]
    assert first["observations"]["sha256"] == second["observations"]["sha256"]


def test_export_rejects_cross_job_records_and_unsafe_payloads_without_temp_artifacts(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(MarketExportError, match="does not match"):
        export_market_snapshot(
            store,
            job=_job(),
            listings=[{"job_id": "another-job", "listing_id": 1}],
            observations=[],
        )

    with pytest.raises(MarketExportError, match="must not embed event payloads"):
        export_market_snapshot(
            store,
            job=_job(),
            listings=[],
            observations=[],
            summary={"events": [{"payload": "not allowed"}]},
        )

    with pytest.raises(MarketExportError, match="not JSON exportable"):
        export_market_snapshot(
            store,
            job=_job(),
            listings=[],
            observations=[],
            metrics={"unsupported": object()},
        )

    exports_dir = tmp_path / "artifacts" / "job_export" / "exports"
    assert not exports_dir.exists() or not list(exports_dir.glob(".*.tmp"))
