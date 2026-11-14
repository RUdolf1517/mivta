"""Telegram Inline Mode handler — multi-card, auto-localized, keystroke-coalesced flow."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    MessageEntity,
)

from mivta.config import Config
from mivta.database import Database
from mivta.handlers.pipeline import (
    TELEGRAM_MAX_MESSAGE_LEN,
    InflightEntry,
    RateLimitedOutcome,
    execute_multilang_translation,
    execute_multistyle_translation,
    execute_translation,
    is_query_cached,
)
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorAuthError,
    TranslatorError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    deduplicate_styles,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.formatting import prepare_text_for_translation, restore_formatted_text
from mivta.utils.i18n import resolve_ui_lang, style_label
from mivta.utils.languages import resolve_auto_swap_target
from mivta.utils.metrics import RuntimeMetrics
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
    *,
    ui_lang: str = "ru",
    extra_langs: tuple[str, ...] = (),
    swap_lang: str = "",
) -> InlineQueryResultsButton:
    st_label = style_label(style, ui_lang)
    primary = (
        f"{target_lang}⇄{swap_lang}"
        if (source_lang == "auto" and swap_lang and swap_lang != target_lang)
        else target_lang
    )
    targets_str = ",".join((primary, *extra_langs)) if extra_langs else primary
    prefix = "Settings" if ui_lang == "en" else "Настройки"
    label = f"{prefix}: {source_lang} → {targets_str} ({st_label})"
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
    source_requested: str,
    result: TranslationResult,
    *,
    body: str,
    ui_lang: str = "ru",
    entities: list[MessageEntity] | None = None,
) -> InlineQueryResultArticle:
    if source_requested == "auto" and result.source_lang != "auto":
        direction = f"auto ({result.source_lang}) → {result.target_lang}"
    else:
        direction = f"{result.source_lang} → {result.target_lang}"
    st_label = style_label(result.style, ui_lang)
    description = f"{direction} · {st_label}"

    return InlineQueryResultArticle(
        id=_article_id("ok", direction, result.style.value, body),
        title=truncate_preview(body),
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=body,
            entities=entities,
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


def _resolve_target_langs(
    effective_source: str,
    effective_target: str,
    explicit_target: str | None,
    extra_langs: tuple[str, ...],
) -> list[str]:
    targets = [effective_target]
    if explicit_target is not None or not extra_langs:
        return targets
    for code in extra_langs:
        if code not in targets and code != effective_source:
            targets.append(code)
    return targets


async def handle_inline_query(
    inline_query: InlineQuery,
    config: Config,
    db: Database,
    translator: Translator,
    cache: TranslationCache,
    rate_limiter: RateLimiter,
    concurrency_sem: asyncio.Semaphore,
    inflight: dict[bytes, InflightEntry],
    inline_active: dict[int, tuple[int, asyncio.Task[Any]]],
    inline_seq: list[int],
    metrics: RuntimeMetrics | None = None,
) -> None:
    """Translate inline input directly inside any Telegram chat with keystroke coalescing."""
    user_id = inline_query.from_user.id
    user_settings = await db.get_user_settings(user_id)
    ui = resolve_ui_lang(
        user_settings.ui_lang,
        inline_query.from_user.language_code,
        default_ui_lang=config.default_ui_lang,
    )
    is_en = ui == "en"
    parsed = parse_inline_query(inline_query.query, ui_lang=ui)

    effective_source = parsed.source_lang or user_settings.source_lang
    effective_target = parsed.target_lang or user_settings.target_lang
    effective_style = parsed.style or user_settings.style
    explicit_override = parsed.source_lang is not None or parsed.target_lang is not None
    active_extras = () if parsed.target_lang is not None else user_settings.extra_langs
    active_swap = "" if explicit_override else user_settings.swap_lang
    button = _settings_button(
        effective_source,
        effective_target,
        effective_style,
        ui_lang=ui,
        extra_langs=active_extras,
        swap_lang=active_swap,
    )

    if parsed.error is not None:
        article = _status_article(
            "bad_lang",
            title=parsed.error,
            description=(
                "Example: >en text · ru>de text · !formal text"
                if is_en
                else "Пример: >en текст · ru>de текст · !formal текст"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    if not parsed.text:
        pair_display = (
            f"{effective_target}⇄{active_swap}"
            if (effective_source == "auto" and active_swap and active_swap != effective_target)
            else f"{effective_source} → {effective_target}"
        )
        st_lbl = style_label(effective_style, ui)
        article = _status_article(
            "empty",
            title=(
                f"Type text to translate ({pair_display})"
                if is_en
                else f"Введите текст для перевода ({pair_display})"
            ),
            description=(
                f"Style: {st_lbl} · Quick: >de text · ru>en text · !formal text"
                if is_en
                else f"Стиль: {st_lbl} · Быстро: >de текст · ru>en текст · !formal текст"
            ),
            fallback_text="…",
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    if len(parsed.text) > config.max_text_length:
        article = _status_article(
            "too_long",
            title=(
                f"Text is too long ({len(parsed.text)} / {config.max_text_length} chars)"
                if is_en
                else f"Текст слишком длинный ({len(parsed.text)} / {config.max_text_length} симв.)"
            ),
            description=(
                "Shorten the message to translate"
                if is_en
                else "Сократите сообщение для перевода"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=1)
        return

    prepared = prepare_text_for_translation(
        parsed.text,
        glossary=config.translator_glossary,
        protect_glossary_tokens=not translator.uses_llm_glossary,
        parse_inline_code_markdown=True,
    )

    if not has_translatable_text(prepared.translatable_probe):
        restored = restore_formatted_text(
            prepared.encoded_text,
            prepared,
            max_length=TELEGRAM_MAX_MESSAGE_LEN,
            emit_entities_only_if_rich=True,
        )
        passthrough = TranslationResult(
            translated_text=restored.text,
            source_lang=effective_source,
            target_lang=effective_target,
            style=effective_style,
            provider=translator.name,
        )
        await _safe_answer(
            inline_query,
            [
                _translation_article(
                    effective_source,
                    passthrough,
                    body=restored.text,
                    ui_lang=ui,
                    entities=restored.entities,
                )
            ],
            button=button,
            cache_time=5,
        )
        return

    resolved_target, provider_swap = resolve_auto_swap_target(
        prepared.translatable_probe,
        source_lang=effective_source,
        target_lang=effective_target,
        swap_lang=user_settings.swap_lang,
        explicit_override=explicit_override,
    )

    base_request = TranslationRequest(
        text=prepared.encoded_text,
        source_lang=effective_source,
        target_lang=resolved_target,
        style=effective_style,
        swap_lang=provider_swap,
    )
    target_langs = _resolve_target_langs(
        effective_source,
        resolved_target,
        parsed.target_lang,
        user_settings.extra_langs,
    )
    multilang_mode = len(target_langs) > 1
    multistyle_mode = (
        not multilang_mode
        and parsed.style is None
        and config.inline_style_variants
        and translator.supports_styles
    )
    styles = deduplicate_styles(
        [
            effective_style,
            TranslationStyle.NEUTRAL,
            TranslationStyle.CASUAL,
            TranslationStyle.FORMAL,
        ]
    )

    # Register this inline query as the latest for `user_id` and cancel any obsolete keystroke task
    current_task = asyncio.current_task()
    inline_seq[0] += 1
    my_seq = inline_seq[0]
    prev = inline_active.get(user_id)
    if prev is not None and prev[1] is not current_task and not prev[1].done():
        prev[1].cancel()
    if current_task is not None:
        inline_active[user_id] = (my_seq, current_task)

    try:
        warm = is_query_cached(
            provider_name=translator.name,
            base_request=base_request,
            cache=cache,
            target_langs=target_langs,
            styles=styles,
            multilang_mode=multilang_mode,
            multistyle_mode=multistyle_mode,
        )
        if not warm and config.inline_debounce_ms > 0:
            await asyncio.sleep(config.inline_debounce_ms / 1000.0)

        if multilang_mode:
            raw_outcome = await execute_multilang_translation(
                user_id=user_id,
                base_request=base_request,
                target_langs=target_langs,
                translator=translator,
                cache=cache,
                rate_limiter=rate_limiter,
                concurrency_sem=concurrency_sem,
                inflight=inflight,
                metrics=metrics,
            )
        elif multistyle_mode:
            raw_outcome = await execute_multistyle_translation(
                user_id=user_id,
                request=base_request,
                styles=styles,
                translator=translator,
                cache=cache,
                rate_limiter=rate_limiter,
                concurrency_sem=concurrency_sem,
                metrics=metrics,
            )
        else:
            single = await execute_translation(
                user_id=user_id,
                request=base_request,
                translator=translator,
                cache=cache,
                rate_limiter=rate_limiter,
                concurrency_sem=concurrency_sem,
                inflight=inflight,
                metrics=metrics,
            )
            raw_outcome = single if isinstance(single, RateLimitedOutcome) else [single]
    except asyncio.CancelledError:
        active_now = inline_active.get(user_id)
        if active_now is not None and active_now[0] != my_seq:
            # Superseded by a fresher keystroke from the same user — drop silently
            return
        inline_active.pop(user_id, None)
        raise
    except TranslatorTimeoutError:
        logger.warning("Translator timed out (provider=%s)", translator.name)
        article = _status_article(
            "timeout",
            title=(
                "Translation timed out"
                if is_en
                else "Превышено время ожидания переводчика"
            ),
            description=(
                "Try again in a couple of seconds"
                if is_en
                else "Повторите ввод через пару секунд"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorRateLimitError:
        logger.warning("Upstream translator rate limit hit (provider=%s)", translator.name)
        article = _status_article(
            "upstream_rl",
            title=(
                "Translation provider quota reached"
                if is_en
                else "Лимит запросов к сервису перевода исчерпан"
            ),
            description=(
                "Please try again shortly"
                if is_en
                else "Попробуйте повторить запрос позже"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorAuthError:
        logger.error("Translator authentication error (provider=%s)", translator.name)
        article = _status_article(
            "auth_err",
            title=(
                "Translation provider configuration error"
                if is_en
                else "Ошибка конфигурации сервиса перевода"
            ),
            description=(
                "Check translator API key configuration"
                if is_en
                else "Проверьте ключ API переводчика"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    except TranslatorError as exc:
        logger.warning("Translator unavailable (provider=%s, err=%s)", translator.name, exc)
        article = _status_article(
            "unavailable",
            title=(
                "Translation service temporarily unavailable"
                if is_en
                else "Сервис перевода временно недоступен"
            ),
            description=(
                "Please retry in a few seconds"
                if is_en
                else "Попробуйте повторить запрос через несколько секунд"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return
    finally:
        active_now = inline_active.get(user_id)
        if active_now is not None and active_now[0] == my_seq:
            inline_active.pop(user_id, None)

    if isinstance(raw_outcome, RateLimitedOutcome):
        article = _status_article(
            "ratelimit",
            title="Too many requests" if is_en else "Слишком много запросов",
            description=(
                f"Wait {raw_outcome.retry_after:.1f} s and continue typing"
                if is_en
                else f"Подождите {raw_outcome.retry_after:.1f} с и продолжите ввод"
            ),
            fallback_text=parsed.text,
        )
        await _safe_answer(inline_query, [article], button=button, cache_time=0)
        return

    articles: list[InlineQueryResultArticle] = []
    seen_texts: set[str] = set()
    seen_lang_texts: set[tuple[str, str]] = set()
    original_stripped = parsed.text.strip()

    for idx, res in enumerate(raw_outcome):
        restored = restore_formatted_text(
            res.translated_text,
            prepared,
            max_length=TELEGRAM_MAX_MESSAGE_LEN,
            emit_entities_only_if_rich=True,
        )
        if idx > 0:
            if multistyle_mode and restored.text in seen_texts:
                continue
            if multilang_mode:
                if (res.target_lang, restored.text) in seen_lang_texts:
                    continue
                if (
                    res.source_lang == res.target_lang
                    and restored.text.strip() == original_stripped
                ):
                    continue
        seen_texts.add(restored.text)
        seen_lang_texts.add((res.target_lang, restored.text))
        articles.append(
            _translation_article(
                effective_source,
                res,
                body=restored.text,
                ui_lang=ui,
                entities=restored.entities,
            )
        )

    await _safe_answer(
        inline_query,
        articles,
        button=button,
        cache_time=5,
    )


def build_inline_router() -> Router:
    """Create a fresh Router bound to the inline query handler."""
    router = Router(name="inline")
    router.inline_query.register(handle_inline_query)
    return router
