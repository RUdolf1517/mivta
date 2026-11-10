"""Tests for all Unicode scripts, complex entity/placeholder collisions, DB migrations,
Config edge cases, run_bot lifecycle, and install.sh v0.3.0 CLI flags."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest
from aiogram.types import MessageEntity, User

import mivta.bot as bot_module
from mivta.bot import (
    _cache_janitor,
    build_webhook_app,
    configure_logging,
    create_dispatcher,
    main,
    run_bot,
)
from mivta.config import Config, ConfigError, load_dotenv_file
from mivta.database import Database
from mivta.translator.google import GoogleTranslator
from mivta.utils.cache import TranslationCache
from mivta.utils.formatting import prepare_text_for_translation, restore_formatted_text
from mivta.utils.i18n import parse_ui_lang, resolve_ui_lang, suggest_swap_lang
from mivta.utils.languages import (
    detect_single_script,
    language_english_name,
    language_label,
    language_name,
    normalize_detected_code,
    resolve_auto_swap_target,
)
from mivta.utils.parser import parse_business_query, parse_inline_query

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


@pytest.mark.parametrize(
    ("sample", "expected_script", "target", "swap", "expected_pair"),
    [
        ("Żółć i gęś", "latin", "en", "ru", ("ru", "")),
        ("Привет мир", "cyrillic", "en", "ru", ("en", "")),
        ("שלום עולם", "hebrew", "he", "en", ("en", "")),
        ("مرحبا بالعالم", "arabic", "ar", "en", ("en", "")),
        ("こんにちは世界", "cjk_kana", "ja", "en", ("en", "")),
        ("안녕하세요", "hangul", "ko", "en", ("en", "")),
        ("Καλημέρα κόσμε", "greek", "el", "en", ("en", "")),
        ("გამარჯობა", "georgian", "ka", "en", ("en", "")),
        ("Բարև աշխարհ", "armenian", "hy", "en", ("en", "")),
        ("नमस्ते दुनिया", "devanagari", "hi", "en", ("en", "")),
        ("สวัสดีครับ", "thai", "th", "en", ("en", "")),
        # Mixed Latin + Cyrillic falls back to provider-side detection safely
        ("Hello Привет", None, "en", "ru", ("en", "ru")),
        # Same-script Latin pair delegates to provider-side detection
        ("Dzień dobry", "latin", "en", "pl", ("en", "pl")),
    ],
)
def test_all_unicode_scripts_and_auto_swap_resolution(
    sample: str,
    expected_script: str | None,
    target: str,
    swap: str,
    expected_pair: tuple[str, str],
) -> None:
    assert detect_single_script(sample) == expected_script
    resolved = resolve_auto_swap_target(
        sample,
        source_lang="auto",
        target_lang=target,
        swap_lang=swap,
    )
    assert resolved == expected_pair


def test_language_labels_detected_codes_and_i18n_helpers() -> None:
    assert normalize_detected_code(None) == "auto"
    assert normalize_detected_code("pt_BR") == "pt"
    assert normalize_detected_code("iw-IL") == "he"
    assert normalize_detected_code("de-AT") == "de"
    assert normalize_detected_code("xyz") == "xyz"

    assert language_label("auto", ui_lang="en") == "Auto (auto)"
    assert language_label("auto", ui_lang="ru") == "Авто (auto)"
    assert language_label("pl", ui_lang="en") == "Polish (pl)"
    assert language_label("pl", ui_lang="ru") == "Польский (pl)"
    assert language_label("xx", ui_lang="en") == "xx"
    assert language_name("auto", ui_lang="en") == "Auto"
    assert language_name("xx", ui_lang="ru") == "xx"
    assert language_english_name("xx") == "xx"

    assert parse_ui_lang("English") == "en"
    assert parse_ui_lang("Русский") == "ru"
    assert parse_ui_lang("авто") == ""
    assert parse_ui_lang("esperanto") is None

    assert resolve_ui_lang("", "be-BY", default_ui_lang="en") == "ru"
    assert resolve_ui_lang("", "kk", default_ui_lang="en") == "ru"
    assert resolve_ui_lang("", "es-MX", default_ui_lang="ru") == "en"
    assert resolve_ui_lang("", None, default_ui_lang="en") == "en"

    assert suggest_swap_lang("pl-PL", target_lang="en") == "pl"
    # Already same as target_lang or missing -> ""
    assert suggest_swap_lang("en-US", target_lang="en") == ""
    assert suggest_swap_lang(None, target_lang="en") == ""


def test_adversarial_formatting_token_collision_crossing_spans_and_guillemets() -> None:
    # 1. Input already contains literal MIVTAK and MIVTAK1Z tokens + code + email + wiki URL
    raw = (
        "Keep MIVTAK and MIVTAK1Z safe, email dev@example.org, "
        "see (https://en.wikipedia.org/wiki/C_(programming_language)), "
        "and run ```python\nprint(1)\n```!"
    )
    prep = prepare_text_for_translation(raw, parse_inline_code_markdown=True)
    assert prep.token_prefix == "MIVTAK2Z"

    # Simulate MT engine replacing <sN> with French guillemets «sN» and adding inner spaces
    styled_text = "🌟 Важное сообщение для @alice_dev"
    # 🌟 is 2 UTF-16 code units; "🌟 " is 3 UTF-16 code units; "Важное" is 6 chars
    entities = [
        MessageEntity(type="bold", offset=3, length=6),
        # Partially crossing span over '@alice_dev' verbatim must be safely skipped!
        MessageEntity(type="italic", offset=20, length=16),
    ]
    prep_styled = prepare_text_for_translation(styled_text, entities=entities)
    assert prep_styled.has_xml_style_tags is True
    # Only the non-crossing bold span was accepted
    assert len(prep_styled.styles) == 1

    simulated_mt = f"🌟 «s0» Important «/s0» message for {prep_styled.token_prefix}0X"
    restored = restore_formatted_text(simulated_mt, prep_styled)
    assert restored.text == "🌟 Important message for @alice_dev"
    assert restored.entities is not None
    bold_ent = next(e for e in restored.entities if e.type == "bold")
    assert bold_ent.offset == 3 and bold_ent.length == len("Important")


def test_parser_edge_cases_multiline_double_gt_and_localized_errors() -> None:
    # `>> quote` without space is NOT treated as bare `>` trigger (avoids eating `>>` quotes),
    # whereas `> > quote` strips the trigger `>` and translates `> quote`.
    assert parse_business_query(">> Цитируемый текст") is None
    b_quote = parse_business_query("> > Цитируемый текст")
    assert b_quote is not None and b_quote.text == "> Цитируемый текст"

    # Unknown source in pair directive localized in EN vs RU
    err_en = parse_inline_query("zz>en hello", ui_lang="en")
    assert err_en.error is not None and "Unknown source language code" in err_en.error
    err_ru = parse_inline_query("zz>en hello", ui_lang="ru")
    assert err_ru.error is not None and "Неизвестный код исходного языка" in err_ru.error


@pytest.mark.parametrize(
    ("overrides", "err_match"),
    [
        ({"DEFAULT_SWAP_LANG": "invalid_code"}, "DEFAULT_SWAP_LANG"),
        ({"DEFAULT_UI_LANG": "fr"}, "DEFAULT_UI_LANG"),
        ({"INLINE_DEBOUNCE_MS": "-5"}, "INLINE_DEBOUNCE_MS"),
        ({"INLINE_DEBOUNCE_MS": "5000"}, "INLINE_DEBOUNCE_MS"),
        ({"INLINE_DEBOUNCE_MS": "abc"}, "INLINE_DEBOUNCE_MS"),
        ({"INLINE_STYLE_VARIANTS": "maybe"}, "INLINE_STYLE_VARIANTS"),
        ({"WEBHOOK_PORT": "70000"}, "WEBHOOK_PORT"),
        ({"WEBHOOK_SECRET_TOKEN": "bad space!"}, "WEBHOOK_SECRET_TOKEN"),
        ({"TRANSLATOR_FALLBACK_PROVIDER": "unknown_fb"}, "TRANSLATOR_FALLBACK_PROVIDER"),
    ],
)
def test_config_v030_validation_errors_and_dotenv_escapes(
    overrides: dict[str, str],
    err_match: str,
    tmp_path: Path,
) -> None:
    base_env = {"BOT_TOKEN": "123:VALID_TOKEN"}
    base_env.update(overrides)
    with pytest.raises(ConfigError, match=err_match):
        Config.from_env(env=base_env, dotenv_path=None)

    env_file = tmp_path / "custom.env"
    env_file.write_text(
        'export FOO="value with spaces"\nBAR=\'single_quoted\'\n# comment\nINVALID_NO_EQUALS\n',
        encoding="utf-8",
    )
    loaded = load_dotenv_file(env_file)
    assert loaded["FOO"] == "value with spaces"
    assert loaded["BAR"] == "single_quoted"


async def test_database_v020_to_v030_migration_delete_user_data_and_unconnected_guard(
    tmp_path: Path,
) -> None:
    unconnected = Database(":memory:")
    with pytest.raises(RuntimeError, match=r"connect\(\)"):
        await unconnected.get_user_settings(1)

    # Create a v0.2.0 SQLite database (has extra_langs, lacks swap_lang and ui_lang)
    db_file = tmp_path / "v020.db"
    async with aiosqlite.connect(db_file) as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE user_settings (
                user_id     INTEGER PRIMARY KEY,
                source_lang TEXT NOT NULL DEFAULT 'auto',
                target_lang TEXT NOT NULL DEFAULT 'de',
                style       TEXT NOT NULL DEFAULT 'formal',
                extra_langs TEXT NOT NULL DEFAULT 'pl',
                updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            """
        )
        await raw_conn.execute(
            "INSERT INTO user_settings (user_id, source_lang, target_lang, style, extra_langs) "
            "VALUES (77, 'auto', 'de', 'formal', 'pl');"
        )
        await raw_conn.commit()

    db = Database(db_file, default_swap_lang="ru")
    await db.connect()
    try:
        migrated = await db.get_user_settings(77)
        assert migrated.target_lang == "de"
        assert migrated.extra_langs == ("pl",)
        assert migrated.swap_lang == "ru"
        assert migrated.ui_lang == ""

        # Delete existing row -> True; delete again -> False
        assert await db.delete_user_data(77) is True
        assert await db.delete_user_data(77) is False
    finally:
        await db.close()


