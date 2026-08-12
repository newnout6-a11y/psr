from __future__ import annotations

import pytest

from src.api.server import _capped_buyer_recovery_workers


@pytest.mark.parametrize(
    ("stored_workers", "configured_limit", "expected"),
    [
        (30, 2, 2),
        (5, 3, 3),
        (1, 30, 1),
        (0, 5, 1),
        ("invalid", 5, 1),
    ],
)
def test_recovery_worker_count_cannot_exceed_the_configured_buyer_limit(
    stored_workers: object,
    configured_limit: int,
    expected: int,
) -> None:
    assert _capped_buyer_recovery_workers(stored_workers, max_workers=configured_limit) == expected


def test_recovery_worker_count_rejects_an_invalid_configured_limit() -> None:
    with pytest.raises(ValueError, match="max_workers"):
        _capped_buyer_recovery_workers(1, max_workers=0)
