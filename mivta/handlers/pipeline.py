"""Shared cached, rate-limited, singleflight translation pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import time
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
from mivta.utils.digest import translation_digest
from mivta.utils.formatting import (
    MAX_TRANSLATION_CHUNKS,
    join_translated_chunks,
    split_for_translation,
)
from mivta.utils.metrics import RuntimeMetrics
from mivta.utils.ratelimit import RateLimiter

TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_CAPTION_LEN = 1024


@dataclass(frozen=True, slots=True)
class RateLimitedOutcome:
    """Returned when a user exceeds their per-minute/burst quota on a cache miss."""

    retry_after: float


@dataclass(slots=True)
class InflightEntry:
    """Reference-counted singleflight task so lone cancelled callers cancel upstream work."""

    task: asyncio.Task[TranslationResult]
    waiters: int = 0
    cached: bool = False


def inflight_key(provider: str, request: TranslationRequest) -> bytes:
    """Collision-free 16-byte digest for deduplicating identical concurrent translations."""
    return translation_digest(provider, request)


def _should_provider_swap(request: TranslationRequest, result: TranslationResult) -> bool:
    """Return True if an upstream detector confirmed source == target and swap_lang is set."""
    return (
        request.source_lang == "auto"
        and bool(request.swap_lang)
        and request.swap_lang != request.target_lang
        and result.target_lang == request.target_lang
        and result.source_lang == request.target_lang
    )


def is_query_cached(
    *,
    provider_name: str,
    base_request: TranslationRequest,
    cache: TranslationCache,
    target_langs: Sequence[str],
    styles: Sequence[TranslationStyle],
    multilang_mode: bool,
    multistyle_mode: bool,
) -> bool:
    """Return True when all requested variants are already warm in TranslationCache."""
    if multilang_mode:
        return all(
            cache.peek(provider_name, replace(base_request, target_lang=code, swap_lang=""))
            for code in target_langs
        )
    if multistyle_mode:
        return all(
            cache.peek(provider_name, replace(base_request, style=st)) for st in styles
        )
    return cache.peek(provider_name, base_request)


async def _singleflight_translate(
    *,
    request: TranslationRequest,
    translator: Translator,
    cache: TranslationCache,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, InflightEntry],
    metrics: RuntimeMetrics | None = None,
) -> TranslationResult:
    cached = cache.get(translator.name, request)
    if cached is not None:
        return cached

    flight_key = inflight_key(translator.name, request)
    entry = inflight.get(flight_key)
    if entry is None:

        async def _run() -> TranslationResult:
            async with concurrency_sem:
                started = time.perf_counter()
                try:
                    first = await translator.translate(request)
                    if _should_provider_swap(request, first):
                        swapped_req = replace(
                            request,
                            target_lang=request.swap_lang,
                            swap_lang="",
                        )
                        return await translator.translate(swapped_req)
                    return first
                finally:
                    if metrics is not None:
                        metrics.record_translation_latency(time.perf_counter() - started)

        task = asyncio.create_task(_run())
        # Suppress "Task exception was never retrieved" if all waiters cancel before completion
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        entry = InflightEntry(task=task, waiters=0)
        inflight[flight_key] = entry

    entry.waiters += 1
    try:
        result = await asyncio.shield(entry.task)
    except asyncio.CancelledError:
        entry.waiters -= 1
        if entry.waiters <= 0:
            inflight.pop(flight_key, None)
            if not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.task
        raise
    except Exception:
        entry.waiters -= 1
        if entry.waiters <= 0:
            inflight.pop(flight_key, None)
        raise
    else:
        entry.waiters -= 1
        if not entry.cached:
            cache.set(translator.name, request, result)
            entry.cached = True
        if entry.waiters <= 0:
            inflight.pop(flight_key, None)
        return result


async def execute_translation(
    *,
    user_id: int,
    request: TranslationRequest,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, InflightEntry],
    metrics: RuntimeMetrics | None = None,
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
        metrics=metrics,
    )


@dataclass(frozen=True, slots=True)
class TextTooLong:
    """Returned when the text needs more chunks than the configured limit allows."""

    length: int
    max_chunks: int


async def execute_chunked_translation(
    *,
    user_id: int,
    request: TranslationRequest,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, InflightEntry],
    max_length: int,
    max_chunks: int = MAX_TRANSLATION_CHUNKS,
    metrics: RuntimeMetrics | None = None,
) -> TranslationResult | RateLimitedOutcome | TextTooLong:
    """Translate a long text by provider-sized chunks (one rate-limit token total)."""
    text = request.text
    if max_length <= 0 or len(text) <= max_length:
        return await execute_translation(
            user_id=user_id,
            request=request,
            translator=translator,
            cache=cache,
            rate_limiter=rate_limiter,
            concurrency_sem=concurrency_sem,
            inflight=inflight,
            metrics=metrics,
        )

    chunks = split_for_translation(text, max_length, max_chunks=max_chunks)
    if chunks.exceeds or len(chunks.chunks) < 2:
        if chunks.exceeds:
            return TextTooLong(length=len(text), max_chunks=max_chunks)
        return await execute_translation(
            user_id=user_id,
            request=request,
            translator=translator,
            cache=cache,
            rate_limiter=rate_limiter,
            concurrency_sem=concurrency_sem,
            inflight=inflight,
            metrics=metrics,
        )

    chunk_requests = [replace(request, text=chunk) for chunk in chunks.chunks]

    # A fully cached text costs nothing; otherwise the whole message still
    # charges exactly one rate-limit token, however many chunks it fans out to.
    cached_hits = [cache.get(translator.name, req) for req in chunk_requests]
    if all(hit is not None for hit in cached_hits):
        results: list[TranslationResult] = [hit for hit in cached_hits if hit is not None]
    else:
        allowed, retry_after = rate_limiter.allow(user_id)
        if not allowed:
            return RateLimitedOutcome(retry_after=retry_after)

        # Chunks are independent, so translate them concurrently instead of
        # paying N sequential provider round-trips (up to ~9 for a 4096-char
        # message at the default MAX_TEXT_LENGTH of 500).
        tasks = [
            asyncio.create_task(
                _singleflight_translate(
                    request=req,
                    translator=translator,
                    cache=cache,
                    concurrency_sem=concurrency_sem,
                    inflight=inflight,
                    metrics=metrics,
                )
            )
            for req in chunk_requests
        ]
        try:
            results = list(await asyncio.gather(*tasks))
        except Exception:
            # Preserve the old fail-fast behaviour: drop the sibling requests
            # instead of letting them run to completion for a result we discard.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    first = results[0]
    return TranslationResult(
        translated_text=join_translated_chunks(
            [item.translated_text for item in results],
            chunks.separators,
        ),
        source_lang=first.source_lang,
        target_lang=first.target_lang,
        provider=f"{first.provider}×{len(results)}",
        style=first.style,
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
    metrics: RuntimeMetrics | None = None,
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
        started = time.perf_counter()
        try:
            fetched = await translator.translate_styles(request, missing)
            if fetched and _should_provider_swap(request, fetched[0]):
                swapped_req = replace(
                    request,
                    target_lang=request.swap_lang,
                    swap_lang="",
                )
                fetched = await translator.translate_styles(swapped_req, ordered)
                cached_by_style.clear()
        finally:
            if metrics is not None:
                metrics.record_translation_latency(time.perf_counter() - started)

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
    inflight: dict[bytes, InflightEntry],
    metrics: RuntimeMetrics | None = None,
) -> list[TranslationResult] | RateLimitedOutcome:
    """Resolve multiple target languages concurrently (charging 1 rate-limit token per query)."""
    ordered_targets: list[str] = []
    for code in target_langs:
        if code not in ordered_targets:
            ordered_targets.append(code)

    requests = [
        replace(base_request, target_lang=code, swap_lang="")
        for code in ordered_targets
    ]
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
                metrics=metrics,
            )
            for req in requests
        )
    )
    return list(results)