async def test_bot_lifecycle_polling_janitor_cli_error_and_empty_webhook_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # 1. Empty secret_token in build_webhook_app raises ValueError
    cfg = Config(bot_token="123:TOK", database_path=str(tmp_path / "bot.db"), cache_ttl=10)
    db = Database(":memory:")
    await db.connect()
    try:
        async with httpx.AsyncClient() as client:
            dp = create_dispatcher(config=cfg, db=db, translator=GoogleTranslator(client))
            fake_bot = bot_module.Bot(token="123:TOK")
            with pytest.raises(ValueError, match="secret_token"):
                build_webhook_app(dp, fake_bot, secret_token="")
            await fake_bot.session.close()
    finally:
        await db.close()

    # 2. _cache_janitor purges expired entries cleanly and cancels cleanly
    cache = TranslationCache(max_size=16, ttl_seconds=1)
    janitor = asyncio.create_task(_cache_janitor(cache, 0.01))
    await asyncio.sleep(0.03)
    janitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await janitor

    # 3. main() returns exit code 2 when BOT_TOKEN is missing
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 2

    # 4. Full run_bot() polling startup & shutdown lifecycle with mocked Bot & Dispatcher
    events: list[str] = []

    class FakeSession:
        async def close(self) -> None:
            events.append("session_closed")

    class FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token
            self.session = FakeSession()

        async def get_me(self) -> User:
            events.append("get_me")
            return User(
                id=1,
                is_bot=True,
                first_name="mivta",
                username="mivta_test_bot",
                supports_inline_queries=False,
            )

        async def set_my_commands(self, commands: Any, language_code: str | None = None) -> bool:
            events.append(f"cmds:{language_code or 'default'}")
            return True

        async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
            events.append("delete_webhook")
            return True

    async def fake_start_polling(self: Any, bot: Any, **kwargs: Any) -> None:
        events.append("start_polling")

    monkeypatch.setattr(bot_module, "Bot", FakeBot)
    monkeypatch.setattr(bot_module.Dispatcher, "start_polling", fake_start_polling)
    configure_logging("INFO", secrets_to_redact=("123:TOK",))
    await run_bot(cfg)

    assert "get_me" in events
    assert "cmds:default" in events
    assert "cmds:ru" in events
    assert "delete_webhook" in events
    assert "start_polling" in events
    assert "session_closed" in events


def test_install_sh_v030_swap_and_webhook_cli_flags(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Non-root --no-systemd test")

    target_dir = tmp_path / "mivta-wh"
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--no-systemd",
            "--non-interactive",
            "--from-local",
            str(REPO_ROOT),
            "--dir",
            str(target_dir),
            "--token",
            "777777:WEBHOOK_TEST_TOKEN",
            "--swap-lang",
            "pl",
            "--webhook-url",
            "https://mivta.example.com/hook",
            "--webhook-secret",
            "Sec_Token_99",
            "--webhook-port",
            "8443",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    env_text = (target_dir / ".env").read_text(encoding="utf-8")
    assert "DEFAULT_SWAP_LANG=pl" in env_text
    assert "WEBHOOK_URL=https://mivta.example.com/hook" in env_text
    assert "WEBHOOK_SECRET_TOKEN=Sec_Token_99" in env_text
    assert "WEBHOOK_PORT=8443" in env_text
