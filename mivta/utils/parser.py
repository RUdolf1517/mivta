"""Inline query and Business Mode message sanitization and directive parsing."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mivta.translator.base import TranslationStyle, parse_style
from mivta.utils.languages import resolve_language
from mivta.utils.utf16 import U16OffsetMap

if TYPE_CHECKING:
    from aiogram.types import MessageEntity

# Leading language directives: either `>target` or `source>target` (no spaces around `>`)
_TARGET_ONLY_RE = re.compile(r"^>(?P<tgt>[A-Za-z]{2,8}(?:-[A-Za-z]{2,6})?)$")
_PAIR_DIRECTIVE_RE = re.compile(
    r"^(?P<src>[A-Za-z]{2,8})>(?P<tgt>[A-Za-z]{2,8}(?:-[A-Za-z]{2,6})?)$"
)
_LEADING_TOKEN_RE = re.compile(r"^(?P<token>\S+)(?:[ \t]+|\r?\n|$)(?P<rest>[\s\S]*)$")


@dataclass(frozen=True, slots=True)
class ParsedInlineQuery:
    """Result of parsing raw user text and optional leading translation directives."""

    text: str
    source_lang: str | None = None
    target_lang: str | None = None
    style: TranslationStyle | None = None
    error: str | None = None
    entities: tuple[MessageEntity, ...] = ()


def _sanitize_with_map(raw: str) -> tuple[str, list[int]]:
    """Sanitize raw input while mapping each output char index back to its index in `raw`."""
    if not raw:
        return "", []

    out_chars: list[str] = []
    out_to_raw: list[int] = []
    idx = 0
    raw_len = len(raw)
    while idx < raw_len:
        ch = raw[idx]
        if ch == "\r":
            out_chars.append("\n")
            out_to_raw.append(idx)
            if idx + 1 < raw_len and raw[idx + 1] == "\n":
                idx += 2
                continue
            idx += 1
            continue
        if ch in ("\n", "\t") or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F)):
            out_chars.append(ch)
            out_to_raw.append(idx)
        idx += 1

    start = 0
    end = len(out_chars)
    while start < end and out_chars[start] in (" ", "\t", "\n"):
        start += 1
    while end > start and out_chars[end - 1] in (" ", "\t", "\n"):
        end -= 1

    return "".join(out_chars[start:end]), out_to_raw[start:end]


def sanitize_text(raw: str) -> str:
    """Normalize line endings and strip non-printable C0/C1 control characters."""
    cleaned, _ = _sanitize_with_map(raw)
    return cleaned


def has_translatable_text(text: str) -> bool:
    """Return True if text contains at least one alphabetic character in any Unicode script."""
    return any(ch.isalpha() for ch in text)


def truncate_preview(text: str, limit: int = 80) -> str:
    """Collapse newlines for single-line Telegram inline result titles."""
    single_line = " ".join(text.split())
    if limit <= 1:
        return "…" if single_line else ""
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1].rstrip() + "…"


def _rebase_entities_from_map(
    raw: str,
    entities: Sequence[MessageEntity] | None,
    body_text: str,
    body_to_raw: list[int],
) -> tuple[MessageEntity, ...]:
    """Rebase Telegram UTF-16 `MessageEntity` offsets from `raw` onto `body_text`."""
    if not entities or not body_text or not body_to_raw:
        return ()

    raw_map = U16OffsetMap(raw)
    body_map = U16OffsetMap(body_text)
    body_len = len(body_text)

    # `body_to_raw` is strictly increasing, so boundary lookups are bisects
    # instead of linear scans (matters for long messages with many entities).
    first_raw = body_to_raw[0]
    last_raw_excl = body_to_raw[-1] + 1

    def _map_raw_start(raw_py: int) -> int:
        if raw_py <= first_raw:
            return 0
        return bisect_left(body_to_raw, raw_py)

    def _map_raw_end(raw_py: int) -> int:
        if raw_py <= first_raw:
            return 0
        if raw_py >= last_raw_excl:
            return body_len
        return bisect_left(body_to_raw, raw_py)

    rebased: list[MessageEntity] = []
    for ent in entities:
        u_start = ent.offset
        u_end = ent.offset + ent.length
        if u_end <= u_start or u_start < 0 or u_end > raw_map.total:
            continue
        raw_py_start = raw_map.to_python(u_start)
        raw_py_end = raw_map.to_python(u_end)
        if raw_py_start is None or raw_py_end is None:
            continue
        if raw_py_end <= first_raw or raw_py_start >= last_raw_excl:
            continue

        b_start = _map_raw_start(raw_py_start)
        b_end = _map_raw_end(raw_py_end)
        if b_end <= b_start:
            continue

        new_u_off = body_map.to_utf16(b_start)
        new_u_len = body_map.to_utf16(b_end) - new_u_off
        if new_u_len <= 0:
            continue
        rebased.append(ent.model_copy(update={"offset": new_u_off, "length": new_u_len}))

    return tuple(rebased)


def _unknown_target_error(raw_tgt: str, ui_lang: str) -> str:
    if ui_lang == "en":
        return f"Unknown target language code: '{raw_tgt}'"
    return f"Неизвестный код языка назначения: «{raw_tgt}»"


def _unknown_source_error(raw_src: str, ui_lang: str) -> str:
    if ui_lang == "en":
        return f"Unknown source language code: '{raw_src}'"
    return f"Неизвестный код исходного языка: «{raw_src}»"


def _parse_directives(
    raw: str,
    *,
    allow_bare_gt: bool,
    entities: Sequence[MessageEntity] | None = None,
    ui_lang: str = "ru",
) -> tuple[ParsedInlineQuery, bool]:
    """Shared parser returning (ParsedInlineQuery, saw_gt_trigger)."""
    sanitized, san_to_raw = _sanitize_with_map(raw)
    if not sanitized:
        return ParsedInlineQuery(text=""), False

    source_override: str | None = None
    target_override: str | None = None
    style_override: TranslationStyle | None = None
    saw_gt_trigger = False
    offset = 0
    san_len = len(sanitized)

    def _trim_slice(start_idx: int) -> tuple[str, list[int]]:
        s = start_idx
        e = san_len
        while s < e and sanitized[s] in (" ", "\t", "\n"):
            s += 1
        while e > s and sanitized[e - 1] in (" ", "\t", "\n"):
            e -= 1
        return sanitized[s:e], san_to_raw[s:e]

    # Inspect at most 2 leading directive tokens (e.g. `>de !formal Hello` or `> !casual Hi`)
    for _ in range(2):
        remainder = sanitized[offset:]
        match = _LEADING_TOKEN_RE.match(remainder)
        if match is None:
            break
        token = match.group("token")
        rest_start = offset + match.start("rest")
        while rest_start < san_len and sanitized[rest_start] in (" ", "\t"):
            rest_start += 1

        if token.startswith("!") and len(token) > 1 and style_override is None:
            parsed_style = parse_style(token[1:])
            if parsed_style is not None:
                style_override = parsed_style
                offset = rest_start
                continue

        if allow_bare_gt and token == ">" and not saw_gt_trigger:
            saw_gt_trigger = True
            offset = rest_start
            continue

        if ">" in token and not saw_gt_trigger:
            target_match = _TARGET_ONLY_RE.match(token)
            if target_match is not None:
                saw_gt_trigger = True
                raw_tgt = target_match.group("tgt")
                resolved_tgt = resolve_language(raw_tgt, allow_auto=False)
                if resolved_tgt is None:
                    err_text, _ = _trim_slice(rest_start)
                    return (
                        ParsedInlineQuery(
                            text=err_text,
                            error=_unknown_target_error(raw_tgt, ui_lang),
                        ),
                        True,
                    )
                target_override = resolved_tgt
                offset = rest_start
                continue

            pair_match = _PAIR_DIRECTIVE_RE.match(token)
            if pair_match is not None:
                saw_gt_trigger = True
                raw_src = pair_match.group("src")
                raw_tgt = pair_match.group("tgt")
                resolved_src = resolve_language(raw_src, allow_auto=True)
                if resolved_src is None:
                    err_text, _ = _trim_slice(rest_start)
                    return (
                        ParsedInlineQuery(
                            text=err_text,
                            error=_unknown_source_error(raw_src, ui_lang),
                        ),
                        True,
                    )
                resolved_tgt = resolve_language(raw_tgt, allow_auto=False)
                if resolved_tgt is None:
                    err_text, _ = _trim_slice(rest_start)
                    return (
                        ParsedInlineQuery(
                            text=err_text,
                            error=_unknown_target_error(raw_tgt, ui_lang),
                        ),
                        True,
                    )
                source_override = resolved_src
                target_override = resolved_tgt
                offset = rest_start
                continue

        break

    body_text, body_map = _trim_slice(offset)
    rebased_entities = _rebase_entities_from_map(raw, entities, body_text, body_map)
    return (
        ParsedInlineQuery(
            text=body_text,
            source_lang=source_override,
            target_lang=target_override,
            style=style_override,
            entities=rebased_entities,
        ),
        saw_gt_trigger,
    )


def parse_inline_query(raw: str, *, ui_lang: str = "ru") -> ParsedInlineQuery:
    """Parse optional inline overrides (`>lang`, `src>tgt`, `!style`) and body text."""
    parsed, _ = _parse_directives(raw, allow_bare_gt=False, ui_lang=ui_lang)
    return parsed


def parse_business_query(
    raw: str,
    entities: Sequence[MessageEntity] | None = None,
    *,
    ui_lang: str = "ru",
) -> ParsedInlineQuery | None:
    """Parse Business Mode text; returns None unless triggered via `>`, `>lang`, or `src>tgt`."""
    parsed, triggered = _parse_directives(
        raw,
        allow_bare_gt=True,
        entities=entities,
        ui_lang=ui_lang,
    )
    if not triggered:
        return None
    return parsed
