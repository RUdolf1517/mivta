"""Bounded in-memory TTL + LRU cache for transient translation results."""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from mivta.translator.base import TranslationRequest, TranslationResult


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    result: TranslationResult


def _make_key(provider: str, request: TranslationRequest) -> bytes:
    """Create a collision-free compact 16-byte key so plaintext inputs aren't duplicated."""
    hasher = hashlib.blake2b(digest_size=16)
    for part in (
        provider,
        request.source_lang,
        request.target_lang,
        request.swap_lang,
        request.style.value,
        request.text,
    ):
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
    return hasher.digest()


class TranslationCache:
    """Memory-bounded LRU cache with strict TTL expiration and zero disk persistence."""

    def __init__(
        self,
        max_size: int = 512,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_size = max(0, max_size)
        self._ttl = max(0.0, ttl_seconds)
        self._clock = clock
        self._entries: OrderedDict[bytes, _CacheEntry] = OrderedDict()
        self._hits: int = 0
        self._misses: int = 0

    @property
    def enabled(self) -> bool:
        return self._max_size > 0 and self._ttl > 0.0

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def __len__(self) -> int:
        self.purge_expired()
        return len(self._entries)

    def get(self, provider: str, request: TranslationRequest) -> TranslationResult | None:
        """Retrieve a valid cached translation or None if missing/expired."""
        if not self.enabled:
            self._misses += 1
            return None
        now = self._clock()
        key = _make_key(provider, request)
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.expires_at <= now:
            self._entries.pop(key, None)
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return entry.result

    def set(
        self,
        provider: str,
        request: TranslationRequest,
        result: TranslationResult,
    ) -> None:
        """Store a translation result until TTL expires."""
        if not self.enabled:
            return
        self.purge_expired()
        now = self._clock()
        key = _make_key(provider, request)
        if key in self._entries:
            self._entries.move_to_end(key)
        elif len(self._entries) >= self._max_size:
            self._entries.popitem(last=False)
        self._entries[key] = _CacheEntry(expires_at=now + self._ttl, result=result)

    def purge_expired(self) -> int:
        """Actively remove all expired entries so translations are not retained past TTL."""
        if not self._entries:
            return 0
        now = self._clock()
        expired_keys = [k for k, v in self._entries.items() if v.expires_at <= now]
        for key in expired_keys:
            self._entries.pop(key, None)
        return len(expired_keys)

    def clear(self) -> None:
        """Drop all cached translations immediately."""
        self._entries.clear()
