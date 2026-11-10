"""End-to-end tests for Inline Mode, user settings, caching, rate limits, and DM policy."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerInlineQuery,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    Update,
    User,
)

from mivta.bot import create_dispatcher
from mivta.config import Config
from mivta.database import Database
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorUnavailableError,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.ratelimit import RateLimiter


class RecordingSession(BaseSession):
    """Captures outgoing Telegram API calls made by aiogram handlers without network I/O."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, (AnswerInlineQuery, AnswerCallbackQuery)):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=999,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=100, type="private"),
                text=getattr(method, "text", ""),
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover


class StubTranslator(Translator):
    name = "stub"

    def __init__(self) -> None:
        self.requests: list[TranslationRequest] = []
        self.fail_with: Exception | None = None
        self.delay_seconds: float = 0.0

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.requests.append(request)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_with is not None:
            raise self.fail_with
        detected = "ru" if request.source_lang == "auto" else request.source_lang
        prefix = {
            TranslationStyle.NEUTRAL: "",
            TranslationStyle.CASUAL: "[casual] ",
            TranslationStyle.FORMAL: "[formal] ",
        }[request.style]
        translated = f"{prefix}Hello, how are you? ({request.target_lang})"
        is_default_greeting = (
            request.text == "Привет, как дела?"
            and request.style == TranslationStyle.NEUTRAL
            and request.target_lang == "en"
        )
        if is_default_greeting:
            translated = "Hello, how are you?"
        return TranslationResult(
            translated_text=translated,
            source_lang=detected,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )


def _user(uid: int = 777) -> User:
    return User(id=uid, is_bot=False, first_name="Rudolf", username="rudolf")


def _inline_update(update_id: int, query: str, uid: int = 777) -> Update:
    return Update(
        update_id=update_id,
        inline_query=InlineQuery(
            id=f"iq-{update_id}",
            from_user=_user(uid),
            query=query,
            offset="",
        ),
    )


def _message_update(update_id: int, text: str, uid: int = 777) -> Update:
    entities = None
    if text.startswith("/"):
        cmd_token = text.split()[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(cmd_token)}]
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=uid, type="private"),
            from_user=_user(uid),
            text=text,
            entities=entities,
        ),
    )


def _callback_update(update_id: int, data: str, uid: int = 777) -> Update:
    msg = Message(
        message_id=50,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=1, is_bot=True, first_name="mivta"),
        text="settings",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=_user(uid),
            chat_instance="ci-1",
            message=msg,
            data=data,
        ),
    )


@pytest.fixture
async def harness():
    config = Config(
        bot_token="123456:TEST_TOKEN",
        max_text_length=80,
        rate_limit_per_minute=60.0,
        rate_limit_burst=3,
    )
    db = Database(":memory:")
    await db.connect()
    translator = StubTranslator()
    cache = TranslationCache(max_size=64, ttl_seconds=300.0)
    limiter = RateLimiter(rate_per_minute=60.0, burst=3)
    session = RecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(
        config=config,
        db=db,
        translator=translator,
        cache=cache,
        rate_limiter=limiter,
        bot_username="mivta_bot",
    )
    try:
        yield dp, bot, session, db, translator, cache, limiter
    finally:
        await db.close()
        await bot.session.close()


