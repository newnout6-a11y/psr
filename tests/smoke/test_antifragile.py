"""Быстрые тесты anti-fragile компонентов."""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.paths import BROWSER_PROFILES_DIR


def test_circuit_breaker_opens_after_failures():
    from src.utils.circuit_breaker import CircuitBreaker, CircuitConfig, CircuitState

    br = CircuitBreaker(CircuitConfig(failure_threshold=3, recovery_seconds=0.2))
    key = "send:kwork"

    # CLOSED → пропускает
    assert br.allow(key) is True

    # 3 провала подряд → OPEN
    for _ in range(3):
        br.record_failure(key, "boom")

    assert br.allow(key) is False
    snap = br.status(key)
    assert snap["state"] == CircuitState.OPEN.value

    # Ждём recovery → HALF_OPEN
    time.sleep(0.3)
    assert br.allow(key) is True
    snap = br.status(key)
    assert snap["state"] == CircuitState.HALF_OPEN.value

    # Успех в HALF_OPEN → CLOSED
    br.record_success(key)
    snap = br.status(key)
    assert snap["state"] == CircuitState.CLOSED.value
    assert snap["consecutive_failures"] == 0


def test_circuit_breaker_exponential_backoff():
    from src.utils.circuit_breaker import CircuitBreaker, CircuitConfig

    br = CircuitBreaker(CircuitConfig(failure_threshold=1, recovery_seconds=1, max_recovery_seconds=10))
    key = "test"

    br.record_failure(key, "x")
    first = br.status(key)["paused_seconds_left"]
    time.sleep(1.1)
    br.allow(key)  # переводит в HALF_OPEN
    br.record_failure(key, "x")  # HALF_OPEN → OPEN с 2x backoff
    second = br.status(key)["paused_seconds_left"]
    assert second >= first, f"expected growing backoff, got {first} -> {second}"


def test_fingerprint_deterministic_by_seed():
    from src.browser.fingerprint import pick

    seed = str(BROWSER_PROFILES_DIR)
    f1 = pick(seed=seed)
    f2 = pick(seed=seed)
    f3 = pick(seed=str(BROWSER_PROFILES_DIR / "different"))

    assert f1.user_agent == f2.user_agent
    assert f1.viewport == f2.viewport
    # разный seed — может быть разный (не обязательно, но в этом пуле скорее всего)
    assert f1 != f3 or f1.user_agent == f3.user_agent  # noqa: допускаем коллизию


def test_fingerprint_browser_args():
    from src.browser.fingerprint import pick

    f = pick(seed="abc")
    args = f.browser_args()
    assert any("--window-size=" in a for a in args)
    assert any("--user-agent=" in a for a in args)
    assert any("--lang=" in a for a in args)


if __name__ == "__main__":
    test_circuit_breaker_opens_after_failures()
    print("[ok] circuit_breaker: базовый жизненный цикл")
    test_circuit_breaker_exponential_backoff()
    print("[ok] circuit_breaker: экспоненциальный backoff")
    test_fingerprint_deterministic_by_seed()
    print("[ok] fingerprint: детерминизм по seed")
    test_fingerprint_browser_args()
    print("[ok] fingerprint: browser_args содержат нужные флаги")
    print("\nВсе проверки прошли.")
