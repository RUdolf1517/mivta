"""Formatting, code/URL/@mention protection, glossary masking, and UTF-16 entity mapping."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import MessageEntity

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


def utf16_len(text: str) -> int:
    """Return string length in UTF-16 code units as required by Telegram Bot API."""
    return len(text.encode("utf-16-le")) // 2


def build_utf16_offsets(text: str) -> list[int]:
    """Build cumulative UTF-16 code-unit offsets for each Python character index."""
    offsets = [0] * (len(text) + 1)
    acc = 0
    for idx, ch in enumerate(text):
        acc += 2 if ord(ch) > 0xFFFF else 1
        offsets[idx + 1] = acc
    return offsets


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


def _overlaps_any(start: int, end: int, intervals: list[tuple[int, int]]) -> bool:
    return any(start < iv_end and iv_start < end for iv_start, iv_end in intervals)


def _crosses_verbatim_boundary(
    start: int,
    end: int,
    verbatims: Sequence[_VerbatimSpan],
) -> bool:
    """Return True if [start, end) partially cuts through a verbatim span."""
    for v in verbatims:
        if start < v.end and v.start < end and not (start <= v.start and v.end <= end):
            return True
    return False


def _compile_glossary_pattern(glossary: Sequence[str]) -> re.Pattern[str] | None:
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

    u16_offsets = build_utf16_offsets(text)
    u16_to_py = {u16: py_i for py_i, u16 in enumerate(u16_offsets)}

    verbatims: list[_VerbatimSpan] = []
    occupied: list[tuple[int, int]] = []
    candidate_styles: list[tuple[int, int, MessageEntity]] = []

    if entities:
        for ent in entities:
            py_start = u16_to_py.get(ent.offset)
            py_end = u16_to_py.get(ent.offset + ent.length)
            if py_start is None or py_end is None or py_end <= py_start:
                continue
            if ent.type in _VERBATIM_ENTITY_TYPES:
                if not _overlaps_any(py_start, py_end, occupied):
                    verbatims.append(
                        _VerbatimSpan(
                            start=py_start,
                            end=py_end,
                            replacement=text[py_start:py_end],
                            entity_template=ent,
                        )
                    )
                    occupied.append((py_start, py_end))
            elif ent.type in _STYLE_ENTITY_TYPES:
                candidate_styles.append((py_start, py_end, ent))

    # Detect backtick code blocks in uncovered text
    for match in _FENCED_CODE_RE.finditer(text):
        s, e = match.span()
        if _overlaps_any(s, e, occupied):
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
                occupied.append((s, e))
        else:
            verbatims.append(_VerbatimSpan(start=s, end=e, replacement=text[s:e]))
            occupied.append((s, e))

    for match in _INLINE_CODE_RE.finditer(text):
        s, e = match.span()
        if _overlaps_any(s, e, occupied):
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
        occupied.append((s, e))

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
            if _overlaps_any(s, e, occupied):
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
            occupied.append((s, e))

    # Optional token-level glossary masking for classic non-LLM translators
    if protect_glossary_tokens and glossary:
        glossary_re = _compile_glossary_pattern(glossary)
        if glossary_re is not None:
            for match in glossary_re.finditer(text):
                s, e = match.span()
                if s < e and not _overlaps_any(s, e, occupied):
                    verbatims.append(_VerbatimSpan(start=s, end=e, replacement=text[s:e]))
                    occupied.append((s, e))

    verbatims.sort(key=lambda v: v.start)

    # Keep properly nested style spans (no partial overlap with verbatim or earlier style spans)
    accepted_styles: list[_StyleSpan] = []
    for py_start, py_end, ent in sorted(candidate_styles, key=lambda item: (item[0], -item[1])):
        if _crosses_verbatim_boundary(py_start, py_end, verbatims):
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
    pos = 0
    text_len = len(text)

    while pos < text_len:
        if pos in closes_at:
            for st in closes_at[pos]:
                encoded_parts.append(f"</s{st.span_id}>")
        if pos in opens_at:
            for st in opens_at[pos]:
                encoded_parts.append(f"<s{st.span_id}>")

        v_entry = verbatim_by_start.get(pos)
        if v_entry is not None:
            v_idx, v_span = v_entry
            encoded_parts.append(f"{token_prefix}{v_idx}X")
            probe_parts.append(" ")
            pos = v_span.end
            continue

        ch = text[pos]
        encoded_parts.append(ch)
        probe_parts.append(ch)
        pos += 1

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

    marker_re = re.compile(
        rf"(?P<open>[<«]\s*s(?P<open_id>\d+)\s*[>»])"
        rf"|(?P<close>[<«]\s*/\s*s(?P<close_id>\d+)\s*[>»])"
        rf"|(?P<verb>{re.escape(prepared.token_prefix)}(?P<verb_id>\d+)X)",
        re.IGNORECASE,
    )

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

    u16_offsets = build_utf16_offsets(final_text)
    final_entities: list[MessageEntity] = []
    for py_start, py_end, tmpl in recorded_spans:
        if py_start >= final_py_len:
            continue
        clamped_end = min(py_end, final_py_len)
        if clamped_end <= py_start:
            continue
        u_off = u16_offsets[py_start]
        u_len = u16_offsets[clamped_end] - u_off
        if u_len <= 0:
            continue
        final_entities.append(tmpl.model_copy(update={"offset": u_off, "length": u_len}))

    if not final_entities:
        return RestoredFormatting(text=final_text, entities=None)

    final_entities.sort(key=lambda e: (e.offset, -e.length))
    return RestoredFormatting(text=final_text, entities=final_entities)
