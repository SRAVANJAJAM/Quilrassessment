import asyncio
import math
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .errors import GatewayError


class TokenLimiter:
    def __init__(self, path: str, limit: int = 50_000, window: float = 60.0, clock=time.time):
        self.path, self.limit, self.window, self.clock = path, limit, window, clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def initialize(self) -> None:
        def setup():
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS usage "
                    "(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, at REAL NOT NULL, "
                    "tokens INTEGER NOT NULL CHECK(tokens >= 0))"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS usage_window ON usage(tenant, at)")
                conn.execute("CREATE INDEX IF NOT EXISTS usage_expiry ON usage(at)")

        await asyncio.to_thread(setup)

    async def reserve(self, tenant: str, tokens: int) -> str:
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("Token reservation must be a positive integer")

        def transaction():
            reservation = uuid.uuid4().hex
            with closing(self._connect()) as conn, conn:
                # All processes sharing this disk file serialize admission here.
                conn.execute("BEGIN IMMEDIATE")
                now = self.clock()
                conn.execute("DELETE FROM usage WHERE at <= ?", (now - self.window,))
                used, earliest = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0), MIN(at) FROM usage WHERE tenant = ?",
                    (tenant,),
                ).fetchone()
                if used + tokens > self.limit:
                    first_at = earliest if earliest is not None else now
                    retry = max(1, math.ceil(first_at + self.window - now))
                    raise GatewayError(
                        "rate_limit_exceeded", "Tenant token budget exceeded", 429, retry
                    )
                conn.execute(
                    "INSERT INTO usage VALUES (?, ?, ?, ?)", (reservation, tenant, now, tokens)
                )
            return reservation

        try:
            return await asyncio.to_thread(transaction)
        except sqlite3.Error:
            raise GatewayError(
                "quota_unavailable", "Token accounting is unavailable", 503
            ) from None

    async def settle(self, reservation: str, actual: int) -> None:
        if type(actual) is not int or actual < 0:
            raise ValueError("Usage must be a nonnegative integer")

        def transaction():
            with closing(self._connect()) as conn, conn:
                conn.execute("UPDATE usage SET tokens = ? WHERE id = ?", (actual, reservation))

        try:
            await asyncio.to_thread(transaction)
        except sqlite3.Error:
            # The existing reservation remains charged when reconciliation fails.
            raise GatewayError(
                "quota_unavailable", "Token accounting is unavailable", 503
            ) from None
