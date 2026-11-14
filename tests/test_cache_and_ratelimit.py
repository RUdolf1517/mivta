"""Tests for bounded TTL+LRU cache and per-user token-bucket rate limiter."""

from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _sample_pair(text: str, style: TranslationStyle = TranslationStyle.NEUTRAL):
    req = TranslationRequest(text=text, target_lang="en", source_lang="auto", style=style)
    res = TranslationResult(
        translated_text=f"T:{text}",
        source_lang="ru",
        target_lang="en",
        style=style,
        provider="mock",
    )
    return req, res


def test_cache_hit_miss_style_isolation_and_ttl() -> None:
    clock = FakeClock()
    cache = TranslationCache(max_size=4, ttl_seconds=60.0, clock=clock)

    req_neutral, res_neutral = _sample_pair("Привет", TranslationStyle.NEUTRAL)
    req_formal, _ = _sample_pair("Привет", TranslationStyle.FORMAL)

    assert cache.get("mock", req_neutral) is None
    cache.set("mock", req_neutral, res_neutral)

    assert cache.get("mock", req_neutral) == res_neutral
    # Different style must not collide
    assert cache.get("mock", req_formal) is None

    clock.advance(59.9)
    assert cache.get("mock", req_neutral) == res_neutral

    clock.advance(0.2)
    assert cache.get("mock", req_neutral) is None
    assert len(cache) == 0


def test_cache_lru_eviction_and_active_purge() -> None:
    clock = FakeClock()
    cache = TranslationCache(max_size=2, ttl_seconds=30.0, clock=clock)

    req1, res1 = _sample_pair("one")
    req2, res2 = _sample_pair("two")
    req3, res3 = _sample_pair("three")

    cache.set("mock", req1, res1)
    cache.set("mock", req2, res2)
    # Touch req1 so req2 becomes least recently used
    assert cache.get("mock", req1) == res1

    cache.set("mock", req3, res3)
    assert cache.get("mock", req1) == res1
    assert cache.get("mock", req2) is None
    assert cache.get("mock", req3) == res3

    clock.advance(31.0)
    evicted = cache.purge_expired()
    assert evicted == 2
    assert len(cache) == 0


def test_rate_limiter_burst_refill_and_memory_bound() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_minute=60.0, burst=3, max_users=3, clock=clock)

    # Burst of 3 allowed immediately
    assert limiter.allow(101)[0] is True
    assert limiter.allow(101)[0] is True
    assert limiter.allow(101)[0] is True

    allowed, retry_after = limiter.allow(101)
    assert allowed is False
    assert retry_after >= 0.9

    # Advance 1.5s -> refills 1.5 tokens at 60/min (1 token/sec)
    clock.advance(1.5)
    assert limiter.allow(101)[0] is True
    assert limiter.allow(101)[0] is False

    # Bound tracked users at max_users=3
    for uid in (201, 202, 203, 204):
        assert limiter.allow(uid)[0] is True
    assert limiter.tracked_users() == 3


def test_cache_peek_is_statistics_neutral() -> None:
    clock = FakeClock()
    cache = TranslationCache(max_size=4, ttl_seconds=60.0, clock=clock)
    req, res = _sample_pair("Привет")

    assert cache.peek("mock", req) is False
    assert (cache.hits, cache.misses) == (0, 0)

    cache.set("mock", req, res)
    assert cache.peek("mock", req) is True
    assert cache.peek("mock", _sample_pair("Пока")[0]) is False
    # Warm-checks must not be counted as lookups
    assert (cache.hits, cache.misses) == (0, 0)
    assert cache.get("mock", req) == res
    assert (cache.hits, cache.misses) == (1, 0)


def test_cache_peek_drops_expired_entry() -> None:
    clock = FakeClock()
    cache = TranslationCache(max_size=4, ttl_seconds=30.0, clock=clock)
    req, res = _sample_pair("Привет")
    cache.set("mock", req, res)

    clock.advance(31.0)
    assert cache.peek("mock", req) is False
    assert len(cache) == 0


def test_cache_set_evicts_expired_before_live_entries() -> None:
    clock = FakeClock()
    cache = TranslationCache(max_size=2, ttl_seconds=10.0, clock=clock)
    req1, res1 = _sample_pair("one")
    req2, res2 = _sample_pair("two")
    req3, res3 = _sample_pair("three")

    cache.set("mock", req1, res1)
    clock.advance(11.0)  # req1 is now stale
    cache.set("mock", req2, res2)  # room available, no eviction needed yet
    cache.set("mock", req3, res3)  # full: the stale req1 goes first

    assert cache.get("mock", req3) == res3
    assert cache.get("mock", req2) == res2
    assert cache.get("mock", req1) is None
