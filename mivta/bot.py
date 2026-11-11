"""Telegram bot initialization, dependency wiring, polling & webhook runner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import signal
import sys
from collections.abc import Sequence
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from mivta import __version__
from mivta.config import Config, ConfigError
from mivta.database import Database
from mivta.handlers import build_root_router
from mivta.handlers.pipeline import InflightEntry
from mivta.translator import (
    Translator,
    create_http_client,
    create_translator,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

ALLOWED_UPDATES: list[str] = [
    "inline_query",
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
]

BOT_COMMANDS_EN: list[BotCommand] = [
    BotCommand(command="settings", description="Language, auto-swap & style settings"),
    BotCommand(command="lang", description="Default target language"),
    BotCommand(command="swap", description="Bidirectional auto-swap language pair"),
    BotCommand(command="langs", description="Extra Inline Mode card languages"),
    BotCommand(command="source", description="Source language or auto-detect"),
    BotCommand(command="style", description="Tone: neutral, casual, formal"),
    BotCommand(command="ui", description="Switch bot interface language (EN/RU)"),
    BotCommand(command="privacy", description="Privacy policy & data handling"),
    BotCommand(command="reset", description="Delete saved preferences"),
    BotCommand(command="help", description="How to use Inline & Secretary Mode"),
]

BOT_COMMANDS_RU: list[BotCommand] = [
    BotCommand(command="settings", description="Настройки языка, авто-смены и стиля"),
    BotCommand(command="lang", description="Язык перевода по умолчанию"),
    BotCommand(command="swap", description="Двусторонняя авто-смена пары языков"),
    BotCommand(command="langs", description="Доп. языки карточек в Inline Mode"),
    BotCommand(command="source", description="Исходный язык или автоопределение"),
    BotCommand(command="style", description="Стиль: neutral, casual, formal"),
    BotCommand(command="ui", description="Язык интерфейса бота (RU/EN)"),
    BotCommand(command="privacy", description="Политика конфиденциальности"),
    BotCommand(command="reset", description="Удалить сохранённые настройки"),
    BotCommand(command="help", description="Как пользоваться Inline и Secretary Mode"),
]


class SecretRedactingFilter(logging.Filter):
    """Scrub configured tokens and API keys if an unexpected exception formats them."""

    def __init__(self, secrets_to_redact: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted({s for s in secrets_to_redact if s and len(s) >= 4}, key=len, reverse=True)
        )

    def _scrub(self, text: str) -> str:
        out = text
        for sec in self._secrets:
            if sec in out:
                out = out.replace(sec, "[REDACTED]")
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        rendered = record.getMessage()
        scrubbed = self._scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


def configure_logging(level: str, *, secrets_to_redact: Sequence[str] = ()) -> None:
    """Configure minimal structured stderr logging without leaking secrets or payloads."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    redactor = SecretRedactingFilter(secrets_to_redact)
    root_logger = logging.getLogger()
    root_logger.addFilter(redactor)
    for handler in root_logger.handlers:
        handler.addFilter(redactor)

    # Prevent httpx/httpcore/aiohttp from logging request URLs that may contain user text
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def create_dispatcher(
    *,
    config: Config,
    db: Database,
    translator: Translator,
    cache: TranslationCache | None = None,
    rate_limiter: RateLimiter | None = None,
    bot_username: str = "BotUsername",
) -> Dispatcher:
    """Wire routers and runtime dependencies into an aiogram Dispatcher."""
    dp = Dispatcher()
    dp.include_router(build_root_router())

    resolved_cache = (
        cache
        if cache is not None
        else TranslationCache(
            max_size=config.cache_max_size,
            ttl_seconds=config.cache_ttl,
        )
    )
    resolved_limiter = (
        rate_limiter
        if rate_limiter is not None
        else RateLimiter(
            rate_per_minute=config.rate_limit_per_minute,
            burst=config.rate_limit_burst,
        )
    )
    inflight: dict[bytes, InflightEntry] = {}
    inline_active: dict[int, tuple[int, asyncio.Task[Any]]] = {}
    inline_seq: list[int] = [0]

    dp["config"] = config
    dp["db"] = db
    dp["translator"] = translator
    dp["cache"] = resolved_cache
    dp["rate_limiter"] = resolved_limiter
    dp["concurrency_sem"] = asyncio.Semaphore(config.max_concurrent_translations)
    dp["inflight"] = inflight
    dp["inline_active"] = inline_active
    dp["inline_seq"] = inline_seq
    dp["bot_username"] = bot_username
    return dp


def build_webhook_app(
    dp: Dispatcher,
    bot: Bot,
    *,
    webhook_path: str = "/webhook",
    secret_token: str,
) -> web.Application:
    """Create an aiohttp web.Application enforcing X-Telegram-Bot-Api-Secret-Token."""
    if not secret_token:
        raise ValueError("secret_token must not be empty in webhook mode")

    app = web.Application()

    async def _healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    app.router.add_get("/healthz", _healthz)
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=secret_token,
        handle_in_background=True,
    )
    handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)
    return app


