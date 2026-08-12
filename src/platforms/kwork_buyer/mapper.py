"""Pure canonical mapping for Buyer Search mobile and web project evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import html
import json
import math
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import unicodedata

from .sources.capabilities import BuyerReadProvenance


MOBILE_PROJECT_SOURCE = "mobile_projects"
WEB_PROJECT_SOURCE = "web_projects"
_PROJECT_SOURCES = frozenset((MOBILE_PROJECT_SOURCE, WEB_PROJECT_SOURCE))
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class BuyerProjectMappingError(ValueError):
    """Raised when source evidence cannot form a canonical Buyer project."""


@dataclass(frozen=True, slots=True)
class BuyerProjectObservationContext:
    """Durable request and account provenance required for every observation."""

    run_id: str
    query_id: str
    task_id: str
    attempt_id: str
    observed_at: str
    page: int
    response_position: int
    provenance: BuyerReadProvenance
    raw_artifact_id: str | None = None
    route_generation: int | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "query_id", "task_id", "attempt_id", "observed_at"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.provenance, BuyerReadProvenance):
            raise TypeError("provenance must be a BuyerReadProvenance")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page <= 0:
            raise ValueError("page must be a positive integer")
        if isinstance(self.response_position, bool) or not isinstance(self.response_position, int) or self.response_position < 0:
            raise ValueError("response_position must be a non-negative integer")
        if self.route_generation is not None and (
            isinstance(self.route_generation, bool) or not isinstance(self.route_generation, int) or self.route_generation < 0
        ):
            raise ValueError("route_generation must be a non-negative integer or None")
        object.__setattr__(self, "raw_artifact_id", _optional_text(self.raw_artifact_id))

    def with_response_position(self, response_position: int) -> BuyerProjectObservationContext:
        return BuyerProjectObservationContext(
            run_id=self.run_id,
            query_id=self.query_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            observed_at=self.observed_at,
            page=self.page,
            response_position=response_position,
            provenance=self.provenance,
            raw_artifact_id=self.raw_artifact_id,
            route_generation=self.route_generation,
        )


@dataclass(frozen=True, slots=True)
class BuyerProjectSnapshot:
    """Normalized source fields before durable canonical and observation records."""

    source: str
    remote_project_id: str
    title: str
    description: str = ""
    status: str | None = None
    category_id: int | None = None
    parent_category_id: int | None = None
    buyer_remote_user_id: str | None = None
    buyer_username: str | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    offers: int | None = None
    views: int | None = None
    orders: int | None = None
    buyer_hired_percent: float | None = None
    buyer_projects_count: int | None = None
    buyer_active_projects_count: int | None = None
    remote_updated_at: str | None = None
    canonical_url: str | None = None
    alternative_remote_ids: tuple[str, ...] = ()
    alternative_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        source = _required_text(self.source, "source")
        if source not in _PROJECT_SOURCES:
            raise BuyerProjectMappingError(f"unsupported Buyer project source {source!r}")
        remote_project_id = _required_text(self.remote_project_id, "remote_project_id")
        title = normalize_buyer_project_text(self.title)
        if not title:
            raise BuyerProjectMappingError("title cannot be blank")
        description = normalize_buyer_project_text(self.description)
        canonical_url = normalize_buyer_project_url(self.canonical_url, remote_project_id=remote_project_id)
        alternative_ids = _normalized_identifiers(self.alternative_remote_ids, exclude={remote_project_id})
        url_project_id = _project_id_from_url(canonical_url)
        if url_project_id and url_project_id != remote_project_id:
            alternative_ids = _normalized_identifiers((*alternative_ids, url_project_id), exclude={remote_project_id})
        alternative_urls = _normalized_urls(self.alternative_urls, exclude={canonical_url})

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "remote_project_id", remote_project_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "status", _optional_normalized_text(self.status))
        object.__setattr__(self, "category_id", _optional_positive_int(self.category_id, "category_id"))
        object.__setattr__(self, "parent_category_id", _optional_positive_int(self.parent_category_id, "parent_category_id"))
        object.__setattr__(self, "buyer_remote_user_id", _optional_text(self.buyer_remote_user_id))
        object.__setattr__(self, "buyer_username", _optional_normalized_text(self.buyer_username))
        object.__setattr__(self, "budget_min", _optional_nonnegative_number(self.budget_min, "budget_min"))
        object.__setattr__(self, "budget_max", _optional_nonnegative_number(self.budget_max, "budget_max"))
        object.__setattr__(self, "offers", _optional_nonnegative_int(self.offers, "offers"))
        object.__setattr__(self, "views", _optional_nonnegative_int(self.views, "views"))
        object.__setattr__(self, "orders", _optional_nonnegative_int(self.orders, "orders"))
        object.__setattr__(self, "buyer_hired_percent", _optional_nonnegative_number(self.buyer_hired_percent, "buyer_hired_percent"))
        object.__setattr__(self, "buyer_projects_count", _optional_nonnegative_int(self.buyer_projects_count, "buyer_projects_count"))
        object.__setattr__(self, "buyer_active_projects_count", _optional_nonnegative_int(self.buyer_active_projects_count, "buyer_active_projects_count"))
        object.__setattr__(self, "remote_updated_at", _optional_text(self.remote_updated_at))
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "alternative_remote_ids", alternative_ids)
        object.__setattr__(self, "alternative_urls", alternative_urls)


@dataclass(frozen=True, slots=True)
class BuyerCanonicalProject:
    """The one platform/project row shared by all Buyer Search runs and sources."""

    platform: str
    remote_project_id: str
    canonical_url: str
    latest_title: str
    latest_description: str
    latest_status: str | None
    latest_category_id: int | None
    buyer_remote_user_id: str | None
    buyer_username: str | None
    latest_remote_updated_at: str | None
    alternative_remote_ids: tuple[str, ...] = ()
    alternative_urls: tuple[str, ...] = ()
    canonical_hash: str = field(init=False)

    def __post_init__(self) -> None:
        platform = _required_text(self.platform, "platform")
        remote_project_id = _required_text(self.remote_project_id, "remote_project_id")
        canonical_url = normalize_buyer_project_url(self.canonical_url, remote_project_id=remote_project_id)
        title = normalize_buyer_project_text(self.latest_title)
        if not title:
            raise BuyerProjectMappingError("latest_title cannot be blank")
        alternative_ids = _normalized_identifiers(self.alternative_remote_ids, exclude={remote_project_id})
        url_project_id = _project_id_from_url(canonical_url)
        if url_project_id and url_project_id != remote_project_id:
            alternative_ids = _normalized_identifiers((*alternative_ids, url_project_id), exclude={remote_project_id})
        alternative_urls = _normalized_urls(self.alternative_urls, exclude={canonical_url})
        canonical_hash = _stable_hash(
            {
                "platform": platform,
                "remote_project_id": remote_project_id,
                "canonical_url": canonical_url,
                "alternative_remote_ids": alternative_ids,
                "alternative_urls": alternative_urls,
                "title": title,
                "description": normalize_buyer_project_text(self.latest_description),
                "status": _optional_normalized_text(self.latest_status),
                "category_id": _optional_positive_int(self.latest_category_id, "latest_category_id"),
                "buyer_remote_user_id": _optional_text(self.buyer_remote_user_id),
                "buyer_username": _optional_normalized_text(self.buyer_username),
                "remote_updated_at": _optional_text(self.latest_remote_updated_at),
            }
        )
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "remote_project_id", remote_project_id)
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "latest_title", title)
        object.__setattr__(self, "latest_description", normalize_buyer_project_text(self.latest_description))
        object.__setattr__(self, "latest_status", _optional_normalized_text(self.latest_status))
        object.__setattr__(self, "latest_category_id", _optional_positive_int(self.latest_category_id, "latest_category_id"))
        object.__setattr__(self, "buyer_remote_user_id", _optional_text(self.buyer_remote_user_id))
        object.__setattr__(self, "buyer_username", _optional_normalized_text(self.buyer_username))
        object.__setattr__(self, "latest_remote_updated_at", _optional_text(self.latest_remote_updated_at))
        object.__setattr__(self, "alternative_remote_ids", alternative_ids)
        object.__setattr__(self, "alternative_urls", alternative_urls)
        object.__setattr__(self, "canonical_hash", canonical_hash)

    @property
    def identity_ids(self) -> frozenset[str]:
        return frozenset((self.remote_project_id, *self.alternative_remote_ids))

    @property
    def identity_urls(self) -> frozenset[str]:
        return frozenset((self.canonical_url, *self.alternative_urls))

    def to_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "remote_project_id": self.remote_project_id,
            "canonical_url": self.canonical_url,
            "latest_title": self.latest_title,
            "latest_description": self.latest_description,
            "latest_status": self.latest_status,
            "latest_category_id": self.latest_category_id,
            "buyer_remote_user_id": self.buyer_remote_user_id,
            "buyer_username": self.buyer_username,
            "latest_remote_updated_at": self.latest_remote_updated_at,
            "alternative_remote_ids": list(self.alternative_remote_ids),
            "alternative_urls": list(self.alternative_urls),
            "canonical_hash": self.canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class BuyerProjectObservation:
    """Immutable source evidence linked to a query/task/account provenance."""

    source: str
    context: BuyerProjectObservationContext
    remote_project_id: str
    title: str
    description: str
    budget_min: float | None
    budget_max: float | None
    offers: int | None
    views: int | None
    orders: int | None
    remote_status: str | None
    category_id: int | None
    parent_category_id: int | None
    buyer_hired_percent: float | None
    buyer_projects_count: int | None
    buyer_active_projects_count: int | None
    normalized_hash: str = field(init=False)

    def __post_init__(self) -> None:
        source = _required_text(self.source, "source")
        if source not in _PROJECT_SOURCES:
            raise BuyerProjectMappingError(f"unsupported Buyer project source {source!r}")
        if not isinstance(self.context, BuyerProjectObservationContext):
            raise TypeError("context must be a BuyerProjectObservationContext")
        if self.context.provenance.source != source:
            raise BuyerProjectMappingError("observation source must match provenance source")
        remote_project_id = _required_text(self.remote_project_id, "remote_project_id")
        title = normalize_buyer_project_text(self.title)
        if not title:
            raise BuyerProjectMappingError("title cannot be blank")
        values = {
            "source": source,
            "remote_project_id": remote_project_id,
            "title": title,
            "description": normalize_buyer_project_text(self.description),
            "budget_min": _optional_nonnegative_number(self.budget_min, "budget_min"),
            "budget_max": _optional_nonnegative_number(self.budget_max, "budget_max"),
            "offers": _optional_nonnegative_int(self.offers, "offers"),
            "views": _optional_nonnegative_int(self.views, "views"),
            "orders": _optional_nonnegative_int(self.orders, "orders"),
            "remote_status": _optional_normalized_text(self.remote_status),
            "category_id": _optional_positive_int(self.category_id, "category_id"),
            "parent_category_id": _optional_positive_int(self.parent_category_id, "parent_category_id"),
            "buyer_hired_percent": _optional_nonnegative_number(self.buyer_hired_percent, "buyer_hired_percent"),
            "buyer_projects_count": _optional_nonnegative_int(self.buyer_projects_count, "buyer_projects_count"),
            "buyer_active_projects_count": _optional_nonnegative_int(
                self.buyer_active_projects_count, "buyer_active_projects_count"
            ),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "normalized_hash", _stable_hash(values))

    def to_payload(self) -> dict[str, Any]:
        provenance = self.context.provenance
        return {
            "source": self.source,
            "remote_project_id": self.remote_project_id,
            "run_id": self.context.run_id,
            "query_id": self.context.query_id,
            "task_id": self.context.task_id,
            "attempt_id": self.context.attempt_id,
            "worker_id": provenance.worker_id,
            "account_registration_id": provenance.account_registration_id,
            "transport_id": provenance.transport_id,
            "egress_ip": provenance.egress_ip,
            "route_generation": self.context.route_generation,
            "page": self.context.page,
            "response_position": self.context.response_position,
            "observed_at": self.context.observed_at,
            "title": self.title,
            "description": self.description,
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "offers": self.offers,
            "views": self.views,
            "orders": self.orders,
            "remote_status": self.remote_status,
            "category_id": self.category_id,
            "parent_category_id": self.parent_category_id,
            "buyer_hired_percent": self.buyer_hired_percent,
            "buyer_projects_count": self.buyer_projects_count,
            "buyer_active_projects_count": self.buyer_active_projects_count,
            "raw_artifact_id": self.context.raw_artifact_id,
            "normalized_hash": self.normalized_hash,
        }


@dataclass(frozen=True, slots=True)
class BuyerMappedProject:
    """Canonical and immutable evidence records created from one source card."""

    canonical: BuyerCanonicalProject
    observation: BuyerProjectObservation

    def __post_init__(self) -> None:
        if self.canonical.remote_project_id != self.observation.remote_project_id:
            raise BuyerProjectMappingError("canonical and observation remote project IDs must match")


def map_buyer_project(
    snapshot: BuyerProjectSnapshot,
    *,
    context: BuyerProjectObservationContext,
    platform: str = "kwork",
) -> BuyerMappedProject:
    """Convert an already normalized source snapshot into durable-ready records."""

    if not isinstance(snapshot, BuyerProjectSnapshot):
        raise TypeError("snapshot must be a BuyerProjectSnapshot")
    if not isinstance(context, BuyerProjectObservationContext):
        raise TypeError("context must be a BuyerProjectObservationContext")
    if context.provenance.source != snapshot.source:
        raise BuyerProjectMappingError("snapshot source must match provenance source")
    canonical = BuyerCanonicalProject(
        platform=platform,
        remote_project_id=snapshot.remote_project_id,
        canonical_url=snapshot.canonical_url or "",
        latest_title=snapshot.title,
        latest_description=snapshot.description,
        latest_status=snapshot.status,
        latest_category_id=snapshot.category_id,
        buyer_remote_user_id=snapshot.buyer_remote_user_id,
        buyer_username=snapshot.buyer_username,
        latest_remote_updated_at=snapshot.remote_updated_at,
        alternative_remote_ids=snapshot.alternative_remote_ids,
        alternative_urls=snapshot.alternative_urls,
    )
    observation = BuyerProjectObservation(
        source=snapshot.source,
        context=context,
        remote_project_id=snapshot.remote_project_id,
        title=snapshot.title,
        description=snapshot.description,
        budget_min=snapshot.budget_min,
        budget_max=snapshot.budget_max,
        offers=snapshot.offers,
        views=snapshot.views,
        orders=snapshot.orders,
        remote_status=snapshot.status,
        category_id=snapshot.category_id,
        parent_category_id=snapshot.parent_category_id,
        buyer_hired_percent=snapshot.buyer_hired_percent,
        buyer_projects_count=snapshot.buyer_projects_count,
        buyer_active_projects_count=snapshot.buyer_active_projects_count,
    )
    return BuyerMappedProject(canonical=canonical, observation=observation)


def merge_canonical_projects(
    first: BuyerCanonicalProject,
    second: BuyerCanonicalProject,
) -> BuyerCanonicalProject:
    """Merge same-project source records that share an alternate ID or URL."""

    if not isinstance(first, BuyerCanonicalProject) or not isinstance(second, BuyerCanonicalProject):
        raise TypeError("projects must be BuyerCanonicalProject values")
    if first.platform != second.platform:
        raise BuyerProjectMappingError("cannot merge projects from different platforms")
    if not (first.identity_ids & second.identity_ids or first.identity_urls & second.identity_urls):
        raise BuyerProjectMappingError("cannot merge projects without a shared canonical ID or URL")
    remote_project_id = first.remote_project_id
    alternative_ids = _normalized_identifiers(
        (*first.identity_ids, *second.identity_ids),
        exclude={remote_project_id},
    )
    canonical_url = first.canonical_url
    alternative_urls = _normalized_urls(
        (*first.identity_urls, *second.identity_urls),
        exclude={canonical_url},
    )
    return BuyerCanonicalProject(
        platform=first.platform,
        remote_project_id=remote_project_id,
        canonical_url=canonical_url,
        latest_title=second.latest_title or first.latest_title,
        latest_description=second.latest_description or first.latest_description,
        latest_status=second.latest_status or first.latest_status,
        latest_category_id=second.latest_category_id or first.latest_category_id,
        buyer_remote_user_id=second.buyer_remote_user_id or first.buyer_remote_user_id,
        buyer_username=second.buyer_username or first.buyer_username,
        latest_remote_updated_at=second.latest_remote_updated_at or first.latest_remote_updated_at,
        alternative_remote_ids=alternative_ids,
        alternative_urls=alternative_urls,
    )


def normalize_buyer_project_text(value: Any) -> str:
    """Normalize text from inconsistent Kwork mobile/web response shapes."""

    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    return " ".join(unicodedata.normalize("NFKC", text).split())


def normalize_buyer_project_url(value: str | None, *, remote_project_id: str) -> str:
    """Drop volatile query/fragment parts and fall back to a stable Kwork URL."""

    if value is not None and isinstance(value, str) and value.strip():
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
            path = parsed.path.rstrip("/") or "/"
            return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))
        if not parsed.scheme and parsed.path.startswith("/"):
            return urlunsplit(("https", "kwork.ru", parsed.path.rstrip("/") or "/", "", ""))
    return f"https://kwork.ru/projects/{quote(remote_project_id, safe='')}"


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BuyerProjectMappingError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_normalized_text(value: Any) -> str | None:
    normalized = normalize_buyer_project_text(value)
    return normalized or None


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BuyerProjectMappingError(f"{name} must be a positive integer or None")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerProjectMappingError(f"{name} must be a positive integer or None") from exc
    if parsed <= 0:
        raise BuyerProjectMappingError(f"{name} must be a positive integer or None")
    return parsed


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BuyerProjectMappingError(f"{name} must be a non-negative integer or None")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerProjectMappingError(f"{name} must be a non-negative integer or None") from exc
    if parsed < 0:
        raise BuyerProjectMappingError(f"{name} must be a non-negative integer or None")
    return parsed


def _optional_nonnegative_number(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BuyerProjectMappingError(f"{name} must be a non-negative number or None")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BuyerProjectMappingError(f"{name} must be a non-negative number or None") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise BuyerProjectMappingError(f"{name} must be a non-negative number or None")
    return parsed


def _normalized_identifiers(values: Any, *, exclude: set[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        normalized = _optional_text(value)
        if normalized and normalized not in exclude:
            result.add(normalized)
    return tuple(sorted(result))


def _normalized_urls(values: Any, *, exclude: set[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            continue
        normalized = urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/") or "/", "", ""))
        if normalized not in exclude:
            result.add(normalized)
    return tuple(sorted(result))


def _project_id_from_url(value: str) -> str | None:
    path = urlsplit(value).path.rstrip("/")
    if not path:
        return None
    candidate = path.rsplit("/", 1)[-1].strip()
    return candidate or None


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "BuyerCanonicalProject",
    "BuyerMappedProject",
    "BuyerProjectMappingError",
    "BuyerProjectObservation",
    "BuyerProjectObservationContext",
    "BuyerProjectSnapshot",
    "MOBILE_PROJECT_SOURCE",
    "WEB_PROJECT_SOURCE",
    "map_buyer_project",
    "merge_canonical_projects",
    "normalize_buyer_project_text",
    "normalize_buyer_project_url",
]