async def test_core_inline_translation_exact_message_without_extra_text(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    await dp.feed_update(bot, _inline_update(1, "Привет, как дела?"))
    assert len(translator.requests) == 1
    assert len(session.calls) == 1

    call = session.calls[0]
    assert isinstance(call, AnswerInlineQuery)
    assert call.is_personal is True
    assert len(call.results) == 1

    article = call.results[0]
    # Sent message text MUST be the exact translation ONLY — no ads, signatures, or tags
    assert article.input_message_content.message_text == "Hello, how are you?"
    assert article.input_message_content.parse_mode is None

    # Repeat identical query -> served from cache without calling translator again
    await dp.feed_update(bot, _inline_update(2, "Привет, как дела?"))
    assert len(translator.requests) == 1
    assert len(session.calls) == 2


async def test_persistent_user_settings_via_commands_and_callbacks(harness) -> None:
    dp, bot, session, db, translator, _, _ = harness

    # Change target language via /lang, manual source via /source, style via callback
    await dp.feed_update(bot, _message_update(10, "/lang de"))
    await dp.feed_update(bot, _message_update(11, "/source ru"))
    await dp.feed_update(bot, _callback_update(12, "cfg:set_style:formal"))

    saved = await db.get_user_settings(777)
    assert saved.target_lang == "de"
    assert saved.source_lang == "ru"
    assert saved.style == TranslationStyle.FORMAL

    session.calls.clear()
    await dp.feed_update(bot, _inline_update(13, "Привет, как дела?"))
    assert len(translator.requests) == 1
    req = translator.requests[0]
    assert req.source_lang == "ru"
    assert req.target_lang == "de"
    assert req.style == TranslationStyle.FORMAL

    call = session.calls[0]
    assert isinstance(call, AnswerInlineQuery)
    expected = "[formal] Hello, how are you? (de)"
    assert call.results[0].input_message_content.message_text == expected


async def test_settings_menus_and_start_deeplink(harness) -> None:
    dp, bot, session, db, _, _, _ = harness

    await dp.feed_update(bot, _message_update(50, "/start settings"))
    assert isinstance(session.calls[-1], SendMessage)
    assert "Настройки перевода" in session.calls[-1].text

    await dp.feed_update(bot, _callback_update(51, "cfg:tgt_menu:2"))
    assert isinstance(session.calls[-1], AnswerCallbackQuery)

    await dp.feed_update(bot, _callback_update(52, "cfg:set_tgt:ko"))
    assert (await db.get_user_settings(777)).target_lang == "ko"

    await dp.feed_update(bot, _callback_update(53, "cfg:set_src:pl"))
    assert (await db.get_user_settings(777)).source_lang == "pl"

    await dp.feed_update(bot, _callback_update(54, "cfg:set_src:auto"))
    assert (await db.get_user_settings(777)).source_lang == "auto"


async def test_inline_quick_overrides_do_not_mutate_saved_settings(harness) -> None:
    dp, bot, _, db, translator, _, _ = harness

    await dp.feed_update(bot, _inline_update(20, ">fr !casual Привет"))
    req = translator.requests[0]
    assert req.target_lang == "fr"
    assert req.style == TranslationStyle.CASUAL

    # Persistent user settings remain unchanged (en / auto / neutral)
    saved = await db.get_user_settings(777)
    assert saved.target_lang == "en"
    assert saved.source_lang == "auto"
    assert saved.style == TranslationStyle.NEUTRAL


async def test_non_translatable_symbols_passthrough_without_api_call(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    await dp.feed_update(bot, _inline_update(25, "12345 🔥"))
    assert len(translator.requests) == 0
    call = session.calls[-1]
    assert isinstance(call, AnswerInlineQuery)
    assert call.results[0].input_message_content.message_text == "12345 🔥"


async def test_concurrent_identical_queries_deduplicated(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness
    translator.delay_seconds = 0.05

    await asyncio.gather(
        dp.feed_update(bot, _inline_update(60, "Привет, как дела?", uid=801)),
        dp.feed_update(bot, _inline_update(61, "Привет, как дела?", uid=802)),
        dp.feed_update(bot, _inline_update(62, "Привет, как дела?", uid=803)),
    )
    assert len(translator.requests) == 1
    assert len(session.calls) == 3
    for call in session.calls:
        assert isinstance(call, AnswerInlineQuery)
        assert call.results[0].input_message_content.message_text == "Hello, how are you?"


async def test_empty_invalid_too_long_ratelimit_and_outage_handling(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    # 1. Empty query -> handled cleanly without calling translator
    await dp.feed_update(bot, _inline_update(30, "   "))
    assert len(translator.requests) == 0
    assert isinstance(session.calls[-1], AnswerInlineQuery)
    assert "Введите текст" in session.calls[-1].results[0].title

    # 2. Unknown language prefix -> helpful message without calling translator
    await dp.feed_update(bot, _inline_update(31, ">zz Привет"))
    assert len(translator.requests) == 0
    assert "Неизвестный код" in session.calls[-1].results[0].title

    # 3. Exceeds max_text_length (80 in fixture)
    await dp.feed_update(bot, _inline_update(32, "А" * 90))
    assert len(translator.requests) == 0
    assert "слишком длинный" in session.calls[-1].results[0].title

    # 4. Translator outage -> graceful cache_time=0 error article
    translator.fail_with = TranslatorUnavailableError("down")
    await dp.feed_update(bot, _inline_update(33, "Первое сообщение"))
    outage_call = session.calls[-1]
    assert isinstance(outage_call, AnswerInlineQuery)
    assert outage_call.cache_time == 0
    assert "недоступен" in outage_call.results[0].title
    translator.fail_with = None

    # 5. Rate limit after burst=3 (1 consumed above; consume 2 more, 4th is rate-limited)
    await dp.feed_update(bot, _inline_update(34, "Второе сообщение"))
    await dp.feed_update(bot, _inline_update(35, "Третье сообщение"))
    await dp.feed_update(bot, _inline_update(36, "Четвёртое сообщение"))
    rl_call = session.calls[-1]
    assert isinstance(rl_call, AnswerInlineQuery)
    assert rl_call.cache_time == 0
    assert "Слишком много запросов" in rl_call.results[0].title


async def test_direct_messages_are_not_translated(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    await dp.feed_update(bot, _message_update(40, "Переведи это сообщение в личке"))
    assert len(translator.requests) == 0
    assert len(session.calls) == 1
    reply = session.calls[0]
    assert isinstance(reply, SendMessage)
    assert "Inline Mode" in reply.text
    assert "@mivta_bot" in reply.text


def test_translator_package_is_independent_of_telegram_and_sqlite() -> None:
    translator_dir = Path(__file__).resolve().parents[1] / "mivta" / "translator"
    forbidden = {"aiogram", "aiosqlite", "sqlite3"}
    for py_file in translator_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    assert root_pkg not in forbidden, f"{py_file} imports {root_pkg}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_pkg = node.module.split(".")[0]
                assert root_pkg not in forbidden, f"{py_file} imports from {root_pkg}"
                assert not node.module.startswith("mivta.handlers")
                assert not node.module.startswith("mivta.database")
                assert not node.module.startswith("mivta.bot")
