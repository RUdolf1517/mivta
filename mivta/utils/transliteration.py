"""Latin "reading" (romanization) for translations written in a non-Latin script.

A translation into Chinese, Japanese, Korean, Greek or Cyrillic is useless to
someone who cannot read the script — they cannot pronounce it either. This adds
a second line under the translation with a Latin transcription:

    Привет, как дела?
    Privet, kak dela?

Which languages are covered
---------------------------
Only those where a readable rendering is recoverable **from the text itself**:
Cyrillic, Greek, Devanagari, Georgian, Armenian, Hangul, Han and Kana.

Hebrew, Arabic, Persian and Thai are deliberately absent. Without vowel points
(niqqud / tashkeel) or a dictionary only a consonant skeleton can be produced
(`mrhba bal'alm` for «مرحبا بالعالم»), which is worse than no reading at all,
so the feature simply stays silent for them.

Backends
--------
* `uroman`  — universal romanizer covering Cyrillic, Greek, Devanagari,
  Georgian, Armenian and Hangul.
* `pypinyin` — Chinese Han -> Hanyu Pinyin with tone marks.
* `pykakasi` — Japanese kana/kanji -> Hepburn romaji.

All three are imported lazily: the romanizers are heavy (uroman alone needs
~2.7 s to build its tables) and the feature is off by default, so startup must
not pay for it. `warm_up()` pre-loads them in the background at boot.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

#: Languages whose Latin reading can be derived from the text without a dictionary.
TRANSLITERATION_LANGS: frozenset[str] = frozenset(
    {
        "ru", "uk", "be", "bg", "sr", "kk",  # Cyrillic
        "el",  # Greek
        "hi",  # Devanagari
        "ka",  # Georgian
        "hy",  # Armenian
        "ko",  # Hangul
        "zh",  # Han -> Hanyu Pinyin
        "ja",  # Kana / kanji -> Hepburn
    }
)

MAX_READING_CHARS = 200

_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(
    r"\s+([,.;:!?)\]}>»”’…。、！？；：，）」』】》])"
)
_SPACE_AFTER_OPEN_RE = re.compile(r"([(\[{<«“‘（「『【《])\s+")
# CJK punctuation is not followed by a space (unlike Latin, where pykakasi and
# uroman keep the gap after a full stop).
_SPACE_AFTER_CJK_PUNCT_RE = re.compile(r"([，。、！？；：）」』】》])\s+")


def supports_reading(lang: str | None) -> bool:
    """True when a Latin reading is available for `lang` (`zh-CN` counts as `zh`)."""
    return _base_code(lang) in TRANSLITERATION_LANGS


def _base_code(lang: str | None) -> str:
    return (lang or "").strip().lower().split("-")[0]


@lru_cache(maxsize=1)
def _uroman() -> object | None:
    try:
        import uroman  # noqa: PLC0415 - heavy tables, loaded only when needed
    except ImportError:
        logger.warning("uroman is not installed — Latin readings will be skipped")
        return None
    try:
        return uroman.Uroman()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to initialise uroman")
        return None


@lru_cache(maxsize=1)
def _pinyin_backend() -> tuple[object, object] | None:
    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415 - see above
    except ImportError:
        logger.warning("pypinyin is not installed — Chinese readings will be skipped")
        return None
    return lazy_pinyin, Style


@lru_cache(maxsize=1)
def _kakasi_backend() -> object | None:
    try:
        import pykakasi  # noqa: PLC0415 - see above
    except ImportError:
        logger.warning("pykakasi is not installed — Japanese readings will be skipped")
        return None
    try:
        return pykakasi.Kakasi()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to initialise pykakasi")
        return None


def warm_up() -> None:
    """Pre-load the romanizers so the first user does not pay the import cost."""
    _uroman()
    _pinyin_backend()
    _kakasi_backend()


def _pinyin(text: str) -> str:
    backend = _pinyin_backend()
    if backend is None:
        return ""
    lazy_pinyin, style = backend
    try:
        return " ".join(lazy_pinyin(text, style=style.TONE))
    except Exception:  # pragma: no cover - defensive
        logger.exception("pypinyin failed")
        return ""


def _romaji(text: str) -> str:
    kakasi = _kakasi_backend()
    if kakasi is None:
        return ""
    try:
        # pykakasi already splits the text into meaningful segments, so joining
        # them with spaces gives readable word boundaries.
        return " ".join(seg.get("hepburn", "") for seg in kakasi.convert(text))
    except Exception:  # pragma: no cover - defensive
        logger.exception("pykakasi failed")
        return ""


def _uroman_text(text: str, lcode: str) -> str:
    romanizer = _uroman()
    if romanizer is None:
        return ""
    try:
        return str(romanizer.romanize_string(text, lcode=lcode))
    except TypeError:  # pragma: no cover - older uroman without lcode
        return str(romanizer.romanize_string(text))
    except Exception:  # pragma: no cover - defensive
        logger.exception("uroman failed")
        return ""


def _tidy(value: str) -> str:
    """Normalise whitespace, keep punctuation glued to words, cap the length."""
    out = _WS_RE.sub(" ", value or "").strip()
    if not out:
        return ""
    out = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", out)
    out = _SPACE_AFTER_OPEN_RE.sub(r"\1", out)
    out = _SPACE_AFTER_CJK_PUNCT_RE.sub(r"\1", out)
    out = out.strip()

    if len(out) > MAX_READING_CHARS:
        cut = out[:MAX_READING_CHARS]
        boundary = cut.rfind(" ")
        out = (cut[:boundary] if boundary > MAX_READING_CHARS // 2 else cut).rstrip() + "…"

    # A reading without Latin letters carries no information (punctuation, emoji)
    if sum(1 for ch in out if ch.isascii() and ch.isalpha()) < 2:
        return ""
    return out


def transliterate(text: str, lang: str | None) -> str:
    """Latin reading of `text`, or an empty string when it is not available."""
    if not text:
        return ""
    code = _base_code(lang)
    if code not in TRANSLITERATION_LANGS:
        return ""
    if code == "zh":
        return _tidy(_pinyin(text))
    if code == "ja":
        return _tidy(_romaji(text))
    return _tidy(_uroman_text(text, code))


def reading_enabled(user_flag: bool | None, default: bool) -> bool:
    """Resolve the per-user toggle against the operator-level default."""
    return default if user_flag is None else user_flag


def with_reading(
    text: str,
    lang: str | None,
    *,
    enabled: bool,
    max_total: int,
) -> str:
    """Append a Latin reading line under `text`, if it adds information.

    The reading is appended **after** entity offsets have been resolved, so it
    never shifts a `MessageEntity` and carries no entities of its own. When the
    combined text would exceed `max_total` the reading is dropped rather than
    truncated mid-word.
    """
    if not enabled or not text:
        return text
    reading = transliterate(text, lang)
    if not reading or reading.casefold() == text.casefold():
        return text
    combined = f"{text}\n{reading}"
    return combined if len(combined) <= max_total else text
