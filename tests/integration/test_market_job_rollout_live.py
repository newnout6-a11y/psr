"""Opt-in durable rollout gate for live Kwork market collection.

Run only with an explicitly configured Session Hub and source route:
```
KWORK_LIVE_MARKET_ROLLOUT=1 python -m pytest tests/integration/test_market_job_rollout_live.py -q
```
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, NetworkPolicy
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor


pytestmark = pytest.mark.asyncio


def _enabled() -> bool:
    return os.getenv("KWORK_LIVE_MARKET_ROLLOUT", "").strip().lower() in {"1", "true", "yes"}


def _targets() -> tuple[int, ...]:
    raw = os.getenv("KWORK_LIVE_MARKET_ROLLOUT_TARGETS", "60,500,2000,10000")
    values = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
    if not values or values != tuple(sorted(set(values))) or values[-1] > 10_000:
        raise ValueError("KWORK_LIVE_MARKET_ROLLOUT_TARGETS must be strictly increasing values up to 10000")
    return values


async def _session_cookies() -> dict[str, str]:
    from src.platforms.kwork import get_kwork_service

    cookies = await get_kwork_service()._fetch_session_hub_cookies()
    return {str(name): str(value) for name, value in cookies.items() if name and value}


async def _wait_for_completion(
    repository: MarketJobRepository,
    job_id: str,
    *,
    target: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        job = await repository.get_job(job_id)
        if job is None:
            raise AssertionError(f"market job {job_id!r} disappeared")
        if job["state"] in {"failed", "blocked", "stopped"}:
            raise AssertionError(f"market job reached {job['state']}: {job.get('last_error') or job.get('last_warning')}")
        if job["state"] == "completed":
            assert int((job.get("counters") or {}).get("unique_cards", 0)) >= target
            return job
        await asyncio.sleep(0.5)
    raise AssertionError(f"market job did not reach {target} cards before the live-gate timeout")


@pytest.mark.skipif(not _enabled(), reason="set KWORK_LIVE_MARKET_ROLLOUT=1 to run controlled live rollout gates")
async def test_durable_job_passes_sequential_live_rollout_gates(tmp_path):
    cookies = await _session_cookies()
    if not cookies:
        pytest.skip("Session Hub did not provide Kwork web cookies")

    targets = _targets()
    category_id = int(os.getenv("KWORK_LIVE_MARKET_CATEGORY_ID", "38"))
    alias = os.getenv("KWORK_LIVE_MARKET_ALIAS", "website-repair").strip()
    timeout_seconds = float(os.getenv("KWORK_LIVE_MARKET_GATE_TIMEOUT_SECONDS", "3600"))
    desired_workers = int(os.getenv("KWORK_LIVE_MARKET_WORKERS", "2"))
    network_policy = NetworkPolicy(os.getenv("KWORK_LIVE_MARKET_NETWORK_POLICY", "direct_only"))

    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    executor = MarketOperationExecutor(
        coordinator,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        web_cookie_provider=_session_cookies,
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers=executor.handlers,
        reconcile_interval_seconds=0.25,
        worker_poll_interval_seconds=0.1,
    )
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=category_id, canonical_alias=alias),
            target_unique_cards=targets[0],
            desired_workers=desired_workers,
            network_policy=network_policy,
        ),
        job_id="job_live_rollout",
    )
    await supervisor.start()
    try:
        completed = await _wait_for_completion(
            repository,
            "job_live_rollout",
            target=targets[0],
            timeout_seconds=timeout_seconds,
        )
        for target in targets[1:]:
            reconfigured = await coordinator.update_job(
                "job_live_rollout",
                target_unique_cards=target,
                expected_revision=completed["revision"],
            )
            resumed = await coordinator.resume_job(
                "job_live_rollout",
                expected_revision=reconfigured["revision"],
            )
            assert resumed["state"] == "running"
            completed = await _wait_for_completion(
                repository,
                "job_live_rollout",
                target=target,
                timeout_seconds=timeout_seconds,
            )
    finally:
        await supervisor.close()

    assert int((completed.get("counters") or {}).get("unique_cards", 0)) >= targets[-1]
    assert len(await repository.list_checkpoints("job_live_rollout", limit=1_000)) >= len(targets)
