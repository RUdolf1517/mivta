"""Tests for async SQLite persistence of user preferences."""

from pathlib import Path

import pytest

from mivta.database import Database
from mivta.translator.base import TranslationStyle


async def test_database_defaults_and_persistence_across_restarts(tmp_path: Path) -> None:
    db_path = str(tmp_path / "sub" / "settings.db")
    db1 = Database(
        db_path,
        default_source_lang="auto",
        default_target_lang="en",
        default_style=TranslationStyle.NEUTRAL,
    )
    await db1.connect()

    # Unseen user gets defaults
    initial = await db1.get_user_settings(42)
    assert initial.user_id == 42
    assert initial.source_lang == "auto"
    assert initial.target_lang == "en"
    assert initial.style == TranslationStyle.NEUTRAL

    # Update target, manual source, and style
    updated = await db1.update_user_settings(
        42,
        source_lang="ru",
        target_lang="de",
        style=TranslationStyle.FORMAL,
    )
    assert updated.source_lang == "ru"
    assert updated.target_lang == "de"
    assert updated.style == TranslationStyle.FORMAL
    await db1.close()

    # Reconnect a fresh Database instance to verify persistence across restarts
    db2 = Database(db_path)
    await db2.connect()
    reloaded = await db2.get_user_settings(42)
    assert reloaded.source_lang == "ru"
    assert reloaded.target_lang == "de"
    assert reloaded.style == TranslationStyle.FORMAL

    # Switch source back to auto while keeping target_lang and style intact
    re_auto = await db2.update_user_settings(42, source_lang="auto")
    assert re_auto.source_lang == "auto"
    assert re_auto.target_lang == "de"
    assert re_auto.style == TranslationStyle.FORMAL
    await db2.close()


async def test_database_rejects_invalid_languages() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        with pytest.raises(ValueError, match="Unsupported target language"):
            await db.update_user_settings(1, target_lang="auto")
        with pytest.raises(ValueError, match="Unsupported source language"):
            await db.update_user_settings(1, source_lang="invalid_code")
    finally:
        await db.close()


async def test_database_business_connections(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "biz.db"))
    await db.connect()
    try:
        assert await db.get_business_connection("bc-1") is None
        saved = await db.upsert_business_connection(
            "bc-1",
            user_id=777,
            user_chat_id=777,
            can_reply=True,
            is_enabled=True,
        )
        assert saved.can_reply is True
        assert saved.is_enabled is True

        updated = await db.upsert_business_connection(
            "bc-1",
            user_id=777,
            user_chat_id=777,
            can_reply=False,
            is_enabled=False,
        )
        assert updated.can_reply is False
        assert updated.is_enabled is False
        fetched = await db.get_business_connection("bc-1")
        assert fetched == updated
    finally:
        await db.close()


async def test_database_extra_langs_and_v010_schema_migration(tmp_path: Path) -> None:
    import aiosqlite

    legacy_path = str(tmp_path / "legacy_v010.db")
    # Create a v0.1.0 database WITHOUT `extra_langs` column
    async with aiosqlite.connect(legacy_path) as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE user_settings (
                user_id INTEGER PRIMARY KEY,
                source_lang TEXT NOT NULL DEFAULT 'auto',
                target_lang TEXT NOT NULL DEFAULT 'en',
                style TEXT NOT NULL DEFAULT 'neutral',
                updated_at INTEGER NOT NULL
            )
            """
        )
        await raw_conn.execute(
            "INSERT INTO user_settings (user_id, source_lang, target_lang, style, updated_at) "
            "VALUES (777, 'ru', 'de', 'formal', 1700000000)"
        )
        await raw_conn.commit()

    db = Database(legacy_path, default_extra_langs=("pl", "fr"))
    await db.connect()
    try:
        # Migrated row inherits default_extra_langs while extra_langs IS NULL
        migrated = await db.get_user_settings(777)
        assert migrated.target_lang == "de"
        assert migrated.style == TranslationStyle.FORMAL
        assert migrated.extra_langs == ("pl", "fr")

        # Explicit user customization overrides defaults, including clearing to ()
        customized = await db.update_user_settings(777, extra_langs=("es", "ja"))
        assert customized.extra_langs == ("es", "ja")

        cleared = await db.update_user_settings(777, extra_langs=())
        assert cleared.extra_langs == ()
    finally:
        await db.close()

