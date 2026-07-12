"""Deterministic local exports for durable Kwork market job records.

The exporter receives already-normalized repository mappings. It has no
dependency on the repository implementation or worker runtime, so exports can
be regenerated from a recovered SQLite snapshot without making network calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .artifacts import LocalArtifactStore


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MarketExportError(ValueError):
    """Raised when supplied repository mappings cannot form a safe export."""


class MarketSnapshotExporter:
    """Write one deterministic export set under a ``LocalArtifactStore`` root."""

    def __init__(self, artifact_store: LocalArtifactStore) -> None:
        if not isinstance(artifact_store, LocalArtifactStore):
            raise TypeError("artifact_store must be a LocalArtifactStore")
        self.artifact_store = artifact_store

    def export(
        self,
        *,
        job: Mapping[str, Any],
        listings: Iterable[Mapping[str, Any]],
        observations: Iterable[Mapping[str, Any]],
        metrics: Mapping[str, Any] | None = None,
        summary: Mapping[str, Any] | None = None,
        events: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Atomically replace the stable summary and JSONL/GZIP export files.

        Event payloads are deliberately not written. ``events`` only contributes
        deterministic per-type counts, so raw response material can remain in
        private raw artifacts rather than leaking into an export summary.
        """

        job_record = _mapping(job, field="job")
        job_id = _job_id(job_record)
        listing_rows = _records(listings, field="listings", job_id=job_id, identifiers=("listing_id", "listing_key"))
        observation_rows = _records(
            observations,
            field="observations",
            job_id=job_id,
            identifiers=("observation_id", "operation_id", "response_position", "listing_id"),
        )
        metrics_record = _mapping(metrics, field="metrics") if metrics is not None else {}
        summary_record = _mapping(summary, field="summary") if summary is not None else {}
        if "events" in summary_record:
            raise MarketExportError("summary must not embed event payloads")
        event_counts = _event_counts(events)
        normalized_job = _json_value(job_record)
        normalized_metrics = _json_value(metrics_record)
        normalized_summary = _json_value(summary_record)

        exports_dir = self.artifact_store.root / job_id / "exports"
        listings_target = exports_dir / "listings.jsonl.gz"
        observations_target = exports_dir / "observations.jsonl.gz"
        summary_target = exports_dir / "summary.json"

        listings_metadata = _atomic_write_jsonl_gzip(
            listings_target,
            listing_rows,
            root=self.artifact_store.root,
            schema_version=self.artifact_store.schema_version,
        )
        observations_metadata = _atomic_write_jsonl_gzip(
            observations_target,
            observation_rows,
            root=self.artifact_store.root,
            schema_version=self.artifact_store.schema_version,
        )
        summary_document = {
            "schema_version": self.artifact_store.schema_version,
            "job_id": job_id,
            "job": normalized_job,
            "summary": normalized_summary,
            "metrics": normalized_metrics,
            "counts": {
                "observed_listing_count": len(listing_rows),
                "observation_count": len(observation_rows),
                "event_count": sum(event_counts.values()),
            },
            "event_counts": event_counts,
            "files": {
                "listings": _summary_file_reference(listings_metadata),
                "observations": _summary_file_reference(observations_metadata),
            },
        }
        summary_metadata = _atomic_write_json(
            summary_target,
            summary_document,
            root=self.artifact_store.root,
            schema_version=self.artifact_store.schema_version,
        )
        return {
            "schema_version": self.artifact_store.schema_version,
            "job_id": job_id,
            "summary": summary_metadata,
            "listings": listings_metadata,
            "observations": observations_metadata,
        }


SnapshotExporter = MarketSnapshotExporter


def export_market_snapshot(
    artifact_store: LocalArtifactStore,
    *,
    job: Mapping[str, Any],
    listings: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    metrics: Mapping[str, Any] | None = None,
    summary: Mapping[str, Any] | None = None,
    events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Convenience function for repository callers that do not retain a service."""

    return MarketSnapshotExporter(artifact_store).export(
        job=job,
        listings=listings,
        observations=observations,
        metrics=metrics,
        summary=summary,
        events=events,
    )


def _mapping(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return dict(value)


def _job_id(job: Mapping[str, Any]) -> str:
    value = job.get("job_id")
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise MarketExportError("job.job_id must be a safe path segment")
    return value


def _records(
    records: Iterable[Mapping[str, Any]],
    *,
    field: str,
    job_id: str,
    identifiers: tuple[str, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        mapped = _mapping(record, field=f"{field}[{index}]")
        record_job_id = mapped.get("job_id")
        if record_job_id is not None and record_job_id != job_id:
            raise MarketExportError(f"{field}[{index}].job_id does not match job.job_id")
        result.append(mapped)
    return sorted(result, key=lambda record: _record_sort_key(record, identifiers))


def _record_sort_key(record: Mapping[str, Any], identifiers: tuple[str, ...]) -> tuple[Any, ...]:
    return (*(_identifier_sort_value(record.get(field)) for field in identifiers), _canonical_json(record))


def _identifier_sort_value(value: Any) -> tuple[int, int | str]:
    if isinstance(value, bool) or value is None:
        return 2, ""
    if isinstance(value, int):
        return 0, value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.lstrip("-").isdigit():
            return 0, int(normalized)
        return 1, normalized
    return 1, str(value)


def _event_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for index, event in enumerate(events):
        mapped = _mapping(event, field=f"events[{index}]")
        raw_type = mapped.get("type", mapped.get("event_type", "unknown"))
        event_type = str(raw_type).strip() or "unknown"
        counts[event_type] += 1
    return {event_type: counts[event_type] for event_type in sorted(counts)}


def _atomic_write_json(
    target: Path,
    payload: Mapping[str, Any],
    *,
    root: Path,
    schema_version: int,
) -> dict[str, Any]:
    serialized = _canonical_json(payload).encode("utf-8") + b"\n"
    _atomic_write_bytes(target, serialized)
    return _file_metadata(target, root=root, schema_version=schema_version)


def _atomic_write_jsonl_gzip(
    target: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    root: Path,
    schema_version: int,
) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressed:
                for record in records:
                    compressed.write(_canonical_json(record).encode("utf-8"))
                    compressed.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return _file_metadata(target, root=root, schema_version=schema_version)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _file_metadata(path: Path, *, root: Path, schema_version: int) -> dict[str, Any]:
    digest = sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    return {
        "path": str(path),
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": digest.hexdigest(),
        "byte_size": byte_size,
        "schema_version": schema_version,
    }


def _summary_file_reference(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "relative_path": metadata["relative_path"],
        "sha256": metadata["sha256"],
        "byte_size": metadata["byte_size"],
        "schema_version": metadata["schema_version"],
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MarketExportError("Decimal values must be finite")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MarketExportError("float values must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        if not all(isinstance(key, str) for key in value):
            raise MarketExportError("mapping keys must be strings")
        for key in sorted(value):
            normalized[key] = _json_value(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise MarketExportError(f"value of type {type(value).__name__} is not JSON exportable")
