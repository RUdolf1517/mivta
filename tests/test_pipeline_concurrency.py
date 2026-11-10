"""Concurrency, singleflight waiter counting, warm-cache zero-delay, and error-card tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerInlineQuery,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, InlineQuery, Message, Update, User

from mivta.bot import create_dispatcher
from mivta.config import Config
from mivta.database import Database
from mivta.handlers.pipeline import (
    InflightEntry,
    RateLimitedOutcome,
    _singleflight_translate,
    execute_multilang_translation,
    execute_multistyle_translation,
)
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorAuthError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    TranslatorUnavailableError,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter


class RecordingSession(BaseSession):
    def __init__(self, *, answer_inline_error: Exception | None = None) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.answer_inline_error = answer_inline_error

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, AnswerInlineQuery) and self.answer_inline_error is not None:
            raise self.answer_inline_error
        if isinstance(method, (AnswerInlineQuery, AnswerCallbackQuery)):
            return True
        if isinstance(method, SendMessage):
            return Message(
                message_id=1,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=1, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _iq(update_id: int, query: str, uid: int, lang_code: str = "en") -> Update:
    return Update(
        update_id=update_id,
        inline_query=InlineQuery(
            id=f"iq-{update_id}",
            from_user=User(id=uid, is_bot=False, first_name="U", language_code=lang_code),
            query=query,
            offset="",
        ),
    )


class ControlledTranslator(Translator):
    """Translator with controllable async gate, cancellation tracking, and style support."""

    name = "controlled"
    supports_styles = True

    def __init__(
        self,
        *,
        error_to_raise: Exception | None = None,
        detected_source: str = "ru",
    ) -> None:
        self.error_to_raise = error_to_raise
        self.detected_source = detected_source
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.single_calls: list[TranslationRequest] = []
        self.style_calls: list[tuple[TranslationRequest, tuple[TranslationStyle, ...]]] = []
        self.cancelled_upstream_count = 0

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.single_calls.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_upstream_count += 1
            raise
        if self.error_to_raise is not None:
            raise self.error_to_raise
        return TranslationResult(
            translated_text=f"[{request.target_lang}:{request.style.value}] {request.text}",
            source_lang=self.detected_source,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )

    async def translate_styles(
        self,
        request: TranslationRequest,
        styles: list[TranslationStyle],
    ) -> list[TranslationResult]:
        self.style_calls.append((request, tuple(styles)))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_upstream_count += 1
            raise
        if self.error_to_raise is not None:
            raise self.error_to_raise
        return [
            TranslationResult(
                translated_text=f"[{request.target_lang}:{st.value}] {request.text}",
                source_lang=self.detected_source,
                target_lang=request.target_lang,
                style=st,
                provider=self.name,
            )
            for st in styles
        ]


async def test_shared_singleflight_survives_when_one_of_two_waiters_cancels() -> None:
    """Global-bot safety invariant: User A cancelling must NEVER kill User B's shared query."""
    translator = ControlledTranslator()
    translator.release.clear()
    cache = TranslationCache(max_size=64, ttl_seconds=60)
    sem = asyncio.Semaphore(10)
    inflight: dict[bytes, InflightEntry] = {}
    req = TranslationRequest(text="Добрый вечер", source_lang="auto", target_lang="en")

    task_a = asyncio.create_task(
        _singleflight_translate(
            request=req,
            translator=translator,
            cache=cache,
            concurrency_sem=sem,
            inflight=inflight,
        )
    )
    await translator.started.wait()

    task_b = asyncio.create_task(
        _singleflight_translate(
            request=req,
            translator=translator,
            cache=cache,
            concurrency_sem=sem,
            inflight=inflight,
        )
    )
    await asyncio.sleep(0)
    assert len(inflight) == 1
    only_entry = next(iter(inflight.values()))
    assert only_entry.waiters == 2

    # User A types a new keystroke -> task_a is cancelled mid-flight
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    # Upstream call MUST still be alive for User B!
    assert translator.cancelled_upstream_count == 0
    assert only_entry.waiters == 1

    # Release upstream -> User B gets the translation and populates cache
    translator.release.set()
    res_b = await task_b
    assert res_b.translated_text == "[en:neutral] Добрый вечер"
    assert len(translator.single_calls) == 1
    assert len(inflight) == 0
    assert cache.get(translator.name, req) == res_b


