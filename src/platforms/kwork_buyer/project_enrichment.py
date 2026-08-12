"""Selective, account-bound project detail enrichment for Buyer Search.

Discovery deliberately stores compact cards first.  This module performs the
slower read-only follow-up against one already selected account capability and
keeps raw responses separate from the stable project-detail projection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .sources.capabilities import BuyerReadCapabilities, BuyerReadCapabilityError


class BuyerProjectEnrichmentError(ValueError):
    """Raised when a selective enrichment request is structurally invalid."""


_SELECTION_ALIASES = {
    "": "manual",
    "all": "all",
    "all_manual": "manual",
    "all_manual_confirmation": "manual",
    "manual": "manual",
    "manual_confirmation": "manual",
    "none": "none",
    "off": "none",
    "disabled": "none",
    "top_n_per_query": "top_n_per_query",
    "top_n_query": "top_n_per_query",
    "top_per_query": "top_n_per_query",
    "top_n_per_run": "top_n_per_run",
    "top_n_run": "top_n_per_run",
    "top_per_run": "top_n_per_run",
    "shortlisted_only": "shortlisted_only",
    "shortlist_only": "shortlisted_only",
    "shortlisted": "shortlisted_only",
    "adaptive": "adaptive_by_preliminary_score",
    "adaptive_by_score": "adaptive_by_preliminary_score",
    "adaptive_by_preliminary_score": "adaptive_by_preliminary_score",
}


@dataclass(frozen=True, slots=True)
class BuyerProjectEnrichmentPolicy:
    """Validated run policy with source-level gates for one detail operation."""

    selection: str = "manual"
    limit: int | None = None
    min_preliminary_score: float | None = None
    project_detail: bool = True
    want_detail: bool = True
    web_state: bool = True
    buyer_history: bool = True
    attachment_manifest: bool = True
    attachment_download: bool = True

    @classmethod
    def from_payload(cls, value: object) -> "BuyerProjectEnrichmentPolicy":
        if value is None:
            payload: Mapping[str, Any] = {}
        elif isinstance(value, str):
            payload = {"mode": value}
        elif isinstance(value, Mapping):
            payload = value
        else:
            raise BuyerProjectEnrichmentError("enrichment_policy must be an object or string")
        if payload.get("enabled") is False:
            return cls(selection="none")

        raw_selection = _text(
            payload.get("selection")
            or payload.get("mode")
            or payload.get("strategy")
            or payload.get("policy")
        )
        selection_key = _normalise_selection(raw_selection)
        selection = _SELECTION_ALIASES.get(selection_key)
        if selection is None:
            raise BuyerProjectEnrichmentError(f"unsupported enrichment selection policy: {raw_selection}")

        limit = _positive_int_or_none(
            payload.get("top_n")
            or payload.get("limit")
            or payload.get("count")
            or payload.get("n")
        )
        if selection in {"top_n_per_query", "top_n_per_run"}:
            limit = limit or 10
        min_score = _number_or_none(
            payload.get("min_preliminary_score")
            or payload.get("minimum_score")
            or payload.get("min_score")
        )
        if selection == "adaptive_by_preliminary_score":
            min_score = min_score if min_score is not None else 60.0

        sources = payload.get("sources")
        return cls(
            selection=selection,
            limit=limit,
            min_preliminary_score=min_score,
            project_detail=_source_enabled(payload, sources, ("project_detail", "project", "mobile_project"), default=True),
            want_detail=_source_enabled(payload, sources, ("want_detail", "want", "mobile_want"), default=True),
            web_state=_source_enabled(payload, sources, ("web_state", "web_projects", "web"), default=True),
            buyer_history=_source_enabled(payload, sources, ("buyer_history", "history"), default=True),
            attachment_manifest=_source_enabled(
                payload,
                sources,
                ("attachment_manifest", "attachments", "files"),
                default=True,
            ),
            attachment_download=_source_enabled(
                payload,
                sources,
                ("attachment_download", "download_attachments", "download_files"),
                default=True,
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "selection": self.selection,
            "limit": self.limit,
            "min_preliminary_score": self.min_preliminary_score,
            "sources": {
                "project_detail": self.project_detail,
                "want_detail": self.want_detail,
                "web_state": self.web_state,
                "buyer_history": self.buyer_history,
                "attachment_manifest": self.attachment_manifest,
                "attachment_download": self.attachment_download,
            },
        }


@dataclass(frozen=True, slots=True)
class BuyerProjectEnrichmentDecision:
    """Selection decision evaluated before borrowing an account capability."""

    allowed: bool
    reason: str
    policy: BuyerProjectEnrichmentPolicy

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": self.policy.to_payload(),
        }


class BuyerProjectEnrichmentController:
    """Run selected detail reads over an already account-bound capability."""

    def __init__(self, search_service: object) -> None:
        for method_name in ("record_project_enrichment", "record_attachment"):
            if not callable(getattr(search_service, method_name, None)):
                raise TypeError(f"search_service must expose {method_name}")
        self.search_service = search_service

    async def selection_decision(
        self,
        *,
        run_id: str,
        project_id: str,
        run: Mapping[str, Any],
        project: Mapping[str, Any],
    ) -> BuyerProjectEnrichmentDecision:
        policy = BuyerProjectEnrichmentPolicy.from_payload(run.get("enrichment_policy"))
        if policy.selection == "none":
            return BuyerProjectEnrichmentDecision(False, "disabled_by_run_policy", policy)
        if policy.selection == "shortlisted_only":
            shortlist = project.get("shortlist")
            state = _text(shortlist.get("state")) if isinstance(shortlist, Mapping) else _text(project.get("shortlist_state"))
            if state.casefold() not in {"shortlisted", "review", "contacted"}:
                return BuyerProjectEnrichmentDecision(False, "project_is_not_shortlisted", policy)
        if policy.selection == "adaptive_by_preliminary_score":
            score = _number_or_none(project.get("preliminary_score"))
            if score is None:
                return BuyerProjectEnrichmentDecision(False, "preliminary_score_is_unavailable", policy)
            if policy.min_preliminary_score is not None and score < policy.min_preliminary_score:
                return BuyerProjectEnrichmentDecision(False, "preliminary_score_below_policy_threshold", policy)
        if policy.selection == "top_n_per_run":
            if not await self._is_top_n_for_run(run_id, project_id, policy.limit or 10):
                return BuyerProjectEnrichmentDecision(False, "outside_top_n_for_run", policy)
        if policy.selection == "top_n_per_query":
            if not await self._is_top_n_for_any_query(run_id, project_id, project, policy.limit or 10):
                return BuyerProjectEnrichmentDecision(False, "outside_top_n_for_query", policy)
        return BuyerProjectEnrichmentDecision(True, "selected", policy)

    async def persist_skipped(
        self,
        *,
        run_id: str,
        project_id: str,
        decision: BuyerProjectEnrichmentDecision,
    ) -> dict[str, Any]:
        stored = await self.search_service.record_project_enrichment(
            run_id,
            project_id,
            {
                "kind": "selection",
                "source": "enrichment_policy",
                "state": "skipped",
                "normalized": decision.to_payload(),
            },
        )
        return {
            "state": "skipped",
            "selection": decision.to_payload(),
            "evidence": [dict(stored)],
            "sources": [],
            "attachment_manifest": [],
        }

    async def enrich_with_capabilities(
        self,
        *,
        run_id: str,
        project_id: str,
        project: Mapping[str, Any],
        capabilities: BuyerReadCapabilities,
        decision: BuyerProjectEnrichmentDecision,
    ) -> dict[str, Any]:
        """Fetch enabled sources without letting one unavailable source block peers."""

        if not decision.allowed:
            raise BuyerProjectEnrichmentError("enrichment selection must be allowed before remote reads")
        remote_project_id = _text(project.get("remote_project_id") or project.get("id"))
        if not remote_project_id:
            raise BuyerProjectEnrichmentError("project is missing remote_project_id")
        policy = decision.policy
        provenance = capabilities.provenance.as_dict()
        source_outcomes: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        attachment_candidates: list[dict[str, Any]] = []
        buyer_username = _buyer_username(project)

        if policy.project_detail:
            outcome, raw = await self._read_source(
                run_id=run_id,
                project_id=project_id,
                kind="project_detail",
                source="kwork_mobile",
                endpoint="/project",
                operation=lambda: capabilities.fetch_project_detail(remote_project_id),
                normalizer=_normalise_detail,
                provenance=provenance,
            )
            source_outcomes.append(outcome)
            evidence.append(dict(outcome["evidence"]))
            if raw is not None:
                buyer_username = buyer_username or _buyer_username(raw)
                attachment_candidates.extend(_extract_attachment_candidates(raw))

        if policy.want_detail:
            outcome, raw = await self._read_source(
                run_id=run_id,
                project_id=project_id,
                kind="want_detail",
                source="kwork_mobile",
                endpoint="/want",
                operation=lambda: capabilities.fetch_want_detail(remote_project_id),
                normalizer=_normalise_detail,
                provenance=provenance,
            )
            source_outcomes.append(outcome)
            evidence.append(dict(outcome["evidence"]))
            if raw is not None:
                buyer_username = buyer_username or _buyer_username(raw)
                attachment_candidates.extend(_extract_attachment_candidates(raw))

        if policy.buyer_history:
            if buyer_username:
                outcome, _ = await self._read_source(
                    run_id=run_id,
                    project_id=project_id,
                    kind="buyer_history",
                    source="kwork_web",
                    endpoint="/projects/list/{buyer}",
                    operation=lambda: capabilities.fetch_buyer_history(buyer_username, page=1, limit=20),
                    normalizer=_normalise_buyer_history,
                    provenance=provenance,
                )
            else:
                outcome = await self._record_nonread_outcome(
                    run_id=run_id,
                    project_id=project_id,
                    kind="buyer_history",
                    source="kwork_web",
                    endpoint="/projects/list/{buyer}",
                    state="skipped",
                    normalized={"reason": "buyer_username_is_unavailable"},
                    provenance=provenance,
                )
            source_outcomes.append(outcome)
            evidence.append(dict(outcome["evidence"]))

        if policy.web_state:
            query = _text(project.get("title")) or remote_project_id
            request: dict[str, Any] = {"page": 1, "query": query}
            category_id = _positive_int_or_none(project.get("category_id"))
            if category_id is not None:
                request["category_id"] = category_id
            outcome, raw = await self._read_source(
                run_id=run_id,
                project_id=project_id,
                kind="web_state",
                source="kwork_web",
                endpoint="/projects",
                operation=lambda: capabilities.fetch_web_projects(**request),
                normalizer=lambda value: _normalise_web_state(value, remote_project_id),
                provenance=provenance,
            )
            source_outcomes.append(outcome)
            evidence.append(dict(outcome["evidence"]))
            if raw is not None:
                attachment_candidates.extend(_extract_attachment_candidates(raw))

        attachment_manifest: list[dict[str, Any]] = []
        if policy.attachment_manifest:
            for attachment in _deduplicate_attachment_candidates(attachment_candidates, project_id=project_id):
                attachment["source_observation_id"] = _optional_text(project.get("latest_observation_id"))
                stored = await self.search_service.record_attachment(run_id, project_id, attachment)
                attachment_manifest.append(dict(stored))

        return {
            "state": "completed",
            "selection": decision.to_payload(),
            "provenance": provenance,
            "sources": source_outcomes,
            "evidence": evidence,
            "attachment_manifest": [_public_attachment_record(item) for item in attachment_manifest],
            "_attachment_records": attachment_manifest,
            "attachment_download_allowed": policy.attachment_download,
        }

    async def _read_source(
        self,
        *,
        run_id: str,
        project_id: str,
        kind: str,
        source: str,
        endpoint: str,
        operation: Callable[[], Awaitable[Any]],
        normalizer: Callable[[Any], Mapping[str, Any]],
        provenance: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Any | None]:
        try:
            raw = await operation()
        except BuyerReadCapabilityError as exc:
            outcome = await self._record_nonread_outcome(
                run_id=run_id,
                project_id=project_id,
                kind=kind,
                source=source,
                endpoint=endpoint,
                state="unavailable",
                normalized={"reason": "read_capability_is_unavailable"},
                error=str(exc)[:500],
                provenance=provenance,
            )
            return outcome, None
        except Exception as exc:  # noqa: BLE001 - failures become durable source evidence.
            outcome = await self._record_nonread_outcome(
                run_id=run_id,
                project_id=project_id,
                kind=kind,
                source=source,
                endpoint=endpoint,
                state="failed",
                normalized={"reason": "remote_read_failed", "exception_type": type(exc).__name__},
                error=f"{type(exc).__name__}: remote read failed",
                provenance=provenance,
            )
            return outcome, None
        normalized = dict(normalizer(raw))
        stored = await self.search_service.record_project_enrichment(
            run_id,
            project_id,
            {
                "kind": kind,
                "source": source,
                "endpoint": endpoint,
                "state": "completed",
                "normalized": normalized,
                "raw": raw,
                "status_code": _response_status_code(raw),
                "provenance": dict(provenance),
            },
        )
        return _source_outcome(kind, stored), raw

    async def _record_nonread_outcome(
        self,
        *,
        run_id: str,
        project_id: str,
        kind: str,
        source: str,
        endpoint: str,
        state: str,
        normalized: Mapping[str, Any],
        provenance: Mapping[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.search_service.record_project_enrichment(
            run_id,
            project_id,
            {
                "kind": kind,
                "source": source,
                "endpoint": endpoint,
                "state": state,
                "normalized": dict(normalized),
                "error": error,
                "provenance": dict(provenance),
            },
        )
        return _source_outcome(kind, stored)

    async def _is_top_n_for_run(self, run_id: str, project_id: str, limit: int) -> bool:
        list_projects = getattr(self.search_service, "list_projects", None)
        if not callable(list_projects):
            raise BuyerProjectEnrichmentError("search_service must expose list_projects for top-N policy")
        page = await list_projects(run_id, sort="score_desc", limit=limit)
        items = page.get("items") if isinstance(page, Mapping) else None
        return any(isinstance(item, Mapping) and _text(item.get("project_id")) == project_id for item in items or ())

    async def _is_top_n_for_any_query(
        self,
        run_id: str,
        project_id: str,
        project: Mapping[str, Any],
        limit: int,
    ) -> bool:
        target_query_ids = {
            _text(match.get("query_id"))
            for match in _sequence_of_mappings(project.get("matches"))
            if _text(match.get("query_id"))
        }
        if not target_query_ids:
            return False
        list_projects = getattr(self.search_service, "list_projects", None)
        get_project = getattr(self.search_service, "get_project", None)
        if not callable(list_projects) or not callable(get_project):
            raise BuyerProjectEnrichmentError("search_service must expose project listing and detail for top-N query policy")
        ranks = {query_id: 0 for query_id in target_query_ids}
        cursor: str | None = None
        scanned = 0
        while scanned < 10_000:
            page = await list_projects(run_id, sort="score_desc", cursor=cursor, limit=min(500, 10_000 - scanned))
            items = page.get("items") if isinstance(page, Mapping) else None
            if not isinstance(items, Sequence):
                return False
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                candidate_id = _text(item.get("project_id"))
                if not candidate_id:
                    continue
                candidate = await get_project(run_id, candidate_id)
                candidate_query_ids = {
                    _text(match.get("query_id"))
                    for match in _sequence_of_mappings(candidate.get("matches") if isinstance(candidate, Mapping) else None)
                    if _text(match.get("query_id")) in target_query_ids
                }
                for query_id in candidate_query_ids:
                    ranks[query_id] += 1
                    if candidate_id == project_id and ranks[query_id] <= limit:
                        return True
                scanned += 1
            next_cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if cursor is None or not items:
                return False
        return False


def _source_outcome(kind: str, stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "state": stored.get("state"),
        "enrichment_id": stored.get("enrichment_id"),
        "raw_artifact_id": stored.get("raw_artifact_id"),
        "observed_at": stored.get("observed_at"),
        "evidence": dict(stored),
    }


def _normalise_detail(value: Any) -> dict[str, Any]:
    record = _detail_mapping(value)
    buyer = _mapping_at(record, ("buyer", "user", "owner", "customer"))
    attachments = _extract_attachment_candidates(value)
    return {
        "remote_project_id": _first_text(record, ("id", "project_id", "want_id", "wantId")),
        "title": _first_text(record, ("title", "name", "headline")),
        "description": _first_text(record, ("description", "text", "content")),
        "status": _first_text(record, ("status", "want_status", "want_status_id")),
        "category_id": _first_text(record, ("category_id", "categoryId", "category")),
        "budget": {
            "min": _first_number(record, ("budget_min", "price", "price_limit", "possible_price_limit")),
            "max": _first_number(record, ("budget_max", "price_limit", "possible_price_limit")),
        },
        "metrics": {
            "offers": _first_number(record, ("offers", "offers_count", "count_offers")),
            "views": _first_number(record, ("views", "views_count", "count_views")),
            "orders": _first_number(record, ("orders", "orders_count")),
        },
        "buyer": {
            "username": _first_text(buyer, ("username", "login", "user_login")) or _first_text(record, ("buyer_username", "username")),
            "remote_user_id": _first_text(buyer, ("id", "user_id", "userId")) or _first_text(record, ("buyer_remote_user_id", "user_id")),
            "hired_percent": _first_number(buyer, ("hired_percent", "user_hired_percent"))
            or _first_number(record, ("buyer_hired_percent", "user_hired_percent")),
            "projects_count": _first_number(buyer, ("projects_count", "user_projects_count"))
            or _first_number(record, ("buyer_projects_count", "user_projects_count")),
        },
        "attachments": [_attachment_summary(item) for item in attachments],
        "attachment_count": len(attachments),
    }


def _normalise_buyer_history(value: Any) -> dict[str, Any]:
    root = value if isinstance(value, Mapping) else {}
    items = _value_list(root, ("items", "projects", "wants", "data"))
    return {
        "username": _first_text(root, ("username", "buyer_username")),
        "page": _first_number(root, ("page",)),
        "total": _first_number(root, ("total", "count", "projects_count")),
        "items": [_project_summary(item) for item in items[:50]],
    }


def _normalise_web_state(value: Any, remote_project_id: str) -> dict[str, Any]:
    root = value if isinstance(value, Mapping) else {}
    items = _value_list(root, ("items", "projects", "wants", "data"))
    matching = next(
        (
            item
            for item in items
            if _first_text(item, ("id", "project_id", "want_id", "wantId")) == remote_project_id
        ),
        None,
    )
    paging = root.get("paging") if isinstance(root.get("paging"), Mapping) else root.get("pagination")
    return {
        "query_result_count": len(items),
        "paging": _compact_mapping(paging) if isinstance(paging, Mapping) else {},
        "matching_project": _project_summary(matching) if isinstance(matching, Mapping) else None,
        "attachment_count": len(_extract_attachment_candidates(matching)) if isinstance(matching, Mapping) else 0,
    }


def _extract_attachment_candidates(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in _attachment_containers(value):
        for item in _value_list(record, ("files", "attachments", "file_list", "documents")):
            remote_url = _first_text(item, ("remote_url", "url", "download_url", "file_url", "href", "link", "path"))
            remote_id = _first_text(item, ("attachment_id", "file_id", "id", "uuid", "hash"))
            if not remote_url and not remote_id:
                continue
            candidate: dict[str, Any] = {
                "attachment_id": remote_id,
                "remote_url": remote_url,
                "resolved_download_url": _first_text(item, ("resolved_download_url", "download_url", "file_url")),
                "filename": _first_text(item, ("filename", "file_name", "name", "original_name")),
                "content_type": _first_text(item, ("content_type", "mime_type", "mime")),
                "size_bytes": _integer_or_none(item.get("size_bytes") or item.get("size") or item.get("file_size")),
                "state": "discovered",
            }
            candidates.append({key: item_value for key, item_value in candidate.items() if item_value is not None})
    return candidates


def _deduplicate_attachment_candidates(candidates: Sequence[Mapping[str, Any]], *, project_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        item = dict(candidate)
        remote_url = _optional_text(item.get("remote_url"))
        supplied_id = _optional_text(item.get("attachment_id"))
        key = remote_url or supplied_id
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        if not supplied_id:
            digest = sha256(f"{project_id}:{key}".encode("utf-8")).hexdigest()[:24]
            item["attachment_id"] = f"buyer_attachment_{digest}"
        result.append(item)
    return result


def _attachment_containers(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    result: list[Mapping[str, Any]] = []
    queue: list[tuple[Mapping[str, Any], int]] = [(value, 0)]
    seen: set[int] = set()
    while queue:
        record, depth = queue.pop(0)
        identifier = id(record)
        if identifier in seen:
            continue
        seen.add(identifier)
        result.append(record)
        if depth >= 3:
            continue
        for key in ("data", "result", "project", "want", "payload", "item"):
            nested = record.get(key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
    return result


def _buyer_username(value: Any) -> str | None:
    record = _detail_mapping(value)
    buyer = _mapping_at(record, ("buyer", "user", "owner", "customer"))
    return _first_text(buyer, ("username", "login", "user_login")) or _first_text(
        record,
        ("buyer_username", "username", "buyer_login"),
    )


def _detail_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for record in _attachment_containers(value):
        if any(key in record for key in ("title", "description", "project_id", "want_id", "id", "buyer", "user")):
            return record
    return value


def _mapping_at(value: Mapping[str, Any], keys: Sequence[str]) -> Mapping[str, Any]:
    for key in keys:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    return {}


def _value_list(value: Mapping[str, Any], keys: Sequence[str]) -> list[Mapping[str, Any]]:
    for record in _attachment_containers(value):
        for key in keys:
            candidate = record.get(key)
            if isinstance(candidate, Mapping):
                for nested_key in ("data", "items", "projects", "wants"):
                    nested = candidate.get(nested_key)
                    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                        return [dict(item) for item in nested if isinstance(item, Mapping)]
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
                return [dict(item) for item in candidate if isinstance(item, Mapping)]
    return []


def _project_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "remote_project_id": _first_text(item, ("id", "project_id", "want_id", "wantId")),
        "title": _first_text(item, ("title", "name", "headline")),
        "status": _first_text(item, ("status", "want_status")),
        "category_id": _first_text(item, ("category_id", "categoryId", "category")),
        "budget": _first_number(item, ("price", "budget", "price_limit", "possible_price_limit")),
        "offers": _first_number(item, ("offers", "offers_count", "count_offers")),
        "views": _first_number(item, ("views", "views_count", "count_views")),
    }


def _attachment_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": item.get("attachment_id"),
        "filename": item.get("filename"),
        "content_type": item.get("content_type"),
        "detected_type": item.get("detected_type"),
        "size_bytes": item.get("size_bytes"),
        "state": item.get("state"),
        "has_remote_url": bool(item.get("remote_url") or item.get("resolved_download_url")),
    }


def _public_attachment_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": item.get("attachment_id"),
        "filename": item.get("filename"),
        "content_type": item.get("content_type"),
        "detected_type": item.get("detected_type"),
        "size_bytes": item.get("size_bytes"),
        "sha256": item.get("sha256"),
        "state": item.get("state"),
        "has_remote_url": bool(item.get("remote_url") or item.get("resolved_download_url")),
    }


def _response_status_code(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    return _integer_or_none(value.get("status_code"))


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if isinstance(item, (str, int, float, bool)) or item is None}


def _source_enabled(
    payload: Mapping[str, Any],
    sources: Any,
    aliases: Sequence[str],
    *,
    default: bool,
) -> bool:
    for key in aliases:
        if key in payload:
            return _as_bool(payload[key], key)
    if isinstance(sources, Mapping):
        for key in aliases:
            if key in sources:
                return _as_bool(sources[key], f"sources.{key}")
    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
        selected = {_normalise_selection(_text(item)) for item in sources}
        return any(_normalise_selection(alias) in selected for alias in aliases)
    return default


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"false", "0", "no", "off"}:
        return False
    raise BuyerProjectEnrichmentError(f"{name} must be a boolean")


def _sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _first_text(value: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        text = _optional_text(value.get(key))
        if text:
            return text
    return None


def _first_number(value: Mapping[str, Any], keys: Sequence[str]) -> float | int | None:
    for key in keys:
        number = _number_or_none(value.get(key))
        if number is not None:
            return number
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _positive_int_or_none(value: Any) -> int | None:
    result = _integer_or_none(value)
    if result is None:
        return None
    if not 1 <= result <= 10_000:
        raise BuyerProjectEnrichmentError("enrichment top-N limit must be between 1 and 10000")
    return result


def _normalise_selection(value: str) -> str:
    return "_".join(value.casefold().replace("-", " ").split())


__all__ = [
    "BuyerProjectEnrichmentController",
    "BuyerProjectEnrichmentDecision",
    "BuyerProjectEnrichmentError",
    "BuyerProjectEnrichmentPolicy",
]
