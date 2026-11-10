"""Telegram Inline Mode handler — core translation flow."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
)

from mivta.config import Config
from mivta.database import Database
from mivta.handlers.pipeline import (
    TELEGRAM_MAX_MESSAGE_LEN,
    RateLimitedOutcome,
    execute_translation,
)
from mivta.translator.base import (
    STYLE_LABELS_RU,
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorAuthError,
    TranslatorError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.parser import has_translatable_text, parse_inline_query, truncate_preview
from mivta.utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)


def _article_id(kind: str, *parts: str) -> str:
    hasher = hashlib.blake2s(digest_size=16)
    hasher.update(kind.encode("utf-8"))
    for part in parts:
        hasher.update(b"\x00")
        hasher.update(part.encode("utf-8"))
    return hasher.hexdigest()


def _settings_button(
    source_lang: str,
    target_lang: str,
    style: TranslationStyle,
) -> InlineQueryResultsButton:
    style_label = STYLE_LABELS_RU[style]
    label = f"Настройки: {source_lang} → {target_lang} ({style_label})"
    return InlineQueryResultsButton(
        text=label[:64],
        start_parameter="settings",
    )


def _status_article(
    kind: str,
    *,
    title: str,
    description: str,
    fallback_text: str,
) -> InlineQueryResultArticle:
    safe_body = (fallback_text or "…")[:TELEGRAM_MAX_MESSAGE_LEN]
    return InlineQueryResultArticle(
        id=_article_id(kind, title, description, safe_body),
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=safe_body,
            parse_mode=None,
        ),
    )


def _translation_article(
    request: TranslationRequest,
    result: TranslationResult,
) -> InlineQueryResultArticle:
    body = result.translated_text[:TELEGRAM_MAX_MESSAGE_LEN]
    if request.source_lang == "auto" and result.source_lang != "auto":
        direction = f"auto ({result.source_lang}) → {result.target_lang}"
    else:
        direction = f"{result.source_lang} → {result.target_lang}"
    style_label = STYLE_LABELS_RU[result.style]
    description = f"{direction} · {style_label}"

    return InlineQueryResultArticle(
        id=_article_id("ok", direction, result.style.value, body),
        title=truncate_preview(body),
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=body,
            parse_mode=None,
        ),
    )


async def _safe_answer(
    inline_query: InlineQuery,
    results: list[InlineQueryResultArticle],
    *,
    button: InlineQueryResultsButton,
    cache_time: int,
) -> None:
    try:
        await inline_query.answer(
            results=results,
            button=button,
            cache_time=cache_time,
            is_personal=True,
        )
    except TelegramBadRequest as exc:
        msg = str(exc).lower()
        if "query is too old" in msg or "query id is invalid" in msg:
            logger.debug("Ignored expired inline query: %s", exc)
            return
        raise


async def handle_inline_query(
    inline_query: InlineQuery,
    config: Config,
    db: Database,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, asyncio.Task[TranslationResult]],
) -> None:
    """Translate inline input directly inside any Telegram chat."""
    user_id = inline_query.from_user.id
    user_settings = await db.get_user_settings(user_id)
    parsed = parse_inline_query(inline_query.query)

    effective_source = parsed.source_lang or user_settings.source_lang
    effective_target = parsed.target_lang or user_settings.target_lang
    effective_style = parsed.style or user_settings.style
    button = _settings_button(effective_source, effective_target, effective_style)

    if parsed.error is not None:
        article = _status_article(
            "bad_lang",
            title=parsed.error,
            description="Пример: >en текст · ru>de текст · !formal текст",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    if not parsed.text:
        article = _status_article(
            "empty",
            title=f"Введите текст для перевода ({effective_source} → {effective_target})",
            description=(
                f"Стиль: {STYLE_LABELS_RU[effective_style]} · "
                "Быстро: >de текст · ru>en текст · !formal текст"
            ),
            fallback_text="…",
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    if len(parsed.text) > config.max_text_length:
        article = _status_article(
            "too_long",
            title=f"Текст слишком длинный ({len(parsed.text)} / {config.max_text_length} симв.)",
            description="Сократите сообщение для перевода",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    request = TranslationRequest(
        text=parsed.text,
        source_lang=effective_source,
        target_lang=effective_target,
        style=effective_style,
    )

    if not has_translatable_text(parsed.text):
        passthrough = TranslationResult(
            translated_text=parsed.text,
            source_lang=effective_source,
            target_lang=effective_target,
            style=effective_style,
            provider=translator.name,
        )
        await _safe_answer(
            inline_query,
            [_translation_article(request, passthrough)],
            button=button,
            cache_time=5,
        )
        return

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
    except TranslatorTimeoutError:
        logger.warning("Translator timed out (provider=%s)", translator.name)
        article = _status_article(
            "timeout",
            title="Превышено время ожидания переводчика",
            description="Повторите ввод через пару секунд",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorRateLimitError:
        logger.warning("Upstream translator rate limit hit (provider=%s)", translator.name)
        article = _status_article(
            "upstream_rl",
            title="Лимит запросов к сервису перевода исчерпан",
            description="Попробуйте повторить запрос позже",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorAuthError:
        logger.error("Translator authentication error (provider=%s)", translator.name)
        article = _status_article(
            "auth_err",
            title="Ошибка конфигурации сервиса перевода",
            description="Проверьте ключ API переводчика",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorError as exc:
        logger.warning("Translator unavailable (provider=%s, err=%s)", translator.name, exc)
        article = _status_article(
            "unavailable",
            title="Сервис перевода временно недоступен",
            description="Попробуйте повторить запрос через несколько секунд",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return

    if isinstance(outcome, RateLimitedOutcome):
        article = _status_article(
            "ratelimit",
            title="Слишком много запросов",
            description=f"Подождите {outcome.retry_after:.1f} с и продолжите ввод",
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return

    await _safe_answer(
        inline_query,
        [_translation_article(request, outcome)],
        button=button,
        cache_time=5,
    )


def build_inline_router() -> Router:
    """Create a fresh Router bound to the inline query handler."""
    router = Router(name="inline")
    router.inline_query.register(handle_inline_query)
    return router