async def test_lone_waiter_cancellation_aborts_upstream_task_and_cleans_inflight() -> None:
    translator = ControlledTranslator()
    translator.release.clear()
    cache = TranslationCache(max_size=64, ttl_seconds=60)
    sem = asyncio.Semaphore(10)
    inflight: dict[bytes, InflightEntry] = {}
    req = TranslationRequest(text="Отмени меня", source_lang="auto", target_lang="en")

    task = asyncio.create_task(
        _singleflight_translate(
            request=req,
            translator=translator,
            cache=cache,
            concurrency_sem=sem,
            inflight=inflight,
        )
    )
    await translator.started.wait()
    assert len(inflight) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert translator.cancelled_upstream_count == 1
    assert len(inflight) == 0
    assert cache.get(translator.name, req) is None


async def test_singleflight_upstream_exception_propagates_to_all_waiters_and_cleans_up() -> None:
    translator = ControlledTranslator(error_to_raise=TranslatorUnavailableError("boom"))
    translator.release.clear()
    cache = TranslationCache(max_size=64, ttl_seconds=60)
    sem = asyncio.Semaphore(10)
    inflight: dict[bytes, InflightEntry] = {}
    req = TranslationRequest(text="Сбой", source_lang="auto", target_lang="en")

    t1 = asyncio.create_task(
        _singleflight_translate(
            request=req,
            translator=translator,
            cache=cache,
            concurrency_sem=sem,
            inflight=inflight,
        )
    )
    await translator.started.wait()
    t2 = asyncio.create_task(
        _singleflight_translate(
            request=req,
            translator=translator,
            cache=cache,
            concurrency_sem=sem,
            inflight=inflight,
        )
    )
    await asyncio.sleep(0)
    translator.release.set()

    with pytest.raises(TranslatorUnavailableError, match="boom"):
        await t1
    with pytest.raises(TranslatorUnavailableError, match="boom"):
        await t2

    assert len(translator.single_calls) == 1
    assert len(inflight) == 0
    assert cache.get(translator.name, req) is None


async def test_warm_cache_skips_inline_debounce_across_single_multistyle_and_multilang() -> None:
    # Configure a large 600ms debounce so any accidental sleep is immediately obvious
    config = Config(
        bot_token="123:TOK",
        inline_debounce_ms=600,
        inline_style_variants=True,
    )
    db = Database(":memory:")
    await db.connect()
    translator = ControlledTranslator()
    cache = TranslationCache(max_size=128, ttl_seconds=300)
    session = RecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=translator, cache=cache)
    # Empty TranslationCache has len(cache) == 0 (falsy) — dispatcher must preserve exact instance!
    assert dp["cache"] is cache

    try:
        # Pre-warm all 3 style variants for "Привет"
        for st in (
            TranslationStyle.NEUTRAL,
            TranslationStyle.CASUAL,
            TranslationStyle.FORMAL,
        ):
            req = TranslationRequest(text="Привет", source_lang="auto", target_lang="en", style=st)
            cache.set(
                translator.name,
                req,
                TranslationResult(
                    translated_text=f"Hi ({st.value})",
                    source_lang="ru",
                    target_lang="en",
                    style=st,
                    provider=translator.name,
                ),
            )

        t0 = time.monotonic()
        await dp.feed_update(bot, _iq(1, "Привет", uid=42))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.15
        assert len(translator.style_calls) == 0
        assert len(translator.single_calls) == 0

        # Pre-warm multilang ('en', 'de') and verify warm multilang also skips 600ms debounce
        await db.update_user_settings(42, extra_langs=("de",))
        for tgt in ("en", "de"):
            req = TranslationRequest(
                text="Спасибо",
                source_lang="auto",
                target_lang=tgt,
                style=TranslationStyle.NEUTRAL,
            )
            cache.set(
                translator.name,
                req,
                TranslationResult(
                    translated_text=f"Thanks-{tgt}",
                    source_lang="ru",
                    target_lang=tgt,
                    style=TranslationStyle.NEUTRAL,
                    provider=translator.name,
                ),
            )

        t1 = time.monotonic()
        await dp.feed_update(bot, _iq(2, "Спасибо", uid=42))
        assert (time.monotonic() - t1) < 0.15
        assert len(translator.single_calls) == 0
    finally:
        await db.close()
        await bot.session.close()


