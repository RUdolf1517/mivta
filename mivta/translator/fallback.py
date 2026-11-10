"""Resilient fallback chain wrapper over multiple Translator backends."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from mivta.translator.base import (
    InvalidTranslationRequestError,
    TextTooLongError,
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorError,
)

logger = logging.getLogger(__name__)


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
    """Tries translators in order, falling back immediately on upstream failure."""

    def __init__(self, providers: Sequence[Translator]) -> None:
        if not providers:
            raise ValueError("FallbackTranslator requires at least one provider")
        self._providers = tuple(providers)
        self.name = "->".join(p.name for p in self._providers)
        self.supports_styles = self._providers[0].supports_styles
        self.uses_llm_glossary = all(p.uses_llm_glossary for p in self._providers)

    @property
    def providers(self) -> tuple[Translator, ...]:
        return self._providers

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        last_exc: TranslatorError | None = None
        total = len(self._providers)
        for idx, provider in enumerate(self._providers):
            try:
                return await provider.translate(request)
            except TranslatorError as exc:
                if _is_local_validation_error(exc, request) or idx == total - 1:
                    raise
                next_provider = self._providers[idx + 1]
                logger.warning(
                    "Translator %s failed (%s: %s); falling back to %s",
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
        last_exc: TranslatorError | None = None
        total = len(self._providers)
        for idx, provider in enumerate(self._providers):
            try:
                return await provider.translate_styles(request, styles)
            except TranslatorError as exc:
                if _is_local_validation_error(exc, request) or idx == total - 1:
                    raise
                next_provider = self._providers[idx + 1]
                logger.warning(
                    "Translator %s multi-style failed (%s: %s); falling back to %s",
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
