"""Shared cached, rate-limited, singleflight translation pipeline."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace

from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    deduplicate_styles,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter

TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_CAPTION_LEN = 1024


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


async def _singleflight_translate(
    *,
    request: TranslationRequest,
    translator: Translator,
    cache: TranslationCache,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, asyncio.Task[TranslationResult]],
) -> TranslationResult:
    cached = cache.get(translator.name, request)
    if cached is not None:
        return cached

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

    return await _singleflight_translate(
        request=request,
        translator=translator,
        cache=cache,
        concurrency_sem=concurrency_sem,
        inflight=inflight,
    )


async def execute_multistyle_translation(
    *,
    user_id: int,
    request: TranslationRequest,
    styles: Sequence[TranslationStyle],
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
) -> list[TranslationResult] | RateLimitedOutcome:
    """Resolve multiple style variants at once (charging at most 1 rate-limit token)."""
    ordered = deduplicate_styles(styles) or [request.style]
    cached_by_style: dict[TranslationStyle, TranslationResult] = {}
    missing: list[TranslationStyle] = []
    for st in ordered:
        st_req = replace(request, style=st)
        hit = cache.get(translator.name, st_req)
        if hit is not None:
            cached_by_style[st] = hit
        else:
            missing.append(st)

    if not missing:
        return [cached_by_style[st] for st in ordered]

    allowed, retry_after = rate_limiter.allow(user_id)
    if not allowed:
        return RateLimitedOutcome(retry_after=retry_after)

    async with concurrency_sem:
        fetched = await translator.translate_styles(request, missing)

    for res in fetched:
        cached_by_style[res.style] = res
        cache.set(translator.name, replace(request, style=res.style), res)

    results: list[TranslationResult] = []
    for st in ordered:
        item = cached_by_style.get(st)
        if item is not None:
            results.append(item)
    return results or fetched


async def execute_multilang_translation(
    *,
    user_id: int,
    base_request: TranslationRequest,
    target_langs: Sequence[str],
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, asyncio.Task[TranslationResult]],
) -> list[TranslationResult] | RateLimitedOutcome:
    """Resolve multiple target languages concurrently (charging 1 rate-limit token per query)."""
    ordered_targets: list[str] = []
    for code in target_langs:
        if code not in ordered_targets:
            ordered_targets.append(code)

    requests = [replace(base_request, target_lang=code) for code in ordered_targets]
    if all(cache.get(translator.name, req) is not None for req in requests):
        return [
            hit
            for req in requests
            if (hit := cache.get(translator.name, req)) is not None
        ]

    allowed, retry_after = rate_limiter.allow(user_id)
    if not allowed:
        return RateLimitedOutcome(retry_after=retry_after)

    results = await asyncio.gather(
        *(
            _singleflight_translate(
                request=req,
                translator=translator,
                cache=cache,
                concurrency_sem=concurrency_sem,
                inflight=inflight,
            )
            for req in requests
        )
    )
    return list(results)
