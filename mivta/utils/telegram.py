"""Bounded retry for Telegram Bot API flood control (`HTTP 429 Retry After`).

`TelegramRetryAfter` derives from `TelegramAPIError`, so the broad
`except TelegramAPIError` guards elsewhere would silently treat "slow down"
as a permanent failure. Automated flows (reposting a group translation,
deleting the `>` draft) are exactly where a 429 is most likely and least
acceptable to lose, so they go through `call_with_retry`.

Waits are capped: a translation that arrives 30 seconds late is worse than an
honest failure, so anything above `MAX_RETRY_WAIT_SECONDS` is re-raised.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger(__name__)

MAX_RETRY_WAIT_SECONDS = 3.0
_T = TypeVar("_T")


async def call_with_retry(
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 2,
    label: str = "telegram call",
    max_wait: float = MAX_RETRY_WAIT_SECONDS,
) -> _T:
    """Await `operation()`, retrying once when Telegram asks us to slow down.

    Retries only when the requested delay is short enough to stay useful;
    longer flood-control windows are re-raised so the caller can degrade.
    """
    total = max(1, attempts)
    for attempt in range(total):
        try:
            return await operation()
        except TelegramRetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 0) or 0)
            last_try = attempt + 1 >= total
            if last_try or wait <= 0 or wait > max_wait:
                raise
            logger.warning(
                "%s: Telegram flood control, retrying in %.1fs",
                label,
                wait,
            )
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
