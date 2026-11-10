"""Asynchronous SQLite storage for persistent per-user preferences and Business metadata."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from mivta.config import MAX_INLINE_EXTRA_LANGS
from mivta.translator.base import TranslationStyle, parse_style
from mivta.utils.i18n import SUPPORTED_UI_LANGS, parse_ui_lang
from mivta.utils.languages import resolve_language

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    source_lang TEXT NOT NULL DEFAULT 'auto',
    target_lang TEXT NOT NULL DEFAULT 'en',
    swap_lang TEXT DEFAULT NULL,
    ui_lang TEXT DEFAULT NULL,
    style TEXT NOT NULL DEFAULT 'neutral'
        CHECK (style IN ('neutral', 'casual', 'formal')),
    extra_langs TEXT DEFAULT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    user_chat_id INTEGER NOT NULL,
    can_reply INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL
);
"""


def _parse_stored_extra_langs(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    langs: list[str] = []
    for piece in raw.split(","):
        code = resolve_language(piece, allow_auto=False)
        if code is not None and code not in langs:
            langs.append(code)
            if len(langs) >= MAX_INLINE_EXTRA_LANGS:
                break
    return tuple(langs)


def _normalize_extra_langs_input(codes: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_code in codes:
        resolved = resolve_language(raw_code, allow_auto=False)
        if resolved is None:
            raise ValueError(f"Unsupported extra target language: {raw_code!r}")
        if resolved not in normalized:
            normalized.append(resolved)
    if len(normalized) > MAX_INLINE_EXTRA_LANGS:
        raise ValueError(
            f"At most {MAX_INLINE_EXTRA_LANGS} extra inline languages are supported"
        )
    return tuple(normalized)


def _normalize_swap_lang_input(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    resolved = resolve_language(stripped, allow_auto=False)
    if resolved is None:
        raise ValueError(f"Unsupported swap language: {raw!r}")
    return resolved


def _normalize_ui_lang_input(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    parsed = parse_ui_lang(stripped)
    if parsed is None:
        raise ValueError(f"Unsupported UI language: {raw!r}; expected 'en', 'ru', or ''")
    return parsed


@dataclass(frozen=True, slots=True)
class UserSettings:
    """Saved translation preferences for a single Telegram user."""

    user_id: int
    source_lang: str
    target_lang: str
    style: TranslationStyle
    extra_langs: tuple[str, ...] = ()
    swap_lang: str = ""
    ui_lang: str = ""


@dataclass(frozen=True, slots=True)
class BusinessConnectionRecord:
    """Metadata of an active or inactive Telegram Business connection."""

    connection_id: str
    user_id: int
    user_chat_id: int
    can_reply: bool
    is_enabled: bool


class Database:
    """Async SQLite repository storing strictly user settings and connection metadata."""

    def __init__(
        self,
        path: str = "data/mivta.db",
        *,
        default_source_lang: str = "auto",
        default_target_lang: str = "en",
        default_swap_lang: str = "",
        default_style: TranslationStyle = TranslationStyle.NEUTRAL,
        default_extra_langs: Sequence[str] = (),
    ) -> None:
        self._path = path
        self._default_source = default_source_lang
        self._default_target = default_target_lang
        self._default_swap = _normalize_swap_lang_input(default_swap_lang)
        self._default_style = default_style
        self._default_extra_langs = _normalize_extra_langs_input(default_extra_langs)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the SQLite database connection and ensure schema + migrations exist."""
        if self._conn is not None:
            return
        if self._path != ":memory:" and not self._path.startswith("file:"):
            db_file = Path(self._path)
            if db_file.parent and str(db_file.parent) != ".":
                db_file.parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        if self._path != ":memory:":
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA temp_store=MEMORY;")
        await conn.executescript(_SCHEMA_SQL)

        # Idempotent migrations for databases created on v0.1.0 / v0.2.x
        async with conn.execute("PRAGMA table_info(user_settings);") as cursor:
            cols = {str(r["name"]) for r in await cursor.fetchall()}
        if "extra_langs" not in cols:
            await conn.execute(
                "ALTER TABLE user_settings ADD COLUMN extra_langs TEXT DEFAULT NULL;"
            )
        if "swap_lang" not in cols:
            await conn.execute(
                "ALTER TABLE user_settings ADD COLUMN swap_lang TEXT DEFAULT NULL;"
            )
        if "ui_lang" not in cols:
            await conn.execute(
                "ALTER TABLE user_settings ADD COLUMN ui_lang TEXT DEFAULT NULL;"
            )

        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Close the active SQLite connection cleanly."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        raise RuntimeError("Database.connect() must be called before executing queries")

    def _defaults_for(self, user_id: int) -> UserSettings:
        return UserSettings(
            user_id=user_id,
            source_lang=self._default_source,
            target_lang=self._default_target,
            style=self._default_style,
            extra_langs=self._default_extra_langs,
            swap_lang=self._default_swap,
            ui_lang="",
        )

    async def get_user_settings(self, user_id: int) -> UserSettings:
        """Fetch saved settings for user_id, falling back to configured defaults."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT source_lang, target_lang, swap_lang, ui_lang, style, extra_langs "
            "FROM user_settings WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return self._defaults_for(user_id)

        src = resolve_language(str(row["source_lang"]), allow_auto=True) or self._default_source
        tgt = resolve_language(str(row["target_lang"]), allow_auto=False) or self._default_target
        style = parse_style(str(row["style"])) or self._default_style
        raw_extra = row["extra_langs"]
        extra_langs = (
            self._default_extra_langs
            if raw_extra is None
            else _parse_stored_extra_langs(str(raw_extra))
        )
        raw_swap = row["swap_lang"]
        if raw_swap is None:
            swap_lang = self._default_swap
        elif str(raw_swap).strip() == "":
            swap_lang = ""
        else:
            swap_lang = resolve_language(str(raw_swap), allow_auto=False) or ""

        raw_ui = row["ui_lang"]
        ui_lang = str(raw_ui).strip().lower() if raw_ui is not None else ""
        if ui_lang not in SUPPORTED_UI_LANGS:
            ui_lang = ""

        return UserSettings(
            user_id=user_id,
            source_lang=src,
            target_lang=tgt,
            style=style,
            extra_langs=extra_langs,
            swap_lang=swap_lang,
            ui_lang=ui_lang,
        )

    async def update_user_settings(
        self,
        user_id: int,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        swap_lang: str | None = None,
        ui_lang: str | None = None,
        style: TranslationStyle | None = None,
        extra_langs: Sequence[str] | None = None,
    ) -> UserSettings:
        """Atomically upsert one or more user preferences and return the resulting settings."""
        conn = self._require_conn()
        async with self._write_lock:
            async with conn.execute(
                "SELECT source_lang, target_lang, swap_lang, ui_lang, style, extra_langs "
                "FROM user_settings WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                existing_row = await cursor.fetchone()

            current = await self.get_user_settings(user_id)
            next_source = current.source_lang
            next_target = current.target_lang
            next_style = current.style
            stored_extra: str | None = (
                None if existing_row is None else existing_row["extra_langs"]
            )
            stored_swap: str | None = (
                None if existing_row is None else existing_row["swap_lang"]
            )
            stored_ui: str | None = (
                None if existing_row is None else existing_row["ui_lang"]
            )
            next_extra = current.extra_langs
            next_swap = current.swap_lang
            next_ui = current.ui_lang

            if source_lang is not None:
                resolved_src = resolve_language(source_lang, allow_auto=True)
                if resolved_src is None:
                    raise ValueError(f"Unsupported source language: {source_lang!r}")
                next_source = resolved_src

            if target_lang is not None:
                resolved_tgt = resolve_language(target_lang, allow_auto=False)
                if resolved_tgt is None:
                    raise ValueError(f"Unsupported target language: {target_lang!r}")
                next_target = resolved_tgt

            if swap_lang is not None:
                next_swap = _normalize_swap_lang_input(swap_lang)
                stored_swap = next_swap

            if ui_lang is not None:
                next_ui = _normalize_ui_lang_input(ui_lang)
                stored_ui = next_ui or None

            if style is not None:
                next_style = style

            if extra_langs is not None:
                next_extra = _normalize_extra_langs_input(extra_langs)
                stored_extra = ",".join(next_extra)

            now_ts = int(time.time())
            await conn.execute(
                """
                INSERT INTO user_settings (
                    user_id, source_lang, target_lang, swap_lang, ui_lang,
                    style, extra_langs, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    source_lang = excluded.source_lang,
                    target_lang = excluded.target_lang,
                    swap_lang = excluded.swap_lang,
                    ui_lang = excluded.ui_lang,
                    style = excluded.style,
                    extra_langs = excluded.extra_langs,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    next_source,
                    next_target,
                    stored_swap,
                    stored_ui,
                    next_style.value,
                    stored_extra,
                    now_ts,
                ),
            )
            await conn.commit()

            return UserSettings(
                user_id=user_id,
                source_lang=next_source,
                target_lang=next_target,
                style=next_style,
                extra_langs=next_extra,
                swap_lang=next_swap,
                ui_lang=next_ui,
            )

    async def delete_user_data(self, user_id: int) -> bool:
        """Delete stored preferences and disabled Business connection rows for user_id."""
        conn = self._require_conn()
        async with self._write_lock:
            cur_settings = await conn.execute(
                "DELETE FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            cur_biz = await conn.execute(
                "DELETE FROM business_connections WHERE user_id = ? AND is_enabled = 0",
                (user_id,),
            )
            await conn.commit()
            return (cur_settings.rowcount or 0) > 0 or (cur_biz.rowcount or 0) > 0

    async def upsert_business_connection(
        self,
        connection_id: str,
        *,
        user_id: int,
        user_chat_id: int,
        can_reply: bool,
        is_enabled: bool,
    ) -> BusinessConnectionRecord:
        """Persist or update metadata for a Telegram Business connection."""
        conn = self._require_conn()
        now_ts = int(time.time())
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO business_connections (
                    connection_id, user_id, user_chat_id, can_reply, is_enabled, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    user_chat_id = excluded.user_chat_id,
                    can_reply = excluded.can_reply,
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    connection_id,
                    user_id,
                    user_chat_id,
                    int(can_reply),
                    int(is_enabled),
                    now_ts,
                ),
            )
            await conn.commit()

        return BusinessConnectionRecord(
            connection_id=connection_id,
            user_id=user_id,
            user_chat_id=user_chat_id,
            can_reply=can_reply,
            is_enabled=is_enabled,
        )

    async def get_business_connection(
        self,
        connection_id: str,
    ) -> BusinessConnectionRecord | None:
        """Look up stored Business connection metadata by connection_id."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT connection_id, user_id, user_chat_id, can_reply, is_enabled
            FROM business_connections
            WHERE connection_id = ?
            """,
            (connection_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return BusinessConnectionRecord(
            connection_id=str(row["connection_id"]),
            user_id=int(row["user_id"]),
            user_chat_id=int(row["user_chat_id"]),
            can_reply=bool(row["can_reply"]),
            is_enabled=bool(row["is_enabled"]),
        )
