"""Application bridge for selective, account-bound attachment enrichment.

The attachment pipeline deliberately has no knowledge of runs, project detail
projections, feature flags, or the HTTP composition layer.  This controller
provides that application boundary while preserving its safety guarantees:

* attachment manifests come from the durable Buyer Search project projection;
* every remote read is performed through an injected ``BuyerReadCapabilities``
  instance tied to one explicitly selected account;
* durable writes go through ``BuyerSearchService`` so existing Buyer events are
  emitted alongside attachment and parser records;
* the response contains audit metadata and a context hash, never extracted
  attachment text.

The server owns how a short-lived account/VPNTE lease becomes a capabilities
factory.  Keeping that composition injected prevents a global Kwork client or
an accidental direct-network fallback from entering the enrichment path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import inspect
from typing import Any

from .attachment_vision import BuyerAttachmentVisionGateway
from .enrichment import (
    BuyerAttachmentEnrichmentPolicy,
    BuyerAttachmentEnrichmentResult,
    BuyerAttachmentEnrichmentPipeline,
    BuyerAttachmentObjectWriter,
)
from .project_enrichment import BuyerProjectEnrichmentController
from .service import BuyerSearchSettings
from .sources.capabilities import BuyerReadCapabilities


class BuyerAttachmentEnrichmentControllerError(ValueError):
    """Raised when an enrichment request cannot retain its account boundary."""


BuyerAttachmentCapabilitiesFactory = Callable[
    [str, str, str], BuyerReadCapabilities | Awaitable[BuyerReadCapabilities]
]


class _BuyerSearchAttachmentRepository:
    """Adapt public Buyer Search persistence methods to the pipeline protocol."""

    def __init__(self, search_service: object, *, run_id: str, project_id: str) -> None:
        self._search_service = search_service
        self._run_id = run_id
        self._project_id = project_id

    async def upsert_attachment(
        self,
        run_id: str,
        project_id: str,
        attachment: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_scope(run_id, project_id)
        stored = await self._search_service.record_attachment(self._run_id, self._project_id, dict(attachment))
        if not isinstance(stored, Mapping):
            raise BuyerAttachmentEnrichmentControllerError("Buyer Search attachment persistence returned an invalid record")
        return stored

    async def record_attachment_derivative(
        self,
        attachment_id: str,
        derivative: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        stored = await self._search_service.record_attachment_derivative(
            self._run_id,
            self._project_id,
            _required_text(attachment_id, "attachment_id"),
            dict(derivative),
        )
        if not isinstance(stored, Mapping):
            raise BuyerAttachmentEnrichmentControllerError("Buyer Search derivative persistence returned an invalid record")
        return stored

    def _require_scope(self, run_id: str, project_id: str) -> None:
        if run_id != self._run_id or project_id != self._project_id:
            raise BuyerAttachmentEnrichmentControllerError("attachment persistence scope does not match the requested project")


class BuyerAttachmentEnrichmentController:
    """Run a bounded parse pass over one durable Buyer Search project.

    ``capabilities_factory`` must return an account-bound read capability for
    exactly the ``account_registration_id`` requested by the operator.  The
    controller validates that provenance again before a file is downloaded.
    """

    def __init__(
        self,
        search_service: object,
        *,
        capabilities_factory: BuyerAttachmentCapabilitiesFactory,
        settings: BuyerSearchSettings,
        policy: BuyerAttachmentEnrichmentPolicy | None = None,
        original_writer: BuyerAttachmentObjectWriter | None = None,
        vision_gateway: BuyerAttachmentVisionGateway | None = None,
    ) -> None:
        for method_name in (
            "get_run",
            "get_project",
            "record_project_enrichment",
            "record_attachment",
            "record_attachment_derivative",
        ):
            if not callable(getattr(search_service, method_name, None)):
                raise TypeError(f"search_service must expose {method_name}")
        if not callable(capabilities_factory):
            raise TypeError("capabilities_factory must be callable")
        if not isinstance(settings, BuyerSearchSettings):
            raise TypeError("settings must be BuyerSearchSettings")
        if policy is not None and not isinstance(policy, BuyerAttachmentEnrichmentPolicy):
            raise TypeError("policy must be BuyerAttachmentEnrichmentPolicy")
        if original_writer is not None and not callable(original_writer):
            raise TypeError("original_writer must be callable")
        if vision_gateway is not None and not callable(getattr(vision_gateway, "analyze_image", None)):
            raise TypeError("vision_gateway must expose analyze_image")
        self.search_service = search_service
        self.capabilities_factory = capabilities_factory
        self.settings = settings
        self.policy = policy
        self.original_writer = original_writer
        self.vision_gateway = vision_gateway
        self.project_enrichment = BuyerProjectEnrichmentController(search_service)

    async def enrich_project(
        self,
        *,
        run_id: str,
        project_id: str,
        account_registration_id: str,
    ) -> dict[str, Any]:
        """Refresh selected detail sources, then optionally parse their files.

        Detail enrichment is useful even when file downloading is feature-gated.
        Both phases reuse the same account/VPNTE-bound capability so a project
        has one coherent provenance record.
        """

        normalized_run_id = _required_text(run_id, "run_id")
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_account_id = _required_text(account_registration_id, "account_registration_id")
        run = await self.search_service.get_run(normalized_run_id)
        if not isinstance(run, Mapping):
            raise BuyerAttachmentEnrichmentControllerError("Buyer Search run must be an object")
        project = await self.search_service.get_project(normalized_run_id, normalized_project_id)
        if not isinstance(project, Mapping):
            raise BuyerAttachmentEnrichmentControllerError("Buyer Search project detail must be an object")
        decision = await self.project_enrichment.selection_decision(
            run_id=normalized_run_id,
            project_id=normalized_project_id,
            run=run,
            project=project,
        )
        if not decision.allowed:
            detail_result = await self.project_enrichment.persist_skipped(
                run_id=normalized_run_id,
                project_id=normalized_project_id,
                decision=decision,
            )
            return _skipped_enrichment_payload(
                run_id=normalized_run_id,
                project_id=normalized_project_id,
                detail_result=detail_result,
                reason=detail_result["selection"]["reason"],
            )

        capabilities = await _await_capabilities(
            self.capabilities_factory,
            normalized_run_id,
            normalized_project_id,
            normalized_account_id,
        )
        try:
            if capabilities.provenance.account_registration_id != normalized_account_id:
                raise BuyerAttachmentEnrichmentControllerError(
                    "account-bound attachment reader does not match account_registration_id"
                )

            detail_result = await self.project_enrichment.enrich_with_capabilities(
                run_id=normalized_run_id,
                project_id=normalized_project_id,
                project=project,
                capabilities=capabilities,
                decision=decision,
            )
            remote_attachment_records = detail_result.pop("_attachment_records", ())
            manifest = _merge_attachment_manifests(
                _attachment_manifest(project),
                remote_attachment_records,
            )
            downloads_allowed = self.settings.attachment_download and bool(detail_result.get("attachment_download_allowed"))
            if not downloads_allowed:
                return _skipped_enrichment_payload(
                    run_id=normalized_run_id,
                    project_id=normalized_project_id,
                    detail_result=detail_result,
                    reason=(
                        "attachment_download_disabled_by_feature_flag"
                        if not self.settings.attachment_download
                        else "attachment_download_disabled_by_run_policy"
                    ),
                    attachment_manifest=manifest,
                )

            pipeline = BuyerAttachmentEnrichmentPipeline(
                _BuyerSearchAttachmentRepository(
                    self.search_service,
                    run_id=normalized_run_id,
                    project_id=normalized_project_id,
                ),
                capabilities,
                policy=self.policy,
                original_writer=self.original_writer,
                vision_gateway=self.vision_gateway if self.settings.attachment_vision else None,
            )
            result = await pipeline.enrich(
                run_id=normalized_run_id,
                project_id=normalized_project_id,
                attachments=manifest,
            )
            payload = _result_payload(result)
            payload["detail_enrichment"] = detail_result
            payload["attachment_enrichment"] = {
                "state": "completed",
                "attachment_manifest": _public_attachment_manifest(manifest),
                "parsed_count": payload["parsed_count"],
                "bytes_downloaded": payload["bytes_downloaded"],
            }
            return payload
        finally:
            await _close_capabilities(capabilities)


async def _await_capabilities(
    factory: BuyerAttachmentCapabilitiesFactory,
    run_id: str,
    project_id: str,
    account_registration_id: str,
) -> BuyerReadCapabilities:
    value = factory(run_id, project_id, account_registration_id)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, BuyerReadCapabilities):
        raise BuyerAttachmentEnrichmentControllerError("capabilities_factory must return BuyerReadCapabilities")
    return value


async def _close_capabilities(capabilities: BuyerReadCapabilities) -> None:
    """Close a short-lived account-bound reader when its factory owns one."""

    close = getattr(capabilities, "close", None)
    if not callable(close):
        return
    value = close()
    if inspect.isawaitable(value):
        await value


def _attachment_manifest(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract only immutable attachment fields from the durable detail view."""

    attachments = project.get("attachments")
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes, bytearray)):
        return []
    manifest: list[dict[str, Any]] = []
    fields = (
        "attachment_id",
        "source_observation_id",
        "remote_url",
        "resolved_download_url",
        "filename",
        "content_type",
        "detected_type",
        "size_bytes",
    )
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        manifest.append({field: attachment[field] for field in fields if field in attachment})
    return manifest


