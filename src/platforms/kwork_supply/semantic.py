"""Local semantic clustering and evidence-first market group classification.

The module deliberately keeps model inference local. It accepts an injected
embedder for repeatable tests, while the production embedder lazily loads a
multilingual sentence-transformers model only when a completed enrichment job
needs semantic analysis.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import math
import os
import unicodedata
from typing import Any, Protocol


JsonDict = dict[str, Any]


class SemanticAnalysisError(RuntimeError):
    """Raised when a local embedding or clustering contract is invalid."""


class EmbeddingProvider(Protocol):
    """Minimal local embedding contract used by the clusterer."""

    @property
    def model_name(self) -> str: ...

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class SemanticClusterPolicy:
    similarity_threshold: float = 0.82
    fine_similarity_threshold: float = 0.89
    max_coarse_cluster_size: int = 80
    representative_limit: int = 12
    dossier_group_limit: int = 100
    fresh_review_days: int = 120

    def __post_init__(self) -> None:
        if not 0 < self.similarity_threshold <= 1:
            raise ValueError("similarity_threshold must be in (0, 1]")
        if not self.similarity_threshold <= self.fine_similarity_threshold <= 1:
            raise ValueError("fine_similarity_threshold must be >= similarity_threshold and <= 1")
        if self.max_coarse_cluster_size < 2:
            raise ValueError("max_coarse_cluster_size must be at least 2")
        if self.representative_limit < 1:
            raise ValueError("representative_limit must be positive")
        if self.dossier_group_limit < 1:
            raise ValueError("dossier_group_limit must be positive")
        if self.fresh_review_days < 1:
            raise ValueError("fresh_review_days must be positive")


class SentenceTransformerEmbedder:
    """Lazy local multilingual embedder backed by sentence-transformers."""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        local_files_only: bool | None = None,
    ) -> None:
        self._model_name = model_name or os.getenv(
            "KWORK_MARKET_EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        self._local_files_only = (
            local_files_only
            if local_files_only is not None
            else os.getenv("KWORK_MARKET_EMBEDDING_LOCAL_ONLY", "false").strip().lower() in {"1", "true", "yes"}
        )
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        model = self._load_model()
        try:
            vectors = model.encode(
                list(texts),
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:  # pragma: no cover - depends on local ML runtime.
            raise SemanticAnalysisError(f"local embedding inference failed: {type(exc).__name__}: {exc}") from exc
        return [[float(value) for value in vector] for vector in vectors]

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, local_files_only=self._local_files_only)
        except Exception as exc:  # pragma: no cover - depends on local model cache/network.
            scope = "local cache" if self._local_files_only else "configured model source"
            raise SemanticAnalysisError(
                f"could not load local embedding model {self._model_name!r} from {scope}: {type(exc).__name__}: {exc}"
            ) from exc
        return self._model


class LocalSemanticAnalyzer:
    """Cluster enriched listings and derive evidence-backed market states."""

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider | None = None,
        policy: SemanticClusterPolicy | None = None,
    ) -> None:
        self.embedder = embedder or SentenceTransformerEmbedder()
        self.policy = policy or SemanticClusterPolicy()

    @property
    def model_name(self) -> str:
        return self.embedder.model_name

    def analyze(
        self,
        listings: Iterable[Mapping[str, Any]],
        *,
        now: datetime | None = None,
        cached_embeddings: Mapping[str, Sequence[float]] | None = None,
    ) -> JsonDict:
        prepared = _prepare_listings(listings)
        if not prepared:
            return {
                "schema_version": 1,
                "embedding_model": self.embedder.model_name,
                "eligible_listing_count": 0,
                "cluster_count": 0,
                "clusters": [],
                "embeddings": [],
                "dossier": {"schema_version": 1, "groups": [], "coverage": {"eligible_listing_count": 0}},
            }

        cached = cached_embeddings or {}
        text_by_hash = {item["text_hash"]: item["semantic_text"] for item in prepared}
        missing_hashes = [text_hash for text_hash in text_by_hash if text_hash not in cached]
        generated = self.embedder.encode([text_by_hash[text_hash] for text_hash in missing_hashes]) if missing_hashes else []
        generated_vectors = _normalize_vectors(generated, expected_count=len(missing_hashes))
        vectors_by_hash: dict[str, Sequence[float]] = {
            text_hash: cached[text_hash]
            for text_hash in text_by_hash
            if text_hash in cached
        }
        vectors_by_hash.update(dict(zip(missing_hashes, generated_vectors, strict=True)))
        vectors = _normalize_vectors(
            [vectors_by_hash[item["text_hash"]] for item in prepared],
            expected_count=len(prepared),
        )
        components = _cluster_vectors(vectors, threshold=self.policy.similarity_threshold)
        fine_components: list[list[int]] = []
        for component in components:
            if len(component) > self.policy.max_coarse_cluster_size:
                sub_vectors = [vectors[index] for index in component]
                for sub_component in _cluster_vectors(sub_vectors, threshold=self.policy.fine_similarity_threshold):
                    fine_components.append([component[index] for index in sub_component])
            else:
                fine_components.append(component)

        effective_now = now or datetime.now(UTC)
        clusters: list[JsonDict] = []
        for member_indexes in fine_components:
            members = [prepared[index] for index in member_indexes]
            clusters.append(_cluster_summary(members, policy=self.policy, now=effective_now))
        clusters.sort(key=lambda cluster: (-int(cluster["metrics"]["demand_proof_score"]), cluster["cluster_id"]))
        dossier = _build_dossier(
            clusters,
            eligible_listing_count=len(prepared),
            group_limit=self.policy.dossier_group_limit,
        )
        return {
            "schema_version": 1,
            "embedding_model": self.embedder.model_name,
            "eligible_listing_count": len(prepared),
            "cluster_count": len(clusters),
            "clusters": clusters,
            "embeddings": [
                {
                    "listing_id": item["listing_id"],
                    "text_hash": item["text_hash"],
                    "vector": vectors[index],
                }
                for index, item in enumerate(prepared)
            ],
            "dossier": dossier,
        }


def _prepare_listings(listings: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    prepared: list[JsonDict] = []
    identities: set[int] = set()
    for raw in listings:
        if not isinstance(raw, Mapping):
            raise SemanticAnalysisError("listings must contain mappings")
        listing_id = _as_int(raw.get("listing_id"))
        if listing_id is None or listing_id <= 0:
            raise SemanticAnalysisError("each listing requires a positive listing_id")
        if listing_id in identities:
            raise SemanticAnalysisError(f"duplicate listing_id {listing_id}")
        identities.add(listing_id)
        price = _as_number(raw.get("price"))
        if price is not None and price > 15_000:
            continue
        title = _text(raw.get("title"))
        description = _text(raw.get("description"))
        semantic_text = _semantic_text(title, description)
        if not semantic_text:
            semantic_text = "untitled service"
        canonical = raw.get("canonical") if isinstance(raw.get("canonical"), Mapping) else {}
        prepared.append(
            {
                "listing_id": listing_id,
                "listing_key": _text(raw.get("listing_key")) or str(listing_id),
                "title": title or "Untitled service",
                "description": description,
                "instructions": _text(raw.get("instructions")),
                "price": price,
                "seller_key": _text(raw.get("seller_key")),
                "seller": dict(raw.get("seller") or {}) if isinstance(raw.get("seller"), Mapping) else {},
                "reviews": [dict(review) for review in raw.get("reviews", []) if isinstance(review, Mapping)],
                "listing_reviews_count": _as_int(raw.get("listing_reviews_count")) or 0,
                "good_reviews": _as_int(raw.get("good_reviews")) or 0,
                "bad_reviews": _as_int(raw.get("bad_reviews")) or 0,
                "queue_count": _as_int(raw.get("queue_count")) or 0,
                "url": _text(raw.get("url") or canonical.get("url") or canonical.get("share_url")),
                "semantic_text": semantic_text,
                "semantic_tokens": _tokens(semantic_text),
                "text_hash": hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
                "data_quality": _text(raw.get("data_quality")) or _text(raw.get("status")) or "unknown",
            }
        )
    prepared.sort(key=lambda item: int(item["listing_id"]))
    return prepared


def _semantic_text(title: str | None, description: str | None) -> str:
    parts = [part for part in (_text(title), _text(description)) if part]
    return " ".join(parts)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return " ".join(text.split()) or None


def _tokens(text: str) -> list[str]:
    token: list[str] = []
    tokens: list[str] = []
    for character in text.casefold():
        if character.isalnum() or character in {"+", "#"}:
            token.append(character)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return tokens


def _normalize_vectors(vectors: Sequence[Sequence[float]], *, expected_count: int) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise SemanticAnalysisError("embedder returned a vector count different from input text count")
    normalized: list[list[float]] = []
    dimension: int | None = None
    for vector in vectors:
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise SemanticAnalysisError("embedder returned a non-finite or empty vector")
        if dimension is None:
            dimension = len(values)
        elif len(values) != dimension:
            raise SemanticAnalysisError("embedder returned inconsistent vector dimensions")
        magnitude = math.sqrt(sum(value * value for value in values))
        if magnitude == 0:
            raise SemanticAnalysisError("embedder returned a zero vector")
        normalized.append([value / magnitude for value in values])
    return normalized


def _cluster_vectors(vectors: Sequence[Sequence[float]], *, threshold: float) -> list[list[int]]:
    size = len(vectors)
    if size == 0:
        return []
    parents = list(range(size))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    try:
        import numpy as np

        matrix = np.asarray(vectors, dtype="float32")
        block_size = 256
        for start in range(0, size, block_size):
            scores = matrix[start : start + block_size] @ matrix.T
            for local_index, row in enumerate(scores):
                index = start + local_index
                neighbors = np.flatnonzero(row >= threshold)
                for neighbor in neighbors:
                    neighbor_index = int(neighbor)
                    if neighbor_index > index:
                        union(index, neighbor_index)
    except ImportError:  # pragma: no cover - sentence-transformers brings numpy in production.
        for left in range(size):
            for right in range(left + 1, size):
                if _dot(vectors[left], vectors[right]) >= threshold:
                    union(left, right)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(size):
        groups[find(index)].append(index)
    return sorted((sorted(group) for group in groups.values()), key=lambda group: (group[0], len(group)))


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cluster_summary(members: Sequence[JsonDict], *, policy: SemanticClusterPolicy, now: datetime) -> JsonDict:
    listing_ids = sorted(int(member["listing_id"]) for member in members)
    cluster_hash = hashlib.sha256(",".join(str(identifier) for identifier in listing_ids).encode("ascii")).hexdigest()[:16]
    cluster_id = f"cluster_{cluster_hash}"
    prices = sorted(member["price"] for member in members if isinstance(member["price"], float))
    sellers = {member["seller_key"] for member in members if member["seller_key"]}
    review_counts = [int(member["listing_reviews_count"]) for member in members]
    recent_reviews = sum(
        1
        for member in members
        for review in member["reviews"]
        if _is_recent(review.get("time_added"), now=now, days=policy.fresh_review_days)
    )
    total_reviews = sum(review_counts)
    queue_total = sum(int(member["queue_count"]) for member in members)
    low_evidence_count = sum(1 for member in members if len(member["reviews"]) < 3)
    seller_rating_counts = [
        _as_int(member["seller"].get("seller_rating_count")) or 0
        for member in members
        if member["seller"]
    ]
    completed_orders = [
        _as_int(member["seller"].get("completed_orders_count")) or 0
        for member in members
        if member["seller"]
    ]
    demand_score = min(total_reviews * 2, 40) + min(recent_reviews * 12, 36) + min(queue_total * 4, 16)
    barrier_score = min(_median(seller_rating_counts) // 5, 45) + min(_median(completed_orders) // 4, 35)
    competition_score = min(len(members) * 4, 55) + min(len(sellers) * 3, 30)
    state, reasons = _classify_group(
        member_count=len(members),
        seller_count=len(sellers),
        demand_score=demand_score,
        barrier_score=barrier_score,
        competition_score=competition_score,
        recent_reviews=recent_reviews,
        low_evidence_count=low_evidence_count,
    )
    label = _cluster_label(members)
    price_summary = {
        "count": len(prices),
        "median": _median(prices) if prices else None,
        "p25": _quantile(prices, 0.25) if prices else None,
        "p75": _quantile(prices, 0.75) if prices else None,
        "min": prices[0] if prices else None,
        "max": prices[-1] if prices else None,
    }
    representatives = _representatives(members, prices, limit=policy.representative_limit)
    # Terra receives only the bounded records it can inspect. The complete
    # member set stays in the durable cluster-members table for local analysis.
    evidence_ids = [representative["evidence_id"] for representative in representatives]
    confidence = min(
        100,
        20 + min(len(members) * 8, 30) + min(recent_reviews * 10, 30) + min((len(members) - low_evidence_count) * 5, 20),
    )
    metrics = {
        "member_count": len(members),
        "unique_seller_count": len(sellers),
        "listing_reviews_total": total_reviews,
        "fresh_review_count": recent_reviews,
        "queue_total": queue_total,
        "low_evidence_listing_count": low_evidence_count,
        "demand_proof_score": demand_score,
        "entry_barrier_score": barrier_score,
        "competition_score": competition_score,
        "price": price_summary,
    }
    return {
        "cluster_id": cluster_id,
        "label": label,
        "state": state,
        "confidence": confidence,
        "reasons": reasons,
        "evidence_ids": evidence_ids,
        "metrics": metrics,
        "representatives": representatives,
        "member_listing_ids": listing_ids,
    }


def _classify_group(
    *,
    member_count: int,
    seller_count: int,
    demand_score: int,
    barrier_score: int,
    competition_score: int,
    recent_reviews: int,
    low_evidence_count: int,
) -> tuple[str, list[str]]:
    if member_count == 0 or (recent_reviews == 0 and low_evidence_count >= member_count):
        return "do_not_take", ["insufficient_recent_listing_level_evidence"]
    if demand_score >= 48 and (competition_score >= 55 or barrier_score >= 55):
        return "popular_crowded", ["demand_is_proven", "competition_or_entry_barrier_is_high"]
    if demand_score >= 30 and competition_score <= 62 and barrier_score <= 60 and seller_count >= 2:
        return "promising_for_entry", ["fresh_demand_signal", "activity_not_limited_to_one_seller", "entry_barrier_is_moderate"]
    return "do_not_take", ["demand_or_accessibility_threshold_not_met"]


def _cluster_label(members: Sequence[JsonDict]) -> str:
    generic = {
        "сделаю", "сделать", "разработаю", "разработка", "услуга", "работа", "качественно", "быстро",
        "под", "ключ", "для", "и", "на", "the", "a", "an", "service", "development",
    }
    counts: Counter[str] = Counter()
    for member in members:
        counts.update(token for token in member["semantic_tokens"] if len(token) > 2 and token not in generic)
    words = [word for word, _count in counts.most_common(5)]
    return " ".join(words) if words else "semantic service group"


def _representatives(members: Sequence[JsonDict], prices: Sequence[float], *, limit: int) -> list[JsonDict]:
    median_price = _median(prices) if prices else None
    sorted_members = sorted(
        members,
        key=lambda member: (
            -int(member["listing_reviews_count"]),
            -len(member["reviews"]),
            int(member["listing_id"]),
        ),
    )
    representatives: list[JsonDict] = []
    seen: set[int] = set()
    for member in sorted_members:
        listing_id = int(member["listing_id"])
        if listing_id in seen:
            continue
        price = member["price"]
        price_outlier = bool(
            median_price is not None and isinstance(price, float) and abs(price - median_price) >= median_price * 0.5
        )
        representatives.append(
            {
                "evidence_id": f"listing_{listing_id}",
                "listing_id": listing_id,
                "title": member["title"],
                "price": price,
                "url": member["url"],
                "seller_key": member["seller_key"],
                "listing_reviews_count": member["listing_reviews_count"],
                "last_reviews": member["reviews"][:3],
                "description": member["description"],
                "price_outlier": price_outlier,
            }
        )
        seen.add(listing_id)
        if len(representatives) >= limit:
            break
    return representatives


def _build_dossier(clusters: Sequence[JsonDict], *, eligible_listing_count: int, group_limit: int) -> JsonDict:
    state_rank = {"promising_for_entry": 0, "popular_crowded": 1, "do_not_take": 2}
    selected = sorted(
        clusters,
        key=lambda cluster: (
            state_rank.get(str(cluster["state"]), 9),
            -int(cluster["confidence"]),
            cluster["cluster_id"],
        ),
    )[:group_limit]
    groups = [
        {
            "cluster_id": cluster["cluster_id"],
            "label": cluster["label"],
            "verdict_input": cluster["state"],
            "confidence": cluster["confidence"],
            "reasons": cluster["reasons"],
            "evidence_ids": cluster["evidence_ids"],
            "metrics": cluster["metrics"],
            "representatives": cluster["representatives"],
        }
        for cluster in selected
    ]
    return {
        "schema_version": 1,
        "coverage": {
            "eligible_listing_count": eligible_listing_count,
            "cluster_count": len(clusters),
            "included_group_count": len(groups),
            "omitted_group_count": max(len(clusters) - len(groups), 0),
            "model_input_scope": "semantic groups with bounded representatives, never the raw market listing set",
        },
        "groups": groups,
        "required_response_schema": {
            "verdict": "promising_for_entry|popular_crowded|do_not_take",
            "confidence": "integer 0..100",
            "reasons": "array of evidence-backed strings",
            "evidence_ids": "array of listing evidence identifiers",
            "recommended_offer": "object or null",
            "rejected_cases": "array",
        },
    }


def _is_recent(value: object, *, now: datetime, days: int) -> bool:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return False
    return 0 <= (now - timestamp).total_seconds() <= days * 86_400


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = _text(value)
    if text is None:
        return None
    if text.isdecimal():
        return _parse_timestamp(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _as_int(value: object) -> int | None:
    number = _as_number(value)
    return int(number) if number is not None else None


def _as_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: Sequence[int | float]) -> int | float:
    if not values:
        return 0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("values cannot be empty")
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


__all__ = [
    "EmbeddingProvider",
    "LocalSemanticAnalyzer",
    "SemanticAnalysisError",
    "SemanticClusterPolicy",
    "SentenceTransformerEmbedder",
]