async def test_multistyle_partial_cache_provider_swap_and_multilang_ratelimit() -> None:
    translator = ControlledTranslator(detected_source="en")
    cache = TranslationCache(max_size=64, ttl_seconds=300)
    limiter = RateLimiter(rate_per_minute=60.0, burst=1)
    sem = asyncio.Semaphore(5)
    inflight: dict[bytes, InflightEntry] = {}

    # 1. Partial cache hit in multistyle: NEUTRAL already cached, only CASUAL+FORMAL fetched
    translator.detected_source = "ru"
    base = TranslationRequest(
        text="Пока",
        source_lang="auto",
        target_lang="en",
        style=TranslationStyle.NEUTRAL,
    )
    cache.set(
        translator.name,
        base,
        TranslationResult(
            translated_text="Bye",
            source_lang="ru",
            target_lang="en",
            style=TranslationStyle.NEUTRAL,
            provider=translator.name,
        ),
    )
    styles = [TranslationStyle.NEUTRAL, TranslationStyle.CASUAL, TranslationStyle.FORMAL]
    res_list = await execute_multistyle_translation(
        user_id=10,
        request=base,
        styles=styles,
        translator=translator,
        cache=cache,
        rate_limiter=limiter,
        concurrency_sem=sem,
    )
    assert isinstance(res_list, list) and len(res_list) == 3
    assert len(translator.style_calls) == 1
    # Only missing styles were requested
    assert translator.style_calls[0][1] == (TranslationStyle.CASUAL, TranslationStyle.FORMAL)

    # Next call hits 100% warm cache EVEN THOUGH burst=1 was just spent!
    warm_list = await execute_multistyle_translation(
        user_id=10,
        request=base,
        styles=styles,
        translator=translator,
        cache=cache,
        rate_limiter=limiter,
        concurrency_sem=sem,
    )
    assert isinstance(warm_list, list) and len(warm_list) == 3
    assert len(translator.style_calls) == 1

    # Cold multistyle call while burst=1 is exhausted -> RateLimitedOutcome
    cold = TranslationRequest(text="Новый", source_lang="auto", target_lang="en")
    rl_out = await execute_multistyle_translation(
        user_id=10,
        request=cold,
        styles=styles,
        translator=translator,
        cache=cache,
        rate_limiter=limiter,
        concurrency_sem=sem,
    )
    assert isinstance(rl_out, RateLimitedOutcome)

    # 2. Multistyle same-script provider auto-swap (en -> en detected, swaps to pl!)
    translator.style_calls.clear()
    translator.detected_source = "en"
    swap_req = TranslationRequest(
        text="Good night",
        source_lang="auto",
        target_lang="en",
        swap_lang="pl",
    )
    swapped_styles = await execute_multistyle_translation(
        user_id=20,
        request=swap_req,
        styles=[TranslationStyle.NEUTRAL, TranslationStyle.FORMAL],
        translator=translator,
        cache=cache,
        rate_limiter=RateLimiter(rate_per_minute=60.0, burst=5),
        concurrency_sem=sem,
    )
    assert isinstance(swapped_styles, list)
    assert [c[0].target_lang for c in translator.style_calls] == ["en", "pl"]
    assert all(r.target_lang == "pl" for r in swapped_styles)

    # 3. Multilang deduplicates target codes & enforces rate limit on cold miss
    exhausted = RateLimiter(rate_per_minute=60.0, burst=1)
    assert exhausted.allow(99)[0] is True
    ml_rl = await execute_multilang_translation(
        user_id=99,
        base_request=TranslationRequest(text="Привет", target_lang="en"),
        target_langs=["en", "en", "de"],
        translator=translator,
        cache=cache,
        rate_limiter=exhausted,
        concurrency_sem=sem,
        inflight=inflight,
    )
    assert isinstance(ml_rl, RateLimitedOutcome)