def _merge_attachment_manifests(*values: Any) -> list[dict[str, Any]]:
    """Preserve durable attachments while adding newly observed remote files."""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            attachment = dict(item)
            key = str(attachment.get("remote_url") or attachment.get("attachment_id") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(attachment)
    return merged


def _skipped_enrichment_payload(
    *,
    run_id: str,
    project_id: str,
    detail_result: Mapping[str, Any],
    reason: str,
    attachment_manifest: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project_id": project_id,
        "detail_enrichment": dict(detail_result),
        "attachment_enrichment": {
            "state": "skipped",
            "reason": reason,
            "attachment_manifest": _public_attachment_manifest(attachment_manifest),
        },
        "attachment_manifest": _public_attachment_manifest(attachment_manifest),
    }


def _public_attachment_manifest(attachments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "attachment_id",
        "filename",
        "content_type",
        "detected_type",
        "size_bytes",
        "sha256",
        "object_ref",
        "state",
    )
    return [
        {
            **{field: item[field] for field in fields if field in item},
            "has_remote_url": bool(item.get("remote_url") or item.get("resolved_download_url")),
        }
        for item in attachments
        if isinstance(item, Mapping)
    ]


def _result_payload(result: BuyerAttachmentEnrichmentResult) -> dict[str, Any]:
    """Expose audit-safe metadata while leaving parsed text in durable storage."""

    return {
        "run_id": result.run_id,
        "project_id": result.project_id,
        "provenance": dict(result.provenance),
        "policy": {
            "max_attachments_per_project": result.policy.max_attachments_per_project,
            "max_bytes_per_attachment": result.policy.max_bytes_per_attachment,
            "max_total_bytes_per_project": result.policy.max_total_bytes_per_project,
            "download_timeout_seconds": result.policy.download_timeout_seconds,
            "max_context_characters": result.policy.max_context_characters,
            "allowed_types": sorted(item.value for item in result.policy.allowed_types),
            "require_original_object_ref": result.policy.require_original_object_ref,
        },
        "items": [item.to_manifest() for item in result.items],
        "bytes_downloaded": result.bytes_downloaded,
        "parsed_count": result.parsed_count,
        "context": {
            "context_hash": result.context.context_hash,
            "character_count": len(result.context.text),
            "attachment_count": len(result.context.manifest),
        },
        "context_manifest": dict(result.context_manifest),
    }


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerAttachmentEnrichmentControllerError(f"{name} cannot be blank")
    return value.strip()


__all__ = [
    "BuyerAttachmentCapabilitiesFactory",
    "BuyerAttachmentEnrichmentController",
    "BuyerAttachmentEnrichmentControllerError",
]
