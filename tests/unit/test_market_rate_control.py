from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.platforms.kwork_supply.rate_control import (
    ConcurrencyAction,
    ConcurrencyPolicy,
    ConcurrencySignals,
    RateControlError,
    RateControlPolicy,
    RateLimitReason,
    RetryScope,
    TokenBucketPolicy,
    acquire_request,
    apply_retry_after,
    initial_rate_control_state,
    parse_retry_after,
    record_protection_response,
    recommend_worker_concurrency,
)


@pytest.fixture
def policy() -> RateControlPolicy:
    return RateControlPolicy(
        global_policy=TokenBucketPolicy(capacity=2, refill_per_second=1),
        default_source_policy=TokenBucketPolicy(capacity=1, refill_per_second=1),
        source_policies={"web_catalog": TokenBucketPolicy(capacity=1, refill_per_second="0.5")},
        fallback_retry_seconds=10,
        max_retry_after_seconds=60,
    )


def test_token_buckets_gate_global_and_per_source_without_partial_spend(policy: RateControlPolicy):
    state = initial_rate_control_state(policy, now=0)

    first_web = acquire_request(policy, state, source="web_catalog", now=0)
    blocked_web = acquire_request(policy, first_web.next_state, source="web_catalog", now=0)
    mobile = acquire_request(policy, blocked_web.next_state, source="mobile", now=0)
    blocked_mobile = acquire_request(policy, mobile.next_state, source="mobile", now=0)

    assert first_web.allowed is True
    assert blocked_web.allowed is False
    assert blocked_web.wait_seconds == Decimal("2")
    assert blocked_web.limited_by == (RateLimitReason.SOURCE_BUCKET,)
    assert blocked_web.next_state.global_bucket.tokens == Decimal("1")
    assert mobile.allowed is True
    assert blocked_mobile.allowed is False
    assert blocked_mobile.wait_seconds == Decimal("1")
    assert blocked_mobile.limited_by == (RateLimitReason.GLOBAL_BUCKET, RateLimitReason.SOURCE_BUCKET)

    web_after_refill = acquire_request(policy, blocked_mobile.next_state, source="web_catalog", now=2)
    assert web_after_refill.allowed is True
    assert web_after_refill.source_bucket_key == "web_catalog"


def test_retry_after_is_bounded_and_protection_never_rotates_transport(policy: RateControlPolicy):
    state = initial_rate_control_state(policy, now=0)
    limited = record_protection_response(
        policy,
        state,
        source="web_catalog",
        status_code=429,
        retry_after="120",
        now=0,
    )

    assert limited.delay_seconds == Decimal("60")
    assert limited.cooldown_until == Decimal("60")
    assert limited.source_quarantine_recommended is False
    assert limited.transport_rotation_requested is False
    before_retry = acquire_request(policy, limited.next_state, source="web_catalog", now=59)
    assert before_retry.allowed is False
    assert before_retry.wait_seconds == Decimal("1")
    assert before_retry.limited_by == (RateLimitReason.SOURCE_COOLDOWN,)
    assert acquire_request(policy, before_retry.next_state, source="web_catalog", now=60).allowed is True

    quarantined = record_protection_response(
        policy,
        state,
        source="web_catalog",
        status_code=403,
        now=0,
    )
    assert quarantined.delay_seconds == Decimal("10")
    assert quarantined.source_quarantine_recommended is True
    assert quarantined.transport_rotation_requested is False

    date_limited = record_protection_response(
        policy,
        state,
        source="web_catalog",
        status_code=429,
        retry_after="Sat, 11 Jul 2026 10:01:00 GMT",
        retry_after_now=datetime(2026, 7, 11, 10, 0, tzinfo=UTC),
        now=0,
    )
    assert date_limited.delay_seconds == Decimal("60.0")

    global_cooldown = apply_retry_after(
        policy,
        state,
        source="web_catalog",
        retry_after="5",
        now=0,
        scope=RetryScope.GLOBAL,
    )
    global_wait = acquire_request(policy, global_cooldown.next_state, source="another-source", now=2)
    assert global_wait.allowed is False
    assert global_wait.wait_seconds == Decimal("3")
    assert global_wait.limited_by == (RateLimitReason.GLOBAL_COOLDOWN,)


