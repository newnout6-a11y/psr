"""Pure deterministic rate and concurrency control for market workers.

The module does not own a clock, make network calls, or control transports.
Callers provide a monotonic ``now`` value and persist the returned immutable
state wherever their runtime keeps durable worker state. Protection feedback
can request a source quarantine, but it never rotates a transport implicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias


DecimalLike: TypeAlias = Decimal | int | float | str

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MAX_BUCKET_CAPACITY = Decimal("10000")
_MAX_REFILL_PER_SECOND = Decimal("10000")
_MAX_RETRY_SECONDS = Decimal("3600")
_MAX_SOURCE_POLICIES = 64
_MAX_WORKERS = 10
_DEFAULT_SOURCE_BUCKET_KEY = "__default_source__"


class RateControlError(ValueError):
    """Raised when an input violates the deterministic rate-control contract."""


class RateLimitReason(StrEnum):
    """Reason a token request was held without consuming either bucket."""

    GLOBAL_COOLDOWN = "global_cooldown"
    SOURCE_COOLDOWN = "source_cooldown"
    GLOBAL_BUCKET = "global_bucket"
    SOURCE_BUCKET = "source_bucket"


class RetryScope(StrEnum):
    """Where a server-provided Retry-After cooldown applies."""

    GLOBAL = "global"
    SOURCE = "source"


class ConcurrencyAction(StrEnum):
    """The bounded worker-count action recommended for one observation window."""

    INCREASE = "increase"
    DECREASE = "decrease"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class TokenBucketPolicy:
    """Capacity and refill rate for one deterministic token bucket."""

    capacity: DecimalLike
    refill_per_second: DecimalLike

    def __post_init__(self) -> None:
        capacity = _positive_decimal(self.capacity, field="capacity")
        refill = _positive_decimal(self.refill_per_second, field="refill_per_second")
        if capacity > _MAX_BUCKET_CAPACITY:
            raise RateControlError(f"capacity must not exceed {_MAX_BUCKET_CAPACITY}")
        if refill > _MAX_REFILL_PER_SECOND:
            raise RateControlError(f"refill_per_second must not exceed {_MAX_REFILL_PER_SECOND}")
        object.__setattr__(self, "capacity", capacity)
        object.__setattr__(self, "refill_per_second", refill)


@dataclass(frozen=True, slots=True)
class RateControlPolicy:
    """Bounded global and source-class token-bucket configuration.

    Source policies are fixed at construction. Unknown source names share the
    default source bucket, which avoids unbounded in-memory state caused by
    arbitrary source labels.
    """

    global_policy: TokenBucketPolicy
    default_source_policy: TokenBucketPolicy
    source_policies: Mapping[str, TokenBucketPolicy] = field(default_factory=dict)
    fallback_retry_seconds: DecimalLike = Decimal("15")
    max_retry_after_seconds: DecimalLike = Decimal("300")

    def __post_init__(self) -> None:
        if not isinstance(self.global_policy, TokenBucketPolicy):
            raise TypeError("global_policy must be a TokenBucketPolicy")
        if not isinstance(self.default_source_policy, TokenBucketPolicy):
            raise TypeError("default_source_policy must be a TokenBucketPolicy")
        fallback = _nonnegative_decimal(self.fallback_retry_seconds, field="fallback_retry_seconds")
        maximum = _positive_decimal(self.max_retry_after_seconds, field="max_retry_after_seconds")
        if maximum > _MAX_RETRY_SECONDS:
            raise RateControlError(f"max_retry_after_seconds must not exceed {_MAX_RETRY_SECONDS}")
        if fallback > maximum:
            raise RateControlError("fallback_retry_seconds cannot exceed max_retry_after_seconds")
        if not isinstance(self.source_policies, Mapping):
            raise TypeError("source_policies must be a mapping")
        if len(self.source_policies) > _MAX_SOURCE_POLICIES:
            raise RateControlError(f"source_policies must not exceed {_MAX_SOURCE_POLICIES}")
        normalized: dict[str, TokenBucketPolicy] = {}
        for source, policy in self.source_policies.items():
            name = _source_name(source)
            if name == _DEFAULT_SOURCE_BUCKET_KEY:
                raise RateControlError(f"{_DEFAULT_SOURCE_BUCKET_KEY!r} is reserved")
            if not isinstance(policy, TokenBucketPolicy):
                raise TypeError("source policy values must be TokenBucketPolicy instances")
            normalized[name] = policy
        object.__setattr__(self, "source_policies", MappingProxyType(normalized))
        object.__setattr__(self, "fallback_retry_seconds", fallback)
        object.__setattr__(self, "max_retry_after_seconds", maximum)


@dataclass(frozen=True, slots=True)
class TokenBucketState:
    """Current token count at an explicit monotonic timestamp."""

    tokens: Decimal
    updated_at: Decimal


@dataclass(frozen=True, slots=True)
class RateControlState:
    """Immutable state for atomic global-plus-source token acquisition."""

    global_bucket: TokenBucketState
    source_buckets: Mapping[str, TokenBucketState]
    global_cooldown_until: Decimal = _ZERO
    source_cooldown_until: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.global_bucket, TokenBucketState):
            raise TypeError("global_bucket must be a TokenBucketState")
        if not isinstance(self.source_buckets, Mapping):
            raise TypeError("source_buckets must be a mapping")
        if not isinstance(self.source_cooldown_until, Mapping):
            raise TypeError("source_cooldown_until must be a mapping")
        object.__setattr__(self, "global_cooldown_until", _nonnegative_decimal(self.global_cooldown_until, field="global_cooldown_until"))
        object.__setattr__(
            self,
            "source_buckets",
            MappingProxyType({str(key): value for key, value in self.source_buckets.items()}),
        )
        object.__setattr__(
            self,
            "source_cooldown_until",
            MappingProxyType(
                {
                    str(key): _nonnegative_decimal(value, field="source_cooldown_until")
                    for key, value in self.source_cooldown_until.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class RateAcquireResult:
    """Outcome and next immutable state of one attempted request acquisition."""

    allowed: bool
    wait_seconds: Decimal
    limited_by: tuple[RateLimitReason, ...]
    source_bucket_key: str
    next_state: RateControlState


@dataclass(frozen=True, slots=True)
class RetryAfterResult:
    """A bounded server cooldown without any transport-side action."""

    delay_seconds: Decimal
    cooldown_until: Decimal
    scope: RetryScope
    source_bucket_key: str
    next_state: RateControlState
    source_quarantine_recommended: bool = False
    transport_rotation_requested: bool = False


@dataclass(frozen=True, slots=True)
class ConcurrencyPolicy:
    """Bounded deterministic worker-concurrency policy for one signal window."""

    min_workers: int = 1
    max_workers: int = _MAX_WORKERS
    scale_up_step: int = 1
    scale_down_step: int = 1
    min_sample_size: int = 10
    min_success_rate: DecimalLike = Decimal("0.90")
    max_timeout_rate: DecimalLike = Decimal("0.10")
    min_novelty_rate: DecimalLike = Decimal("0.10")

    def __post_init__(self) -> None:
        for field_name in ("min_workers", "max_workers", "scale_up_step", "scale_down_step", "min_sample_size"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RateControlError(f"{field_name} must be an integer")
        if self.min_workers < 0:
            raise RateControlError("min_workers cannot be negative")
        if self.max_workers < self.min_workers or self.max_workers > _MAX_WORKERS:
            raise RateControlError(f"max_workers must be between min_workers and {_MAX_WORKERS}")
        if self.scale_up_step <= 0 or self.scale_down_step <= 0 or self.min_sample_size <= 0:
            raise RateControlError("scale steps and min_sample_size must be positive")
        for field_name in ("min_success_rate", "max_timeout_rate", "min_novelty_rate"):
            value = _unit_interval_decimal(getattr(self, field_name), field=field_name)
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class ConcurrencySignals:
    """Deterministic totals collected from a completed observation window."""

    success_count: int
    http_403_count: int = 0
    http_429_count: int = 0
    timeout_count: int = 0
    novelty_rate: DecimalLike | None = None
    healthy_transport_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("success_count", "http_403_count", "http_429_count", "timeout_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RateControlError(f"{field_name} must be a non-negative integer")
        if self.novelty_rate is not None:
            object.__setattr__(self, "novelty_rate", _unit_interval_decimal(self.novelty_rate, field="novelty_rate"))
        if self.healthy_transport_count is not None and (
            isinstance(self.healthy_transport_count, bool)
            or not isinstance(self.healthy_transport_count, int)
            or self.healthy_transport_count < 0
        ):
            raise RateControlError("healthy_transport_count must be a non-negative integer or None")

    @property
    def attempt_count(self) -> int:
        """Return total classified attempts in the window."""

        return self.success_count + self.http_403_count + self.http_429_count + self.timeout_count


@dataclass(frozen=True, slots=True)
class ConcurrencyRecommendation:
    """One bounded change recommendation; it never rotates transports."""

    recommended_workers: int
    action: ConcurrencyAction
    reasons: tuple[str, ...]
    success_rate: Decimal | None
    timeout_rate: Decimal | None
    effective_max_workers: int
    transport_rotation_requested: bool = False


def initial_rate_control_state(policy: RateControlPolicy, *, now: DecimalLike = 0) -> RateControlState:
    """Create a full-capacity immutable state at an explicit timestamp."""

    _require_policy(policy)
    timestamp = _time_value(now)
    source_buckets = {
        _DEFAULT_SOURCE_BUCKET_KEY: _full_bucket(policy.default_source_policy, timestamp),
        **{source: _full_bucket(source_policy, timestamp) for source, source_policy in policy.source_policies.items()},
    }
    return RateControlState(
        global_bucket=_full_bucket(policy.global_policy, timestamp),
        source_buckets=source_buckets,
    )


def acquire_request(
    policy: RateControlPolicy,
    state: RateControlState,
    *,
    source: str,
    now: DecimalLike,
    cost: DecimalLike = 1,
) -> RateAcquireResult:
    """Acquire global and source tokens atomically, or return one exact wait.

    If either bucket is limited, neither bucket spends a token. The returned
    state is still advanced to ``now`` so repeated decisions with the same
    input remain deterministic.
    """

    _require_policy(policy)
    _require_state(state)
    timestamp = _time_value(now)
    _ensure_state_time(state, timestamp)
    request_cost = _positive_decimal(cost, field="cost")
    source_key = _source_bucket_key(policy, source)
    global_bucket = _refill_bucket(state.global_bucket, policy.global_policy, timestamp)
    source_policy = _source_policy(policy, source_key)
    source_bucket = _refill_bucket(_source_bucket(state, source_key, source_policy, timestamp), source_policy, timestamp)
    if request_cost > policy.global_policy.capacity or request_cost > source_policy.capacity:
        raise RateControlError("cost cannot exceed either bucket capacity")

    reasons: list[RateLimitReason] = []
    waits: list[Decimal] = []
    global_cooldown_wait = _remaining_wait(state.global_cooldown_until, timestamp)
    if global_cooldown_wait > _ZERO:
        reasons.append(RateLimitReason.GLOBAL_COOLDOWN)
        waits.append(global_cooldown_wait)
    source_cooldown_wait = _remaining_wait(state.source_cooldown_until.get(source_key, _ZERO), timestamp)
    if source_cooldown_wait > _ZERO:
        reasons.append(RateLimitReason.SOURCE_COOLDOWN)
        waits.append(source_cooldown_wait)
    global_bucket_wait = _bucket_wait(global_bucket, request_cost, policy.global_policy)
    if global_bucket_wait > _ZERO:
        reasons.append(RateLimitReason.GLOBAL_BUCKET)
        waits.append(global_bucket_wait)
    source_bucket_wait = _bucket_wait(source_bucket, request_cost, source_policy)
    if source_bucket_wait > _ZERO:
        reasons.append(RateLimitReason.SOURCE_BUCKET)
        waits.append(source_bucket_wait)

    source_buckets = dict(state.source_buckets)
    source_buckets[source_key] = source_bucket
    next_state = RateControlState(
        global_bucket=global_bucket,
        source_buckets=source_buckets,
        global_cooldown_until=state.global_cooldown_until,
        source_cooldown_until=state.source_cooldown_until,
    )
    if waits:
        return RateAcquireResult(
            allowed=False,
            wait_seconds=max(waits),
            limited_by=tuple(reasons),
            source_bucket_key=source_key,
            next_state=next_state,
        )

    source_buckets[source_key] = TokenBucketState(tokens=source_bucket.tokens - request_cost, updated_at=timestamp)
    return RateAcquireResult(
        allowed=True,
        wait_seconds=_ZERO,
        limited_by=(),
        source_bucket_key=source_key,
        next_state=RateControlState(
            global_bucket=TokenBucketState(tokens=global_bucket.tokens - request_cost, updated_at=timestamp),
            source_buckets=source_buckets,
            global_cooldown_until=state.global_cooldown_until,
            source_cooldown_until=state.source_cooldown_until,
        ),
    )


def parse_retry_after(
    value: object,
    *,
    now: datetime | None = None,
    max_seconds: DecimalLike = _MAX_RETRY_SECONDS,
) -> Decimal | None:
    """Parse a Retry-After delta or HTTP-date without consulting the clock.

    Invalid values produce ``None`` so callers can use their explicit bounded
    fallback. HTTP-date parsing requires a caller-supplied ``now`` timestamp.
    """

    maximum = _positive_decimal(max_seconds, field="max_seconds")
    if maximum > _MAX_RETRY_SECONDS:
        raise RateControlError(f"max_seconds must not exceed {_MAX_RETRY_SECONDS}")
    if value is None or isinstance(value, bool):
        return None
    numeric = _try_nonnegative_decimal(value)
    if numeric is not None:
        return min(numeric, maximum)
    if not isinstance(value, str):
        return None
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if target is None or now is None:
        return None
    target = _utc_datetime(target)
    reference = _utc_datetime(now)
    seconds = Decimal(str((target - reference).total_seconds()))
    return min(max(seconds, _ZERO), maximum)


def apply_retry_after(
    policy: RateControlPolicy,
    state: RateControlState,
    *,
    source: str,
    retry_after: object,
    now: DecimalLike,
    scope: RetryScope | str = RetryScope.SOURCE,
    retry_after_now: datetime | None = None,
) -> RetryAfterResult:
    """Apply one bounded Retry-After cooldown without touching transports."""

    _require_policy(policy)
    _require_state(state)
    timestamp = _time_value(now)
    _ensure_state_time(state, timestamp)
    source_key = _source_bucket_key(policy, source)
    retry_scope = RetryScope(scope)
    delay = parse_retry_after(
        retry_after,
        now=retry_after_now,
        max_seconds=policy.max_retry_after_seconds,
    )
    if delay is None:
        delay = policy.fallback_retry_seconds
    return _apply_cooldown(policy, state, source_key=source_key, delay=delay, now=timestamp, scope=retry_scope)


def record_protection_response(
    policy: RateControlPolicy,
    state: RateControlState,
    *,
    source: str,
    status_code: int,
    now: DecimalLike,
    retry_after: object = None,
    retry_after_now: datetime | None = None,
) -> RetryAfterResult:
    """Record a 403/429 cooldown and expose only a quarantine recommendation.

    A 403 recommends source/transport quarantine to the caller. It deliberately
    does not rotate anything: a supervisor may decide on drain and rotation in
    a separate, explicit workflow.
    """

    if status_code not in {403, 429}:
        raise RateControlError("status_code must be 403 or 429")
    result = apply_retry_after(
        policy,
        state,
        source=source,
        retry_after=retry_after,
        now=now,
        scope=RetryScope.SOURCE,
        retry_after_now=retry_after_now,
    )
    return RetryAfterResult(
        delay_seconds=result.delay_seconds,
        cooldown_until=result.cooldown_until,
        scope=result.scope,
        source_bucket_key=result.source_bucket_key,
        next_state=result.next_state,
        source_quarantine_recommended=status_code == 403,
        transport_rotation_requested=False,
    )


def recommend_worker_concurrency(
    policy: ConcurrencyPolicy,
    *,
    current_workers: int,
    signals: ConcurrencySignals,
) -> ConcurrencyRecommendation:
    """Recommend a bounded worker count from a completed signal window."""

    if not isinstance(policy, ConcurrencyPolicy):
        raise TypeError("policy must be a ConcurrencyPolicy")
    if not isinstance(signals, ConcurrencySignals):
        raise TypeError("signals must be a ConcurrencySignals")
    if isinstance(current_workers, bool) or not isinstance(current_workers, int) or current_workers < 0:
        raise RateControlError("current_workers must be a non-negative integer")

    effective_max = policy.max_workers
    if signals.healthy_transport_count is not None:
        effective_max = min(effective_max, signals.healthy_transport_count)
    effective_min = min(policy.min_workers, effective_max)
    bounded_current = min(max(current_workers, effective_min), effective_max)
    attempts = signals.attempt_count
    success_rate = _ratio(signals.success_count, attempts)
    timeout_rate = _ratio(signals.timeout_count, attempts)

    if current_workers > effective_max:
        return _concurrency_result(
            target=effective_max,
            current=current_workers,
            reasons=("healthy_transport_cap",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    if signals.http_403_count or signals.http_429_count:
        return _concurrency_result(
            target=max(effective_min, bounded_current - policy.scale_down_step),
            current=bounded_current,
            reasons=("protection_signal",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    if attempts < policy.min_sample_size:
        return _concurrency_result(
            target=bounded_current,
            current=bounded_current,
            reasons=("insufficient_sample",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    assert timeout_rate is not None
    if timeout_rate >= policy.max_timeout_rate:
        return _concurrency_result(
            target=max(effective_min, bounded_current - policy.scale_down_step),
            current=bounded_current,
            reasons=("timeout_rate",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    if signals.novelty_rate is None:
        return _concurrency_result(
            target=bounded_current,
            current=bounded_current,
            reasons=("novelty_unavailable",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    if signals.novelty_rate < policy.min_novelty_rate:
        return _concurrency_result(
            target=max(effective_min, bounded_current - policy.scale_down_step),
            current=bounded_current,
            reasons=("low_novelty",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    assert success_rate is not None
    if success_rate >= policy.min_success_rate and bounded_current < effective_max:
        return _concurrency_result(
            target=min(effective_max, bounded_current + policy.scale_up_step),
            current=bounded_current,
            reasons=("stable_success",),
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            effective_max=effective_max,
        )
    return _concurrency_result(
        target=bounded_current,
        current=bounded_current,
        reasons=("stable_window",),
        success_rate=success_rate,
        timeout_rate=timeout_rate,
        effective_max=effective_max,
    )


def _apply_cooldown(
    policy: RateControlPolicy,
    state: RateControlState,
    *,
    source_key: str,
    delay: Decimal,
    now: Decimal,
    scope: RetryScope,
) -> RetryAfterResult:
    _require_policy(policy)
    cooldown_until = now + min(max(delay, _ZERO), policy.max_retry_after_seconds)
    if scope is RetryScope.GLOBAL:
        next_state = RateControlState(
            global_bucket=state.global_bucket,
            source_buckets=state.source_buckets,
            global_cooldown_until=max(state.global_cooldown_until, cooldown_until),
            source_cooldown_until=state.source_cooldown_until,
        )
        effective_until = next_state.global_cooldown_until
    else:
        source_cooldowns = dict(state.source_cooldown_until)
        source_cooldowns[source_key] = max(source_cooldowns.get(source_key, _ZERO), cooldown_until)
        next_state = RateControlState(
            global_bucket=state.global_bucket,
            source_buckets=state.source_buckets,
            global_cooldown_until=state.global_cooldown_until,
            source_cooldown_until=source_cooldowns,
        )
        effective_until = next_state.source_cooldown_until[source_key]
    return RetryAfterResult(
        delay_seconds=effective_until - now,
        cooldown_until=effective_until,
        scope=scope,
        source_bucket_key=source_key,
        next_state=next_state,
    )


def _concurrency_result(
    *,
    target: int,
    current: int,
    reasons: tuple[str, ...],
    success_rate: Decimal | None,
    timeout_rate: Decimal | None,
    effective_max: int,
) -> ConcurrencyRecommendation:
    if target > current:
        action = ConcurrencyAction.INCREASE
    elif target < current:
        action = ConcurrencyAction.DECREASE
    else:
        action = ConcurrencyAction.HOLD
    return ConcurrencyRecommendation(
        recommended_workers=target,
        action=action,
        reasons=reasons,
        success_rate=success_rate,
        timeout_rate=timeout_rate,
        effective_max_workers=effective_max,
        transport_rotation_requested=False,
    )


def _source_bucket_key(policy: RateControlPolicy, source: str) -> str:
    name = _source_name(source)
    return name if name in policy.source_policies else _DEFAULT_SOURCE_BUCKET_KEY


def _source_policy(policy: RateControlPolicy, source_key: str) -> TokenBucketPolicy:
    return policy.source_policies.get(source_key, policy.default_source_policy)


def _source_bucket(
    state: RateControlState,
    source_key: str,
    policy: TokenBucketPolicy,
    now: Decimal,
) -> TokenBucketState:
    bucket = state.source_buckets.get(source_key)
    if bucket is None:
        return _full_bucket(policy, now)
    if not isinstance(bucket, TokenBucketState):
        raise TypeError("source bucket state must be a TokenBucketState")
    return bucket


def _full_bucket(policy: TokenBucketPolicy, now: Decimal) -> TokenBucketState:
    return TokenBucketState(tokens=policy.capacity, updated_at=now)


def _refill_bucket(bucket: TokenBucketState, policy: TokenBucketPolicy, now: Decimal) -> TokenBucketState:
    if bucket.updated_at > now:
        raise RateControlError("now cannot move backwards from token bucket state")
    if bucket.tokens < _ZERO or bucket.tokens > policy.capacity:
        raise RateControlError("token bucket state is outside policy capacity")
    elapsed = now - bucket.updated_at
    return TokenBucketState(tokens=min(policy.capacity, bucket.tokens + elapsed * policy.refill_per_second), updated_at=now)


def _bucket_wait(bucket: TokenBucketState, cost: Decimal, policy: TokenBucketPolicy) -> Decimal:
    if bucket.tokens >= cost:
        return _ZERO
    return (cost - bucket.tokens) / policy.refill_per_second


def _remaining_wait(cooldown_until: Decimal, now: Decimal) -> Decimal:
    return max(cooldown_until - now, _ZERO)


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _require_policy(policy: RateControlPolicy) -> None:
    if not isinstance(policy, RateControlPolicy):
        raise TypeError("policy must be a RateControlPolicy")


def _require_state(state: RateControlState) -> None:
    if not isinstance(state, RateControlState):
        raise TypeError("state must be a RateControlState")


def _ensure_state_time(state: RateControlState, now: Decimal) -> None:
    if state.global_bucket.updated_at > now:
        raise RateControlError("now cannot move backwards from rate-control state")
    for bucket in state.source_buckets.values():
        if not isinstance(bucket, TokenBucketState):
            raise TypeError("source bucket state must be a TokenBucketState")
        if bucket.updated_at > now:
            raise RateControlError("now cannot move backwards from rate-control state")


def _source_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("source must be a string")
    normalized = value.strip()
    if not normalized:
        raise RateControlError("source cannot be blank")
    return normalized


def _time_value(value: DecimalLike) -> Decimal:
    return _nonnegative_decimal(value, field="now")


def _positive_decimal(value: DecimalLike, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed <= _ZERO:
        raise RateControlError(f"{field} must be positive")
    return parsed


def _nonnegative_decimal(value: DecimalLike, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed < _ZERO:
        raise RateControlError(f"{field} cannot be negative")
    return parsed


def _unit_interval_decimal(value: DecimalLike, *, field: str) -> Decimal:
    parsed = _nonnegative_decimal(value, field=field)
    if parsed > _ONE:
        raise RateControlError(f"{field} must be between 0 and 1")
    return parsed


def _try_nonnegative_decimal(value: object) -> Decimal | None:
    try:
        parsed = _decimal(value, field="retry_after")
    except RateControlError:
        return None
    return max(parsed, _ZERO)


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise RateControlError(f"{field} must be numeric, not boolean")
    if isinstance(value, float):
        if not isfinite(value):
            raise RateControlError(f"{field} must be finite")
        value = str(value)
    if not isinstance(value, (Decimal, int, str)):
        raise RateControlError(f"{field} must be numeric")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise RateControlError(f"{field} cannot be blank")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RateControlError(f"{field} must be numeric") from error
    if not parsed.is_finite():
        raise RateControlError(f"{field} must be finite")
    return parsed


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime for Retry-After HTTP-date parsing")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "ConcurrencyAction",
    "ConcurrencyPolicy",
    "ConcurrencyRecommendation",
    "ConcurrencySignals",
    "RateAcquireResult",
    "RateControlError",
    "RateControlPolicy",
    "RateControlState",
    "RateLimitReason",
    "RetryAfterResult",
    "RetryScope",
    "TokenBucketPolicy",
    "TokenBucketState",
    "acquire_request",
    "apply_retry_after",
    "initial_rate_control_state",
    "parse_retry_after",
    "record_protection_response",
    "recommend_worker_concurrency",
]
