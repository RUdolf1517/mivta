"""Resilient fallback chain with per-provider circuit breaker / cooldown."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from mivta.translator.base import (
    InvalidTranslationRequestError,
    TextTooLongError,
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorAuthError,
    TranslatorError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    TranslatorUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_UNAVAILABLE_COOLDOWN = 30.0
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0
DEFAULT_AUTH_COOLDOWN = 300.0


def _is_local_validation_error(exc: TranslatorError, request: TranslationRequest) -> bool:
    if isinstance(exc, TextTooLongError):
        return True
    if isinstance(exc, InvalidTranslationRequestError):
        if not request.text or not request.text.strip():
            return True
        if not request.target_lang or request.target_lang == "auto":
            return True
        if not request.source_lang:
            return True
    return False


class FallbackTranslator(Translator):
    """Tries translators in priority order with automatic per-provider cooldown.

    When an upstream provider times out, hits HTTP 429 quota limits, or returns 5xx/401,
    it is temporarily placed on cooldown so subsequent requests route straight to a
    healthy backup in 0ms. Once the cooldown expires, the primary provider is
    automatically probed again and restored on success.
    """

    def __init__(
        self,
        providers: Sequence[Translator],
        *,
        unavailable_cooldown: float = DEFAULT_UNAVAILABLE_COOLDOWN,
        rate_limit_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN,
        auth_cooldown: float = DEFAULT_AUTH_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("FallbackTranslator requires at least one provider")
        self._providers = tuple(providers)
        self.name = "->".join(p.name for p in self._providers)
        self.supports_styles = self._providers[0].supports_styles
        self.uses_llm_glossary = all(p.uses_llm_glossary for p in self._providers)

        self._unavailable_cooldown = max(0.0, unavailable_cooldown)
        self._rate_limit_cooldown = max(0.0, rate_limit_cooldown)
        self._auth_cooldown = max(0.0, auth_cooldown)
        self._clock = clock
        # Maps provider index -> monotonic timestamp until which it is cooling down
        self._cooldown_until: dict[int, float] = {}

    @property
    def providers(self) -> tuple[Translator, ...]:
        return self._providers

    def is_cooling_down(self, provider_name: str) -> bool:
        """Return True if the named provider is currently in its failure cooldown window."""
        now = self._clock()
        for idx, prov in enumerate(self._providers):
            if prov.name == provider_name:
                return self._cooldown_until.get(idx, 0.0) > now
        return False

    def reset_cooldowns(self) -> None:
        """Clear all active provider cooldowns immediately."""
        self._cooldown_until.clear()

    def _cooldown_duration_for(self, exc: TranslatorError) -> float:
        if isinstance(exc, TranslatorRateLimitError):
            return self._rate_limit_cooldown
        if isinstance(exc, TranslatorAuthError):
            return self._auth_cooldown
        if isinstance(exc, (TranslatorTimeoutError, TranslatorUnavailableError)):
            return self._unavailable_cooldown
        return 0.0

    def _record_failure(self, idx: int, exc: TranslatorError) -> None:
        duration = self._cooldown_duration_for(exc)
        if duration > 0.0:
            self._cooldown_until[idx] = self._clock() + duration

    def _record_success(self, idx: int) -> None:
        if idx in self._cooldown_until:
            del self._cooldown_until[idx]
            logger.info("Translator %s recovered and is healthy again", self._providers[idx].name)

    def _ordered_candidates(self) -> list[tuple[int, Translator]]:
        """Return ready providers first (by priority), then cooling-down ones last."""
        now = self._clock()
        ready: list[tuple[int, Translator]] = []
        cooling: list[tuple[float, int, Translator]] = []
        for idx, prov in enumerate(self._providers):
            until = self._cooldown_until.get(idx, 0.0)
            if until > now:
                cooling.append((until, idx, prov))
            else:
                if idx in self._cooldown_until:
                    del self._cooldown_until[idx]
                ready.append((idx, prov))
        cooling.sort(key=lambda item: (item[0], item[1]))
        return ready + [(idx, prov) for _, idx, prov in cooling]

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        candidates = self._ordered_candidates()
        total = len(candidates)
        last_exc: TranslatorError | None = None
        for pos, (orig_idx, provider) in enumerate(candidates):
            try:
                result = await provider.translate(request)
                self._record_success(orig_idx)
                return result
            except TranslatorError as exc:
                if _is_local_validation_error(exc, request):
                    raise
                self._record_failure(orig_idx, exc)
                if pos == total - 1:
                    raise
                next_provider = candidates[pos + 1][1]
                logger.warning(
                    "Translator %s failed (%s: %s); switching to %s",
                    provider.name,
                    type(exc).__name__,
                    exc,
                    next_provider.name,
                )
                last_exc = exc
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    async def translate_styles(
        self,
        request: TranslationRequest,
        styles: Sequence[TranslationStyle],
    ) -> list[TranslationResult]:
        candidates = self._ordered_candidates()
        total = len(candidates)
        last_exc: TranslatorError | None = None
        for pos, (orig_idx, provider) in enumerate(candidates):
            try:
                results = await provider.translate_styles(request, styles)
                self._record_success(orig_idx)
                return results
            except TranslatorError as exc:
                if _is_local_validation_error(exc, request):
                    raise
                self._record_failure(orig_idx, exc)
                if pos == total - 1:
                    raise
                next_provider = candidates[pos + 1][1]
                logger.warning(
                    "Translator %s multi-style failed (%s: %s); switching to %s",
                    provider.name,
                    type(exc).__name__,
                    exc,
                    next_provider.name,
                )
                last_exc = exc
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()
