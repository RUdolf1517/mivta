"""Tests for translation latency percentiles and Telegram flood-control retries."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter

from mivta.utils.metrics import MAX_LATENCY_SAMPLES, RuntimeMetrics
from mivta.utils.telegram import MAX_RETRY_WAIT_SECONDS, call_with_retry


def test_latency_percentiles_and_sample_cap() -> None:
    metrics = RuntimeMetrics()

    assert metrics.latency_samples == 0
    assert metrics.latency_percentile(50) is None
    assert metrics.latency_percentile(95) is None

    for idx in range(100):
        metrics.record_translation_latency(idx / 1000.0)

    assert metrics.translation_count == 100
    assert metrics.latency_samples == 100
    assert metrics.latency_percentile(50) == pytest.approx(0.050, abs=0.002)
    assert metrics.latency_percentile(95) == pytest.approx(0.095, abs=0.002)
    assert metrics.latency_percentile(99) == pytest.approx(0.099, abs=0.002)
    assert metrics.latency_percentile(0) == pytest.approx(0.0, abs=0.002)

    # The ring buffer is bounded: a burst of slow calls must not leak memory
    for _ in range(MAX_LATENCY_SAMPLES + 50):
        metrics.record_translation_latency(10.0)
    assert metrics.latency_samples == MAX_LATENCY_SAMPLES
    assert metrics.latency_percentile(50) == pytest.approx(10.0)


def test_latency_ignores_negative_values() -> None:
    metrics = RuntimeMetrics()
    metrics.record_translation_latency(-1.0)
    assert metrics.latency_samples == 1
    assert metrics.latency_percentile(50) == 0.0


def test_metrics_record_activity_counts_updates_and_users() -> None:
    metrics = RuntimeMetrics()
    metrics.record_activity(101)
    metrics.record_activity(101)
    metrics.record_activity(102)
    metrics.record_activity(None)
    metrics.record_activity(0)

    assert metrics.total_updates == 5
    # Duplicates collapse, bots/anonymous are ignored
    assert metrics.session_unique_users == 2


@pytest.mark.asyncio
async def test_call_with_retry_waits_short_flood_control() -> None:
    slept: list[float] = []
    calls: list[int] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    original_sleep = asyncio.sleep
    asyncio.sleep = _sleep  # type: ignore[assignment]
    try:

        async def operation() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(
                    method=None,  # type: ignore[arg-type]
                    message="Too Many Requests",
                    retry_after=1,
                )
            return "ok"

        assert await call_with_retry(operation, label="unit") == "ok"
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert slept == [1.0]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_call_with_retry_gives_up_on_long_flood_control() -> None:
    calls: list[int] = []

    async def operation() -> str:
        calls.append(1)
        raise TelegramRetryAfter(
            method=None,  # type: ignore[arg-type]
            message="Too Many Requests",
            retry_after=int(MAX_RETRY_WAIT_SECONDS) + 30,
        )

    with pytest.raises(TelegramRetryAfter):
        await call_with_retry(operation, attempts=3)

    # A 30 s wait is worse than failing: no retry was attempted
    assert calls == [1]


@pytest.mark.asyncio
async def test_call_with_retry_honours_attempt_budget() -> None:
    calls: list[int] = []

    async def operation() -> str:
        calls.append(1)
        raise TelegramRetryAfter(
            method=None,  # type: ignore[arg-type]
            message="Too Many Requests",
            retry_after=1,
        )

    with pytest.raises(TelegramRetryAfter):
        await call_with_retry(operation, attempts=1)
    assert calls == [1]


@pytest.mark.asyncio
async def test_call_with_retry_passes_other_errors_through() -> None:
    async def operation() -> str:
        raise ValueError("not a flood control error")

    with pytest.raises(ValueError, match="not a flood control error"):
        await call_with_retry(operation, attempts=3)


@pytest.mark.asyncio
async def test_call_with_retry_returns_first_success() -> None:
    async def operation() -> int:
        return 42

    assert await call_with_retry(operation) == 42
