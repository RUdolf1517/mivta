"""UTF-16 code-unit helpers for Telegram Bot API entity offsets.

Telegram counts offsets in UTF-16 code units while Python indexes `str` by code
points, so every entity has to be translated between the two.

Most messages are BMP-only (1 code point == 1 UTF-16 unit), so the mapping is
the identity and costs nothing. Emoji and other astral characters are handled
by remembering **only their positions** (`k` of them) instead of building an
`O(n)` prefix-sum array: lookups become `O(log k)` and memory `O(k)`.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from itertools import accumulate

# Any code point outside the BMP takes two UTF-16 code units (a surrogate pair).
_ASTRAL_RE = re.compile("[\U00010000-\U0010FFFF]")


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


def astral_indices(text: str) -> list[int]:
    """Python indices of every astral character in `text`, ascending."""
    return [m.start() for m in _ASTRAL_RE.finditer(text)]


def build_utf16_offsets(text: str) -> list[int]:
    """Build cumulative UTF-16 code-unit offsets for each Python character index."""
    if not has_astral_chars(text):
        # BMP-only: one code point is exactly one UTF-16 unit
        return list(range(len(text) + 1))
    # Each astral char makes the *next* step two units wide; `accumulate` does
    # the prefix sum in C instead of a per-character Python loop.
    steps = bytearray(b"\x01" * (len(text) + 1))
    steps[0] = 0
    for idx in astral_indices(text):
        steps[idx + 1] = 2
    return list(accumulate(steps))


class U16OffsetMap:
    """Maps UTF-16 offsets of `text` to Python string indices and back.

    O(1) per lookup and O(1) memory for BMP-only text (the common case). With
    astral characters present it keeps only their positions, so lookups are
    `O(log k)` and memory `O(k)` — no `O(n)` offset array is built at all.

    Offsets that land on the low surrogate half of a pair are not character
    boundaries; `to_python` returns `None` for them so callers drop the entity
    instead of slicing a pair in half and emitting mojibake.
    """

    __slots__ = ("_astral", "_holes", "_length")

    def __init__(self, text: str) -> None:
        self._length = len(text)
        astral = astral_indices(text)
        if astral:
            self._astral: tuple[int, ...] = tuple(astral)
            # `_holes[i]` is the first UTF-16 offset *after* astral char #i; the
            # unit just below it is the unreachable low-surrogate half.
            self._holes: tuple[int, ...] = tuple(
                idx + i + 2 for i, idx in enumerate(astral)
            )
        else:
            self._astral = ()
            self._holes = ()

    @property
    def total(self) -> int:
        """Total length of the text in UTF-16 code units."""
        return self._length + len(self._astral)

    def to_python(self, u16_offset: int) -> int | None:
        """Python index of a UTF-16 offset, or None when it is out of range or not a boundary."""
        if u16_offset < 0:
            return None
        if not self._astral:
            return u16_offset if u16_offset <= self._length else None
        py_index = u16_offset - bisect_right(self._holes, u16_offset)
        if py_index < 0 or py_index > self._length:
            return None
        if py_index + bisect_left(self._astral, py_index) != u16_offset:
            return None
        return py_index

    def to_utf16(self, py_index: int) -> int:
        """UTF-16 offset of a Python index (clamped to the end of the text)."""
        if py_index <= 0:
            return 0
        clamped = min(py_index, self._length)
        if not self._astral:
            return clamped
        return clamped + bisect_left(self._astral, clamped)
