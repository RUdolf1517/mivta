"""Property-based invariants of the text pipeline, checked against seeded random input.

These are deterministic (fixed seed) so CI stays reproducible, and they cover the
promises that are easy to break while optimising:

* `restore(prepare(x)) == x` — masking and unmasking never lose a character;
* `join(split(x)) == x`       — chunking never loses a character;
* `U16OffsetMap` round-trips every Python index and rejects mid-surrogate offsets.

The only intentional exception is markdown: with `parse_inline_code_markdown`
enabled, a *pair* of backticks is consumed as an inline-code delimiter, so texts
containing backticks are excluded from the round-trip property for that mode.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest
from aiogram.types import MessageEntity

from mivta.utils.formatting import (
    join_translated_chunks,
    prepare_text_for_translation,
    restore_formatted_text,
    split_for_translation,
)
from mivta.utils.utf16 import U16OffsetMap, build_utf16_offsets, utf16_len

SEED = 20241110
ROUNDS = 600

# Cyrillic, astral chars, whitespace, URLs, mentions, quotes and Telegram-ish words —
# but no backticks: see the module docstring.
_ALPHABET = "abc абв 🙂🎉🇵🇱\n\t .,!\"'<>https://x.y/z @mivta mivta Telegram слова"
_ALPHABET_WITH_BACKTICK = _ALPHABET + "`"

_ENTITY_TYPES = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "code",
    "pre",
    "url",
    "mention",
    "email",
    "text_link",
    "spoiler",
)


def _random_text(rng: random.Random, max_len: int = 200) -> str:
    return "".join(
        rng.choice(_ALPHABET if rng.random() < 0.5 else _ALPHABET_WITH_BACKTICK)
        for _ in range(rng.randint(1, max_len))
    )


def _random_entities(rng: random.Random, text: str) -> list[MessageEntity]:
    entities: list[MessageEntity] = []
    for _ in range(rng.randint(0, 4)):
        start = rng.randrange(0, max(1, len(text)))
        length = rng.randint(0, max(1, len(text) - start))
        entities.append(
            MessageEntity(type=rng.choice(_ENTITY_TYPES), offset=start, length=length)
        )
    return entities


@pytest.fixture()
def rng() -> Iterator[random.Random]:
    yield random.Random(SEED)


@pytest.mark.parametrize("parse_markdown", [True, False])
def test_prepare_restore_roundtrip_is_lossless(
    rng: random.Random, parse_markdown: bool
) -> None:
    """Masking text for a provider and restoring the untouched result returns the input."""
    for _ in range(ROUNDS):
        text = _random_text(rng)
        if parse_markdown and "`" in text:
            continue  # backtick pairs are consumed as inline-code delimiters by design
        entities = _random_entities(rng, text)
        prepared = prepare_text_for_translation(
            text,
            entities=entities,
            glossary=rng.choice([(), ("mivta",), ("mivta", "Telegram")]),
            protect_glossary_tokens=rng.choice([True, False]),
            parse_inline_code_markdown=parse_markdown,
        )
        restored = restore_formatted_text(
            prepared.encoded_text, prepared, max_length=4096
        )
        assert restored.text == text


def test_prepare_restore_roundtrip_without_backticks(
    rng: random.Random,
) -> None:
    """Same invariant, restricted so the markdown path is exercised too."""
    for _ in range(ROUNDS):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 200)))
        prepared = prepare_text_for_translation(
            text,
            entities=_random_entities(rng, text),
            parse_inline_code_markdown=True,
        )
        restored = restore_formatted_text(
            prepared.encoded_text, prepared, max_length=4096
        )
        assert restored.text == text


def test_chunking_is_lossless_at_production_widths(rng: random.Random) -> None:
    """Splitting and rejoining never drops or duplicates a character."""
    for _ in range(ROUNDS):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 3000)))
        max_length = rng.choice([50, 100, 200, 500])
        chunks = split_for_translation(text, max_length)
        if chunks.exceeds or not chunks.chunks:
            continue
        assert all(len(chunk) <= max_length for chunk in chunks.chunks)
        assert join_translated_chunks(list(chunks.chunks), chunks.separators) == text


def test_utf16_offset_map_roundtrips_every_index(rng: random.Random) -> None:
    for _ in range(ROUNDS):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 200)))
        offsets = build_utf16_offsets(text)
        u16 = U16OffsetMap(text)

        assert u16.total == utf16_len(text) == offsets[-1]
        for py_index in range(len(text) + 1):
            assert u16.to_utf16(py_index) == offsets[py_index]
            assert u16.to_python(offsets[py_index]) == py_index

        # Offsets outside the text, and offsets landing mid surrogate pair, are rejected
        assert u16.to_python(-1) is None
        assert u16.to_python(u16.total + 1) is None
        for hole in _mid_surrogate_offsets(offsets):
            assert u16.to_python(hole) is None


def _mid_surrogate_offsets(offsets: list[int]) -> list[int]:
    """UTF-16 offsets that fall on the low surrogate half of a pair."""
    holes: list[int] = []
    for prev, nxt in zip(offsets, offsets[1:], strict=False):
        if nxt - prev == 2:
            holes.append(prev + 1)
    return holes


def test_entities_survive_roundtrip_with_astral_text(rng: random.Random) -> None:
    """Rich entities keep their UTF-16 offsets when the text contains surrogate pairs."""
    checked = 0
    for _ in range(ROUNDS):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 200)))
        if not any(ord(ch) > 0xFFFF for ch in text):
            continue
        entities = [
            MessageEntity(type="bold", offset=0, length=max(1, len(text) // 3)),
            MessageEntity(
                type="italic", offset=max(1, len(text) // 2), length=max(1, len(text) // 4)
            ),
        ]
        prepared = prepare_text_for_translation(text, entities=entities)
        restored = restore_formatted_text(
            prepared.encoded_text, prepared, max_length=4096
        )
        assert restored.text == text
        if restored.entities:
            for ent in restored.entities:
                assert ent.offset >= 0
                assert ent.offset + ent.length <= utf16_len(restored.text)
            checked += 1
    assert checked > 0, "the astral path was never exercised"
