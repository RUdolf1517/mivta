"""In-memory runtime metrics (uptime and anonymized unique session user count)."""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_SESSION_USERS = 50_000
# Bounded ring of recent provider round-trip times; percentiles are computed on
# demand (owner-only `/status`), so sorting a copy here is cheaper than keeping
# a running histogram updated on every translation.
MAX_LATENCY_SAMPLES = 512


class RuntimeMetrics:
    """Lightweight RAM-only process metrics storing zero plaintext user IDs or text."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._salt = os.urandom(16)
        self._seen_digests: OrderedDict[bytes, None] = OrderedDict()
        self._latencies: deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self.total_updates: int = 0
        self.translation_count: int = 0

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    @property
    def session_unique_users(self) -> int:
        return len(self._seen_digests)

    @property
    def latency_samples(self) -> int:
        return len(self._latencies)

    def record_translation_latency(self, seconds: float) -> None:
        """Record one provider round-trip time (never any text or user data)."""
        self.translation_count += 1
        self._latencies.append(max(0.0, seconds))

    def latency_percentile(self, percentile: float) -> float | None:
        """Percentile (0..100) of recent round-trip times, or None when no data yet."""
        if not self._latencies:
            return None
        ordered = sorted(self._latencies)
        rank = (percentile / 100.0) * (len(ordered) - 1)
        return ordered[min(len(ordered) - 1, max(0, int(round(rank))))]

    def record_activity(self, user_id: int | None) -> None:
        """Increment update count and record anonymized user digest if human user_id given."""
        self.total_updates += 1
        if user_id is None or user_id <= 0:
            return
        digest = hashlib.blake2b(
            user_id.to_bytes(8, "big", signed=False),
            key=self._salt,
            digest_size=8,
        ).digest()
        self._seen_digests[digest] = None
        self._seen_digests.move_to_end(digest)
        while len(self._seen_digests) > MAX_TRACKED_SESSION_USERS:
            self._seen_digests.popitem(last=False)
