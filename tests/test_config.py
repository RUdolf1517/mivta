"""Tests for Config and .env parsing."""

from pathlib import Path

import pytest

from mivta.config import Config, ConfigError, load_dotenv_file
from mivta.translator.base import TranslationStyle


def test_load_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "BOT_TOKEN='123456:ABCDEF'\n"
        'export TRANSLATOR_PROVIDER="deepl"\n'
        "TRANSLATOR_API_KEY=secret:fx\n"
        "EMPTY_LINE=\n",
        encoding="utf-8",
    )
    parsed = load_dotenv_file(env_file)
    assert parsed["BOT_TOKEN"] == "123456:ABCDEF"
    assert parsed["TRANSLATOR_PROVIDER"] == "deepl"
    assert parsed["TRANSLATOR_API_KEY"] == "secret:fx"
    assert parsed["EMPTY_LINE"] == ""


def test_config_defaults_and_env_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=111:from_dotenv\nDEFAULT_TARGET_LANG=de\n",
        encoding="utf-8",
    )
    cfg = Config.from_env(
        env={
            "BOT_TOKEN": "222:from_environ",
            "DEFAULT_STYLE": "formal",
        },
        dotenv_path=env_file,
    )
    assert cfg.bot_token == "222:from_environ"
    assert cfg.default_target_lang == "de"
    assert cfg.default_source_lang == "auto"
    assert cfg.default_style == TranslationStyle.FORMAL
    assert cfg.translator_provider == "google"


@pytest.mark.parametrize(
    ("env_overrides", "expected_substr"),
    [
        ({}, "BOT_TOKEN"),
        ({"BOT_TOKEN": "invalid_token_without_colon"}, "BOT_TOKEN"),
        ({"BOT_TOKEN": "1:abc", "TRANSLATOR_PROVIDER": "unknown"}, "TRANSLATOR_PROVIDER"),
        ({"BOT_TOKEN": "1:abc", "TRANSLATOR_PROVIDER": "openai"}, "TRANSLATOR_API_KEY"),
        ({"BOT_TOKEN": "1:abc", "TRANSLATOR_PROVIDER": "deepl"}, "TRANSLATOR_API_KEY"),
        ({"BOT_TOKEN": "1:abc", "TRANSLATOR_TIMEOUT": "0"}, "TRANSLATOR_TIMEOUT"),
        ({"BOT_TOKEN": "1:abc", "DEFAULT_TARGET_LANG": "auto"}, "DEFAULT_TARGET_LANG"),
        ({"BOT_TOKEN": "1:abc", "DEFAULT_SOURCE_LANG": "zzz"}, "DEFAULT_SOURCE_LANG"),
        ({"BOT_TOKEN": "1:abc", "DEFAULT_STYLE": "poetic"}, "DEFAULT_STYLE"),
        ({"BOT_TOKEN": "1:abc", "MAX_TEXT_LENGTH": "5000"}, "MAX_TEXT_LENGTH"),
    ],
)
def test_config_validation_errors(
    env_overrides: dict[str, str],
    expected_substr: str,
) -> None:
    with pytest.raises(ConfigError, match=expected_substr):
        Config.from_env(env=env_overrides, dotenv_path=None)
