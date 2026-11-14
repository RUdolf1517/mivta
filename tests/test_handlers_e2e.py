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
    EditMessageCaption,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    BusinessBotRights,
    BusinessConnection,
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    MessageEntity,
    PhotoSize,
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
        if isinstance(method, (SendMessage, EditMessageText, EditMessageCaption)):
            return Message(
                message_id=999,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=100, type="private"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
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


def _business_conn_update(
    update_id: int,
    conn_id: str = "bc-100",
    owner_uid: int = 777,
    *,
    can_reply: bool = True,
    is_enabled: bool = True,
) -> Update:
    return Update(
        update_id=update_id,
        business_connection=BusinessConnection(
            id=conn_id,
            user=_user(owner_uid),
            user_chat_id=owner_uid,
            date=dt.datetime.now(dt.UTC),
            is_enabled=is_enabled,
            rights=BusinessBotRights(can_reply=can_reply),
        ),
    )


def _business_msg_update(
    update_id: int,
    text: str,
    *,
    conn_id: str = "bc-100",
    sender_uid: int = 777,
    interlocutor_chat_id: int = 999,
    edited: bool = False,
) -> Update:
    msg = Message(
        message_id=update_id,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=interlocutor_chat_id, type="private"),
        from_user=_user(sender_uid),
        business_connection_id=conn_id,
        text=text,
    )
    if edited:
        return Update(update_id=update_id, edited_business_message=msg)
    return Update(update_id=update_id, business_message=msg)


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
    assert "инлайн-режим" in reply.text
    assert "@mivta_bot" in reply.text


async def test_business_mode_translates_outgoing_in_place_without_via_bot(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    # 1. Connect Business account (owner=777)
    await dp.feed_update(bot, _business_conn_update(70, conn_id="bc-live", owner_uid=777))
    assert isinstance(session.calls[-1], SendMessage)
    assert session.calls[-1].chat_id == 777
    session.calls.clear()

    # 2. Normal outgoing message without `>` -> completely untouched
    await dp.feed_update(
        bot,
        _business_msg_update(71, "Обычное сообщение другу", conn_id="bc-live"),
    )
    assert len(translator.requests) == 0
    assert len(session.calls) == 0

    # 3. Incoming message from interlocutor (sender_uid == interlocutor_chat_id == 999)
    #    even with `>` prefix -> completely untouched!
    await dp.feed_update(
        bot,
        _business_msg_update(
            72,
            "> Привет, как дела?",
            conn_id="bc-live",
            sender_uid=999,
            interlocutor_chat_id=999,
        ),
    )
    assert len(translator.requests) == 0
    assert len(session.calls) == 0

    # 4. Outgoing message from owner with `>` -> edited in-place via EditMessageText!
    await dp.feed_update(
        bot,
        _business_msg_update(73, "> Привет, как дела?", conn_id="bc-live"),
    )
    assert len(translator.requests) == 1
    assert len(session.calls) == 1
    edit_call = session.calls[0]
    assert isinstance(edit_call, EditMessageText)
    assert edit_call.business_connection_id == "bc-live"
    assert edit_call.chat_id == 999
    assert edit_call.message_id == 73
    assert edit_call.text == "Hello, how are you?"
    assert edit_call.parse_mode is None

    # 5. Edited business message with `>de !formal` -> edited in-place with German formal!
    session.calls.clear()
    await dp.feed_update(
        bot,
        _business_msg_update(
            74,
            ">de !formal Привет, как дела?",
            conn_id="bc-live",
            edited=True,
        ),
    )
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].text == "[formal] Hello, how are you? (de)"

    # 6. Upstream error in Business Mode -> NEVER overwrites interlocutor chat, notifies owner DM
    session.calls.clear()
    translator.fail_with = TranslatorUnavailableError("upstream down")
    await dp.feed_update(
        bot,
        _business_msg_update(75, "> Новая фраза для теста сбоя", conn_id="bc-live"),
    )
    assert all(not isinstance(c, EditMessageText) for c in session.calls)
    assert len(session.calls) == 1
    assert isinstance(session.calls[0], SendMessage)
    assert session.calls[0].chat_id == 777



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
                assert not node.module.startswith("mivta.config")


