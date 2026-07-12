from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from src.platforms.kwork_supply.semantic import LocalSemanticAnalyzer, SemanticClusterPolicy


class _DeterministicEmbedder:
    model_name = "test-local-embedder"

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.casefold()
            if "telegram" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            elif "wordpress" in normalized:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class _CountingEmbedder(_DeterministicEmbedder):
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.batch_sizes.append(len(texts))
        return super().encode(texts)


def _review(index: int) -> dict[str, object]:
    return {
        "review_key": f"review_{index}",
        "time_added": "2026-07-01T12:00:00Z",
        "is_good": True,
        "is_bad": False,
        "text": "Recent completed order",
    }


def _listing(
    listing_id: int,
    title: str,
    *,
    seller: str,
    price: int,
    review_count: int = 0,
    reviews: list[dict[str, object]] | None = None,
    rating_count: int = 0,
    completed_orders: int = 0,
) -> dict[str, object]:
    return {
        "listing_id": listing_id,
        "listing_key": str(listing_id),
        "title": title,
        "description": f"Detailed description for {title}",
        "seller_key": seller,
        "price": price,
        "listing_reviews_count": review_count,
        "queue_count": 1 if review_count else 0,
        "reviews": reviews or [],
        "seller": {
            "seller_rating_count": rating_count,
            "completed_orders_count": completed_orders,
        },
        "url": f"https://kwork.ru/{listing_id}",
    }


def test_local_semantic_analysis_clusters_fine_groups_and_builds_bounded_terra_dossier():
    analyzer = LocalSemanticAnalyzer(
        embedder=_DeterministicEmbedder(),
        policy=SemanticClusterPolicy(similarity_threshold=0.8, fine_similarity_threshold=0.9),
    )
    result = analyzer.analyze(
        [
            _listing(1, "Telegram bot for leads", seller="alice", price=4900, review_count=8, reviews=[_review(1)] * 3),
            _listing(2, "Telegram bot for sales", seller="bob", price=5900, review_count=7, reviews=[_review(2)] * 3),
            _listing(
                3,
                "WordPress landing page",
                seller="carol",
                price=7000,
                review_count=30,
                reviews=[_review(3)] * 3,
                rating_count=500,
                completed_orders=300,
            ),
            _listing(
                4,
                "WordPress website development",
                seller="dave",
                price=7500,
                review_count=25,
                reviews=[_review(4)] * 3,
                rating_count=450,
                completed_orders=280,
            ),
            _listing(5, "Design a logo", seller="eve", price=3000),
            _listing(6, "Telegram premium bot", seller="skip", price=15001, review_count=999, reviews=[_review(6)] * 3),
        ],
        now=datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert result["embedding_model"] == "test-local-embedder"
    assert result["eligible_listing_count"] == 5
    assert len(result["embeddings"]) == 5
    by_state = {cluster["state"] for cluster in result["clusters"]}
    assert by_state == {"promising_for_entry", "popular_crowded", "do_not_take"}
    telegram = next(cluster for cluster in result["clusters"] if set(cluster["member_listing_ids"]) == {1, 2})
    wordpress = next(cluster for cluster in result["clusters"] if set(cluster["member_listing_ids"]) == {3, 4})
    assert telegram["state"] == "promising_for_entry"
    assert wordpress["state"] == "popular_crowded"
    assert all("listing_6" not in cluster["evidence_ids"] for cluster in result["clusters"])
    dossier = result["dossier"]
    assert dossier["coverage"]["model_input_scope"].startswith("semantic groups")
    assert dossier["required_response_schema"]["verdict"].startswith("promising_for_entry")
    assert all(len(group["representatives"]) <= 12 for group in dossier["groups"])


def test_local_semantic_analysis_reuses_vectors_by_exact_text_hash():
    embedder = _CountingEmbedder()
    analyzer = LocalSemanticAnalyzer(embedder=embedder)
    listings = [
        _listing(1, "Telegram bot for leads", seller="alice", price=4900),
        _listing(2, "WordPress landing page", seller="bob", price=5900),
    ]

    first = analyzer.analyze(listings)
    cached = {item["text_hash"]: item["vector"] for item in first["embeddings"]}
    second = analyzer.analyze(listings, cached_embeddings=cached)

    assert embedder.batch_sizes == [2]
    assert second["embeddings"] == first["embeddings"]


def test_terra_dossier_never_exposes_all_cluster_member_ids_as_evidence():
    analyzer = LocalSemanticAnalyzer(
        embedder=_DeterministicEmbedder(),
        policy=SemanticClusterPolicy(representative_limit=3),
    )
    result = analyzer.analyze(
        [
            _listing(index, f"Telegram bot variant {index}", seller=f"seller_{index}", price=4900)
            for index in range(1, 15)
        ],
        now=datetime(2026, 7, 12, tzinfo=UTC),
    )

    cluster = result["clusters"][0]
    group = result["dossier"]["groups"][0]
    assert len(cluster["member_listing_ids"]) == 14
    assert len(cluster["evidence_ids"]) == 3
    assert group["evidence_ids"] == cluster["evidence_ids"]
    assert len(group["representatives"]) == 3
