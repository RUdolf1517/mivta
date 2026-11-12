"""Application configuration loaded from environment variables (and optional .env)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mivta.translator.base import (
    LLM_PROVIDER_DEFAULTS,
    ProviderSpec,
    TranslationStyle,
    parse_style,
)
from mivta.utils.i18n import SUPPORTED_UI_LANGS
from mivta.utils.languages import resolve_language

KEYED_PROVIDERS: frozenset[str] = frozenset({*LLM_PROVIDER_DEFAULTS.keys(), "deepl"})
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {*KEYED_PROVIDERS, "google", "libretranslate", "mymemory"}
)
VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_DISABLE_TOKENS: frozenset[str] = frozenset({"off", "none", "false", "-"})
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

MAX_INLINE_EXTRA_LANGS = 3
MAX_GLOSSARY_TERMS = 100


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


def _parse_bool(name: str, raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _parse_owner_ids(raw: str) -> tuple[int, ...]:
    if not raw.strip() or raw.strip().lower() in _DISABLE_TOKENS:
        return ()
    ids: list[int] = []
    for piece in raw.replace(";", ",").split(","):
        token = piece.strip()
        if not token:
            continue
        try:
            uid = int(token)
        except ValueError as exc:
            raise ConfigError(
                f"OWNER_ID must be a positive integer Telegram ID, got {token!r}"
            ) from exc
        if uid <= 0:
            raise ConfigError(f"OWNER_ID must be > 0, got {uid}")
        if uid not in ids:
            ids.append(uid)
    return tuple(ids)


def _parse_glossary(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    seen: set[str] = set()
    terms: list[str] = []
    for piece in raw.split(","):
        term = piece.strip()
        if not term:
            continue
        if term not in seen:
            seen.add(term)
            terms.append(term)
    if len(terms) > MAX_GLOSSARY_TERMS:
        raise ConfigError(
            f"TRANSLATOR_GLOSSARY supports at most {MAX_GLOSSARY_TERMS} terms, got {len(terms)}"
        )
    return tuple(terms)


def _parse_extra_langs(name: str, raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    langs: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        token = piece.strip()
        if not token:
            continue
        resolved = resolve_language(token, allow_auto=False)
        if resolved is None:
            raise ConfigError(f"Unsupported language {token!r} in {name}")
        if resolved not in langs:
            langs.append(resolved)
    if len(langs) > MAX_INLINE_EXTRA_LANGS:
        raise ConfigError(
            f"{name} supports at most {MAX_INLINE_EXTRA_LANGS} languages, got {len(langs)}"
        )
    return tuple(langs)


def _validate_api_url(
    name: str,
    url: str,
    *,
    has_api_key: bool,
    allow_insecure_http: bool,
) -> str:
    if not url:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ConfigError(f"{name} must not contain whitespace or control characters")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(
            f"{name} must be a valid https:// (or local http://) URL, got {url!r}"
        )
    if parts.username is not None or parts.password is not None:
        raise ConfigError(
            f"{name} must not embed credentials in the URL; use API_KEY variables instead"
        )
    host = parts.hostname.lower()
    if (
        scheme == "http"
        and has_api_key
        and host not in _LOOPBACK_HOSTS
        and not allow_insecure_http
    ):
        raise ConfigError(
            f"{name} refuses unencrypted http:// to remote host {host!r} when an API key "
            "is configured; use https:// (or set ALLOW_INSECURE_HTTP_API=true on a private LAN)"
        )
    return url


def _normalize_webhook(raw_url: str, raw_path: str | None) -> tuple[str, str]:
    """Validate and align (webhook_url, webhook_path) for Telegram Webhook mode."""
    explicit_path = (raw_path or "").strip()
    if explicit_path and not explicit_path.startswith("/"):
        explicit_path = "/" + explicit_path

    if not raw_url:
        return "", explicit_path or "/webhook"

    if any(ch.isspace() or ord(ch) < 32 for ch in raw_url):
        raise ConfigError("WEBHOOK_URL must not contain whitespace or control characters")

    parts = urlsplit(raw_url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ConfigError(
            f"WEBHOOK_URL must be an https:// URL (required by Telegram Bot API), got {raw_url!r}"
        )
    if parts.username is not None or parts.password is not None:
        raise ConfigError("WEBHOOK_URL must not embed credentials")

    url_path = parts.path.rstrip("/") if parts.path and parts.path != "/" else ""
    if explicit_path:
        final_path = explicit_path
    elif url_path:
        final_path = url_path
    else:
        final_path = "/webhook"

    if final_path == "/healthz":
        raise ConfigError("WEBHOOK_PATH cannot be '/healthz' (reserved for health probe)")

    full_url = urlunsplit(("https", parts.netloc, final_path, parts.query, ""))
    return full_url, final_path


def _parse_provider_list(raw: str, *, var_name: str) -> tuple[str, ...]:
    supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
    items: list[str] = []
    for piece in raw.split(","):
        item = piece.strip().lower()
        if not item:
            continue
        if item not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"Unsupported {var_name}={item!r}; expected one of: {supported}"
            )
        if item not in items:
            items.append(item)
    return tuple(items)


def _resolve_chain_names(merged: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    """Resolve `(primary_provider, fallback_providers)` from env variables."""
    raw_chain = merged.get("TRANSLATOR_CHAIN", "").strip()
    if raw_chain:
        parsed_chain = _parse_provider_list(raw_chain, var_name="TRANSLATOR_CHAIN")
        if not parsed_chain:
            raise ConfigError("TRANSLATOR_CHAIN must name at least one provider")
        return parsed_chain[0], parsed_chain[1:]

    provider = merged.get("TRANSLATOR_PROVIDER", "google").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ConfigError(
            f"Unsupported TRANSLATOR_PROVIDER={provider!r}; expected one of: {supported}"
        )

    has_explicit_fb = "TRANSLATOR_FALLBACK_PROVIDER" in merged
    raw_fb = merged.get("TRANSLATOR_FALLBACK_PROVIDER", "").strip()
    if raw_fb.lower() in _DISABLE_TOKENS:
        return provider, ()
    if raw_fb:
        parsed_fb = _parse_provider_list(raw_fb, var_name="TRANSLATOR_FALLBACK_PROVIDER")
        return provider, tuple(p for p in parsed_fb if p != provider)

    # Zero-config out-of-the-box resilience: `google` automatically falls back to `mymemory`
    # unless TRANSLATOR_FALLBACK_PROVIDER was explicitly set (e.g. to `none` or empty).
    if provider == "google" and not has_explicit_fb:
        return provider, ("mymemory",)
    return provider, ()


def _build_provider_specs(
    primary: str,
    fallbacks: tuple[str, ...],
    merged: Mapping[str, str],
    *,
    allow_insecure_http: bool,
) -> tuple[ProviderSpec, ...]:
    """Build validated per-provider specs supporting both shared and `<PROVIDER>_*` env vars."""
    shared_primary_key = merged.get("TRANSLATOR_API_KEY", "").strip()
    shared_primary_url = merged.get("TRANSLATOR_API_URL", "").strip().rstrip("/")
    shared_primary_model = merged.get("TRANSLATOR_MODEL", "").strip()

    shared_fb_key = merged.get("TRANSLATOR_FALLBACK_API_KEY", "").strip()
    shared_fb_url = merged.get("TRANSLATOR_FALLBACK_API_URL", "").strip().rstrip("/")
    shared_fb_model = merged.get("TRANSLATOR_FALLBACK_MODEL", "").strip()
    mymemory_email = merged.get("MYMEMORY_EMAIL", "").strip()

    ordered = (primary, *fallbacks)
    specs: list[ProviderSpec] = []
    for idx, prov in enumerate(ordered):
        prefix = prov.upper()
        prov_key = merged.get(f"{prefix}_API_KEY", "").strip()
        prov_url = merged.get(f"{prefix}_API_URL", "").strip().rstrip("/")
        prov_model = merged.get(f"{prefix}_MODEL", "").strip()
        preset_url, preset_model = LLM_PROVIDER_DEFAULTS.get(prov, ("", "gpt-4o-mini"))

        if idx == 0:
            key = shared_primary_key or prov_key
            raw_url = shared_primary_url or prov_url
            url_var = "TRANSLATOR_API_URL" if shared_primary_url else f"{prefix}_API_URL"
            model = shared_primary_model or prov_model or preset_model
            if prov in KEYED_PROVIDERS and not key:
                raise ConfigError(
                    f"TRANSLATOR_API_KEY (or {prefix}_API_KEY) is required when "
                    f"primary provider is {prov!r}"
                )
        else:
            if prov == "google":
                key = ""
            elif prov == "mymemory":
                key = prov_key
            else:
                key = prov_key or shared_fb_key

            if prov_url:
                raw_url = prov_url
                url_var = f"{prefix}_API_URL"
            elif shared_fb_url and len(fallbacks) == 1:
                raw_url = shared_fb_url
                url_var = "TRANSLATOR_FALLBACK_API_URL"
            else:
                raw_url = ""
                url_var = f"{prefix}_API_URL"

            default_fb_model = preset_model if prov in LLM_PROVIDER_DEFAULTS else ""
            model = prov_model or shared_fb_model or default_fb_model
            if prov in KEYED_PROVIDERS and not key:
                raise ConfigError(
                    f"TRANSLATOR_FALLBACK_API_KEY (or {prefix}_API_KEY) is required when "
                    f"fallback provider includes {prov!r}"
                )

        if raw_url:
            _validate_api_url(
                url_var,
                raw_url,
                has_api_key=bool(key),
                allow_insecure_http=allow_insecure_http,
            )
        if idx == 1 and shared_fb_url and not raw_url:
            _validate_api_url(
                "TRANSLATOR_FALLBACK_API_URL",
                shared_fb_url,
                has_api_key=bool(shared_fb_key),
                allow_insecure_http=allow_insecure_http,
            )

        specs.append(
            ProviderSpec(
                provider=prov,
                api_key=key,
                api_url=raw_url or preset_url,
                model=model if prov in LLM_PROVIDER_DEFAULTS else "",
                email=mymemory_email if prov == "mymemory" else "",
            )
        )
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime settings for mivta."""

    bot_token: str
    translator_provider: str = "google"
    translator_api_key: str = ""
    translator_api_url: str = ""
    translator_model: str = "gpt-4o-mini"
    translator_timeout: float = 5.0
    translator_cooldown: float = 30.0
    translator_fallback_providers: tuple[str, ...] = ()
    translator_fallback_api_key: str = ""
    translator_fallback_api_url: str = ""
    translator_fallback_model: str = ""
    translator_chain_specs: tuple[ProviderSpec, ...] = ()
    translator_glossary: tuple[str, ...] = ()

    default_source_lang: str = "auto"
    default_target_lang: str = "en"
    default_swap_lang: str = ""
    default_style: TranslationStyle = TranslationStyle.NEUTRAL
    default_ui_lang: str = "ru"
    inline_extra_langs: tuple[str, ...] = ()
    inline_style_variants: bool = True
    inline_debounce_ms: int = 180
    allow_insecure_http_api: bool = False

    webhook_url: str = ""
    webhook_path: str = "/webhook"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_secret_token: str = ""

    database_path: str = "data/mivta.db"
    max_text_length: int = 500
    cache_ttl: float = 300.0
    cache_max_size: int = 512
    rate_limit_per_minute: float = 30.0
    rate_limit_burst: int = 15
    max_concurrent_translations: int = 20
    owner_ids: tuple[int, ...] = ()
    sync_bot_profile: bool = True
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

        allow_insecure_http = _parse_bool(
            "ALLOW_INSECURE_HTTP_API",
            merged.get("ALLOW_INSECURE_HTTP_API", "false"),
        )

        provider, fallback_providers = _resolve_chain_names(merged)
        chain_specs = _build_provider_specs(
            provider,
            fallback_providers,
            merged,
            allow_insecure_http=allow_insecure_http,
        )
        primary_spec = chain_specs[0]

        fallback_api_key = merged.get("TRANSLATOR_FALLBACK_API_KEY", "").strip()
        fallback_api_url = merged.get("TRANSLATOR_FALLBACK_API_URL", "").strip().rstrip("/")
        fallback_model = merged.get("TRANSLATOR_FALLBACK_MODEL", "").strip()
        if fallback_api_url and not fallback_providers:
            _validate_api_url(
                "TRANSLATOR_FALLBACK_API_URL",
                fallback_api_url,
                has_api_key=bool(fallback_api_key),
                allow_insecure_http=allow_insecure_http,
            )

        timeout = _parse_float(
            "TRANSLATOR_TIMEOUT",
            merged.get("TRANSLATOR_TIMEOUT", "5.0"),
            minimum=0.0,
            exclusive_min=True,
        )
        cooldown = _parse_float(
            "TRANSLATOR_COOLDOWN",
            merged.get("TRANSLATOR_COOLDOWN", "30.0"),
            minimum=0.0,
        )

        glossary = _parse_glossary(merged.get("TRANSLATOR_GLOSSARY", ""))

        raw_source = merged.get("DEFAULT_SOURCE_LANG", "auto").strip()
        default_source = resolve_language(raw_source, allow_auto=True)
        if default_source is None:
            raise ConfigError(f"Unsupported DEFAULT_SOURCE_LANG={raw_source!r}")

        raw_target = merged.get("DEFAULT_TARGET_LANG", "en").strip()
        default_target = resolve_language(raw_target, allow_auto=False)
        if default_target is None:
            raise ConfigError(f"Unsupported DEFAULT_TARGET_LANG={raw_target!r}")

        raw_swap = merged.get("DEFAULT_SWAP_LANG", "").strip()
        default_swap = ""
        if raw_swap and raw_swap.lower() not in _DISABLE_TOKENS:
            resolved_swap = resolve_language(raw_swap, allow_auto=False)
            if resolved_swap is None:
                raise ConfigError(f"Unsupported DEFAULT_SWAP_LANG={raw_swap!r}")
            default_swap = resolved_swap

        raw_style = merged.get("DEFAULT_STYLE", "neutral").strip()
        default_style = parse_style(raw_style)
        if default_style is None:
            raise ConfigError(
                f"Unsupported DEFAULT_STYLE={raw_style!r}; expected neutral, casual, or formal"
            )

        default_ui_lang = merged.get("DEFAULT_UI_LANG", "ru").strip().lower()
        if default_ui_lang not in SUPPORTED_UI_LANGS:
            raise ConfigError(
                f"Unsupported DEFAULT_UI_LANG={default_ui_lang!r}; expected 'en' or 'ru'"
            )

        inline_extra_langs = _parse_extra_langs(
            "INLINE_EXTRA_LANGS",
            merged.get("INLINE_EXTRA_LANGS", ""),
        )
        inline_style_variants = _parse_bool(
            "INLINE_STYLE_VARIANTS",
            merged.get("INLINE_STYLE_VARIANTS", "true"),
        )
        inline_debounce_ms = _parse_int(
            "INLINE_DEBOUNCE_MS",
            merged.get("INLINE_DEBOUNCE_MS", "180"),
            minimum=0,
            maximum=2000,
        )

        webhook_url, webhook_path = _normalize_webhook(
            merged.get("WEBHOOK_URL", "").strip(),
            merged.get("WEBHOOK_PATH"),
        )
        webhook_host = merged.get("WEBHOOK_HOST", "127.0.0.1").strip()
        if not webhook_host:
            raise ConfigError("WEBHOOK_HOST must not be empty")
        webhook_port = _parse_int(
            "WEBHOOK_PORT",
            merged.get("WEBHOOK_PORT", "8080"),
            minimum=1,
            maximum=65535,
        )
        webhook_secret = merged.get("WEBHOOK_SECRET_TOKEN", "").strip()
        if webhook_secret and not _WEBHOOK_SECRET_RE.match(webhook_secret):
            raise ConfigError(
                "WEBHOOK_SECRET_TOKEN must be 1..256 chars matching [A-Za-z0-9_-]"
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
        owner_ids = _parse_owner_ids(merged.get("OWNER_ID", ""))
        sync_bot_profile = _parse_bool(
            "SYNC_BOT_PROFILE",
            merged.get("SYNC_BOT_PROFILE", "true"),
        )

        log_level = merged.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"Unsupported LOG_LEVEL={log_level!r}; expected one of {sorted(VALID_LOG_LEVELS)}"
            )

        preset_model = LLM_PROVIDER_DEFAULTS.get(provider, ("", "gpt-4o-mini"))[1]
        effective_model = (
            primary_spec.model
            or merged.get("TRANSLATOR_MODEL", "").strip()
            or preset_model
        )

        return cls(
            bot_token=bot_token,
            translator_provider=provider,
            translator_api_key=primary_spec.api_key,
            translator_api_url=primary_spec.api_url,
            translator_model=effective_model,
            translator_timeout=timeout,
            translator_cooldown=cooldown,
            translator_fallback_providers=fallback_providers,
            translator_fallback_api_key=fallback_api_key,
            translator_fallback_api_url=fallback_api_url,
            translator_fallback_model=fallback_model,
            translator_chain_specs=chain_specs,
            translator_glossary=glossary,
            default_source_lang=default_source,
            default_target_lang=default_target,
            default_swap_lang=default_swap,
            default_style=default_style,
            default_ui_lang=default_ui_lang,
            inline_extra_langs=inline_extra_langs,
            inline_style_variants=inline_style_variants,
            inline_debounce_ms=inline_debounce_ms,
            allow_insecure_http_api=allow_insecure_http,
            webhook_url=webhook_url,
            webhook_path=webhook_path,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
            webhook_secret_token=webhook_secret,
            database_path=database_path,
            max_text_length=max_text_length,
            cache_ttl=cache_ttl,
            cache_max_size=cache_max_size,
            rate_limit_per_minute=rate_limit_per_minute,
            rate_limit_burst=rate_limit_burst,
            max_concurrent_translations=max_concurrent,
            owner_ids=owner_ids,
            sync_bot_profile=sync_bot_profile,
            log_level=log_level,
        )
