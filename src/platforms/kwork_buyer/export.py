"""Reproducible Buyer Search data exports and manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from io import BytesIO, StringIO
import csv
import json
from typing import Any, Iterable, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile


class BuyerExportFormat(StrEnum):
    JSONL = "jsonl"
    CSV = "csv"
    MARKDOWN = "markdown"
    TXT = "txt"
    ZIP = "zip"


@dataclass(frozen=True, slots=True)
class BuyerExportSnapshot:
    run_id: str
    format: BuyerExportFormat
    filters: Mapping[str, Any]
    selected_project_ids: tuple[str, ...] = ()
    include_attachments: bool = False
    include_raw: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id cannot be blank")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "format", BuyerExportFormat(self.format))
        if not isinstance(self.filters, Mapping):
            raise TypeError("filters must be a mapping")
        object.__setattr__(self, "filters", dict(self.filters))
        object.__setattr__(
            self,
            "selected_project_ids",
            tuple(str(project_id).strip() for project_id in self.selected_project_ids if str(project_id).strip()),
        )


@dataclass(frozen=True, slots=True)
class BuyerExportArtifact:
    filename: str
    content_type: str
    content: bytes
    manifest: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        return f"sha256:{sha256(self.content).hexdigest()}"


def build_buyer_export(
    snapshot: BuyerExportSnapshot,
    projects: Iterable[Mapping[str, Any]],
) -> BuyerExportArtifact:
    """Build a deterministic export from a server-side project selection."""

    if not isinstance(snapshot, BuyerExportSnapshot):
        raise TypeError("snapshot must be a BuyerExportSnapshot")
    rows = [_export_row(project) for project in projects]
    if snapshot.selected_project_ids:
        wanted = set(snapshot.selected_project_ids)
        rows = [row for row in rows if row["project_id"] in wanted]
    rows.sort(key=lambda row: (row["project_id"], row["title"]))
    manifest = _manifest(snapshot, rows)

    if snapshot.format is BuyerExportFormat.JSONL:
        content = _jsonl(rows).encode("utf-8")
        return BuyerExportArtifact(f"buyer-search-{snapshot.run_id}.jsonl", "application/x-ndjson", content, manifest)
    if snapshot.format is BuyerExportFormat.CSV:
        content = _csv(rows).encode("utf-8")
        return BuyerExportArtifact(f"buyer-search-{snapshot.run_id}.csv", "text/csv; charset=utf-8", content, manifest)
    if snapshot.format is BuyerExportFormat.MARKDOWN:
        content = _markdown(rows).encode("utf-8")
        return BuyerExportArtifact(f"buyer-search-{snapshot.run_id}.md", "text/markdown; charset=utf-8", content, manifest)
    if snapshot.format is BuyerExportFormat.TXT:
        content = _text(rows).encode("utf-8")
        return BuyerExportArtifact(f"buyer-search-{snapshot.run_id}.txt", "text/plain; charset=utf-8", content, manifest)

    jsonl = _jsonl(rows).encode("utf-8")
    csv_content = _csv(rows).encode("utf-8")
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("projects.jsonl", jsonl)
        archive.writestr("projects.csv", csv_content)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return BuyerExportArtifact(f"buyer-search-{snapshot.run_id}.zip", "application/zip", buffer.getvalue(), manifest)


def _export_row(project: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(project, Mapping):
        raise TypeError("projects must contain mappings")
    project_id = project.get("project_id") or project.get("buyer_project_id") or project.get("remote_project_id")
    if project_id is None or not str(project_id).strip():
        raise ValueError("export project requires project_id, buyer_project_id, or remote_project_id")
    description = project.get("description") or project.get("latest_description") or ""
    return {
        "project_id": str(project_id),
        "title": str(project.get("title") or project.get("latest_title") or ""),
        "description": str(description),
        "budget_min": project.get("budget_min"),
        "budget_max": project.get("budget_max"),
        "offers": project.get("offers") or project.get("offers_count"),
        "views": project.get("views"),
        "score": project.get("final_score") if project.get("final_score") is not None else project.get("preliminary_score"),
        "matched_queries": list(project.get("matched_queries") or ()),
        "attachment_manifest": list(project.get("attachment_manifest") or ()),
        "url": str(project.get("canonical_url") or project.get("url") or ""),
    }


def _manifest(snapshot: BuyerExportSnapshot, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selection = {
        "run_id": snapshot.run_id,
        "format": snapshot.format.value,
        "filters": snapshot.filters,
        "selected_project_ids": list(snapshot.selected_project_ids),
        "include_attachments": snapshot.include_attachments,
        "include_raw": snapshot.include_raw,
    }
    canonical = json.dumps(selection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "selection": selection,
        "selection_hash": f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}",
        "row_count": len(rows),
        "project_ids": [str(row["project_id"]) for row in rows],
        "attachment_count": sum(len(row["attachment_manifest"]) for row in rows),
    }


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)


def _csv(rows: Sequence[Mapping[str, Any]]) -> str:
    columns = ("project_id", "title", "description", "budget_min", "budget_max", "offers", "views", "score", "url")
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _markdown(rows: Sequence[Mapping[str, Any]]) -> str:
    output = ["# Buyer Search Export", ""]
    for row in rows:
        output.extend(
            [
                f"## {row['title'] or row['project_id']}",
                f"Project: `{row['project_id']}`",
                f"Budget: {row['budget_min'] or '-'} - {row['budget_max'] or '-'}",
                f"Score: {row['score'] if row['score'] is not None else '-'}",
                "",
                row["description"],
                "",
            ]
        )
    return "\n".join(output)


def _text(rows: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        f"{row['title'] or row['project_id']}\n{row['description']}\nURL: {row['url']}" for row in rows
    ) + ("\n" if rows else "")


__all__ = ["BuyerExportArtifact", "BuyerExportFormat", "BuyerExportSnapshot", "build_buyer_export"]
