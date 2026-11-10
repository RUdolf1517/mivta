"""Language registry, normalization, and lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass

AUTO_LANG = "auto"


@dataclass(frozen=True, slots=True)
class LanguageInfo:
    """Metadata for a supported language."""

    code: str
    english_name: str
    russian_name: str
    native_name: str


# Curated set of widely used languages across scripts (Latin, Cyrillic, CJK, RTL, etc.)
LANGUAGES: tuple[LanguageInfo, ...] = (
    LanguageInfo("en", "English", "Английский", "English"),
    LanguageInfo("ru", "Russian", "Русский", "Русский"),
    LanguageInfo("de", "German", "Немецкий", "Deutsch"),
    LanguageInfo("fr", "French", "Французский", "Français"),
    LanguageInfo("es", "Spanish", "Испанский", "Español"),
    LanguageInfo("it", "Italian", "Итальянский", "Italiano"),
    LanguageInfo("pt", "Portuguese", "Португальский", "Português"),
    LanguageInfo("pl", "Polish", "Польский", "Polski"),
    LanguageInfo("uk", "Ukrainian", "Украинский", "Українська"),
    LanguageInfo("be", "Belarusian", "Белорусский", "Беларуская"),
    LanguageInfo("cs", "Czech", "Чешский", "Čeština"),
    LanguageInfo("nl", "Dutch", "Нидерландский", "Nederlands"),
    LanguageInfo("sv", "Swedish", "Шведский", "Svenska"),
    LanguageInfo("tr", "Turkish", "Турецкий", "Türkçe"),
    LanguageInfo("he", "Hebrew", "Иврит", "עברית"),
    LanguageInfo("ar", "Arabic", "Арабский", "العربية"),
    LanguageInfo("zh", "Chinese", "Китайский", "中文"),
    LanguageInfo("ja", "Japanese", "Японский", "日本語"),
    LanguageInfo("ko", "Korean", "Корейский", "한국어"),
    LanguageInfo("hi", "Hindi", "Хинди", "हिन्दी"),
    LanguageInfo("fa", "Persian", "Персидский", "فارسی"),
    LanguageInfo("vi", "Vietnamese", "Вьетнамский", "Tiếng Việt"),
    LanguageInfo("id", "Indonesian", "Индонезийский", "Indonesia"),
    LanguageInfo("th", "Thai", "Тайский", "ไทย"),
    LanguageInfo("ka", "Georgian", "Грузинский", "ქართული"),
    LanguageInfo("hy", "Armenian", "Армянский", "Հայերեն"),
    LanguageInfo("kk", "Kazakh", "Казахский", "Қазақша"),
    LanguageInfo("uz", "Uzbek", "Узбекский", "Oʻzbekcha"),
    LanguageInfo("az", "Azerbaijani", "Азербайджанский", "Azərbaycanca"),
    LanguageInfo("el", "Greek", "Греческий", "Ελληνικά"),
    LanguageInfo("ro", "Romanian", "Румынский", "Română"),
    LanguageInfo("hu", "Hungarian", "Венгерский", "Magyar"),
    LanguageInfo("fi", "Finnish", "Финский", "Suomi"),
    LanguageInfo("da", "Danish", "Датский", "Dansk"),
    LanguageInfo("no", "Norwegian", "Норвежский", "Norsk"),
    LanguageInfo("bg", "Bulgarian", "Болгарский", "Български"),
    LanguageInfo("sr", "Serbian", "Сербский", "Српски"),
    LanguageInfo("hr", "Croatian", "Хорватский", "Hrvatski"),
    LanguageInfo("sk", "Slovak", "Словацкий", "Slovenčina"),
    LanguageInfo("lt", "Lithuanian", "Литовский", "Lietuvių"),
    LanguageInfo("lv", "Latvian", "Латышский", "Latviešu"),
    LanguageInfo("et", "Estonian", "Эстонский", "Eesti"),
)

LANGUAGE_BY_CODE: dict[str, LanguageInfo] = {lang.code: lang for lang in LANGUAGES}

# Quick-pick languages shown in interactive settings keyboards (page 1 & page 2)
QUICK_LANGUAGES_PRIMARY: tuple[str, ...] = (
    "en",
    "ru",
    "de",
    "fr",
    "es",
    "it",
    "pl",
    "uk",
    "zh",
    "ja",
    "he",
    "tr",
)

QUICK_LANGUAGES_SECONDARY: tuple[str, ...] = (
    "pt",
    "ko",
    "ar",
    "cs",
    "nl",
    "sv",
    "ka",
    "hy",
    "kk",
    "el",
    "fi",
    "hi",
)

# Legacy / regional / colloquial code aliases mapped to canonical codes
_CODE_ALIASES: dict[str, str] = {
    "iw": "he",
    "ji": "yi",
    "in": "id",
    "jw": "jv",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-tw": "zh",
    "zh-hant": "zh",
    "pt-br": "pt",
    "pt-pt": "pt",
    "en-us": "en",
    "en-gb": "en",
    "nb": "no",
    "nn": "no",
    "ua": "uk",  # Common country-code confusion (UA -> uk)
    "jp": "ja",  # Common country-code confusion (JP -> ja)
    "kr": "ko",  # Common country-code confusion (KR -> ko)
    "cn": "zh",  # Common country-code confusion (CN -> zh)
    "cz": "cs",  # Common country-code alias (CZ -> cs)
    "gr": "el",  # Common country-code alias (GR -> el)
}

_NAME_LOOKUP: dict[str, str] = {}
for _info in LANGUAGES:
    _NAME_LOOKUP[_info.code] = _info.code
    _NAME_LOOKUP[_info.english_name.lower()] = _info.code
    _NAME_LOOKUP[_info.russian_name.lower()] = _info.code
    _NAME_LOOKUP[_info.native_name.lower()] = _info.code
_NAME_LOOKUP.update(_CODE_ALIASES)

_AUTO_ALIASES: frozenset[str] = frozenset({"auto", "авто", "autodetect", "detect"})


def resolve_language(raw: str, *, allow_auto: bool = False) -> str | None:
    """Resolve an ISO code, alias, or language name to a canonical code, or None if unknown."""
    key = raw.strip().lower().replace("_", "-")
    if not key:
        return None
    if allow_auto and key in _AUTO_ALIASES:
        return AUTO_LANG
    code = _NAME_LOOKUP.get(key)
    if code in LANGUAGE_BY_CODE:
        return code
    return None


def normalize_detected_code(raw: str | None) -> str:
    """Normalize a provider-returned source language code for display."""
    if not raw:
        return AUTO_LANG
    key = raw.strip().lower().replace("_", "-")
    if key in _CODE_ALIASES:
        return _CODE_ALIASES[key]
    if "-" in key:
        base = key.split("-", 1)[0]
        if base in _CODE_ALIASES:
            return _CODE_ALIASES[base]
        if base in LANGUAGE_BY_CODE:
            return base
    return key


def language_label(code: str, *, ui_lang: str = "ru") -> str:
    """Human-readable label for settings UI in Russian or English."""
    is_en = ui_lang == "en"
    if code == AUTO_LANG:
        return "Auto (auto)" if is_en else "Авто (auto)"
    info = LANGUAGE_BY_CODE.get(code)
    if info is None:
        return code
    display = info.english_name if is_en else info.russian_name
    return f"{display} ({info.code})"


def language_name(code: str, *, ui_lang: str = "ru") -> str:
    """Short localized language name without code suffix."""
    if code == AUTO_LANG:
        return "Auto" if ui_lang == "en" else "Авто"
    info = LANGUAGE_BY_CODE.get(code)
    if info is None:
        return code
    return info.english_name if ui_lang == "en" else info.russian_name


def language_english_name(code: str) -> str:
    """English language name for LLM translation prompts."""
    info = LANGUAGE_BY_CODE.get(code)
    return info.english_name if info is not None else code


_LANGUAGE_SCRIPTS: dict[str, str] = {
    "en": "latin",
    "de": "latin",
    "fr": "latin",
    "es": "latin",
    "it": "latin",
    "pt": "latin",
    "pl": "latin",
    "cs": "latin",
    "nl": "latin",
    "sv": "latin",
    "tr": "latin",
    "vi": "latin",
    "id": "latin",
    "az": "latin",
    "ro": "latin",
    "hu": "latin",
    "fi": "latin",
    "da": "latin",
    "no": "latin",
    "hr": "latin",
    "sk": "latin",
    "lt": "latin",
    "lv": "latin",
    "et": "latin",
    "ru": "cyrillic",
    "uk": "cyrillic",
    "be": "cyrillic",
    "kk": "cyrillic",
    "bg": "cyrillic",
    "he": "hebrew",
    "ar": "arabic",
    "fa": "arabic",
    "zh": "cjk_kana",
    "ja": "cjk_kana",
    "ko": "hangul",
    "hi": "devanagari",
    "th": "thai",
    "ka": "georgian",
    "hy": "armenian",
    "el": "greek",
}


def language_script(code: str) -> str | None:
    """Return the primary script family for code, or None for multi-script languages."""
    return _LANGUAGE_SCRIPTS.get(code)


def _char_script(cp: int) -> str:
    if (0x0041 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF):
        return "latin"
    if 0x0400 <= cp <= 0x052F:
        return "cyrillic"
    if 0x0590 <= cp <= 0x05FF:
        return "hebrew"
    if (
        (0x0600 <= cp <= 0x06FF)
        or (0x0750 <= cp <= 0x077F)
        or (0xFB50 <= cp <= 0xFDFF)
        or (0xFE70 <= cp <= 0xFEFF)
    ):
        return "arabic"
    if (
        (0x3040 <= cp <= 0x30FF)
        or (0x3400 <= cp <= 0x4DBF)
        or (0x4E00 <= cp <= 0x9FFF)
        or (0xF900 <= cp <= 0xFAFF)
    ):
        return "cjk_kana"
    if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F):
        return "hangul"
    if (0x0370 <= cp <= 0x03FF) or (0x1F00 <= cp <= 0x1FFF):
        return "greek"
    if (0x10A0 <= cp <= 0x10FF) or (0x2D00 <= cp <= 0x2D2F):
        return "georgian"
    if 0x0530 <= cp <= 0x058F:
        return "armenian"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    return "other"


def detect_single_script(text: str) -> str | None:
    """Return the script family if all alphabetic chars in text share one script, else None."""
    seen: str | None = None
    for ch in text:
        if not ch.isalpha():
            continue
        sc = _char_script(ord(ch))
        if seen is None:
            seen = sc
        elif seen != sc:
            return None
    return seen


def resolve_auto_swap_target(
    probe_text: str,
    *,
    source_lang: str,
    target_lang: str,
    swap_lang: str,
    explicit_override: bool = False,
) -> tuple[str, str]:
    """Resolve `(effective_target, provider_swap_fallback)` for bidirectional auto-swap.

    - Cross-script pairs (e.g. `en` <-> `ru`) flip immediately in 0 extra network calls.
    - Same-script pairs (e.g. `en` <-> `pl`) preserve `provider_swap_fallback = swap_lang`
      so the translator / pipeline swaps accurately using detected source language.
    """
    if (
        explicit_override
        or source_lang != AUTO_LANG
        or not swap_lang
        or swap_lang == target_lang
    ):
        return target_lang, ""

    tgt_script = language_script(target_lang)
    swap_script = language_script(swap_lang)
    if tgt_script is not None and swap_script is not None and tgt_script != swap_script:
        text_script = detect_single_script(probe_text)
        if text_script == tgt_script:
            return swap_lang, ""
        if text_script is not None and text_script != tgt_script:
            return target_lang, ""

    return target_lang, swap_lang
