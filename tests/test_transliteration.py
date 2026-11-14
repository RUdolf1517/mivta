"""Tests for «как читается» — the optional Latin reading line under a translation."""

from __future__ import annotations

import importlib.util

import pytest

from mivta.utils.transliteration import (
    MAX_READING_CHARS,
    TRANSLITERATION_LANGS,
    reading_enabled,
    supports_reading,
    transliterate,
    with_reading,
)

# Languages without vowel points / without a dictionary-driven romanizer: the
# feature must stay silent rather than show a consonant skeleton.
UNSUPPORTED = ("he", "ar", "fa", "th", "en", "de", "fr", "pl")

# The romanizers live in the optional `reading` extra; without them the feature
# degrades to a no-op and the content-level assertions cannot run.
needs_uroman = pytest.mark.skipif(
    importlib.util.find_spec("uroman") is None,
    reason="optional [reading] extra is not installed",
)
needs_pinyin = pytest.mark.skipif(
    importlib.util.find_spec("pypinyin") is None,
    reason="optional [reading] extra is not installed",
)
needs_kakasi = pytest.mark.skipif(
    importlib.util.find_spec("pykakasi") is None,
    reason="optional [reading] extra is not installed",
)


def test_supported_languages_cover_all_non_latin_scripts_we_can_read() -> None:
    for code in ("ru", "uk", "be", "bg", "sr", "kk", "el", "hi", "ka", "hy", "ko", "zh", "ja"):
        assert supports_reading(code), code


def test_scripts_we_cannot_read_are_absent() -> None:
    for code in UNSUPPORTED:
        assert not supports_reading(code), code


def test_region_subtags_fall_back_to_base_language() -> None:
    assert supports_reading("zh-CN")
    assert supports_reading("zh-TW")
    assert supports_reading("pt-BR") is False


def test_unknown_and_empty_language_is_not_supported() -> None:
    assert not supports_reading("")
    assert not supports_reading(None)
    assert not supports_reading("xx")


@needs_uroman
def test_cyrillic_reading_is_latin_and_keeps_punctuation() -> None:
    reading = transliterate("Привет, как дела?", "ru")
    assert reading
    assert reading.isascii()
    assert "Privet" in reading
    assert reading.endswith("?")


@needs_uroman
def test_greek_georgian_armenian_and_hangul_are_romanized() -> None:
    assert transliterate("Καλημέρα", "el").isascii()
    assert transliterate("გამარჯობა", "ka").isascii()
    assert transliterate("Բարև", "hy").isascii()
    assert transliterate("안녕하세요", "ko").isascii()


@needs_pinyin
def test_chinese_reading_uses_tone_marks() -> None:
    reading = transliterate("你好", "zh")
    assert reading
    # Tone-marked pinyin is non-ASCII by design; it must still be pinyin.
    assert any(ch in reading for ch in "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ")
    assert not any("一" <= ch <= "鿿" for ch in reading)


@needs_kakasi
def test_japanese_reading_is_hepburn_romaji() -> None:
    reading = transliterate("日本語", "ja")
    assert reading
    assert reading.isascii()
    assert "nihongo" in reading


def test_unsupported_language_produces_no_reading() -> None:
    for code in UNSUPPORTED:
        assert transliterate("שלום עולם", code) == ""


def test_empty_input_produces_no_reading() -> None:
    assert transliterate("", "ru") == ""
    assert transliterate("   ", "ru") == ""


def test_reading_is_empty_when_text_has_no_letters() -> None:
    assert transliterate("🙂🙂", "ru") == ""
    assert transliterate("...", "ru") == ""


@needs_uroman
def test_long_text_reading_is_capped_with_ellipsis() -> None:
    reading = transliterate("Привет " * 200, "ru")
    assert len(reading) <= MAX_READING_CHARS + 1
    assert reading.endswith("…")


def test_toggle_resolution_prefers_explicit_user_choice() -> None:
    assert reading_enabled(None, False) is False
    assert reading_enabled(None, True) is True
    assert reading_enabled(False, True) is False
    assert reading_enabled(True, False) is True


@needs_uroman
def test_with_reading_appends_a_second_line() -> None:
    out = with_reading("Привет", "ru", enabled=True, max_total=4096)
    assert out.startswith("Привет\n")
    assert out.splitlines()[1].isascii()


def test_with_reading_is_a_noop_when_disabled() -> None:
    assert with_reading("Привет", "ru", enabled=False, max_total=4096) == "Привет"


def test_with_reading_skips_latin_targets() -> None:
    assert with_reading("Hello", "en", enabled=True, max_total=4096) == "Hello"


@needs_uroman
def test_with_reading_does_not_truncate_the_translation() -> None:
    text = "Приветик"
    # Not enough room for the reading — the translation must survive untouched.
    assert with_reading(text, "ru", enabled=True, max_total=len(text) + 2) == text
    assert with_reading(text, "ru", enabled=True, max_total=4096) != text


def test_with_reading_ignores_empty_text() -> None:
    assert with_reading("", "ru", enabled=True, max_total=4096) == ""


def test_reading_never_repeats_the_source_text() -> None:
    # «Hello» translated into a Latin script has no useful reading.
    assert with_reading("Hello", "ru", enabled=True, max_total=4096) == "Hello"


def test_supported_set_is_frozen_and_documented_size() -> None:
    assert isinstance(TRANSLITERATION_LANGS, frozenset)
    assert len(TRANSLITERATION_LANGS) == 13
