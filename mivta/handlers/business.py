"""Telegram Business Mode handlers — in-place translation without `via @bot`."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BusinessConnection, Message

from mivta.config import Config
from mivta.database import BusinessConnectionRecord, Database
from mivta.handlers.pipeline import (
    TELEGRAM_MAX_MESSAGE_LEN,
    RateLimitedOutcome,
    execute_translation,
)
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorError,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.parser import has_translatable_text, parse_business_query
from mivta.utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)


def _extract_can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None and getattr(rights, "can_reply", None) is not None:
        return bool(rights.can_reply)
    legacy = getattr(connection, "can_reply", None)
    if legacy is not None:
        return bool(legacy)
    return False


async def _notify_owner_privately(bot: Bot, user_chat_id: int | None, text: str) -> None:
    if not user_chat_id:
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(chat_id=user_chat_id, text=text)


async def _resolve_connection(
    conn_id: str,
    bot: Bot,
    db: Database,
) -> BusinessConnectionRecord | None:
    record = await db.get_business_connection(conn_id)
    if record is not None:
        return record
    try:
        live = await bot.get_business_connection(business_connection_id=conn_id)
    except TelegramAPIError:
        return None
    return await db.upsert_business_connection(
        live.id,
        user_id=live.user.id,
        user_chat_id=live.user_chat_id,
        can_reply=_extract_can_reply(live),
        is_enabled=live.is_enabled,
    )


async def handle_business_connection(
    connection: BusinessConnection,
    bot: Bot,
    db: Database,
) -> None:
    """Persist Business connection status and notify owner about permissions if needed."""
    can_reply = _extract_can_reply(connection)
    await db.upsert_business_connection(
        connection.id,
        user_id=connection.user.id,
        user_chat_id=connection.user_chat_id,
        can_reply=can_reply,
        is_enabled=connection.is_enabled,
    )
    logger.info(
        "Business connection updated (enabled=%s, can_reply=%s)",
        connection.is_enabled,
        can_reply,
    )

    if not connection.is_enabled:
        return

    if not can_reply:
        await _notify_owner_privately(
            bot,
            connection.user_chat_id,
            "mivta подключён к аккаунту, но право «Ответы на сообщения» (Reply to messages) "
            "отключено.\nВключите его в Настройки → Автоматизация чатов (или Telegram для бизнеса "
            "→ Чат-боты), чтобы бот мог заменять ваши сообщения переводом без метки via @bot.",
        )
        return

    await _notify_owner_privately(
        bot,
        connection.user_chat_id,
        "mivta подключён к Telegram Business.\n\n"
        "Чтобы отправить перевод без метки via @bot в личном чате, "
        "начните сообщение с символа «>»:\n"
        "• > Привет, как дела? — перевод по вашим настройкам (/settings)\n"
        "• >de Привет, мир! — разовая смена языка\n"
        "• ru>pl !formal Добрый день — пара языков и стиль\n\n"
        "Обычные сообщения без префикса «>» бот не трогает.",
    )


async def handle_business_message(
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, asyncio.Task[TranslationResult]],
) -> None:
    """Translate outgoing owner messages prefixed with `>` in-place via editMessageText."""
    conn_id = message.business_connection_id
    if not conn_id or not message.text or message.from_user is None:
        return
    if message.sender_business_bot is not None or message.from_user.is_bot:
        return
    # In 1-on-1 Business chats, message.chat.id is the interlocutor's ID;
    # incoming messages have from_user.id == chat.id and must never be touched.
    if message.from_user.id == message.chat.id:
        return

    parsed = parse_business_query(message.text)
    if parsed is None:
        return

    record = await _resolve_connection(conn_id, bot, db)
    user_chat_id: int | None = None
    if record is not None:
        if not record.is_enabled or message.from_user.id != record.user_id:
            return
        user_chat_id = record.user_chat_id
        if not record.can_reply:
            await _notify_owner_privately(
                bot,
                user_chat_id,
                "Нет права «Отвечать на сообщения» в настройках Telegram Business — "
                "сообщение не может быть заменено переводом.",
            )
            return

    user_id = message.from_user.id
    if parsed.error is not None:
        await _notify_owner_privately(bot, user_chat_id, f"mivta: {parsed.error}")
        return

    if not parsed.text:
        return

    if len(parsed.text) > config.max_text_length:
        await _notify_owner_privately(
            bot,
            user_chat_id,
            f"mivta: текст слишком длинный ({len(parsed.text)} / {config.max_text_length} симв.).",
        )
        return

    user_settings = await db.get_user_settings(user_id)
    effective_source = parsed.source_lang or user_settings.source_lang
    effective_target = parsed.target_lang or user_settings.target_lang
    effective_style = parsed.style or user_settings.style

    if not has_translatable_text(parsed.text):
        final_text = parsed.text[:TELEGRAM_MAX_MESSAGE_LEN]
    else:
        request = TranslationRequest(
            text=parsed.text,
            source_lang=effective_source,
            target_lang=effective_target,
            style=effective_style,
        )
        try:
            outcome = await execute_translation(
                user_id=user_id,
                request=request,
                translator=translator,
                cache=cache,
                rate_limiter=rate_limiter,
                concurrency_sem=concurrency_sem,
                inflight=inflight,
            )
        except TranslatorError as exc:
            logger.warning(
                "Business Mode translation failed (provider=%s, err=%s)",
                translator.name,
                exc,
            )
            await _notify_owner_privately(
                bot,
                user_chat_id,
                "mivta: сервис перевода временно недоступен — сообщение оставлено без изменений.",
            )
            return

        if isinstance(outcome, RateLimitedOutcome):
            await _notify_owner_privately(
                bot,
                user_chat_id,
                f"mivta: превышен лимит запросов, подождите {outcome.retry_after:.1f} с.",
            )
            return

        final_text = outcome.translated_text[:TELEGRAM_MAX_MESSAGE_LEN]

    if not final_text or final_text == message.text:
        return

    try:
        await bot.edit_message_text(
            business_connection_id=conn_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=final_text,
            parse_mode=None,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Failed to edit Business message: %s", exc)


def build_business_router() -> Router:
    """Create a fresh Router bound to Telegram Business Mode handlers."""
    router = Router(name="business")
    router.business_connection.register(handle_business_connection)
    router.business_message.register(handle_business_message)
    router.edited_business_message.register(handle_business_message)
    return router
