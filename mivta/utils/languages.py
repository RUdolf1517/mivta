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


def language_label(code: str) -> str:
    """Human-readable label for settings UI (e.g. 'English (en)' or 'Авто (auto)')."""
    if code == AUTO_LANG:
        return "Авто (auto)"
    info = LANGUAGE_BY_CODE.get(code)
    if info is None:
        return code
    return f"{info.russian_name} ({info.code})"


def language_english_name(code: str) -> str:
    """English language name for LLM translation prompts."""
    info = LANGUAGE_BY_CODE.get(code)
    return info.english_name if info is not None else code
