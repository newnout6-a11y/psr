"""Circuit breaker по платформе/операции.

Идея: если на платформе подряд много ошибок — ставим её на 'паузу'
на заданное время. Защищает от баннинга, рейт-лимитов, сессии-трупа.

Три состояния:
  CLOSED   — всё ок, пропускаем.
  OPEN     — сломано, отказываем до восстановления.
  HALF_OPEN — пробный вызов; успех → CLOSED, провал → OPEN с увеличенной паузой.

Экспоненциальный бэкофф при повторных OPEN, сброс после успеха.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock

from loguru import logger

from src.utils.log_db import get_log_db


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitConfig:
    failure_threshold: int = 3     # сколько провалов подряд → OPEN
    recovery_seconds: float = 120  # базовый таймаут до HALF_OPEN
    max_recovery_seconds: float = 1800  # потолок (30 мин)


@dataclass
class _Circuit:
    key: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    open_until: float = 0.0           # epoch
    consecutive_opens: int = 0        # для экспоненциального backoff
    last_error: str = ""
    metrics: dict[str, int] = field(
        default_factory=lambda: {"allow": 0, "deny": 0, "success": 0, "failure": 0}
    )


class CircuitBreaker:
    """Thread-safe хранилище цепей по ключам (обычно — имя платформы)."""

    def __init__(self, config: CircuitConfig | None = None):
        self.cfg = config or CircuitConfig()
        self._circuits: dict[str, _Circuit] = {}
        self._lock = RLock()
        self._restore()

    def _get(self, key: str) -> _Circuit:
        with self._lock:
            c = self._circuits.get(key)
            if c is None:
                c = _Circuit(key=key)
                self._circuits[key] = c
            return c

    # ----- основной API -----

    def allow(self, key: str) -> bool:
        """Можно ли сейчас делать вызов?"""
        c = self._get(key)
        now = time.time()
        with self._lock:
            if c.state == CircuitState.CLOSED:
                c.metrics["allow"] += 1
                return True
            if c.state == CircuitState.OPEN:
                if now >= c.open_until:
                    c.state = CircuitState.HALF_OPEN
                    logger.info(f"CircuitBreaker[{key}]: OPEN → HALF_OPEN (пробный вызов)")
                    self._persist(c)
                    c.metrics["allow"] += 1
                    return True
                c.metrics["deny"] += 1
                return False
            # HALF_OPEN — пропускаем ровно один вызов
            c.metrics["allow"] += 1
            return True

    def record_success(self, key: str) -> None:
        c = self._get(key)
        with self._lock:
            c.metrics["success"] += 1
            if c.state != CircuitState.CLOSED:
                logger.info(f"CircuitBreaker[{key}]: восстановление → CLOSED")
            c.state = CircuitState.CLOSED
            c.consecutive_failures = 0
            c.consecutive_opens = 0
            c.open_until = 0.0
            c.last_error = ""
            self._persist(c)

    def record_failure(self, key: str, error: str = "") -> None:
        c = self._get(key)
        with self._lock:
            c.metrics["failure"] += 1
            c.consecutive_failures += 1
            c.last_error = error or c.last_error

            if c.state == CircuitState.HALF_OPEN:
                # Пробный вызов не прошёл — снова OPEN, с большим backoff
                self._trip(c)
                return
            if c.consecutive_failures >= self.cfg.failure_threshold:
                self._trip(c)

    def _trip(self, c: _Circuit) -> None:
        c.consecutive_opens += 1
        # Экспоненциальный бэкофф: T, 2T, 4T ... ≤ max
        backoff = min(
            self.cfg.recovery_seconds * (2 ** (c.consecutive_opens - 1)),
            self.cfg.max_recovery_seconds,
        )
        c.state = CircuitState.OPEN
        c.open_until = time.time() + backoff
        logger.warning(
            f"CircuitBreaker[{c.key}]: OPEN на {int(backoff)}s "
            f"(провалов: {c.consecutive_failures}, причина: {c.last_error[:80]})"
        )
        self._persist(c)

    # ----- diagnostics -----

    def status(self, key: str | None = None) -> dict:
        with self._lock:
            if key:
                c = self._circuits.get(key)
                if not c:
                    return {"key": key, "state": "unknown"}
                return self._snapshot(c)
            return {k: self._snapshot(c) for k, c in self._circuits.items()}

    @staticmethod
    def _snapshot(c: _Circuit) -> dict:
        now = time.time()
        return {
            "state": c.state.value,
            "consecutive_failures": c.consecutive_failures,
            "consecutive_opens": c.consecutive_opens,
            "paused_seconds_left": max(0, int(c.open_until - now)) if c.state == CircuitState.OPEN else 0,
            "last_error": c.last_error,
            "metrics": dict(c.metrics),
        }

    def _persist(self, c: _Circuit) -> None:
        try:
            get_log_db().save_breaker_state(c.key, self._snapshot(c))
        except Exception as e:
            logger.debug(f"CircuitBreaker[{c.key}]: persist failed: {e}")

    def _restore(self) -> None:
        try:
            rows = get_log_db().get_breaker_states()
        except Exception as e:
            logger.debug(f"CircuitBreaker: restore skipped: {e}")
            return

        now = time.time()
        for row in rows:
            state_name = row.get("state", CircuitState.CLOSED.value)
            try:
                state = CircuitState(state_name)
            except Exception:
                state = CircuitState.CLOSED

            paused_left = int(row.get("paused_seconds_left", 0) or 0)
            circuit = _Circuit(
                key=row["key"],
                state=state,
                consecutive_failures=int(row.get("consecutive_failures", 0) or 0),
                consecutive_opens=int(row.get("consecutive_opens", 0) or 0),
                open_until=now + max(paused_left, 0) if state == CircuitState.OPEN else 0.0,
                last_error=row.get("last_error") or "",
                metrics=dict(row.get("metrics") or {}),
            )
            self._circuits[circuit.key] = circuit


# Глобальный экземпляр — удобно для всего проекта
_default_breaker: CircuitBreaker | None = None


def get_breaker() -> CircuitBreaker:
    global _default_breaker
    if _default_breaker is None:
        _default_breaker = CircuitBreaker()
    return _default_breaker
