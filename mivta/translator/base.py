"""Provider-agnostic translation types, interface, and errors."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum


class TranslationStyle(StrEnum):
    """Supported translation tone/register styles."""

    NEUTRAL = "neutral"
    CASUAL = "casual"
    FORMAL = "formal"


STYLE_LABELS_RU: dict[TranslationStyle, str] = {
    TranslationStyle.NEUTRAL: "нейтральный",
    TranslationStyle.CASUAL: "разговорный",
    TranslationStyle.FORMAL: "формальный",
}

# OpenAI-compatible LLM presets: provider -> (default_base_url, default_model)
LLM_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
}


def parse_style(value: str) -> TranslationStyle | None:
    """Parse a style identifier or localized alias into TranslationStyle."""
    normalized = value.strip().lower()
    aliases: dict[str, TranslationStyle] = {
        "neutral": TranslationStyle.NEUTRAL,
        "нейтральный": TranslationStyle.NEUTRAL,
        "нейтр": TranslationStyle.NEUTRAL,
        "std": TranslationStyle.NEUTRAL,
        "casual": TranslationStyle.CASUAL,
        "informal": TranslationStyle.CASUAL,
        "разговорный": TranslationStyle.CASUAL,
        "разг": TranslationStyle.CASUAL,
        "formal": TranslationStyle.FORMAL,
        "формальный": TranslationStyle.FORMAL,
        "форм": TranslationStyle.FORMAL,
    }
    return aliases.get(normalized)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Per-provider settings for single-provider or multi-API failover chains."""

    provider: str
    api_key: str = ""
    api_url: str = ""
    model: str = ""
    email: str = ""


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """Input contract for any translation provider."""

    text: str
    target_lang: str
    source_lang: str = "auto"
    style: TranslationStyle = TranslationStyle.NEUTRAL
    swap_lang: str = ""


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Output contract returned by any translation provider."""

    translated_text: str
    source_lang: str
    target_lang: str
    style: TranslationStyle
    provider: str


class TranslatorError(Exception):
    """Base error for all translator failures."""


class InvalidTranslationRequestError(TranslatorError):
    """Raised when the translation request itself is invalid."""


class TextTooLongError(InvalidTranslationRequestError):
    """Raised when input text exceeds the configured maximum length."""


class UnsupportedLanguageError(InvalidTranslationRequestError):
    """Raised when a language code is invalid or unsupported by the provider."""


class TranslatorTimeoutError(TranslatorError):
    """Raised when the upstream translation API times out."""


class TranslatorRateLimitError(TranslatorError):
    """Raised when the upstream translation API rejects due to rate/quota limits."""


class TranslatorAuthError(TranslatorError):
    """Raised when upstream API credentials are missing or rejected."""


class TranslatorUnavailableError(TranslatorError):
    """Raised when the upstream translation API fails or returns malformed data."""


def deduplicate_styles(styles: Sequence[TranslationStyle]) -> list[TranslationStyle]:
    """Preserve order while removing duplicate TranslationStyle items."""
    seen: set[TranslationStyle] = set()
    ordered: list[TranslationStyle] = []
    for style in styles:
        if style not in seen:
            seen.add(style)
            ordered.append(style)
    return ordered


class Translator(ABC):
    """Abstract asynchronous translator independent of Telegram."""

    name: str = "base"
    supports_styles: bool = False
    uses_llm_glossary: bool = False

    @abstractmethod
    async def translate(self, request: TranslationRequest) -> TranslationResult:
        """Translate text according to request parameters."""

    async def translate_styles(
        self,
        request: TranslationRequest,
        styles: Sequence[TranslationStyle],
    ) -> list[TranslationResult]:
        """Translate text across multiple tone registers (concurrently or batched)."""
        ordered = deduplicate_styles(styles) or [request.style]
        if not self.supports_styles or len(ordered) == 1:
            single = await self.translate(replace(request, style=ordered[0]))
            return [single]
        results = await asyncio.gather(
            *(self.translate(replace(request, style=st)) for st in ordered)
        )
        return list(results)

    async def aclose(self) -> None:
        """Release any resources owned exclusively by the translator."""
        return None