def test_retry_after_parser_uses_explicit_clock_and_never_exceeds_bound():
    now = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)

    assert parse_retry_after("Sat, 11 Jul 2026 10:01:00 GMT", now=now, max_seconds=120) == Decimal("60.0")
    assert parse_retry_after("-5", max_seconds=120) == Decimal("0")
    assert parse_retry_after("not-a-header", max_seconds=120) is None
    assert parse_retry_after("999", max_seconds=120) == Decimal("120")


def test_concurrency_recommendation_reacts_to_success_protection_timeout_novelty_and_capacity():
    policy = ConcurrencyPolicy(
        min_workers=1,
        max_workers=5,
        min_sample_size=10,
        min_success_rate="0.80",
        max_timeout_rate="0.20",
        min_novelty_rate="0.25",
    )

    stable = recommend_worker_concurrency(
        policy,
        current_workers=2,
        signals=ConcurrencySignals(success_count=12, novelty_rate="0.50", healthy_transport_count=5),
    )
    protected = recommend_worker_concurrency(
        policy,
        current_workers=3,
        signals=ConcurrencySignals(
            success_count=11,
            http_429_count=1,
            novelty_rate="0.50",
            healthy_transport_count=5,
        ),
    )
    timed_out = recommend_worker_concurrency(
        policy,
        current_workers=3,
        signals=ConcurrencySignals(
            success_count=8,
            timeout_count=2,
            novelty_rate="0.50",
            healthy_transport_count=5,
        ),
    )
    stale = recommend_worker_concurrency(
        policy,
        current_workers=3,
        signals=ConcurrencySignals(success_count=12, novelty_rate="0.10", healthy_transport_count=5),
    )
    capped = recommend_worker_concurrency(
        policy,
        current_workers=5,
        signals=ConcurrencySignals(success_count=12, novelty_rate="0.50", healthy_transport_count=2),
    )

    assert (stable.action, stable.recommended_workers, stable.reasons) == (
        ConcurrencyAction.INCREASE,
        3,
        ("stable_success",),
    )
    assert (protected.action, protected.recommended_workers, protected.reasons) == (
        ConcurrencyAction.DECREASE,
        2,
        ("protection_signal",),
    )
    assert (timed_out.action, timed_out.recommended_workers, timed_out.reasons) == (
        ConcurrencyAction.DECREASE,
        2,
        ("timeout_rate",),
    )
    assert (stale.action, stale.recommended_workers, stale.reasons) == (
        ConcurrencyAction.DECREASE,
        2,
        ("low_novelty",),
    )
    assert (capped.action, capped.recommended_workers, capped.reasons) == (
        ConcurrencyAction.DECREASE,
        2,
        ("healthy_transport_cap",),
    )
    assert all(result.transport_rotation_requested is False for result in (stable, protected, timed_out, stale, capped))


def test_policies_are_bounded_and_time_never_moves_backwards(policy: RateControlPolicy):
    with pytest.raises(RateControlError, match="max_workers"):
        ConcurrencyPolicy(max_workers=11)
    with pytest.raises(RateControlError, match="max_retry_after_seconds"):
        RateControlPolicy(
            global_policy=TokenBucketPolicy(1, 1),
            default_source_policy=TokenBucketPolicy(1, 1),
            max_retry_after_seconds=3601,
        )

    state = initial_rate_control_state(policy, now=2)
    with pytest.raises(RateControlError, match="cannot move backwards"):
        acquire_request(policy, state, source="web_catalog", now=1)
    with pytest.raises(RateControlError, match="cannot move backwards"):
        apply_retry_after(policy, state, source="web_catalog", retry_after="1", now=1)
