"""Provider-agnostic translation types, interface, and errors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
class TranslationRequest:
    """Input contract for any translation provider."""

    text: str
    target_lang: str
    source_lang: str = "auto"
    style: TranslationStyle = TranslationStyle.NEUTRAL


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


class Translator(ABC):
    """Abstract asynchronous translator independent of Telegram."""

    name: str = "base"

    @abstractmethod
    async def translate(self, request: TranslationRequest) -> TranslationResult:
        """Translate text according to request parameters."""

    async def aclose(self) -> None:
        """Release any resources owned exclusively by the translator."""
        return None
