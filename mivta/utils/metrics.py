"""In-memory runtime metrics (uptime and anonymized unique session user count)."""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from collections.abc import Callable

MAX_TRACKED_SESSION_USERS = 50_000


class RuntimeMetrics:
    """Lightweight RAM-only process metrics storing zero plaintext user IDs or text."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._salt = os.urandom(16)
        self._seen_digests: OrderedDict[bytes, None] = OrderedDict()
        self.total_updates: int = 0

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    @property
    def session_unique_users(self) -> int:
        return len(self._seen_digests)

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
