"""Inline query and Business Mode message sanitization and directive parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mivta.translator.base import TranslationStyle, parse_style
from mivta.utils.languages import resolve_language

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


def sanitize_text(raw: str) -> str:
    """Normalize line endings and strip non-printable C0/C1 control characters."""
    if not raw:
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Preserve \n (0x0A), \t (0x09), and all printable Unicode (including ZWJ / emoji / RTL)
    cleaned_chars = [
        ch
        for ch in normalized
        if ch in ("\n", "\t") or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F))
    ]
    return "".join(cleaned_chars).strip(" \t\n")


def has_translatable_text(text: str) -> bool:
    """Return True if text contains at least one alphabetic character in any Unicode script."""
    return any(ch.isalpha() for ch in text)


def truncate_preview(text: str, limit: int = 80) -> str:
    """Collapse newlines for single-line Telegram inline result titles."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1].rstrip() + "…"


def _parse_directives(
    raw: str,
    *,
    allow_bare_gt: bool,
) -> tuple[ParsedInlineQuery, bool]:
    """Shared parser returning (ParsedInlineQuery, saw_gt_trigger)."""
    remainder = sanitize_text(raw)
    if not remainder:
        return ParsedInlineQuery(text=""), False

    source_override: str | None = None
    target_override: str | None = None
    style_override: TranslationStyle | None = None
    saw_gt_trigger = False

    # Inspect at most 2 leading directive tokens (e.g. `>de !formal Hello` or `> !casual Hi`)
    for _ in range(2):
        match = _LEADING_TOKEN_RE.match(remainder)
        if match is None:
            break
        token = match.group("token")
        rest = match.group("rest")

        if token.startswith("!") and len(token) > 1 and style_override is None:
            parsed_style = parse_style(token[1:])
            if parsed_style is not None:
                style_override = parsed_style
                remainder = rest.lstrip(" \t")
                continue

        if allow_bare_gt and token == ">" and not saw_gt_trigger:
            saw_gt_trigger = True
            remainder = rest.lstrip(" \t")
            continue

        if ">" in token and not saw_gt_trigger:
            target_match = _TARGET_ONLY_RE.match(token)
            if target_match is not None:
                saw_gt_trigger = True
                raw_tgt = target_match.group("tgt")
                resolved_tgt = resolve_language(raw_tgt, allow_auto=False)
                if resolved_tgt is None:
                    return (
                        ParsedInlineQuery(
                            text=rest.strip(" \t\n"),
                            error=f"Неизвестный код языка назначения: «{raw_tgt}»",
                        ),
                        True,
                    )
                target_override = resolved_tgt
                remainder = rest.lstrip(" \t")
                continue

            pair_match = _PAIR_DIRECTIVE_RE.match(token)
            if pair_match is not None:
                saw_gt_trigger = True
                raw_src = pair_match.group("src")
                raw_tgt = pair_match.group("tgt")
                resolved_src = resolve_language(raw_src, allow_auto=True)
                if resolved_src is None:
                    return (
                        ParsedInlineQuery(
                            text=rest.strip(" \t\n"),
                            error=f"Неизвестный код исходного языка: «{raw_src}»",
                        ),
                        True,
                    )
                resolved_tgt = resolve_language(raw_tgt, allow_auto=False)
                if resolved_tgt is None:
                    return (
                        ParsedInlineQuery(
                            text=rest.strip(" \t\n"),
                            error=f"Неизвестный код языка назначения: «{raw_tgt}»",
                        ),
                        True,
                    )
                source_override = resolved_src
                target_override = resolved_tgt
                remainder = rest.lstrip(" \t")
                continue

        break

    return (
        ParsedInlineQuery(
            text=remainder.strip(" \t\n"),
            source_lang=source_override,
            target_lang=target_override,
            style=style_override,
        ),
        saw_gt_trigger,
    )


def parse_inline_query(raw: str) -> ParsedInlineQuery:
    """Parse optional inline overrides (`>lang`, `src>tgt`, `!style`) and body text."""
    parsed, _ = _parse_directives(raw, allow_bare_gt=False)
    return parsed


def parse_business_query(raw: str) -> ParsedInlineQuery | None:
    """Parse Business Mode text; returns None unless triggered via `>`, `>lang`, or `src>tgt`."""
    parsed, triggered = _parse_directives(raw, allow_bare_gt=True)
    if not triggered:
        return None
    return parsed
