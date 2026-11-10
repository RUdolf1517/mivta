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

