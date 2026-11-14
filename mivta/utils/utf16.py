"""UTF-16 code-unit helpers for Telegram Bot API entity offsets.

Telegram counts offsets in UTF-16 code units while Python indexes `str` by code
points, so every entity has to be translated between the two. Most messages are
BMP-only (1 code point == 1 UTF-16 unit), so the mapping is the identity and can
be resolved in O(1); only emoji and other astral characters need real work.
"""

from __future__ import annotations

from bisect import bisect_left


def utf16_len(text: str) -> int:
    """Return string length in UTF-16 code units as required by Telegram Bot API."""
    return len(text.encode("utf-16-le")) // 2


def has_astral_chars(text: str) -> bool:
    """True when `text` contains characters outside the BMP (UTF-16 surrogate pairs)."""
    if not text:
        return False
    try:
        return len(text.encode("utf-16-le", "surrogatepass")) != 2 * len(text)
    except UnicodeEncodeError:  # pragma: no cover - defensive, never hit with str
        return True


def build_utf16_offsets(text: str) -> list[int]:
    """Build cumulative UTF-16 code-unit offsets for each Python character index."""
    if not has_astral_chars(text):
        # BMP-only: one code point is exactly one UTF-16 unit
        return list(range(len(text) + 1))
    offsets = [0] * (len(text) + 1)
    acc = 0
    for idx, ch in enumerate(text):
        acc += 2 if ord(ch) > 0xFFFF else 1
        offsets[idx + 1] = acc
    return offsets


class U16OffsetMap:
    """Maps UTF-16 offsets of `text` to Python string indices.

    O(1) per lookup and O(1) memory for BMP-only text (the common case); falls
    back to a bisect over precomputed offsets when astral characters are present.
    """

    __slots__ = ("_length", "_offsets")

    def __init__(self, text: str) -> None:
        self._length = len(text)
        self._offsets = build_utf16_offsets(text) if has_astral_chars(text) else None

    @property
    def total(self) -> int:
        """Total length of the text in UTF-16 code units."""
        return self._length if self._offsets is None else self._offsets[-1]

    def to_python(self, u16_offset: int) -> int | None:
        """Python index of a UTF-16 offset, or None when it is out of range or not a boundary."""
        if u16_offset < 0:
            return None
        if self._offsets is None:
            return u16_offset if u16_offset <= self._length else None
        idx = bisect_left(self._offsets, u16_offset)
        if idx < len(self._offsets) and self._offsets[idx] == u16_offset:
            return idx
        return None

    def to_utf16(self, py_index: int) -> int:
        """UTF-16 offset of a Python index (clamped to the end of the text)."""
        if py_index <= 0:
            return 0
        if self._offsets is None:
            return min(py_index, self._length)
        return self._offsets[min(py_index, self._length)]
