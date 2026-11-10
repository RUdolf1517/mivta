"""Application configuration loaded from environment variables (and optional .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mivta.translator.base import TranslationStyle, parse_style
from mivta.utils.languages import resolve_language

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"google", "openai", "deepl", "libretranslate"}
)
VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class ConfigError(ValueError):
    """Raised when configuration values are missing or invalid."""


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without external dependencies."""
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _parse_int(name: str, raw: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    if maximum is not None and val > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {val}")
    return val


def _parse_float(
    name: str,
    raw: str,
    *,
    minimum: float = 0.0,
    exclusive_min: bool = False,
) -> float:
    try:
        val = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if exclusive_min and val <= minimum:
        raise ConfigError(f"{name} must be > {minimum}, got {val}")
    if not exclusive_min and val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    return val


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime settings for mivta."""

    bot_token: str
    translator_provider: str = "google"
    translator_api_key: str = ""
    translator_api_url: str = ""
    translator_model: str = "gpt-4o-mini"
    translator_timeout: float = 5.0

    default_source_lang: str = "auto"
    default_target_lang: str = "en"
    default_style: TranslationStyle = TranslationStyle.NEUTRAL

    database_path: str = "data/mivta.db"
    max_text_length: int = 500
    cache_ttl: float = 300.0
    cache_max_size: int = 512
    rate_limit_per_minute: float = 30.0
    rate_limit_burst: int = 15
    max_concurrent_translations: int = 20
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | None = Path(".env"),
    ) -> Config:
        """Load and validate configuration from environment variables (precedence over .env)."""
        merged: dict[str, str] = {}
        if dotenv_path is not None:
            merged.update(load_dotenv_file(dotenv_path))
        source_env = os.environ if env is None else env
        merged.update(source_env)

        bot_token = merged.get("BOT_TOKEN", "").strip()
        if not bot_token or ":" not in bot_token:
            raise ConfigError(
                "BOT_TOKEN is missing or invalid (expected Telegram token format '<id>:<secret>')"
            )

        provider = merged.get("TRANSLATOR_PROVIDER", "google").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ConfigError(
                f"Unsupported TRANSLATOR_PROVIDER={provider!r}; expected one of: {supported}"
            )

        api_key = merged.get("TRANSLATOR_API_KEY", "").strip()
        if provider in ("openai", "deepl") and not api_key:
            raise ConfigError(
                f"TRANSLATOR_API_KEY is required when TRANSLATOR_PROVIDER={provider!r}"
            )

        api_url = merged.get("TRANSLATOR_API_URL", "").strip().rstrip("/")
        model = merged.get("TRANSLATOR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        timeout = _parse_float(
            "TRANSLATOR_TIMEOUT",
            merged.get("TRANSLATOR_TIMEOUT", "5.0"),
            minimum=0.0,
            exclusive_min=True,
        )

        raw_source = merged.get("DEFAULT_SOURCE_LANG", "auto").strip()
        default_source = resolve_language(raw_source, allow_auto=True)
        if default_source is None:
            raise ConfigError(f"Unsupported DEFAULT_SOURCE_LANG={raw_source!r}")

        raw_target = merged.get("DEFAULT_TARGET_LANG", "en").strip()
        default_target = resolve_language(raw_target, allow_auto=False)
        if default_target is None:
            raise ConfigError(f"Unsupported DEFAULT_TARGET_LANG={raw_target!r}")

        raw_style = merged.get("DEFAULT_STYLE", "neutral").strip()
        default_style = parse_style(raw_style)
        if default_style is None:
            raise ConfigError(
                f"Unsupported DEFAULT_STYLE={raw_style!r}; expected neutral, casual, or formal"
            )

        database_path = merged.get("DATABASE_PATH", "data/mivta.db").strip()
        if not database_path:
            raise ConfigError("DATABASE_PATH must not be empty")

        max_text_length = _parse_int(
            "MAX_TEXT_LENGTH",
            merged.get("MAX_TEXT_LENGTH", "500"),
            minimum=1,
            maximum=4096,
        )
        cache_ttl = _parse_float(
            "CACHE_TTL",
            merged.get("CACHE_TTL", "300"),
            minimum=0.0,
        )
        cache_max_size = _parse_int(
            "CACHE_MAX_SIZE",
            merged.get("CACHE_MAX_SIZE", "512"),
            minimum=0,
        )
        rate_limit_per_minute = _parse_float(
            "RATE_LIMIT_PER_MINUTE",
            merged.get("RATE_LIMIT_PER_MINUTE", "30"),
            minimum=0.0,
        )
        rate_limit_burst = _parse_int(
            "RATE_LIMIT_BURST",
            merged.get("RATE_LIMIT_BURST", "15"),
            minimum=0,
        )
        max_concurrent = _parse_int(
            "MAX_CONCURRENT_TRANSLATIONS",
            merged.get("MAX_CONCURRENT_TRANSLATIONS", "20"),
            minimum=1,
        )

        log_level = merged.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"Unsupported LOG_LEVEL={log_level!r}; expected one of {sorted(VALID_LOG_LEVELS)}"
            )

        return cls(
            bot_token=bot_token,
            translator_provider=provider,
            translator_api_key=api_key,
            translator_api_url=api_url,
            translator_model=model,
            translator_timeout=timeout,
            default_source_lang=default_source,
            default_target_lang=default_target,
            default_style=default_style,
            database_path=database_path,
            max_text_length=max_text_length,
            cache_ttl=cache_ttl,
            cache_max_size=cache_max_size,
            rate_limit_per_minute=rate_limit_per_minute,
            rate_limit_burst=rate_limit_burst,
            max_concurrent_translations=max_concurrent,
            log_level=log_level,
        )
