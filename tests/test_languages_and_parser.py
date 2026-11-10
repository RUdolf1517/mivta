"""Tests for language normalization and inline query parsing across scripts."""

import pytest

from mivta.translator.base import TranslationStyle
from mivta.utils.languages import normalize_detected_code, resolve_language
from mivta.utils.parser import (
    has_translatable_text,
    parse_inline_query,
    sanitize_text,
    truncate_preview,
)


def test_resolve_language_codes_and_aliases() -> None:
    assert resolve_language("en") == "en"
    assert resolve_language("English") == "en"
    assert resolve_language("Английский") == "en"
    assert resolve_language("немецкий") == "de"
    assert resolve_language("עברית") == "he"
    assert resolve_language("iw") == "he"
    assert resolve_language("zh-CN") == "zh"
    assert resolve_language("auto", allow_auto=False) is None
    assert resolve_language("auto", allow_auto=True) == "auto"
    assert resolve_language("авто", allow_auto=True) == "auto"
    assert resolve_language("zzz", allow_auto=True) is None


def test_normalize_detected_code() -> None:
    assert normalize_detected_code("iw") == "he"
    assert normalize_detected_code("zh-CN") == "zh"
    assert normalize_detected_code("EN-US") == "en"
    assert normalize_detected_code(None) == "auto"


def test_parse_inline_query_plain_and_unicode() -> None:
    parsed = parse_inline_query("Привет, как дела? 🔥")
    assert parsed.text == "Привет, как дела? 🔥"
    assert parsed.source_lang is None
    assert parsed.target_lang is None
    assert parsed.style is None
    assert parsed.error is None


def test_parse_inline_query_multiline_and_punctuation_preserved() -> None:
    raw = "Первая строка!\r\n— Вторая строка... А как же <теги> & символы?"
    parsed = parse_inline_query(raw)
    assert parsed.text == "Первая строка!\n— Вторая строка... А как же <теги> & символы?"


def test_parse_inline_query_target_and_pair_directives() -> None:
    p1 = parse_inline_query(">de Привет, мир!")
    assert p1.target_lang == "de"
    assert p1.source_lang is None
    assert p1.text == "Привет, мир!"

    p2 = parse_inline_query("ru>pl !formal Добрый день, коллеги.")
    assert p2.source_lang == "ru"
    assert p2.target_lang == "pl"
    assert p2.style == TranslationStyle.FORMAL
    assert p2.text == "Добрый день, коллеги."

    p3 = parse_inline_query("!casual auto>es Ну чё, погнали?")
    assert p3.source_lang == "auto"
    assert p3.target_lang == "es"
    assert p3.style == TranslationStyle.CASUAL
    assert p3.text == "Ну чё, погнали?"


def test_parse_inline_query_does_not_eat_normal_punctuation() -> None:
    # Spaces around `>` (math comparison) must remain literal text
    math_q = parse_inline_query("a > b and x < y")
    assert math_q.target_lang is None
    assert math_q.text == "a > b and x < y"

    # Markdown quote `> text` has space after `>`, must remain literal
    quote_q = parse_inline_query("> цитата сообщения")
    assert quote_q.target_lang is None
    assert quote_q.text == "> цитата сообщения"

    # Spanish inverted/normal exclamation `!Hola!` must remain literal
    excl_q = parse_inline_query("!Hola amigo!")
    assert excl_q.style is None
    assert excl_q.text == "!Hola amigo!"


def test_parse_inline_query_unknown_language_reports_error() -> None:
    bad_tgt = parse_inline_query(">zz Привет")
    assert bad_tgt.error is not None
    assert "zz" in bad_tgt.error

    bad_src = parse_inline_query("zz>en Привет")
    assert bad_src.error is not None
    assert "zz" in bad_src.error


def test_empty_and_control_characters() -> None:
    assert sanitize_text("   \x00\x07\x1f  \n\t ") == ""
    empty_q = parse_inline_query("   ")
    assert empty_q.text == ""
    assert empty_q.error is None

    directive_only = parse_inline_query(">fr   ")
    assert directive_only.target_lang == "fr"
    assert directive_only.text == ""
    assert directive_only.error is None


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("Привет", True),
        ("Hello", True),
        ("שלום", True),
        ("مرحبا", True),
        ("你好", True),
        ("こんにちは", True),
        ("안녕하세요", True),
        ("გამარჯობა", True),
        ("12345 + 67890", False),
        ("🔥🔥🔥 ??? !!!", False),
    ],
)
def test_has_translatable_text(sample: str, expected: bool) -> None:
    assert has_translatable_text(sample) is expected


def test_truncate_preview() -> None:
    assert truncate_preview("line 1\nline 2", limit=50) == "line 1 line 2"
    long_text = "x" * 100
    preview = truncate_preview(long_text, limit=20)
    assert len(preview) == 20
    assert preview.endswith("…")
