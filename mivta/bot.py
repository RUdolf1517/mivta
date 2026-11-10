"""Telegram bot initialization, dependency wiring, and lifecycle runner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from mivta.config import Config, ConfigError
from mivta.database import Database
from mivta.handlers import build_root_router
from mivta.translator import (
    TranslationResult,
    Translator,
    create_http_client,
    create_translator,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="settings", description="Настройки языка и стиля перевода"),
    BotCommand(command="lang", description="Язык перевода по умолчанию"),
    BotCommand(command="source", description="Исходный язык или автоопределение"),
    BotCommand(command="style", description="Стиль: neutral, casual, formal"),
    BotCommand(command="help", description="Как пользоваться Inline Mode"),
]


def configure_logging(level: str) -> None:
    """Configure minimal structured stderr logging without leaking HTTP query payloads."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Prevent httpx/httpcore from logging request URLs that may contain user text
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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

    resolved_cache = cache or TranslationCache(
        max_size=config.cache_max_size,
        ttl_seconds=config.cache_ttl,
    )
    resolved_limiter = rate_limiter or RateLimiter(
        rate_per_minute=config.rate_limit_per_minute,
        burst=config.rate_limit_burst,
    )
    inflight: dict[bytes, asyncio.Task[TranslationResult]] = {}

    dp["config"] = config
    dp["db"] = db
    dp["translator"] = translator
    dp["cache"] = resolved_cache
    dp["rate_limiter"] = resolved_limiter
    dp["concurrency_sem"] = asyncio.Semaphore(config.max_concurrent_translations)
    dp["inflight"] = inflight
    dp["bot_username"] = bot_username
    return dp


async def _cache_janitor(cache: TranslationCache, interval_seconds: float) -> None:
    """Periodically evict expired cache entries so translations never outlive TTL in RAM."""
    while True:
        await asyncio.sleep(interval_seconds)
        cache.purge_expired()


async def run_bot(config: Config) -> None:
    """Start SQLite, HTTP client, cache janitor, and Telegram polling with clean shutdown."""
    db = Database(
        config.database_path,
        default_source_lang=config.default_source_lang,
        default_target_lang=config.default_target_lang,
        default_style=config.default_style,
    )
    http_client = create_http_client(config.translator_timeout)
    translator = create_translator(
        config.translator_provider,
        http_client,
        api_key=config.translator_api_key,
        api_url=config.translator_api_url,
        model=config.translator_model,
        max_length=config.max_text_length,
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

        with contextlib.suppress(Exception):
            await bot.set_my_commands(BOT_COMMANDS)

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
        logger.info(
            "Starting @%s (provider=%s, default=%s->%s, style=%s, business_mode=%s)",
            bot_username,
            translator.name,
            config.default_source_lang,
            config.default_target_lang,
            config.default_style.value,
            business_enabled,
        )
        await dp.start_polling(
            bot,
            allowed_updates=[
                "inline_query",
                "message",
                "callback_query",
                "business_connection",
                "business_message",
                "edited_business_message",
            ],
        )
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


def main() -> int:
    """CLI entry point."""
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"mivta: configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_bot(config))
    return 0