async def _cache_janitor(cache: TranslationCache, interval_seconds: float) -> None:
    """Periodically evict expired cache entries so translations never outlive TTL in RAM."""
    while True:
        await asyncio.sleep(interval_seconds)
        cache.purge_expired()


async def _register_localized_commands(bot: Bot) -> None:
    with contextlib.suppress(Exception):
        await bot.set_my_commands(BOT_COMMANDS_EN)
        for lc in ("ru", "be", "kk"):
            await bot.set_my_commands(BOT_COMMANDS_RU, language_code=lc)


async def _run_webhook_server(
    dp: Dispatcher,
    bot: Bot,
    config: Config,
) -> None:
    effective_secret = config.webhook_secret_token or secrets.token_urlsafe(32)
    app = build_webhook_app(
        dp,
        bot,
        webhook_path=config.webhook_path,
        secret_token=effective_secret,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=config.webhook_host, port=config.webhook_port)
        await site.start()
        await bot.set_webhook(
            url=config.webhook_url,
            secret_token=effective_secret,
            allowed_updates=ALLOWED_UPDATES,
        )
        logger.info(
            "Webhook active at %s (listening on %s:%d%s)",
            config.webhook_url,
            config.webhook_host,
            config.webhook_port,
            config.webhook_path,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def run_bot(config: Config) -> None:
    """Start SQLite, HTTP client, cache janitor, and Telegram polling or webhook server."""
    db = Database(
        config.database_path,
        default_source_lang=config.default_source_lang,
        default_target_lang=config.default_target_lang,
        default_swap_lang=config.default_swap_lang,
        default_style=config.default_style,
        default_extra_langs=config.inline_extra_langs,
    )
    http_client = create_http_client(config.translator_timeout)
    translator = create_translator(
        config.translator_provider,
        http_client,
        api_key=config.translator_api_key,
        api_url=config.translator_api_url,
        model=config.translator_model,
        max_length=config.max_text_length,
        glossary=config.translator_glossary,
        fallback_providers=config.translator_fallback_providers,
        fallback_api_key=config.translator_fallback_api_key,
        fallback_api_url=config.translator_fallback_api_url,
        fallback_model=config.translator_fallback_model,
        provider_specs=config.translator_chain_specs,
        cooldown_seconds=config.translator_cooldown,
    )
    cache = TranslationCache(
        max_size=config.cache_max_size,
        ttl_seconds=config.cache_ttl,
    )
    rate_limiter = RateLimiter(
        rate_per_minute=config.rate_limit_per_minute,
        burst=config.rate_limit_burst,
    )
    bot = Bot(token=config.bot_token)
    janitor_task: asyncio.Task[None] | None = None

    try:
        await db.connect()
        me = await bot.get_me()
        bot_username = me.username or "BotUsername"
        if not me.supports_inline_queries:
            logger.warning(
                "Inline Mode is NOT enabled for @%s! Enable it via @BotFather -> /setinline",
                bot_username,
            )
        if getattr(me, "has_topics_enabled", False):
            logger.warning(
                "Threaded Mode is enabled for @%s — disabling it in @BotFather is recommended "
                "so /settings opens directly without topic threads.",
                bot_username,
            )

        await _register_localized_commands(bot)

        dp = create_dispatcher(
            config=config,
            db=db,
            translator=translator,
            cache=cache,
            rate_limiter=rate_limiter,
            bot_username=bot_username,
        )

        if cache.enabled:
            interval = max(5.0, min(60.0, config.cache_ttl / 2.0))
            janitor_task = asyncio.create_task(_cache_janitor(cache, interval))

        business_enabled = bool(getattr(me, "can_connect_to_business", False))
        mode_name = "webhook" if config.webhook_url else "polling"
        logger.info(
            "Starting @%s (mode=%s, provider=%s, default=%s->%s, swap=%s, secretary_mode=%s)",
            bot_username,
            mode_name,
            translator.name,
            config.default_source_lang,
            config.default_target_lang,
            config.default_swap_lang or "off",
            business_enabled,
        )

        if config.webhook_url:
            await _run_webhook_server(dp, bot, config)
        else:
            with contextlib.suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        if janitor_task is not None:
            janitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor_task
        cache.clear()
        await translator.aclose()
        await http_client.aclose()
        await db.close()
        await bot.session.close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point supporting `--version` and `--check`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "-V" in args or "--version" in args:
        print(f"mivta {__version__}")
        return 0

    check_only = "--check" in args
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"mivta: configuration error: {exc}", file=sys.stderr)
        return 2

    if check_only:
        mode = "webhook" if config.webhook_url else "polling"
        fb = ",".join(config.translator_fallback_providers) or "none"
        print(
            f"mivta {__version__} ok "
            f"(mode={mode}, provider={config.translator_provider}, fallback={fb})"
        )
        return 0

    configure_logging(
        config.log_level,
        secrets_to_redact=(
            config.bot_token,
            config.translator_api_key,
            config.translator_fallback_api_key,
            config.webhook_secret_token,
            *(spec.api_key for spec in config.translator_chain_specs),
            *(spec.email for spec in config.translator_chain_specs),
        ),
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_bot(config))
    return 0
