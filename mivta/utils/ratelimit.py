"""Bounded-memory per-user token-bucket rate limiter."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """Token-bucket rate limiter per user ID with bounded memory footprint."""

    def __init__(
        self,
        rate_per_minute: float = 30.0,
        burst: int = 15,
        max_users: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate_per_sec = max(0.0, rate_per_minute) / 60.0
        self._burst = float(max(0, burst))
        self._max_users = max(1, max_users)
        self._clock = clock
        self._buckets: OrderedDict[int, _Bucket] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._rate_per_sec > 0.0 and self._burst >= 1.0

    def allow(self, user_id: int, cost: float = 1.0) -> tuple[bool, float]:
        """Check whether user_id may proceed; returns (allowed, retry_after_seconds)."""
        if not self.enabled:
            return True, 0.0

        now = self._clock()
        bucket = self._buckets.get(user_id)
        if bucket is None:
            if len(self._buckets) >= self._max_users:
                self._buckets.popitem(last=False)
            bucket = _Bucket(tokens=self._burst, updated_at=now)
            self._buckets[user_id] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate_per_sec)
            bucket.updated_at = now
            self._buckets.move_to_end(user_id)

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, 0.0

        deficit = cost - bucket.tokens
        retry_after = math.ceil(deficit / self._rate_per_sec * 10) / 10.0
        return False, max(0.1, retry_after)

    def tracked_users(self) -> int:
        return len(self._buckets)
