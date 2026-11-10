"""Boundary, URL-credential security, truncation entity-clamping, CRLF rebasing,
live TCP webhook runner, and `python -m mivta` entrypoint tests."""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
from typing import Any

import httpx
import pytest
from aiogram.types import MessageEntity, User

import mivta.bot as bot_module
from mivta.bot import ALLOWED_UPDATES, SecretRedactingFilter, run_bot
from mivta.config import Config, ConfigError
from mivta.database import Database
from mivta.translator.base import TranslationRequest
from mivta.utils.cache import TranslationCache
from mivta.utils.formatting import prepare_text_for_translation, restore_formatted_text
from mivta.utils.parser import parse_business_query


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("env_overrides", "match_text"),
    [
        (
            {
                "TRANSLATOR_PROVIDER": "libretranslate",
                "TRANSLATOR_API_URL": "https://user:secret@lt.example.com",
            },
            "must not embed credentials",
        ),
        (
            {
                "TRANSLATOR_PROVIDER": "libretranslate",
                "TRANSLATOR_API_URL": "file:///etc/passwd",
            },
            "must be a valid https://",
        ),
        (
            {
                "TRANSLATOR_PROVIDER": "libretranslate",
                "TRANSLATOR_API_URL": "https://lt.example.com/v1\nheader",
            },
            "whitespace or control",
        ),
        (
            {"WEBHOOK_URL": "https://admin:pw@bot.example.com/hook"},
            "must not embed credentials",
        ),
        (
            {"WEBHOOK_URL": "https://bot.example.com/hook with space"},
            "whitespace or control",
        ),
        (
            {"WEBHOOK_URL": "https://bot.example.com/healthz"},
            "reserved for health probe",
        ),
        (
            {"INLINE_EXTRA_LANGS": "de,fr,es,it"},
            "at most 3 languages",
        ),
        (
            {"INLINE_EXTRA_LANGS": "de,unknownlang"},
            "Unsupported language",
        ),
        (
            {"LOG_LEVEL": "VERBOSE"},
            "LOG_LEVEL",
        ),
        (
            {"TRANSLATOR_TIMEOUT": "not_a_float"},
            "TRANSLATOR_TIMEOUT",
        ),
        (
            {"TRANSLATOR_TIMEOUT": "0"},
            "TRANSLATOR_TIMEOUT",
        ),
    ],
)
def test_url_security_and_operational_config_boundaries(
    env_overrides: dict[str, str],
    match_text: str,
) -> None:
    env = {"BOT_TOKEN": "123:VALID_TOK"}
    env.update(env_overrides)
    with pytest.raises(ConfigError, match=match_text):
        Config.from_env(env=env, dotenv_path=None)


def test_allow_insecure_http_api_lan_override_and_webhook_path_normalization() -> None:
    cfg = Config.from_env(
        env={
            "BOT_TOKEN": "123:VALID_TOK",
            "TRANSLATOR_PROVIDER": "openai",
            "TRANSLATOR_API_KEY": "lan-key",
            "TRANSLATOR_API_URL": "http://10.0.0.15:8000/v1",
            "ALLOW_INSECURE_HTTP_API": "true",
            "WEBHOOK_URL": "https://bot.example.com",
            "WEBHOOK_PATH": "tg/updates",
        },
        dotenv_path=None,
    )
    assert cfg.allow_insecure_http_api is True
    assert cfg.webhook_path == "/tg/updates"
    assert cfg.webhook_url == "https://bot.example.com/tg/updates"


def test_formatting_truncation_entity_clamping_crossing_styles_and_bracket_urls() -> None:
    # 1. Empty text fast-path
    empty_prep = prepare_text_for_translation("")
    assert empty_prep.encoded_text == ""
    assert empty_prep.has_markers is False

    # 2. Bracket-wrapped URL `[https://example.com/docs]` trims outer `]` cleanly
    bracket_prep = prepare_text_for_translation(
        "Read [https://example.com/docs] and [https://example.com/a[1]]!",
        parse_inline_code_markdown=True,
    )
    replacements = [v.replacement for v in bracket_prep.verbatims]
    assert "https://example.com/docs" in replacements
    assert "https://example.com/a[1]" in replacements

    # 3. Crossing style spans: [0, 8) bold and [4, 12) italic cross -> second is skipped,
    #    while nested [0, 8) underline is kept; then truncate at max_length=10!
    src = "ABCDEFGHIJKL @bob_user"
    ents = [
        MessageEntity(type="bold", offset=0, length=12),
        MessageEntity(type="italic", offset=6, length=14),  # crosses bold [0,12) AND @bob_user
        MessageEntity(type="underline", offset=2, length=4),  # cleanly nested inside bold
    ]
    prep = prepare_text_for_translation(src, entities=ents)
    assert [s.entity_template.type for s in prep.styles] == ["bold", "underline"]

    # Simulate translation where bold spans [0..14) and @bob_user is at [15..24),
    # then clamp at max_length=10 -> @bob_user is beyond max_length (dropped) and bold clamps to 10!
    translated = f"<s0>01<s1>2345</s1>6789ABCD</s0> {prep.token_prefix}0X"
    clamped = restore_formatted_text(translated, prep, max_length=10)
    assert clamped.text == "0123456789"
    assert clamped.entities is not None
    by_type = {e.type: (e.offset, e.length) for e in clamped.entities}
    assert by_type["bold"] == (0, 10)
    assert by_type["underline"] == (2, 4)
    assert "mention" not in by_type


