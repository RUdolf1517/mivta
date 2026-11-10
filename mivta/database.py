"""Asynchronous SQLite storage for persistent per-user preferences."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from mivta.translator.base import TranslationStyle, parse_style
from mivta.utils.languages import resolve_language

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    source_lang TEXT NOT NULL DEFAULT 'auto',
    target_lang TEXT NOT NULL DEFAULT 'en',
    style TEXT NOT NULL DEFAULT 'neutral'
        CHECK (style IN ('neutral', 'casual', 'formal')),
    updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class UserSettings:
    """Saved translation preferences for a single Telegram user."""

    user_id: int
    source_lang: str
    target_lang: str
    style: TranslationStyle


class Database:
    """Async SQLite repository storing strictly user settings (never message text)."""

    def __init__(
        self,
        path: str = "data/mivta.db",
        *,
        default_source_lang: str = "auto",
        default_target_lang: str = "en",
        default_style: TranslationStyle = TranslationStyle.NEUTRAL,
    ) -> None:
        self._path = path
        self._default_source = default_source_lang
        self._default_target = default_target_lang
        self._default_style = default_style
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the SQLite database connection and ensure schema exists."""
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
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Close the active SQLite connection cleanly."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before executing queries")
        return self._conn

    def _defaults_for(self, user_id: int) -> UserSettings:
        return UserSettings(
            user_id=user_id,
            source_lang=self._default_source,
            target_lang=self._default_target,
            style=self._default_style,
        )

    async def get_user_settings(self, user_id: int) -> UserSettings:
        """Fetch saved settings for user_id, falling back to configured defaults."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT source_lang, target_lang, style FROM user_settings WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return self._defaults_for(user_id)

        src = resolve_language(str(row["source_lang"]), allow_auto=True) or self._default_source
        tgt = resolve_language(str(row["target_lang"]), allow_auto=False) or self._default_target
        style = parse_style(str(row["style"])) or self._default_style
        return UserSettings(
            user_id=user_id,
            source_lang=src,
            target_lang=tgt,
            style=style,
        )

    async def update_user_settings(
        self,
        user_id: int,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        style: TranslationStyle | None = None,
    ) -> UserSettings:
        """Atomically upsert one or more user preferences and return the resulting settings."""
        conn = self._require_conn()
        async with self._write_lock:
            current = await self.get_user_settings(user_id)
            next_source = current.source_lang
            next_target = current.target_lang
            next_style = current.style

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

            if style is not None:
                next_style = style

            now_ts = int(time.time())
            await conn.execute(
                """
                INSERT INTO user_settings (user_id, source_lang, target_lang, style, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    source_lang = excluded.source_lang,
                    target_lang = excluded.target_lang,
                    style = excluded.style,
                    updated_at = excluded.updated_at
                """,
                (user_id, next_source, next_target, next_style.value, now_ts),
            )
            await conn.commit()

            return UserSettings(
                user_id=user_id,
                source_lang=next_source,
                target_lang=next_target,
                style=next_style,
            )
