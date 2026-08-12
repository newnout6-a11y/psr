"""Bounded, account-scoped attachment enrichment for Buyer Search.

Discovery intentionally captures only a lightweight attachment manifest.  This
module performs the expensive follow-up work through a ``BuyerReadCapabilities``
instance that exposes only authenticated reads.  It never receives a general
Kwork client and never performs a remote mutation.

The pipeline owns policy selection, timeout/size validation, parser dispatch,
durable state transitions, and the honest context manifest consumed by scoring
and proposal composition.  Local object storage remains an injected concern so
the module can be used by workers, API jobs, and tests without choosing a
filesystem or cloud-storage implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import inspect
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from .attachments import (
    BuyerAttachmentContext,
    BuyerAttachmentMetadata,
    BuyerAttachmentParseLimits,
    BuyerAttachmentParseResult,
    BuyerAttachmentParseStatus,
    BuyerAttachmentType,
    build_attachment_context,
    detect_attachment_type,
    normalize_attachment_content_type,
    normalize_attachment_filename,
    parse_attachment,
    sanitize_attachment_context_text,
)
from .attachment_vision import BuyerAttachmentVisionGateway, apply_buyer_attachment_vision
from .sources.capabilities import BuyerReadCapabilities


_PIPELINE_VERSION = "buyer-attachment-enrichment-v1"
_HARD_MAX_ATTACHMENTS_PER_PROJECT = 100
_HARD_MAX_BYTES_PER_ATTACHMENT = 100 * 1024 * 1024
_HARD_MAX_TOTAL_BYTES_PER_PROJECT = 500 * 1024 * 1024
_MAX_DOWNLOAD_TIMEOUT_SECONDS = 120.0


class BuyerAttachmentEnrichmentError(RuntimeError):
    """Raised when an attachment reader or injected object writer is invalid."""


class BuyerAttachmentEnrichmentPolicyError(ValueError):
    """Raised when a policy would create an unbounded enrichment workload."""


class BuyerAttachmentEnrichmentState(StrEnum):
    """Durable and UI-facing states emitted by this pipeline."""

    PARSED = "parsed"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    CORRUPT = "corrupt"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BuyerAttachmentEnrichmentPolicy:
    """Explicit limits for one project's selective attachment pass.

    ``max_attachments_per_project`` and byte budgets are enforced before parser
    invocation.  The reader is also awaited through a timeout, while the
    returned body is checked again because a remote server can ignore a content
    length hint or an adapter-side cap.
    """

    max_attachments_per_project: int = 10
    hard_max_attachments_per_project: int = _HARD_MAX_ATTACHMENTS_PER_PROJECT
    max_bytes_per_attachment: int = 10 * 1024 * 1024
    max_total_bytes_per_project: int = 25 * 1024 * 1024
    download_timeout_seconds: float = 20.0
    max_context_characters: int = 48_000
    parse_limits: BuyerAttachmentParseLimits = field(default_factory=BuyerAttachmentParseLimits)
    allowed_types: frozenset[BuyerAttachmentType] = field(
        default_factory=lambda: frozenset(BuyerAttachmentType)
    )
    require_original_object_ref: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_attachments_per_project",
            "hard_max_attachments_per_project",
            "max_bytes_per_attachment",
            "max_total_bytes_per_project",
            "max_context_characters",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BuyerAttachmentEnrichmentPolicyError(f"{name} must be a positive integer")
        if (
            self.hard_max_attachments_per_project > _HARD_MAX_ATTACHMENTS_PER_PROJECT
            or self.max_attachments_per_project > self.hard_max_attachments_per_project
        ):
            raise BuyerAttachmentEnrichmentPolicyError(
                f"max_attachments_per_project must be at most {_HARD_MAX_ATTACHMENTS_PER_PROJECT}"
            )
        if self.max_bytes_per_attachment > _HARD_MAX_BYTES_PER_ATTACHMENT:
            raise BuyerAttachmentEnrichmentPolicyError(
                f"max_bytes_per_attachment must be at most {_HARD_MAX_BYTES_PER_ATTACHMENT}"
            )
        if self.max_total_bytes_per_project > _HARD_MAX_TOTAL_BYTES_PER_PROJECT:
            raise BuyerAttachmentEnrichmentPolicyError(
                f"max_total_bytes_per_project must be at most {_HARD_MAX_TOTAL_BYTES_PER_PROJECT}"
            )
        if (
            isinstance(self.download_timeout_seconds, bool)
            or not isinstance(self.download_timeout_seconds, (int, float))
            or not 0 < float(self.download_timeout_seconds) <= _MAX_DOWNLOAD_TIMEOUT_SECONDS
        ):
            raise BuyerAttachmentEnrichmentPolicyError(
                f"download_timeout_seconds must be between 0 and {_MAX_DOWNLOAD_TIMEOUT_SECONDS}"
            )
        if not isinstance(self.parse_limits, BuyerAttachmentParseLimits):
            raise BuyerAttachmentEnrichmentPolicyError("parse_limits must be BuyerAttachmentParseLimits")
        if not isinstance(self.allowed_types, frozenset) or not all(
            isinstance(item, BuyerAttachmentType) for item in self.allowed_types
        ):
            raise BuyerAttachmentEnrichmentPolicyError("allowed_types must be a frozenset of BuyerAttachmentType")
        if not isinstance(self.require_original_object_ref, bool):
            raise BuyerAttachmentEnrichmentPolicyError("require_original_object_ref must be a boolean")


@dataclass(frozen=True, slots=True)
class BuyerAttachmentCandidate:
    """One normalized attachment manifest entry before it is downloaded."""

    source_index: int
    remote_url: str | None
    resolved_download_url: str | None
    filename: str
    content_type: str | None
    declared_size_bytes: int | None
    source_observation_id: str | None
    attachment_id: str | None

    @property
    def download_url(self) -> str | None:
        return self.resolved_download_url or self.remote_url

    @property
    def detected_type(self) -> BuyerAttachmentType | None:
        return detect_attachment_type(self.filename, self.content_type)

    @property
    def identity(self) -> str:
        if self.remote_url:
            return f"url:{self.remote_url}"
        if self.attachment_id:
            return f"attachment_id:{self.attachment_id}"
        return f"index:{self.source_index}"


@dataclass(frozen=True, slots=True)
class BuyerAttachmentEnrichmentDecision:
    """Deterministic decision made before any network read is issued."""

    candidate: BuyerAttachmentCandidate
    selected: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BuyerAttachmentDownload:
    """A fully-buffered reader result that the pipeline validates again."""

    content: bytes
    filename: str | None = None
    content_type: str | None = None
    resolved_download_url: str | None = None


class BuyerAttachmentRepository(Protocol):
    """The narrow durable repository surface required by enrichment."""

    async def upsert_attachment(
        self,
        run_id: str,
        project_id: str,
        attachment: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def record_attachment_derivative(
        self,
        attachment_id: str,
        derivative: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BuyerAttachmentReadRequest:
    """Bounded read request passed to an injected attachment reader."""

    candidate: BuyerAttachmentCandidate
    max_bytes: int
    timeout_seconds: float


class BuyerAttachmentReader(Protocol):
    """Authenticated, read-only attachment reader.

    Implementations should stream or otherwise cap the response at
    ``request.max_bytes``.  The pipeline separately verifies the final body,
    which preserves fail-closed behavior for simpler adapters.
    """

    def __call__(self, request: BuyerAttachmentReadRequest) -> Awaitable[BuyerAttachmentDownload | Mapping[str, Any] | bytes]: ...


class BuyerAttachmentObjectWriter(Protocol):
    """Persist the immutable original outside SQLite and return its object ref."""

    def __call__(
        self,
        candidate: BuyerAttachmentCandidate,
        download: BuyerAttachmentDownload,
        metadata: BuyerAttachmentMetadata,
    ) -> Awaitable[str | None]: ...


@dataclass(frozen=True, slots=True)
class BuyerAttachmentEnrichmentItem:
    """Outcome for one discovered attachment, including skipped evidence."""

    candidate: BuyerAttachmentCandidate
    state: BuyerAttachmentEnrichmentState
    attachment_id: str | None
    reason: str | None = None
    parse_result: BuyerAttachmentParseResult | None = None
    bytes_downloaded: int = 0
    checksum_deduplicated: bool = False
    context_characters: int = 0
    context_omission: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "source_index": self.candidate.source_index,
            "attachment_id": self.attachment_id,
            "remote_url": _safe_remote_url(self.candidate.remote_url),
            "resolved_download_url": _safe_remote_url(self.candidate.resolved_download_url),
            "filename": sanitize_attachment_context_text(self.candidate.filename),
            "declared_content_type": self.candidate.content_type,
            "declared_size_bytes": self.candidate.declared_size_bytes,
            "state": self.state.value,
            "reason": self.reason,
            "bytes_downloaded": self.bytes_downloaded,
            "checksum_deduplicated": self.checksum_deduplicated,
            "context_characters": self.context_characters,
        }
        if self.context_omission:
            manifest["context_omission"] = self.context_omission
        if self.parse_result is not None:
            manifest["parse"] = self.parse_result.to_manifest()
        return manifest


@dataclass(frozen=True, slots=True)
class BuyerAttachmentEnrichmentResult:
    """Complete enrichment evidence plus bounded text for a downstream model."""

    run_id: str
    project_id: str
    provenance: Mapping[str, str]
    policy: BuyerAttachmentEnrichmentPolicy
    items: tuple[BuyerAttachmentEnrichmentItem, ...]
    context: BuyerAttachmentContext
    context_manifest: Mapping[str, Any]

    @property
    def bytes_downloaded(self) -> int:
        return sum(item.bytes_downloaded for item in self.items)

    @property
    def parsed_count(self) -> int:
        return sum(item.state is BuyerAttachmentEnrichmentState.PARSED for item in self.items)


def plan_buyer_attachment_enrichment(
    attachments: Iterable[Mapping[str, Any]],
    *,
    policy: BuyerAttachmentEnrichmentPolicy | None = None,
) -> tuple[BuyerAttachmentEnrichmentDecision, ...]:
    """Select a deterministic, bounded subset before any attachment read.

    The ordering deliberately follows the source manifest.  This makes a manual
    enrichment run reproducible while preventing an arbitrary large file list
    from becoming an unbounded background workload.
    """

    active_policy = policy or BuyerAttachmentEnrichmentPolicy()
    if not isinstance(active_policy, BuyerAttachmentEnrichmentPolicy):
        raise TypeError("policy must be BuyerAttachmentEnrichmentPolicy")

    candidates = tuple(_candidate_from_mapping(item, index) for index, item in enumerate(attachments))
    decisions: list[BuyerAttachmentEnrichmentDecision] = []
    seen: set[str] = set()
    selected_count = 0
    reserved_bytes = 0
    for candidate in candidates:
        if candidate.identity in seen:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "duplicate_manifest_entry"))
            continue
        seen.add(candidate.identity)
        if candidate.download_url is None:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "missing_download_url"))
            continue
        if selected_count >= active_policy.max_attachments_per_project:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "attachment_limit"))
            continue
        attachment_type = candidate.detected_type
        if attachment_type is None or attachment_type not in active_policy.allowed_types:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "unsupported_type"))
            continue
        declared_size = candidate.declared_size_bytes
        if declared_size is not None and declared_size > active_policy.max_bytes_per_attachment:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "attachment_byte_limit"))
            continue
        if declared_size is not None and reserved_bytes + declared_size > active_policy.max_total_bytes_per_project:
            decisions.append(BuyerAttachmentEnrichmentDecision(candidate, False, "project_byte_limit"))
            continue
        selected_count += 1
        reserved_bytes += declared_size or 0
        decisions.append(BuyerAttachmentEnrichmentDecision(candidate, True))
    return tuple(decisions)


class BuyerAttachmentEnrichmentPipeline:
    """Orchestrate selective attachment parsing through an account-bound reader."""

    def __init__(
        self,
        repository: BuyerAttachmentRepository,
        capabilities: BuyerReadCapabilities,
        *,
        policy: BuyerAttachmentEnrichmentPolicy | None = None,
        attachment_reader: BuyerAttachmentReader | None = None,
        original_writer: BuyerAttachmentObjectWriter | None = None,
        vision_gateway: BuyerAttachmentVisionGateway | None = None,
    ) -> None:
        for method_name in ("upsert_attachment", "record_attachment_derivative"):
            if not callable(getattr(repository, method_name, None)):
                raise TypeError(f"repository must expose {method_name}")
        if not isinstance(capabilities, BuyerReadCapabilities):
            raise TypeError("capabilities must be BuyerReadCapabilities")
        if attachment_reader is not None and not callable(attachment_reader):
            raise TypeError("attachment_reader must be callable")
        if original_writer is not None and not callable(original_writer):
            raise TypeError("original_writer must be callable")
        if vision_gateway is not None and not callable(getattr(vision_gateway, "analyze_image", None)):
            raise TypeError("vision_gateway must expose analyze_image")
        self._repository = repository
        self._capabilities = capabilities
        self.policy = policy or BuyerAttachmentEnrichmentPolicy()
        if not isinstance(self.policy, BuyerAttachmentEnrichmentPolicy):
            raise TypeError("policy must be BuyerAttachmentEnrichmentPolicy")
        self._attachment_reader = attachment_reader or self._read_with_capabilities
        self._original_writer = original_writer
        self._vision_gateway = vision_gateway

    async def enrich(
        self,
        *,
        run_id: str,
        project_id: str,
        attachments: Iterable[Mapping[str, Any]],
    ) -> BuyerAttachmentEnrichmentResult:
        """Persist one bounded project attachment pass and return its context.

        A failure for one file is recorded as that file's terminal state.  A
        repository failure still propagates: silently losing a local audit row
        would be worse than asking the caller to retry the durable operation.
        """

        normalized_run_id = _required_text(run_id, "run_id")
        normalized_project_id = _required_text(project_id, "project_id")
        decisions = plan_buyer_attachment_enrichment(attachments, policy=self.policy)
        items: list[BuyerAttachmentEnrichmentItem] = []
        parse_pairs: list[tuple[int, BuyerAttachmentParseResult]] = []
        parsed_cache: dict[tuple[str, BuyerAttachmentType | None], BuyerAttachmentParseResult] = {}
        total_downloaded = 0

        for decision in decisions:
            if not decision.selected:
                item = await self._persist_skipped(
                    normalized_run_id,
                    normalized_project_id,
                    decision,
                )
                items.append(item)
                continue

            item, parse_result = await self._process_selected(
                normalized_run_id,
                normalized_project_id,
                decision.candidate,
                total_downloaded=total_downloaded,
                parsed_cache=parsed_cache,
            )
            items.append(item)
            total_downloaded += item.bytes_downloaded
            if parse_result is not None:
                parse_pairs.append((len(items) - 1, parse_result))

        context, contextual_items = _build_result_context(items, parse_pairs, max_characters=self.policy.max_context_characters)
        context_manifest = {
            "schema_version": 1,
            "pipeline_version": _PIPELINE_VERSION,
            "run_id": normalized_run_id,
            "project_id": normalized_project_id,
            "context_hash": context.context_hash,
            "context_characters": len(context.text),
            "attachment_count": len(contextual_items),
            "parsed_attachment_count": sum(
                item.state is BuyerAttachmentEnrichmentState.PARSED for item in contextual_items
            ),
            "attachments": [item.to_manifest() for item in contextual_items],
        }
        return BuyerAttachmentEnrichmentResult(
            run_id=normalized_run_id,
            project_id=normalized_project_id,
            provenance=self._capabilities.provenance.as_dict(),
            policy=self.policy,
            items=tuple(contextual_items),
            context=context,
            context_manifest=context_manifest,
        )

    async def enrich_project(
        self,
        *,
        run_id: str,
        project_id: str,
        attachments: Iterable[Mapping[str, Any]],
    ) -> BuyerAttachmentEnrichmentResult:
        """Compatibility spelling for callers that name the project explicitly."""

        return await self.enrich(run_id=run_id, project_id=project_id, attachments=attachments)

    async def _persist_skipped(
        self,
        run_id: str,
        project_id: str,
        decision: BuyerAttachmentEnrichmentDecision,
    ) -> BuyerAttachmentEnrichmentItem:
        candidate = decision.candidate
        reason = decision.reason or "not_selected"
        state = (
            BuyerAttachmentEnrichmentState.UNSUPPORTED
            if reason == "unsupported_type"
            else BuyerAttachmentEnrichmentState.SKIPPED
        )
        attachment_id = await self._upsert(
            run_id,
            project_id,
            candidate,
            state=state.value,
            error=reason,
        )
        if attachment_id and state is BuyerAttachmentEnrichmentState.UNSUPPORTED:
            await self._record_derivative(
                attachment_id,
                state=state.value,
                parser_name="policy",
                parser_version=_PIPELINE_VERSION,
                extracted_text="",
                metadata={"reason": reason, "pipeline_version": _PIPELINE_VERSION},
                content_hash=_derivative_hash(candidate.identity, state.value, ""),
                error=reason,
            )
        return BuyerAttachmentEnrichmentItem(
            candidate=candidate,
            state=state,
            attachment_id=attachment_id,
            reason=reason,
        )

    async def _process_selected(
        self,
        run_id: str,
        project_id: str,
        candidate: BuyerAttachmentCandidate,
        *,
        total_downloaded: int,
        parsed_cache: dict[tuple[str, BuyerAttachmentType | None], BuyerAttachmentParseResult],
    ) -> tuple[BuyerAttachmentEnrichmentItem, BuyerAttachmentParseResult | None]:
        attachment_id = await self._upsert(run_id, project_id, candidate, state="downloading")
        if attachment_id is None:
            return (
                BuyerAttachmentEnrichmentItem(
                    candidate=candidate,
                    state=BuyerAttachmentEnrichmentState.FAILED,
                    attachment_id=None,
                    reason="attachment_persistence_unavailable",
                ),
                None,
            )
        try:
            download = await asyncio.wait_for(
                self._read_download(candidate),
                timeout=float(self.policy.download_timeout_seconds),
            )
        except TimeoutError:
            return await self._persist_download_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                "download_timeout",
            )
        except Exception as exc:  # noqa: BLE001 - one remote file must not abort the entire manifest.
            return await self._persist_download_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                f"download_failed: {_safe_error(exc)}",
            )

        body_size = len(download.content)
        if body_size > self.policy.max_bytes_per_attachment:
            return await self._persist_size_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                body_size,
                "attachment_byte_limit",
            )
        if total_downloaded + body_size > self.policy.max_total_bytes_per_project:
            return await self._persist_size_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                body_size,
                "project_byte_limit",
            )

        filename = normalize_attachment_filename(download.filename or candidate.filename)
        content_type = normalize_attachment_content_type(download.content_type or candidate.content_type)
        effective_limits = _effective_parse_limits(self.policy)
        metadata = BuyerAttachmentMetadata(
            filename=filename,
            content_type=content_type,
            attachment_type=detect_attachment_type(filename, content_type),
            size_bytes=body_size,
            sha256=sha256(download.content).hexdigest(),
        )
        object_ref: str | None = None
        try:
            object_ref = await self._write_original(candidate, download, metadata)
        except Exception as exc:  # noqa: BLE001 - object persistence is part of the local audit boundary.
            return await self._persist_download_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                f"object_store_failed: {_safe_error(exc)}",
                size_bytes=body_size,
                metadata=metadata,
            )
        if self.policy.require_original_object_ref and not object_ref:
            return await self._persist_download_failure(
                run_id,
                project_id,
                candidate,
                attachment_id,
                "object_store_missing_ref",
                size_bytes=body_size,
                metadata=metadata,
            )

        cache_key = (metadata.sha256, metadata.attachment_type)
        cached = parsed_cache.get(cache_key)
        deduplicated = cached is not None
        if cached is None:
            parse_result = parse_attachment(
                download.content,
                filename=metadata.filename,
                content_type=metadata.content_type,
                limits=effective_limits,
            )
            if self._vision_gateway is not None:
                parse_result = await apply_buyer_attachment_vision(
                    parse_result,
                    content=download.content,
                    gateway=self._vision_gateway,
                    max_characters=effective_limits.max_characters,
                )
            parsed_cache[cache_key] = parse_result
        else:
            parse_result = replace(cached, metadata=metadata)

        state = BuyerAttachmentEnrichmentState(parse_result.status.value)
        await self._upsert(
            run_id,
            project_id,
            candidate,
            attachment_id=attachment_id,
            state=state.value,
            error=_parse_error(parse_result),
            metadata=metadata,
            resolved_download_url=download.resolved_download_url or candidate.resolved_download_url,
            object_ref=object_ref,
        )
        await self._record_derivative(
            attachment_id,
            state=state.value,
            parser_name=parse_result.parser or "policy",
            parser_version=_PIPELINE_VERSION,
            extracted_text=parse_result.text,
            metadata={
                "parse": parse_result.to_manifest(),
                "pipeline_version": _PIPELINE_VERSION,
                "checksum_deduplicated": deduplicated,
                "provenance": self._capabilities.provenance.as_dict(),
            },
            content_hash=_derivative_hash(metadata.sha256, parse_result.parser or "policy", parse_result.text),
            error=_parse_error(parse_result),
        )
        return (
            BuyerAttachmentEnrichmentItem(
                candidate=replace(
                    candidate,
                    filename=metadata.filename,
                    content_type=metadata.content_type,
                    resolved_download_url=download.resolved_download_url or candidate.resolved_download_url,
                ),
                state=state,
                attachment_id=attachment_id,
                reason=_parse_error(parse_result),
                parse_result=parse_result,
                bytes_downloaded=body_size,
                checksum_deduplicated=deduplicated,
            ),
            parse_result,
        )

    async def _persist_download_failure(
        self,
        run_id: str,
        project_id: str,
        candidate: BuyerAttachmentCandidate,
        attachment_id: str,
        reason: str,
        *,
        size_bytes: int | None = None,
        metadata: BuyerAttachmentMetadata | None = None,
    ) -> tuple[BuyerAttachmentEnrichmentItem, None]:
        await self._upsert(
            run_id,
            project_id,
            candidate,
            attachment_id=attachment_id,
            state=BuyerAttachmentEnrichmentState.FAILED.value,
            error=reason,
            metadata=metadata,
            size_bytes=size_bytes,
        )
        return (
            BuyerAttachmentEnrichmentItem(
                candidate=candidate,
                state=BuyerAttachmentEnrichmentState.FAILED,
                attachment_id=attachment_id,
                reason=reason,
                bytes_downloaded=size_bytes or 0,
            ),
            None,
        )

    async def _persist_size_failure(
        self,
        run_id: str,
        project_id: str,
        candidate: BuyerAttachmentCandidate,
        attachment_id: str,
        body_size: int,
        reason: str,
    ) -> tuple[BuyerAttachmentEnrichmentItem, None]:
        state = BuyerAttachmentEnrichmentState.TOO_LARGE
        await self._upsert(
            run_id,
            project_id,
            candidate,
            attachment_id=attachment_id,
            state=state.value,
            error=reason,
            size_bytes=body_size,
        )
        await self._record_derivative(
            attachment_id,
            state=state.value,
            parser_name="policy",
            parser_version=_PIPELINE_VERSION,
            extracted_text="",
            metadata={"reason": reason, "size_bytes": body_size, "pipeline_version": _PIPELINE_VERSION},
            content_hash=_derivative_hash(candidate.identity, state.value, str(body_size)),
            error=reason,
        )
        return (
            BuyerAttachmentEnrichmentItem(
                candidate=candidate,
                state=state,
                attachment_id=attachment_id,
                reason=reason,
                bytes_downloaded=body_size,
            ),
            None,
        )

    async def _upsert(
        self,
        run_id: str,
        project_id: str,
        candidate: BuyerAttachmentCandidate,
        *,
        state: str,
        attachment_id: str | None = None,
        error: str | None = None,
        metadata: BuyerAttachmentMetadata | None = None,
        size_bytes: int | None = None,
        resolved_download_url: str | None = None,
        object_ref: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {
            "attachment_id": attachment_id or candidate.attachment_id,
            "source_observation_id": candidate.source_observation_id,
            "remote_url": _safe_remote_url(candidate.remote_url),
            "resolved_download_url": _safe_remote_url(resolved_download_url or candidate.resolved_download_url),
            "filename": metadata.filename if metadata is not None else candidate.filename,
            "content_type": metadata.content_type if metadata is not None else candidate.content_type,
            "detected_type": (
                metadata.attachment_type.value
                if metadata is not None and metadata.attachment_type is not None
                else candidate.detected_type.value if candidate.detected_type is not None else None
            ),
            "size_bytes": metadata.size_bytes if metadata is not None else size_bytes or candidate.declared_size_bytes,
            "sha256": metadata.sha256 if metadata is not None else None,
            "object_ref": object_ref,
            "state": state,
            "error": error,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        # An attachment without a durable remote URL or supplied local ID has no
        # idempotent repository key, so retain it in the manifest only.
        if "remote_url" not in payload and "attachment_id" not in payload:
            return None
        record = await self._repository.upsert_attachment(run_id, project_id, payload)
        value = record.get("attachment_id")
        return _optional_text(value)

    async def _record_derivative(
        self,
        attachment_id: str,
        *,
        state: str,
        parser_name: str,
        parser_version: str,
        extracted_text: str,
        metadata: Mapping[str, Any],
        content_hash: str,
        error: str | None,
    ) -> None:
        await self._repository.record_attachment_derivative(
            attachment_id,
            {
                "kind": "text",
                "parser_name": parser_name,
                "parser_version": parser_version,
                "content_hash": content_hash,
                "extracted_text": extracted_text,
                "metadata": dict(metadata),
                "state": state,
                "error": error,
            },
        )

    async def _read_download(self, candidate: BuyerAttachmentCandidate) -> BuyerAttachmentDownload:
        request = BuyerAttachmentReadRequest(
            candidate=candidate,
            max_bytes=self.policy.max_bytes_per_attachment,
            timeout_seconds=float(self.policy.download_timeout_seconds),
        )
        value = self._attachment_reader(request)
        if not inspect.isawaitable(value):
            raise BuyerAttachmentEnrichmentError("attachment_reader must return an awaitable")
        return _coerce_download(await value)

    async def _read_with_capabilities(self, request: BuyerAttachmentReadRequest) -> BuyerAttachmentDownload | Mapping[str, Any] | bytes:
        url = request.candidate.download_url
        if not url:
            raise BuyerAttachmentEnrichmentError("attachment has no download URL")
        return await self._capabilities.download_attachment(
            url,
            max_bytes=request.max_bytes,
            timeout_seconds=request.timeout_seconds,
        )

    async def _write_original(
        self,
        candidate: BuyerAttachmentCandidate,
        download: BuyerAttachmentDownload,
        metadata: BuyerAttachmentMetadata,
    ) -> str | None:
        if self._original_writer is None:
            return None
        value = self._original_writer(candidate, download, metadata)
        if not inspect.isawaitable(value):
            raise BuyerAttachmentEnrichmentError("original_writer must return an awaitable")
        object_ref = await value
        if object_ref is None:
            return None
        return _required_text(object_ref, "original_writer object reference")


def _candidate_from_mapping(value: Mapping[str, Any], index: int) -> BuyerAttachmentCandidate:
    if not isinstance(value, Mapping):
        raise TypeError(f"attachment at index {index} must be a mapping")
    remote_url = _optional_text(value.get("remote_url") or value.get("url") or value.get("download_url"))
    resolved_download_url = _optional_text(value.get("resolved_download_url") or value.get("resolved_url"))
    fallback_filename = _filename_from_url(resolved_download_url or remote_url) or f"attachment-{index + 1}"
    filename_value = _optional_text(value.get("filename") or value.get("file_name") or value.get("name"))
    filename = normalize_attachment_filename(filename_value or fallback_filename)
    content_type = normalize_attachment_content_type(
        _optional_text(value.get("content_type") or value.get("mime_type") or value.get("declared_mime"))
    )
    return BuyerAttachmentCandidate(
        source_index=index,
        remote_url=remote_url,
        resolved_download_url=resolved_download_url,
        filename=filename,
        content_type=content_type,
        declared_size_bytes=_non_negative_int_or_none(
            value.get("size_bytes", value.get("size", value.get("file_size")))
        ),
        source_observation_id=_optional_text(value.get("source_observation_id") or value.get("observation_id")),
        attachment_id=_optional_text(value.get("attachment_id")),
    )


def _coerce_download(value: BuyerAttachmentDownload | Mapping[str, Any] | bytes) -> BuyerAttachmentDownload:
    if isinstance(value, BuyerAttachmentDownload):
        return BuyerAttachmentDownload(
            content=_as_bytes(value.content),
            filename=_optional_text(value.filename),
            content_type=normalize_attachment_content_type(value.content_type),
            resolved_download_url=_optional_text(value.resolved_download_url),
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return BuyerAttachmentDownload(content=bytes(value))
    if not isinstance(value, Mapping):
        raise BuyerAttachmentEnrichmentError("attachment reader returned an unsupported response shape")
    status_code = value.get("status_code")
    if isinstance(status_code, int) and status_code >= 400:
        raise BuyerAttachmentEnrichmentError(f"attachment download returned HTTP {status_code}")
    body = next((value[key] for key in ("content", "body", "data", "bytes") if key in value), None)
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise BuyerAttachmentEnrichmentError("attachment reader response has no bytes-like body")
    headers = value.get("headers")
    header_content_type = headers.get("content-type") if isinstance(headers, Mapping) else None
    return BuyerAttachmentDownload(
        content=bytes(body),
        filename=_optional_text(value.get("filename") or value.get("file_name") or value.get("name")),
        content_type=normalize_attachment_content_type(
            _optional_text(value.get("content_type") or value.get("mime_type") or header_content_type)
        ),
        resolved_download_url=_optional_text(
            value.get("resolved_download_url") or value.get("final_url") or value.get("url")
        ),
    )


def _build_result_context(
    items: list[BuyerAttachmentEnrichmentItem],
    parse_pairs: list[tuple[int, BuyerAttachmentParseResult]],
    *,
    max_characters: int,
) -> tuple[BuyerAttachmentContext, list[BuyerAttachmentEnrichmentItem]]:
    context = build_attachment_context((result for _, result in parse_pairs), max_total_characters=max_characters)
    contextual_items = list(items)
    for (index, _result), entry in zip(parse_pairs, context.manifest, strict=True):
        context_characters = _non_negative_int_or_none(entry.get("context_characters")) or 0
        contextual_items[index] = replace(
            contextual_items[index],
            context_characters=context_characters,
            context_omission=_optional_text(entry.get("context_omission")),
        )
    return context, contextual_items


def _effective_parse_limits(policy: BuyerAttachmentEnrichmentPolicy) -> BuyerAttachmentParseLimits:
    return replace(policy.parse_limits, max_bytes=min(policy.parse_limits.max_bytes, policy.max_bytes_per_attachment))


def _parse_error(result: BuyerAttachmentParseResult) -> str | None:
    if result.status is BuyerAttachmentParseStatus.PARSED:
        return None
    reasons = ", ".join(omission.reason for omission in result.omissions)
    return reasons or result.status.value


def _derivative_hash(*values: str) -> str:
    return sha256("\0".join(values).encode("utf-8")).hexdigest()


def _safe_remote_url(value: str | None) -> str | None:
    return sanitize_attachment_context_text(value) if value else None


def _safe_error(exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return sanitize_attachment_context_text(detail[:500])


def _filename_from_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        filename = unquote(urlsplit(value).path.rsplit("/", 1)[-1])
    except ValueError:
        return None
    return filename or None


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _non_negative_int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_bytes(value: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise BuyerAttachmentEnrichmentError("attachment content must be bytes-like")
    return bytes(value)


__all__ = [
    "BuyerAttachmentCandidate",
    "BuyerAttachmentDownload",
    "BuyerAttachmentEnrichmentDecision",
    "BuyerAttachmentEnrichmentError",
    "BuyerAttachmentEnrichmentItem",
    "BuyerAttachmentEnrichmentPipeline",
    "BuyerAttachmentEnrichmentPolicy",
    "BuyerAttachmentEnrichmentPolicyError",
    "BuyerAttachmentEnrichmentResult",
    "BuyerAttachmentEnrichmentState",
    "BuyerAttachmentObjectWriter",
    "BuyerAttachmentReadRequest",
    "BuyerAttachmentReader",
    "BuyerAttachmentRepository",
    "plan_buyer_attachment_enrichment",
]
