"""Formatting, code/URL/@mention protection, glossary masking, and UTF-16 entity mapping."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from aiogram.types import MessageEntity

from mivta.utils.utf16 import U16OffsetMap, build_utf16_offsets, utf16_len

_VERBATIM_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "code",
        "pre",
        "url",
        "mention",
        "email",
        "bot_command",
        "cashtag",
        "phone_number",
        "custom_emoji",
    }
)

_STYLE_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "blockquote",
        "expandable_blockquote",
        "text_link",
        "text_mention",
    }
)

_AUTO_LINK_TYPES: frozenset[str] = frozenset({"url", "mention", "email"})

_FENCED_CODE_RE = re.compile(r"```(?:([A-Za-z0-9_+-]+)[ \t]*\n)?([\s\S]+?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"https?://[^\s<>]+")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{2,31}\b")


# Re-exported for backwards compatibility: these helpers live in `mivta.utils.utf16`
# so that `formatting.py` and `parser.py` share one implementation.
__all__ = ["build_utf16_offsets", "utf16_len"]


def _trim_url_match(raw_url: str) -> str:
    url = raw_url
    while url:
        last = url[-1]
        if last in ".,;:!?\"'":
            url = url[:-1]
            continue
        if last == ")" and url.count(")") > url.count("("):
            url = url[:-1]
            continue
        if last == "]" and url.count("]") > url.count("["):
            url = url[:-1]
            continue
        break
    return url


@dataclass(frozen=True, slots=True)
class _VerbatimSpan:
    start: int
    end: int
    replacement: str
    entity_template: MessageEntity | None = None


@dataclass(frozen=True, slots=True)
class _StyleSpan:
    span_id: int
    start: int
    end: int
    entity_template: MessageEntity
    leading_space: bool
    trailing_space: bool


@dataclass(frozen=True, slots=True)
class PreparedFormatting:
    """Intermediate representation before sending text to a translation provider."""

    original_text: str
    encoded_text: str
    translatable_probe: str
    token_prefix: str
    verbatims: tuple[_VerbatimSpan, ...]
    styles: tuple[_StyleSpan, ...]
    had_explicit_entities: bool
    has_xml_style_tags: bool

    @property
    def has_markers(self) -> bool:
        return bool(self.verbatims or self.styles)


@dataclass(frozen=True, slots=True)
class RestoredFormatting:
    """Final clean translated string and optional Telegram MessageEntity list."""

    text: str
    entities: list[MessageEntity] | None


def _choose_token_prefix(text: str) -> str:
    candidate = "MIVTAK"
    counter = 0
    upper = text.upper()
    while candidate in upper:
        counter += 1
        candidate = f"MIVTAK{counter}Z"
    return candidate


class _IntervalSet:
    """Sorted non-overlapping [start, end) intervals with O(log n) overlap checks.

    `prepare_text_for_translation` collects verbatim spans (links, code, mentions,
    glossary hits) and tests every new candidate against all of them. A long
    message can hold dozens of spans, so a linear scan per candidate costs
    O(n^2); keeping the intervals sorted makes each test a bisect.
    """

    __slots__ = ("_starts", "_ends")

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def add(self, start: int, end: int) -> None:
        idx = bisect_left(self._starts, start)
        self._starts.insert(idx, start)
        self._ends.insert(idx, end)

    def overlaps(self, start: int, end: int) -> bool:
        # Intervals never overlap each other, so only the neighbours of the
        # insertion point can intersect [start, end).
        idx = bisect_left(self._starts, start)
        if idx > 0 and self._ends[idx - 1] > start:
            return True
        return idx < len(self._starts) and self._starts[idx] < end

    def __len__(self) -> int:
        return len(self._starts)


def _crosses_verbatim_boundary(
    start: int,
    end: int,
    verbatim_starts: Sequence[int],
    verbatim_ends: Sequence[int],
) -> bool:
    """Return True if [start, end) partially cuts through a verbatim span.

    `verbatim_starts` must be sorted ascending; only the spans that can possibly
    intersect [start, end) are then inspected.
    """
    idx = bisect_right(verbatim_starts, start)
    low = max(0, idx - 1)
    for i in range(low, len(verbatim_starts)):
        v_start = verbatim_starts[i]
        if v_start >= end:
            break
        v_end = verbatim_ends[i]
        if start < v_end and v_start < end and not (start <= v_start and v_end <= end):
            return True
    return False


MAX_TRANSLATION_CHUNKS = 12

# Atoms that must never be split: formatting spans `<s0>…</s0>` and verbatim tokens
# `MIVTAK0X` / `MIVTAK1Z3X`. Everything else may break on whitespace runs.
_PROTECTED_ATOM_RE = re.compile(
    r"<s\d+>.*?</s\d+>"  # keep open+close formatting tags in one piece
    r"|MIVTAK\d*(?:Z\d+)?X"  # verbatim placeholder token
    r"|\s+"  # whitespace runs are the only safe split points
    r"|<|[^\s<]+",  # lone '<' or a plain word
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class TextChunks:
    """Long text split into provider-sized chunks plus the gaps between them.

    `exceeds` means the text needs more chunks than allowed, so the caller should
    reject it instead of burning the provider quota on a wall of text.
    """

    chunks: tuple[str, ...] = ()
    separators: tuple[str, ...] = ()
    exceeds: bool = False


def split_for_translation(
    text: str,
    max_length: int,
    *,
    max_chunks: int = MAX_TRANSLATION_CHUNKS,
) -> TextChunks:
    """Split `text` into `max_length` chunks without breaking protected placeholders.

    Breaks prefer sentence and paragraph ends, fall back to word boundaries, and
    never split inside a `<sN>…</sN>` span or a `MIVTAK…X` verbatim token.
    `separators[i]` is the untouched whitespace run between chunk `i` and `i+1`,
    so `join_translated_chunks` reproduces the original text exactly.
    """
    if max_length <= 0 or not text:
        return TextChunks()
    if len(text) <= max_length:
        return TextChunks(chunks=(text,))

    atoms = _PROTECTED_ATOM_RE.findall(text)
    if "".join(atoms) != text:  # defensive: never risk corrupting the payload
        return TextChunks(chunks=(text,))

    chunks: list[str] = []
    seps: list[str] = []
    buf: list[str] = []
    buf_len = 0
    pending_ws = ""
    break_at: int | None = None

    def close_chunk(following_gap: str) -> None:
        """Emit the buffer as a chunk; `following_gap` is the gap after it."""
        nonlocal buf, buf_len, break_at
        if not buf:
            return
        chunks.append("".join(buf))
        seps.append(following_gap)
        buf = []
        buf_len = 0
        break_at = None

    def stash_leading_whitespace() -> None:
        """Keep whitespace that cannot start a chunk: attach it to the previous one.

        At the very beginning of the text there is no previous chunk, so the run is
        simply kept and prefixed to the first chunk.
        """
        nonlocal pending_ws
        if pending_ws and chunks:
            chunks[-1] = chunks[-1] + pending_ws
            pending_ws = ""

    def split_at_sentence_boundary() -> bool:
        """Close the buffer at its last sentence/paragraph break instead of its end."""
        nonlocal buf, buf_len, break_at
        if break_at is None or break_at <= 0:
            return False
        head = buf[:break_at]
        if len("".join(head)) < max_length // 2:
            return False
        gap = buf[break_at]
        chunks.append("".join(head))
        seps.append(gap)
        buf = buf[break_at + 1 :]
        buf_len = sum(len(atom) for atom in buf)
        break_at = None
        return True

    for atom in atoms:
        if atom.isspace():
            pending_ws += atom
            if len(pending_ws) > max_length:
                close_chunk(pending_ws)
                pending_ws = ""
            continue

        if len(atom) > max_length:
            # A single unbreakable atom (huge URL token, unbroken run): hard-split it
            if buf:
                close_chunk(pending_ws)
                pending_ws = ""
            pieces = [atom[i : i + max_length] for i in range(0, len(atom), max_length)]
            if pending_ws:
                # Start of text: keep the leading whitespace glued to the first piece
                pieces[0] = pending_ws + pieces[0]
                pending_ws = ""
            for piece in pieces:
                chunks.append(piece)
                seps.append("")
            continue

        if buf and buf_len + len(pending_ws) + len(atom) > max_length:
            # A sentence break keeps `pending_ws` inside the new chunk: only a plain
            # chunk close consumes it as the gap between two chunks.
            if not split_at_sentence_boundary():
                close_chunk(pending_ws)
                pending_ws = ""
            if buf and buf_len + len(pending_ws) + len(atom) > max_length:
                close_chunk(pending_ws)
                pending_ws = ""
        elif not buf and len(pending_ws) + len(atom) > max_length:
            # A word nearly as long as the limit: keep its leading whitespace by
            # attaching it to the previous chunk instead of losing it.
            stash_leading_whitespace()

        if buf and pending_ws and (buf[-1][-1:] in ".!?:…" or "\n" in pending_ws):
            break_at = len(buf)

        buf.append(pending_ws)
        buf.append(atom)
        buf_len += len(pending_ws) + len(atom)
        pending_ws = ""

    close_chunk(pending_ws)
    if chunks and pending_ws:
        chunks[-1] = chunks[-1] + pending_ws
    if len(chunks) > max_chunks:
        return TextChunks(exceeds=True)
    if chunks:
        seps = seps[: len(chunks) - 1]
    return TextChunks(chunks=tuple(chunks), separators=tuple(seps))


def join_translated_chunks(
    pieces: Sequence[str],
    separators: Sequence[str],
) -> str:
    """Reassemble translated chunks using the original (untranslated) gaps."""
    out: list[str] = []
    for index, piece in enumerate(pieces):
        out.append(piece)
        if index < len(separators):
            out.append(separators[index])
    return "".join(out)


@lru_cache(maxsize=32)
def _compile_glossary_pattern(glossary: tuple[str, ...]) -> re.Pattern[str] | None:
    cleaned = [term.strip() for term in glossary if term and term.strip()]
    if not cleaned:
        return None
    # Match longer multi-word terms before shorter substrings
    ordered = sorted(set(cleaned), key=len, reverse=True)
    parts: list[str] = []
    for term in ordered:
        esc = re.escape(term)
        left = r"(?<!\w)" if term[0].isalnum() or term[0] == "_" else ""
        right = r"(?!\w)" if term[-1].isalnum() or term[-1] == "_" else ""
        parts.append(f"{left}{esc}{right}")
    return re.compile("|".join(parts), re.IGNORECASE)


def prepare_text_for_translation(
    text: str,
    *,
    entities: Sequence[MessageEntity] | None = None,
    glossary: Sequence[str] = (),
    protect_glossary_tokens: bool = False,
    parse_inline_code_markdown: bool = False,
) -> PreparedFormatting:
    """Mask code/URLs/@mentions/glossary and wrap formatting entities before translation."""
    if not text:
        return PreparedFormatting(
            original_text="",
            encoded_text="",
            translatable_probe="",
            token_prefix="MIVTAK",
            verbatims=(),
            styles=(),
            had_explicit_entities=bool(entities),
            has_xml_style_tags=False,
        )

    u16_map = U16OffsetMap(text)

    verbatims: list[_VerbatimSpan] = []
    occupied = _IntervalSet()
    candidate_styles: list[tuple[int, int, MessageEntity]] = []

    if entities:
        for ent in entities:
            py_start = u16_map.to_python(ent.offset)
            py_end = u16_map.to_python(ent.offset + ent.length)
            if py_start is None or py_end is None or py_end <= py_start:
                continue
            if ent.type in _VERBATIM_ENTITY_TYPES:
                if not occupied.overlaps(py_start, py_end):
                    verbatims.append(
                        _VerbatimSpan(
                            start=py_start,
                            end=py_end,
                            replacement=text[py_start:py_end],
                            entity_template=ent,
                        )
                    )
                    occupied.add(py_start, py_end)
            elif ent.type in _STYLE_ENTITY_TYPES:
                candidate_styles.append((py_start, py_end, ent))

    # Detect backtick code blocks in uncovered text
    for match in _FENCED_CODE_RE.finditer(text):
        s, e = match.span()
        if occupied.overlaps(s, e):
            continue
        if parse_inline_code_markdown:
            lang = match.group(1) or None
            inner = match.group(2)
            if inner:
                verbatims.append(
                    _VerbatimSpan(
                        start=s,
                        end=e,
                        replacement=inner,
                        entity_template=MessageEntity(
                            type="pre",
                            offset=0,
                            length=0,
                            language=lang,
                        ),
                    )
                )
                occupied.add(s, e)
        else:
            verbatims.append(_VerbatimSpan(start=s, end=e, replacement=text[s:e]))
            occupied.add(s, e)

    for match in _INLINE_CODE_RE.finditer(text):
        s, e = match.span()
        if occupied.overlaps(s, e):
            continue
        inner = match.group(1)
        if parse_inline_code_markdown and inner:
            verbatims.append(
                _VerbatimSpan(
                    start=s,
                    end=e,
                    replacement=inner,
                    entity_template=MessageEntity(type="code", offset=0, length=0),
                )
            )
        else:
            verbatims.append(_VerbatimSpan(start=s, end=e, replacement=text[s:e]))
        occupied.add(s, e)

    # Emails, URLs, and @mentions in uncovered text
    for regex, ent_type in (
        (_EMAIL_RE, "email"),
        (_URL_RE, "url"),
        (_MENTION_RE, "mention"),
    ):
        for match in regex.finditer(text):
            s, e = match.span()
            if ent_type == "url":
                trimmed = _trim_url_match(match.group(0))
                if not trimmed:
                    continue
                e = s + len(trimmed)
            if occupied.overlaps(s, e):
                continue
            tmpl = (
                MessageEntity(type=ent_type, offset=0, length=0)
                if parse_inline_code_markdown
                else None
            )
            verbatims.append(
                _VerbatimSpan(
                    start=s,
                    end=e,
                    replacement=text[s:e],
                    entity_template=tmpl,
                )
            )
            occupied.add(s, e)

    # Optional token-level glossary masking for classic non-LLM translators
    if protect_glossary_tokens and glossary:
        glossary_re = _compile_glossary_pattern(tuple(glossary))
        if glossary_re is not None:
            for match in glossary_re.finditer(text):
                s, e = match.span()
                if s < e and not occupied.overlaps(s, e):
                    verbatims.append(_VerbatimSpan(start=s, end=e, replacement=text[s:e]))
                    occupied.add(s, e)

    verbatims.sort(key=lambda v: v.start)
    # Verbatim spans never overlap each other, so sorted starts imply sorted ends
    # and boundary checks become a bisect instead of a scan over every span.
    verbatim_starts = [v.start for v in verbatims]
    verbatim_ends = [v.end for v in verbatims]

    # Keep properly nested style spans (no partial overlap with verbatim or earlier style spans)
    accepted_styles: list[_StyleSpan] = []
    for py_start, py_end, ent in sorted(candidate_styles, key=lambda item: (item[0], -item[1])):
        if _crosses_verbatim_boundary(py_start, py_end, verbatim_starts, verbatim_ends):
            continue
        crosses_style = False
        for existing in accepted_styles:
            if (
                py_start < existing.end
                and existing.start < py_end
                and not (existing.start <= py_start and py_end <= existing.end)
                and not (py_start <= existing.start and existing.end <= py_end)
            ):
                crosses_style = True
                break
        if crosses_style:
            continue
        span_text = text[py_start:py_end]
        accepted_styles.append(
            _StyleSpan(
                span_id=len(accepted_styles),
                start=py_start,
                end=py_end,
                entity_template=ent,
                leading_space=span_text[0].isspace(),
                trailing_space=span_text[-1].isspace(),
            )
        )

    if not verbatims and not accepted_styles:
        return PreparedFormatting(
            original_text=text,
            encoded_text=text,
            translatable_probe=text,
            token_prefix="MIVTAK",
            verbatims=(),
            styles=(),
            had_explicit_entities=bool(entities),
            has_xml_style_tags=False,
        )

    token_prefix = _choose_token_prefix(text)

    opens_at: dict[int, list[_StyleSpan]] = {}
    closes_at: dict[int, list[_StyleSpan]] = {}
    for st in accepted_styles:
        opens_at.setdefault(st.start, []).append(st)
        closes_at.setdefault(st.end, []).append(st)

    for pos_list in opens_at.values():
        pos_list.sort(key=lambda st: (-st.end, st.span_id))
    for pos_list in closes_at.values():
        pos_list.sort(key=lambda st: (-st.start, -st.span_id))

    verbatim_by_start: dict[int, tuple[int, _VerbatimSpan]] = {
        v.start: (idx, v) for idx, v in enumerate(verbatims)
    }

    encoded_parts: list[str] = []
    probe_parts: list[str] = []
    text_len = len(text)

    # Walk only the positions where a marker is emitted and copy the plain runs
    # between them in one slice (instead of appending character by character).
    cut_points = sorted(
        cut for cut in {*verbatim_by_start, *opens_at, *closes_at} if cut < text_len
    )
    pos = 0
    for cut in cut_points:
        if cut < pos:
            continue  # inside a verbatim span: the original loop skips it too
        if cut > pos:
            chunk = text[pos:cut]
            encoded_parts.append(chunk)
            probe_parts.append(chunk)
            pos = cut
        for st in closes_at.get(cut, ()):
            encoded_parts.append(f"</s{st.span_id}>")
        for st in opens_at.get(cut, ()):
            encoded_parts.append(f"<s{st.span_id}>")

        v_entry = verbatim_by_start.get(cut)
        if v_entry is not None:
            v_idx, v_span = v_entry
            encoded_parts.append(f"{token_prefix}{v_idx}X")
            probe_parts.append(" ")
            pos = v_span.end

    if pos < text_len:
        tail = text[pos:]
        encoded_parts.append(tail)
        probe_parts.append(tail)

    if text_len in closes_at:
        for st in closes_at[text_len]:
            encoded_parts.append(f"</s{st.span_id}>")

    return PreparedFormatting(
        original_text=text,
        encoded_text="".join(encoded_parts),
        translatable_probe="".join(probe_parts),
        token_prefix=token_prefix,
        verbatims=tuple(verbatims),
        styles=tuple(accepted_styles),
        had_explicit_entities=bool(entities),
        has_xml_style_tags=bool(accepted_styles),
    )


@lru_cache(maxsize=8)
def _marker_pattern(token_prefix: str) -> re.Pattern[str]:
    """Compiled marker scanner for `token_prefix` (compiled once per process)."""
    return re.compile(
        rf"(?P<open>[<«]\s*s(?P<open_id>\d+)\s*[>»])"
        rf"|(?P<close>[<«]\s*/\s*s(?P<close_id>\d+)\s*[>»])"
        rf"|(?P<verb>{re.escape(token_prefix)}(?P<verb_id>\d+)X)",
        re.IGNORECASE,
    )


def restore_formatted_text(
    translated: str,
    prepared: PreparedFormatting,
    *,
    max_length: int = 4096,
    emit_entities_only_if_rich: bool = False,
) -> RestoredFormatting:
    """Restore verbatim placeholders and compute UTF-16 `MessageEntity` offsets."""
    if not prepared.has_markers:
        clipped = translated[:max_length]
        return RestoredFormatting(text=clipped, entities=None)

    marker_re = _marker_pattern(prepared.token_prefix)

    styles_by_id = {st.span_id: st for st in prepared.styles}
    out_chars: list[str] = []
    open_positions: dict[int, int] = {}
    closed_ids: set[int] = set()
    recorded_spans: list[tuple[int, int, MessageEntity]] = []

    cursor = 0
    for match in marker_re.finditer(translated):
        if match.start() > cursor:
            out_chars.extend(translated[cursor : match.start()])
        cursor = match.end()

        if match.group("open") is not None:
            sid = int(match.group("open_id"))
            if sid in styles_by_id and sid not in open_positions and sid not in closed_ids:
                open_positions[sid] = len(out_chars)
        elif match.group("close") is not None:
            sid = int(match.group("close_id"))
            start_py = open_positions.pop(sid, None)
            if start_py is not None:
                closed_ids.add(sid)
                style_meta = styles_by_id[sid]
                # Strip artificial single space injected right inside <sN> ... </sN> by MT engines
                if (
                    not style_meta.leading_space
                    and start_py < len(out_chars)
                    and out_chars[start_py] == " "
                ):
                    if start_py == 0 or out_chars[start_py - 1].isspace():
                        del out_chars[start_py]
                        for k, v in list(open_positions.items()):
                            if v > start_py:
                                open_positions[k] = v - 1
                        recorded_spans = [
                            (
                                rs - (1 if rs > start_py else 0),
                                re_ - (1 if re_ > start_py else 0),
                                rt,
                            )
                            for rs, re_, rt in recorded_spans
                        ]
                    else:
                        start_py += 1
                if (
                    not style_meta.trailing_space
                    and len(out_chars) > start_py
                    and out_chars[-1] == " "
                ):
                    next_ch = translated[cursor] if cursor < len(translated) else ""
                    if not next_ch or next_ch.isspace() or next_ch in ".,!?;:)]}":
                        out_chars.pop()
                end_py = len(out_chars)
                if (
                    not style_meta.trailing_space
                    and end_py > start_py
                    and out_chars[end_py - 1] == " "
                ):
                    end_py -= 1
                if end_py > start_py:
                    recorded_spans.append((start_py, end_py, style_meta.entity_template))
        elif match.group("verb") is not None:
            vid = int(match.group("verb_id"))
            if 0 <= vid < len(prepared.verbatims):
                v_span = prepared.verbatims[vid]
                v_start = len(out_chars)
                out_chars.extend(v_span.replacement)
                v_end = len(out_chars)
                if v_span.entity_template is not None and v_end > v_start:
                    recorded_spans.append((v_start, v_end, v_span.entity_template))

    if cursor < len(translated):
        out_chars.extend(translated[cursor:])

    final_text = "".join(out_chars)[:max_length]
    final_py_len = len(final_text)

    if not recorded_spans:
        return RestoredFormatting(text=final_text, entities=None)

    has_rich_entity = any(tmpl.type not in _AUTO_LINK_TYPES for _, _, tmpl in recorded_spans)
    if emit_entities_only_if_rich and not has_rich_entity and not prepared.had_explicit_entities:
        return RestoredFormatting(text=final_text, entities=None)

    u16_map = U16OffsetMap(final_text)
    final_entities: list[MessageEntity] = []
    for py_start, py_end, tmpl in recorded_spans:
        if py_start >= final_py_len:
            continue
        clamped_end = min(py_end, final_py_len)
        if clamped_end <= py_start:
            continue
        u_off = u16_map.to_utf16(py_start)
        u_len = u16_map.to_utf16(clamped_end) - u_off
        if u_len <= 0:
            continue
        final_entities.append(tmpl.model_copy(update={"offset": u_off, "length": u_len}))

    if not final_entities:
        return RestoredFormatting(text=final_text, entities=None)

    final_entities.sort(key=lambda e: (e.offset, -e.length))
    return RestoredFormatting(text=final_text, entities=final_entities)
