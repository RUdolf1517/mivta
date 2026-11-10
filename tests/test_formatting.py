"""Tests for UTF-16 entity rebasing, formatting, code/URL/@mention, and glossary protection."""

from __future__ import annotations

from aiogram.types import MessageEntity

from mivta.utils.formatting import (
    build_utf16_offsets,
    prepare_text_for_translation,
    restore_formatted_text,
    utf16_len,
)
from mivta.utils.parser import parse_business_query


def _slice_utf16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def test_utf16_surrogate_pair_emoji_offsets() -> None:
    text = "👋 Привет 🇵🇱!"
    # 👋 (2) + " Привет " (8) + 🇵 (2) + 🇱 (2) + "!" (1) = 15 UTF-16 code units
    assert utf16_len(text) == 15
    offsets = build_utf16_offsets(text)
    assert offsets[0] == 0
    assert offsets[1] == 2
    assert offsets[-1] == 15


def test_parse_business_query_rebases_utf16_entities_across_directives_and_emoji() -> None:
    raw = ">de !formal 👋 Важно: запусти git status"
    # Locate "Важно" and "git status" in UTF-16 units on `raw`
    bold_word = "Важно"
    code_word = "git status"
    b_py = raw.index(bold_word)
    c_py = raw.index(code_word)
    raw_u16 = build_utf16_offsets(raw)

    entities = [
        MessageEntity(
            type="bold",
            offset=raw_u16[b_py],
            length=utf16_len(bold_word),
        ),
        MessageEntity(
            type="code",
            offset=raw_u16[c_py],
            length=utf16_len(code_word),
        ),
    ]
    parsed = parse_business_query(raw, entities=entities)
    assert parsed is not None
    assert parsed.target_lang == "de"
    assert parsed.text == "👋 Важно: запусти git status"
    assert len(parsed.entities) == 2

    b_ent, c_ent = parsed.entities
    assert _slice_utf16(parsed.text, b_ent.offset, b_ent.length) == "Важно"
    assert _slice_utf16(parsed.text, c_ent.offset, c_ent.length) == "git status"


def test_preserve_bold_italic_text_link_and_emoji_utf16_after_translation() -> None:
    src = "👋 Привет, дорогой друг! Читай документацию"
    src_u16 = build_utf16_offsets(src)
    friend_start = src.index("дорогой друг")
    docs_start = src.index("документацию")

    entities = [
        MessageEntity(
            type="bold",
            offset=src_u16[friend_start],
            length=utf16_len("дорогой друг"),
        ),
        MessageEntity(
            type="italic",
            offset=src_u16[friend_start],
            length=utf16_len("дорогой друг"),
        ),
        MessageEntity(
            type="text_link",
            offset=src_u16[docs_start],
            length=utf16_len("документацию"),
            url="https://github.com/RUdolf1517/mivta",
        ),
    ]

    prepared = prepare_text_for_translation(src, entities=entities)
    assert prepared.has_xml_style_tags is True
    assert "<s0><s1>дорогой друг</s1></s0>" in prepared.encoded_text
    assert "<s2>документацию</s2>" in prepared.encoded_text

    # Simulate MT output with extra spaces inside tags and guillemet conversion on s2
    mt_output = "👋 Hello, < s0 >< s1 > dear friend < / s1 >< / s0 >! Read «s2»the docs«/s2»"
    restored = restore_formatted_text(mt_output, prepared)

    assert restored.text == "👋 Hello, dear friend! Read the docs"
    assert restored.entities is not None
    assert len(restored.entities) == 3

    by_type = {e.type: e for e in restored.entities}
    bold_e = by_type["bold"]
    italic_e = by_type["italic"]
    assert _slice_utf16(restored.text, bold_e.offset, bold_e.length) == "dear friend"
    assert _slice_utf16(restored.text, italic_e.offset, italic_e.length) == "dear friend"
    link_ent = by_type["text_link"]
    assert _slice_utf16(restored.text, link_ent.offset, link_ent.length) == "the docs"
    assert link_ent.url == "https://github.com/RUdolf1517/mivta"


def test_inline_backtick_code_urls_wikipedia_parens_and_mentions_protected() -> None:
    src = (
        "Проверь `pytest -q` у @rudolf_15 и статью "
        "https://en.wikipedia.org/wiki/Rust_(programming_language)."
    )
    prepared = prepare_text_for_translation(src, parse_inline_code_markdown=True)
    assert "`pytest -q`" not in prepared.encoded_text
    assert "@rudolf_15" not in prepared.encoded_text
    assert "wikipedia.org" not in prepared.encoded_text
    assert prepared.encoded_text.endswith(".")

    simulated = (
        prepared.encoded_text.replace("Проверь", "Check")
        .replace("у", "with")
        .replace("и статью", "and")
    )
    restored = restore_formatted_text(
        simulated,
        prepared,
        emit_entities_only_if_rich=True,
    )
    assert restored.text == (
        "Check pytest -q with @rudolf_15 and "
        "https://en.wikipedia.org/wiki/Rust_(programming_language)."
    )
    assert restored.entities is not None
    slices = {
        e.type: _slice_utf16(restored.text, e.offset, e.length)
        for e in restored.entities
    }
    assert slices["code"] == "pytest -q"
    assert slices["mention"] == "@rudolf_15"
    assert slices["url"] == "https://en.wikipedia.org/wiki/Rust_(programming_language)"


def test_plain_url_without_code_in_inline_mode_keeps_entities_none_for_telegram_autolink() -> None:
    src = "Смотри https://example.com/path?a=1 и пиши dev@example.com"
    prepared = prepare_text_for_translation(src, parse_inline_code_markdown=True)
    simulated = prepared.encoded_text.replace("Смотри", "See").replace("и пиши", "and email")
    restored = restore_formatted_text(simulated, prepared, emit_entities_only_if_rich=True)

    assert restored.text == "See https://example.com/path?a=1 and email dev@example.com"
    assert restored.entities is None


def test_glossary_word_boundary_masking_for_non_llm_providers() -> None:
    src = "Мы пишем mivta на Rust, этому можно trust"
    prepared = prepare_text_for_translation(
        src,
        glossary=("mivta", "Rust"),
        protect_glossary_tokens=True,
    )
    # "mivta" and standalone "Rust" are masked, while "trust" is untouched!
    assert "mivta" not in prepared.encoded_text
    assert "Rust" not in prepared.encoded_text
    assert "trust" in prepared.encoded_text

    simulated = (
        prepared.encoded_text.replace("Мы пишем", "We write")
        .replace("на", "in")
        .replace("этому можно", "you can")
    )
    restored = restore_formatted_text(simulated, prepared)
    assert restored.text == "We write mivta in Rust, you can trust"