def test_parser_crlf_control_chars_and_invalid_utf16_entity_rebasing() -> None:
    # Raw has leading `>de `, CRLF `\r\n`, bare `\r`, bell `\x07`, and emoji `🚀` (2 UTF-16 units)
    raw = ">de \r\nHello\r\n\x07🚀 World  "
    # Compute exact UTF-16 offsets in `raw`:
    # ">de \r\n" = 6, "Hello\r\n\x07" = 8 (total 14), "🚀" = 2 (14..16), " World" = 6 (16..22)
    entities = [
        MessageEntity(type="italic", offset=0, length=3),  # inside stripped `>de` -> dropped
        MessageEntity(type="bold", offset=14, length=8),  # covers "🚀 World"
        MessageEntity(type="underline", offset=15, length=2),  # mid-surrogate offset 15 -> ignored
        MessageEntity(type="code", offset=0, length=999),  # out-of-bounds -> ignored
    ]
    parsed = parse_business_query(raw, entities=entities, ui_lang="en")
    assert parsed is not None
    assert parsed.target_lang == "de"
    assert parsed.text == "Hello\n🚀 World"
    # In sanitized `Hello\n🚀 World`: "Hello\n" is 6 UTF-16 units, "🚀 World" is 2 + 6 = 8 units!
    assert len(parsed.entities) == 1
    assert parsed.entities[0].type == "bold"
    assert (parsed.entities[0].offset, parsed.entities[0].length) == (6, 8)


async def test_database_validation_errors_and_disabled_cache() -> None:
    disabled_cache = TranslationCache(max_size=0, ttl_seconds=60)
    assert disabled_cache.enabled is False
    req = TranslationRequest(text="hi", target_lang="ru")
    assert disabled_cache.get("google", req) is None

    with pytest.raises(ValueError, match="Unsupported swap language"):
        Database(":memory:", default_swap_lang="zz")
    with pytest.raises(ValueError, match="Unsupported extra target language"):
        Database(":memory:", default_extra_langs=("zz",))
    with pytest.raises(ValueError, match="At most 3"):
        Database(":memory:", default_extra_langs=("de", "fr", "es", "it"))

    db = Database(":memory:")
    await db.connect()
    try:
        with pytest.raises(ValueError, match="Unsupported UI language"):
            await db.update_user_settings(1, ui_lang="esperanto")
        with pytest.raises(ValueError, match="Unsupported swap language"):
            await db.update_user_settings(1, swap_lang="zz")
    finally:
        await db.close()


async def test_live_webhook_runner_binds_port_sets_secret_serves_healthz_and_cleans_up(
    monkeypatch,
    tmp_path,
) -> None:
    port = _free_tcp_port()
    cfg = Config(
        bot_token="123:WEBHOOK_BOT_TOK",
        database_path=str(tmp_path / "wh.db"),
        webhook_url="https://bot.example.com/tg-hook",
        webhook_path="/tg-hook",
        webhook_host="127.0.0.1",
        webhook_port=port,
        webhook_secret_token="",  # Must auto-generate 32-byte urlsafe secret!
    )
    webhook_registrations: list[dict[str, Any]] = []
    ready = asyncio.Event()

    class FakeSession:
        async def close(self) -> None:
            return None

    class FakeWebhookBot:
        def __init__(self, token: str) -> None:
            self.token = token
            self.session = FakeSession()

        async def get_me(self) -> User:
            return User(
                id=7,
                is_bot=True,
                first_name="mivta",
                username="mivta_wh_bot",
                supports_inline_queries=True,
            )

        async def set_my_commands(self, commands: Any, language_code: str | None = None) -> bool:
            return True

        async def set_webhook(
            self,
            *,
            url: str,
            secret_token: str,
            allowed_updates: list[str],
        ) -> bool:
            webhook_registrations.append(
                {
                    "url": url,
                    "secret_token": secret_token,
                    "allowed_updates": allowed_updates,
                }
            )
            ready.set()
            return True

    monkeypatch.setattr(bot_module, "Bot", FakeWebhookBot)
    runner_task = asyncio.create_task(run_bot(cfg))
    await asyncio.wait_for(ready.wait(), timeout=5.0)

    assert len(webhook_registrations) == 1
    reg = webhook_registrations[0]
    assert reg["url"] == "https://bot.example.com/tg-hook"
    assert len(reg["secret_token"]) >= 32
    assert reg["allowed_updates"] == ALLOWED_UPDATES

    # Probe live TCP HTTP server on 127.0.0.1:<port>/healthz and unauthenticated /tg-hook
    async with httpx.AsyncClient() as client:
        h_resp = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert h_resp.status_code == 200
        assert h_resp.json()["status"] == "ok"

        forged = await client.post(f"http://127.0.0.1:{port}/tg-hook", json={"update_id": 1})
        assert forged.status_code == 401

    runner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner_task


def test_dunder_main_module_cli_and_traceback_secret_redaction() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "mivta", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "mivta 0.3.0" in proc.stdout

    # Verify SecretRedactingFilter also scrubs cached exc_text tracebacks
    filt = SecretRedactingFilter(("sk-TRACEBACK_SECRET_9999",))
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), None)
    rec.exc_text = "RuntimeError: key=sk-TRACEBACK_SECRET_9999 leaked"
    assert filt.filter(rec) is True
    assert "sk-TRACEBACK_SECRET_9999" not in rec.exc_text
    assert "[REDACTED]" in rec.exc_text
