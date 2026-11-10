"""Shared cached, rate-limited, singleflight translation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter

TELEGRAM_MAX_MESSAGE_LEN = 4096


@dataclass(frozen=True, slots=True)
class RateLimitedOutcome:
    """Returned when a user exceeds their per-minute/burst quota on a cache miss."""

    retry_after: float


def inflight_key(provider: str, request: TranslationRequest) -> bytes:
    """Collision-free 16-byte digest for deduplicating identical concurrent translations."""
    hasher = hashlib.blake2b(digest_size=16)
    for item in (
        provider,
        request.source_lang,
        request.target_lang,
        request.style.value,
        request.text,
    ):
        encoded = item.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
    return hasher.digest()


async def execute_translation(
    *,
    user_id: int,
    request: TranslationRequest,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, asyncio.Task[TranslationResult]],
) -> TranslationResult | RateLimitedOutcome:
    """Resolve translation via cache, per-user rate limiter, and singleflight upstream call."""
    cached = cache.get(translator.name, request)
    if cached is not None:
        return cached

    allowed, retry_after = rate_limiter.allow(user_id)
    if not allowed:
        return RateLimitedOutcome(retry_after=retry_after)

    flight_key = inflight_key(translator.name, request)
    task = inflight.get(flight_key)
    created_task = False
    if task is None:

        async def _run() -> TranslationResult:
            async with concurrency_sem:
                return await translator.translate(request)

        task = asyncio.create_task(_run())
        inflight[flight_key] = task
        created_task = True

    try:
        result = await asyncio.shield(task)
    finally:
        if created_task:
            inflight.pop(flight_key, None)

    if created_task:
        cache.set(translator.name, request, result)
    return result