@pytest.mark.parametrize(
    ("exc", "expected_en", "expected_ru"),
    [
        (
            TranslatorTimeoutError("slow"),
            "Translation timed out",
            "Превышено время ожидания переводчика",
        ),
        (
            TranslatorRateLimitError("429"),
            "Translation provider quota reached",
            "Лимит запросов к сервису перевода исчерпан",
        ),
        (
            TranslatorAuthError("401"),
            "Translation provider configuration error",
            "Ошибка конфигурации сервиса перевода",
        ),
        (
            TranslatorUnavailableError("502"),
            "Translation service temporarily unavailable",
            "Сервис перевода временно недоступен",
        ),
    ],
)
async def test_inline_error_status_cards_localized_en_and_ru(
    exc: Exception,
    expected_en: str,
    expected_ru: str,
) -> None:
    config = Config(bot_token="123:TOK", inline_debounce_ms=0, inline_style_variants=False)
    db = Database(":memory:")
    await db.connect()
    translator = ControlledTranslator(error_to_raise=exc)
    session = RecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=translator)

    try:
        await dp.feed_update(bot, _iq(1, "Привет", uid=1, lang_code="en"))
        ans_en = session.calls[-1]
        assert isinstance(ans_en, AnswerInlineQuery)
        assert ans_en.cache_time == 0
        assert ans_en.results[0].title == expected_en

        await dp.feed_update(bot, _iq(2, "Привет", uid=2, lang_code="ru"))
        ans_ru = session.calls[-1]
        assert isinstance(ans_ru, AnswerInlineQuery)
        assert ans_ru.cache_time == 0
        assert ans_ru.results[0].title == expected_ru
    finally:
        await db.close()
        await bot.session.close()


async def test_inline_expired_query_bad_request_is_ignored_while_other_errors_raise() -> None:
    config = Config(bot_token="123:TOK", inline_debounce_ms=0)
    db = Database(":memory:")
    await db.connect()
    translator = ControlledTranslator()

    expired_err = TelegramBadRequest(
        method=AnswerInlineQuery(inline_query_id="iq-1", results=[]),
        message="Bad Request: query is too old and response timeout expired or query ID is invalid",
    )
    session_expired = RecordingSession(answer_inline_error=expired_err)
    bot_expired = Bot(token=config.bot_token, session=session_expired)
    dp_expired = create_dispatcher(config=config, db=db, translator=translator)

    try:
        # Must NOT raise on expired inline query
        await dp_expired.feed_update(bot_expired, _iq(1, "", uid=1))

        # Genuine TelegramBadRequest (e.g. invalid entities) MUST propagate
        real_err = TelegramBadRequest(
            method=AnswerInlineQuery(inline_query_id="iq-2", results=[]),
            message="Bad Request: ENTITY_BOUNDS_INVALID",
        )
        session_real = RecordingSession(answer_inline_error=real_err)
        bot_real = Bot(token=config.bot_token, session=session_real)
        dp_real = create_dispatcher(config=config, db=db, translator=translator)
        with pytest.raises(TelegramBadRequest, match="ENTITY_BOUNDS_INVALID"):
            await dp_real.feed_update(bot_real, _iq(2, "", uid=1))
        await bot_real.session.close()
    finally:
        await db.close()
        await bot_expired.session.close()