async def test_business_mode_media_captions_and_entities_preserved(harness) -> None:
    dp, bot, session, _, translator, _, _ = harness

    await dp.feed_update(bot, _business_conn_update(200, conn_id="bc-cap", owner_uid=777))
    session.calls.clear()

    photo = [PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)]

    # 1. Outgoing photo without `>` in caption -> ignored
    msg_normal = Message(
        message_id=201,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=999, type="private"),
        from_user=_user(777),
        business_connection_id="bc-cap",
        photo=photo,
        caption="Просто фото без префикса",
    )
    await dp.feed_update(bot, Update(update_id=201, business_message=msg_normal))
    assert len(session.calls) == 0

    # 2. Incoming photo from interlocutor even with `>` -> ignored
    msg_incoming = Message(
        message_id=202,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=999, type="private"),
        from_user=_user(999),
        business_connection_id="bc-cap",
        photo=photo,
        caption="> Привет, как дела?",
    )
    await dp.feed_update(bot, Update(update_id=202, business_message=msg_incoming))
    assert len(session.calls) == 0

    # 3. Outgoing photo from owner with `>` caption + bold & code entities -> EditMessageCaption!
    raw_caption = "> Важно: запусти pytest -q"
    # "> " is 2 UTF-16 units; "Важно" at offset 2..7, "pytest -q" at offset 17..26
    caption_entities = [
        MessageEntity(type="bold", offset=2, length=5),
        MessageEntity(type="code", offset=17, length=9),
    ]

    class TagAwareStub(StubTranslator):
        async def translate(self, request: TranslationRequest) -> TranslationResult:
            self.requests.append(request)
            out = request.text.replace("Важно", "Important").replace("запусти", "run")
            return TranslationResult(
                translated_text=out,
                source_lang="ru",
                target_lang=request.target_lang,
                style=request.style,
                provider=self.name,
            )

    dp["translator"] = TagAwareStub()
    msg_owner = Message(
        message_id=203,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=999, type="private"),
        from_user=_user(777),
        business_connection_id="bc-cap",
        photo=photo,
        caption=raw_caption,
        caption_entities=caption_entities,
    )
    await dp.feed_update(bot, Update(update_id=203, business_message=msg_owner))
    assert len(session.calls) == 1
    cap_call = session.calls[0]
    assert isinstance(cap_call, EditMessageCaption)
    assert cap_call.business_connection_id == "bc-cap"
    assert cap_call.caption == "Important: run pytest -q"
    assert cap_call.caption_entities is not None
    by_type = {e.type: e for e in cap_call.caption_entities}
    b_ent = by_type["bold"]
    c_ent = by_type["code"]
    assert cap_call.caption[b_ent.offset : b_ent.offset + b_ent.length] == "Important"
    assert cap_call.caption[c_ent.offset : c_ent.offset + c_ent.length] == "pytest -q"
    assert translator.requests == []


async def test_inline_multicard_styles_extra_langs_and_code_entities(harness) -> None:
    dp, bot, session, db, _, _, _ = harness

    class StyleBatchStub(StubTranslator):
        supports_styles = True
        batch_calls = 0

        async def translate_styles(self, request, styles):
            self.batch_calls += 1
            return [
                TranslationResult(
                    translated_text={
                        TranslationStyle.NEUTRAL: f"Neutral: {request.text}",
                        TranslationStyle.CASUAL: f"Casual: {request.text}",
                        TranslationStyle.FORMAL: f"Formal: {request.text}",
                    }[st],
                    source_lang="ru",
                    target_lang=request.target_lang,
                    style=st,
                    provider="groq",
                )
                for st in styles
            ]

    style_stub = StyleBatchStub()
    dp["translator"] = style_stub

    # 1. Style-capable provider returns 3 style cards in 1 batched call (preferred style first)
    await dp.feed_update(bot, _message_update(300, "/style formal"))
    session.calls.clear()
    await dp.feed_update(bot, _inline_update(301, "Привет `git status` @rudolf"))
    assert style_stub.batch_calls == 1
    ans = session.calls[-1]
    assert isinstance(ans, AnswerInlineQuery)
    assert len(ans.results) == 3
    assert ans.results[0].input_message_content.message_text.startswith("Formal:")
    assert "git status" in ans.results[0].input_message_content.message_text
    assert "`" not in ans.results[0].input_message_content.message_text
    ent_types = {e.type for e in (ans.results[0].input_message_content.entities or [])}
    assert {"code", "mention"} <= ent_types

    # 2. Configure extra inline languages via /langs and settings callbacks -> multi-language cards!
    await dp.feed_update(bot, _message_update(302, "/langs pl de"))
    assert (await db.get_user_settings(777)).extra_langs == ("pl", "de")

    session.calls.clear()
    await dp.feed_update(bot, _inline_update(303, "Привет, как дела?"))
    multilang_ans = session.calls[-1]
    assert isinstance(multilang_ans, AnswerInlineQuery)
    assert len(multilang_ans.results) == 3
    descriptions = [r.description for r in multilang_ans.results]
    assert "→ en" in descriptions[0]
    assert "→ pl" in descriptions[1]
    assert "→ de" in descriptions[2]

    # 3. Toggle & clear extra languages via /settings inline callbacks and /langs off
    await dp.feed_update(bot, _callback_update(304, "cfg:extra_menu:1"))
    await dp.feed_update(bot, _callback_update(305, "cfg:toggle_extra:fr:1"))
    assert (await db.get_user_settings(777)).extra_langs == ("pl", "de", "fr")
    await dp.feed_update(bot, _callback_update(306, "cfg:clear_extra:1"))
    assert (await db.get_user_settings(777)).extra_langs == ()

    await dp.feed_update(bot, _message_update(307, "/langs es"))
    assert (await db.get_user_settings(777)).extra_langs == ("es",)
    await dp.feed_update(bot, _message_update(308, "/langs off"))
    assert (await db.get_user_settings(777)).extra_langs == ()
