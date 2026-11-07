import asyncio

import pytest

from quilr_assessment.errors import GatewayError
from quilr_assessment.limiter import TokenLimiter


async def test_concurrent_admission_across_instances_is_atomic(tmp_path):
    path = str(tmp_path / "quota.db")
    limiters = [TokenLimiter(path, limit=100, clock=lambda: 100) for _ in range(5)]
    await limiters[0].initialize()
    results = await asyncio.gather(
        *(limiters[i % 5].reserve("tenant-a", 10) for i in range(40)), return_exceptions=True
    )
    assert sum(isinstance(result, str) for result in results) == 10
    assert all(
        isinstance(result, str) or (isinstance(result, GatewayError) and result.status == 429)
        for result in results
    )
    assert await limiters[0].reserve("tenant-b", 100)


async def test_sliding_expiry_reconciliation_and_persistence(tmp_path):
    now = [100.0]
    path = str(tmp_path / "quota.db")
    limiter = TokenLimiter(path, limit=100, clock=lambda: now[0])
    await limiter.initialize()
    reservation = await limiter.reserve("tenant", 100)
    await limiter.settle(reservation, 20)
    now[0] = 130
    await limiter.reserve("tenant", 80)
    restarted = TokenLimiter(path, limit=100, clock=lambda: now[0])
    with pytest.raises(GatewayError):
        await restarted.reserve("tenant", 1)
    now[0] = 160  # Only the first request expires; this is not a fixed-minute reset.
    await restarted.reserve("tenant", 20)
    with pytest.raises(GatewayError):
        await restarted.reserve("tenant", 1)
