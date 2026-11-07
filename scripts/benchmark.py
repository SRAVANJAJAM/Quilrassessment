"""Small reproducible measurements; not a production capacity claim."""

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from quilr_assessment.errors import GatewayError
from quilr_assessment.limiter import TokenLimiter
from quilr_assessment.redaction import StreamingRedactor


async def main():
    text = "Normal text alice@example.com and 4111 1111 1111 1111. " * 20_000
    redactor = StreamingRedactor()
    start = time.perf_counter()
    for offset in range(0, len(text), 37):
        redactor.feed(text[offset : offset + 37])
    redactor.finish()
    elapsed = time.perf_counter() - start
    result = {
        "redaction_input_bytes": len(text.encode()),
        "redaction_seconds": round(elapsed, 3),
        "redaction_mib_per_second": round(len(text.encode()) / elapsed / 1024**2, 2),
        "peak_candidate_characters": redactor.peak_buffer,
    }
    with tempfile.TemporaryDirectory() as directory:
        limiter = TokenLimiter(str(Path(directory) / "usage.db"), limit=50_000)
        await limiter.initialize()
        times = []

        async def reserve():
            start = time.perf_counter()
            try:
                await limiter.reserve("tenant", 1000)
                return True
            except GatewayError as exc:
                assert exc.status == 429
                return False
            finally:
                times.append((time.perf_counter() - start) * 1000)

        admitted = await asyncio.gather(*(reserve() for _ in range(100)))
        assert sum(admitted) == 50
        result.update(
            concurrent_requests=100,
            admitted=sum(admitted),
            rejected=100 - sum(admitted),
            sqlite_p50_ms=round(statistics.median(times), 2),
            sqlite_p95_ms=round(sorted(times)[94], 2),
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
