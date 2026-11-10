"""UI localization helpers (EN / RU auto-detection from Telegram language_code)."""

from __future__ import annotations

from mivta.translator.base import STYLE_LABELS_RU, TranslationStyle
from mivta.utils.languages import normalize_detected_code, resolve_language

SUPPORTED_UI_LANGS: frozenset[str] = frozenset({"en", "ru"})
_RU_LOCALE_PREFIXES: frozenset[str] = frozenset({"ru", "be", "kk"})

STYLE_LABELS_EN: dict[TranslationStyle, str] = {
    TranslationStyle.NEUTRAL: "neutral",
    TranslationStyle.CASUAL: "casual",
    TranslationStyle.FORMAL: "formal",
}


def parse_ui_lang(raw: str) -> str | None:
    """Parse an explicit UI language ('en', 'ru', or '' for auto), or return None."""
    token = raw.strip().lower()
    if token in ("auto", "default", "off", "reset", "авто", "сброс"):
        return ""
    if token in ("en", "eng", "english", "англ", "английский"):
        return "en"
    if token in ("ru", "rus", "russian", "рус", "русский"):
        return "ru"
    return None


def resolve_ui_lang(
    saved_ui_lang: str,
    telegram_language_code: str | None,
    *,
    default_ui_lang: str = "ru",
) -> str:
    """Determine 'en' or 'ru' UI language from saved override or Telegram client locale."""
    if saved_ui_lang in SUPPORTED_UI_LANGS:
        return saved_ui_lang
    if telegram_language_code:
        base = telegram_language_code.strip().lower().replace("_", "-").split("-", 1)[0]
        if base in _RU_LOCALE_PREFIXES:
            return "ru"
        if base:
            return "en"
    return default_ui_lang if default_ui_lang in SUPPORTED_UI_LANGS else "en"


def style_label(style: TranslationStyle, ui_lang: str = "ru") -> str:
    """Return localized register label ('neutral'/'casual'/'formal' or Russian equivalent)."""
    if ui_lang == "en":
        return STYLE_LABELS_EN[style]
    return STYLE_LABELS_RU[style]


def suggest_swap_lang(telegram_language_code: str | None, target_lang: str) -> str:
    """Suggest a 1-tap auto-swap language from the user's Telegram client locale if distinct."""
    if not telegram_language_code:
        return ""
    norm = normalize_detected_code(telegram_language_code)
    resolved = resolve_language(norm, allow_auto=False)
    if resolved is None or resolved == target_lang:
        return ""
    return resolved
