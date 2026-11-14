"""Tests for the shared UTF-16 offset map and the cache/inflight digest helper."""

from mivta.translator.base import TranslationRequest, TranslationStyle
from mivta.utils.digest import translation_digest
from mivta.utils.utf16 import (
    U16OffsetMap,
    build_utf16_offsets,
    has_astral_chars,
    utf16_len,
)

BMP_TEXT = "hello, привет! 42"


def test_utf16_len_matches_telegram_units() -> None:
    assert utf16_len("") == 0
    assert utf16_len("abc") == 3
    assert utf16_len("привет") == 6
    assert utf16_len("🙂") == 2
    assert utf16_len("a🙂b") == 4


def test_has_astral_chars_detects_surrogate_pairs() -> None:
    assert has_astral_chars(BMP_TEXT) is False
    assert has_astral_chars("🙂") is True
    assert has_astral_chars("привет") is False
    assert has_astral_chars("a🇵🇱b") is True


def test_build_utf16_offsets_is_monotonic_and_complete() -> None:
    for text in ("", "abc", "привет", "a🙂b", "🙂🙂", "a🇵🇱b🙂"):
        offsets = build_utf16_offsets(text)
        assert len(offsets) == len(text) + 1
        assert offsets[0] == 0
        assert offsets[-1] == utf16_len(text)
        assert offsets == sorted(offsets)


def test_u16_offset_map_roundtrip_bmp() -> None:
    umap = U16OffsetMap(BMP_TEXT)
    assert umap.total == utf16_len(BMP_TEXT)
    for py_i in range(len(BMP_TEXT) + 1):
        u16 = umap.to_utf16(py_i)
        assert u16 is not None
        assert umap.to_python(u16) == py_i


def test_u16_offset_map_roundtrip_astral() -> None:
    for text in ("🙂abc", "a🙂b🙂c", "🇵🇱 test 🙂", "x" * 5 + "🙂"):
        umap = U16OffsetMap(text)
        assert umap.total == utf16_len(text)
        for py_i in range(len(text) + 1):
            u16 = umap.to_utf16(py_i)
            assert u16 is not None
            assert umap.to_python(u16) == py_i


def test_u16_offset_map_out_of_range_is_none() -> None:
    umap = U16OffsetMap(BMP_TEXT)
    assert umap.to_python(-1) is None
    assert umap.to_python(umap.total + 1) is None


def test_u16_offset_map_rejects_mid_surrogate_offset() -> None:
    """An offset landing on the low surrogate half is not a boundary and yields None.

    Dropping such an entity is safer than slicing the pair in half and emitting
    mojibake — Telegram itself never produces mid-pair entity offsets.
    """
    umap = U16OffsetMap("ab🙂cd")
    pair_start = umap.to_utf16(2)
    assert pair_start == 2
    assert umap.to_python(pair_start) == 2
    assert umap.to_python(pair_start + 1) is None
    # The emoji occupies two units: the next boundary after it is pair_start + 2
    assert umap.to_utf16(3) == pair_start + 2
    assert umap.to_python(pair_start + 2) == 3


def test_translation_digest_is_stable_and_distinct() -> None:
    def req(text: str, target: str = "en", **kw) -> TranslationRequest:
        return TranslationRequest(text=text, target_lang=target, **kw)

    a = translation_digest("google", req("Привет"))
    assert a == translation_digest("google", req("Привет"))
    assert len(a) == 16
    # Provider, target, style and text must all change the key
    assert a != translation_digest("deepl", req("Привет"))
    assert a != translation_digest("google", req("Привет", target="de"))
    assert a != translation_digest("google", req("Пока"))
    assert a != translation_digest(
        "google", req("Привет", style=TranslationStyle.FORMAL)
    )


def test_translation_digest_length_prefix_prevents_concat_collision() -> None:
    def req(text: str) -> TranslationRequest:
        return TranslationRequest(text=text, target_lang="en", swap_lang="de")

    assert translation_digest("google", req("ab")) != translation_digest(
        "google", req("a")
    )
    # Fields are delimited by their length, so ("ab","c") and ("a","bc") differ
    req1 = TranslationRequest(text="ab", target_lang="c", swap_lang="d")
    req2 = TranslationRequest(text="a", target_lang="bc", swap_lang="d")
    assert translation_digest("google", req1) != translation_digest("google", req2)
