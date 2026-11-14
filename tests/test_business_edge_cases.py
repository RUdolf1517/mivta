"""Tests for Telegram Secretary / Business Mode connection lifecycle, guards, and error DMs."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    EditMessageCaption,
    EditMessageText,
    GetBusinessConnection,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    BusinessBotRights,
    BusinessConnection,
    Chat,
    Message,
    PhotoSize,
    Update,
    User,
)

from mivta.bot import create_dispatcher
from mivta.config import Config
from mivta.database import Database
from mivta.handlers.pipeline import TELEGRAM_MAX_CAPTION_LEN
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorUnavailableError,
)
from mivta.utils.ratelimit import RateLimiter


class BusinessRecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.live_connections: dict[str, BusinessConnection | Exception] = {}
        self.edit_error: Exception | None = None
        self.send_error: Exception | None = None

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, GetBusinessConnection):
            item = self.live_connections.get(method.business_connection_id)
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return item
            raise TelegramBadRequest(method=method, message="Bad Request: connection not found")
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            if self.edit_error is not None:
                raise self.edit_error
            return Message(
                message_id=1,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=999, type="private"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        if isinstance(method, SendMessage):
            if self.send_error is not None:
                raise self.send_error
            return Message(
                message_id=2,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class SimpleStubTranslator(Translator):
    name = "stub"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[TranslationRequest] = []

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.requests.append(request)
        if self.fail:
            raise TranslatorUnavailableError("upstream down")
        return TranslationResult(
            translated_text=f"[{request.target_lang}] {request.text}",
            source_lang="ru",
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )


def _owner(uid: int = 100, lang_code: str = "en") -> User:
    return User(id=uid, is_bot=False, first_name="Owner", language_code=lang_code)


def _conn(
    conn_id: str,
    *,
    uid: int = 100,
    lang_code: str = "en",
    can_reply: bool = True,
    is_enabled: bool = True,
) -> BusinessConnection:
    return BusinessConnection(
        id=conn_id,
        user=_owner(uid, lang_code),
        user_chat_id=uid,
        date=dt.datetime.now(dt.UTC),
        rights=BusinessBotRights(can_reply=can_reply),
        is_enabled=is_enabled,
    )


async def test_business_connection_events_welcome_permission_warning_and_disabled() -> None:
    config = Config(bot_token="123:TOK")
    db = Database(":memory:")
    await db.connect()
    session = BusinessRecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=SimpleStubTranslator())

    try:
        # 1. Disabled connection -> stored in DB, zero DM notifications sent
        await dp.feed_update(
            bot,
            Update(update_id=1, business_connection=_conn("bc-off", is_enabled=False)),
        )
        rec_off = await db.get_business_connection("bc-off")
        assert rec_off is not None and rec_off.is_enabled is False
        assert not any(isinstance(c, SendMessage) for c in session.calls)

        # 2. Enabled WITHOUT can_reply (English user) -> localized permission warning DM
        await dp.feed_update(
            bot,
            Update(
                update_id=2,
                business_connection=_conn(
                    "bc-noreply-en",
                    uid=101,
                    lang_code="pl",
                    can_reply=False,
                ),
            ),
        )
        dm_en = session.calls[-1]
        assert isinstance(dm_en, SendMessage)
        assert "'Reply to messages' permission is off" in dm_en.text

        # 3. Enabled WITHOUT can_reply (Russian user) -> Russian permission warning DM
        await dp.feed_update(
            bot,
            Update(
                update_id=3,
                business_connection=_conn(
                    "bc-noreply-ru",
                    uid=102,
                    lang_code="ru",
                    can_reply=False,
                ),
            ),
        )
        dm_ru = session.calls[-1]
        assert isinstance(dm_ru, SendMessage)
        assert "Reply to messages" in dm_ru.text and "Автоматизация чатов" in dm_ru.text

        # 4. Enabled WITH can_reply -> localized welcome DM (EN & RU)
        await dp.feed_update(
            bot,
            Update(
                update_id=4,
                business_connection=_conn("bc-ok-en", uid=103, lang_code="de", can_reply=True),
            ),
        )
        assert "mivta is connected to Telegram Chat Automation" in session.calls[-1].text

        await dp.feed_update(
            bot,
            Update(
                update_id=5,
                business_connection=_conn("bc-ok-ru", uid=104, lang_code="ru", can_reply=True),
            ),
        )
        assert "подключён к Telegram Business" in session.calls[-1].text

        # 5. If owner blocked DM (SendMessage raises TelegramBadRequest), handler does not crash
        session.send_error = TelegramBadRequest(
            method=SendMessage(chat_id=105, text="x"),
            message="Bad Request: chat not found",
        )
        await dp.feed_update(
            bot,
            Update(update_id=6, business_connection=_conn("bc-blocked", uid=105)),
        )
    finally:
        await db.close()
        await bot.session.close()


async def test_business_live_connection_lookup_safety_guards_and_edited_message() -> None:
    config = Config(bot_token="123:TOK", max_text_length=50)
    db = Database(":memory:")
    await db.connect()
    translator = SimpleStubTranslator()
    limiter = RateLimiter(rate_per_minute=60.0, burst=2)
    session = BusinessRecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(
        config=config,
        db=db,
        translator=translator,
        rate_limiter=limiter,
    )

    try:
        # 1. Incoming message from interlocutor (from_user.id == chat.id) MUST NEVER be touched
        incoming = Message(
            message_id=10,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=555, type="private"),
            from_user=User(id=555, is_bot=False, first_name="Peer"),
            business_connection_id="bc-live",
            text="> Привет",
        )
        await dp.feed_update(bot, Update(update_id=10, business_message=incoming))
        assert len(translator.requests) == 0
        assert len(session.calls) == 0

        # 2. Message sent by a business bot (sender_business_bot set) MUST NEVER be touched
        bot_echo = Message(
            message_id=11,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=555, type="private"),
            from_user=_owner(100),
            sender_business_bot=User(id=1, is_bot=True, first_name="mivta"),
            business_connection_id="bc-live",
            text="> Привет",
        )
        await dp.feed_update(bot, Update(update_id=11, business_message=bot_echo))
        assert len(translator.requests) == 0

        # 3. Live lookup via GetBusinessConnection when DB has no cached record yet!
        session.live_connections["bc-live"] = _conn("bc-live", uid=100, lang_code="en")
        outgoing = Message(
            message_id=12,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=555, type="private"),
            from_user=_owner(100, "en"),
            business_connection_id="bc-live",
            text="> Привет",
        )
        await dp.feed_update(bot, Update(update_id=12, business_message=outgoing))
        assert any(isinstance(c, GetBusinessConnection) for c in session.calls)
        edit = session.calls[-1]
        assert isinstance(edit, EditMessageText)
        assert edit.text == "[en] Привет"

        # 4. edited_business_message also translates in-place
        edited = outgoing.model_copy(update={"message_id": 13, "text": ">de Пока"})
        await dp.feed_update(bot, Update(update_id=13, edited_business_message=edited))
        edit2 = session.calls[-1]
        assert isinstance(edit2, EditMessageText)
        assert edit2.text == "[de] Пока"

        # 5. Non-translatable symbols (`> 12345 🔥`) strip `>` without calling translator API!
        prev_reqs = len(translator.requests)
        symbols_msg = outgoing.model_copy(update={"message_id": 14, "text": "> 12345 🔥"})
        await dp.feed_update(bot, Update(update_id=14, business_message=symbols_msg))
        assert len(translator.requests) == prev_reqs
        edit_sym = session.calls[-1]
        assert isinstance(edit_sym, EditMessageText)
        assert edit_sym.text == "12345 🔥"

        # 6. Empty `>   ` does nothing
        calls_before = len(session.calls)
        empty_trig = outgoing.model_copy(update={"message_id": 15, "text": ">   "})
        await dp.feed_update(bot, Update(update_id=15, business_message=empty_trig))
        assert len(session.calls) == calls_before
    finally:
        await db.close()
        await bot.session.close()


async def test_business_error_notifications_and_message_not_modified_handling() -> None:
    config = Config(bot_token="123:TOK", max_text_length=20)
    db = Database(":memory:")
    await db.connect()
    translator = SimpleStubTranslator()
    limiter = RateLimiter(rate_per_minute=60.0, burst=1)
    session = BusinessRecordingSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(
        config=config,
        db=db,
        translator=translator,
        rate_limiter=limiter,
    )

    try:
        # 1. Connection with can_reply=False notifies owner via DM and does NOT edit message
        await db.upsert_business_connection(
            "bc-no-perm",
            user_id=200,
            user_chat_id=200,
            can_reply=False,
            is_enabled=True,
        )
        msg_no_perm = Message(
            message_id=20,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=888, type="private"),
            from_user=_owner(200, "en"),
            business_connection_id="bc-no-perm",
            text="> Привет",
        )
        await dp.feed_update(bot, Update(update_id=20, business_message=msg_no_perm))
        assert isinstance(session.calls[-1], SendMessage)
        assert "'Reply to messages' permission is disabled" in session.calls[-1].text

        # 2. Unknown language directive `>zz Привет` notifies owner privately
        await db.upsert_business_connection(
            "bc-err",
            user_id=200,
            user_chat_id=200,
            can_reply=True,
            is_enabled=True,
        )
        bad_dir = msg_no_perm.model_copy(
            update={"business_connection_id": "bc-err", "text": ">zz Привет"}
        )
        await dp.feed_update(bot, Update(update_id=21, business_message=bad_dir))
        assert isinstance(session.calls[-1], SendMessage)
        assert "Unknown target language code" in session.calls[-1].text

        # 3. Caption longer than Telegram's own caption limit notifies owner privately
        over_limit = TELEGRAM_MAX_CAPTION_LEN + 6
        long_caption = Message(
            message_id=22,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=888, type="private"),
            from_user=_owner(200, "en"),
            business_connection_id="bc-err",
            photo=[PhotoSize(file_id="p1", file_unique_id="u1", width=10, height=10)],
            caption="> " + ("A" * over_limit),
        )
        await dp.feed_update(bot, Update(update_id=22, business_message=long_caption))
        assert isinstance(session.calls[-1], SendMessage)
        assert (
            f"text is too long ({over_limit} / {TELEGRAM_MAX_CAPTION_LEN} chars)"
            in session.calls[-1].text
        )

        # 3b. Text above MAX_TEXT_LENGTH is no longer rejected: it is chunked and stitched.
        # Uses a separate owner so the rate-limit bucket of owner 200 stays untouched.
        await db.upsert_business_connection(
            "bc-long",
            user_id=202,
            user_chat_id=202,
            can_reply=True,
            is_enabled=True,
        )
        long_text = " ".join(f"word{index}" for index in range(30))
        assert len(long_text) > config.max_text_length
        long_msg = Message(
            message_id=25,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=888, type="private"),
            from_user=_owner(202, "en"),
            business_connection_id="bc-long",
            text=f"> {long_text}",
        )
        await dp.feed_update(bot, Update(update_id=25, business_message=long_msg))
        assert isinstance(session.calls[-1], EditMessageText)
        stitched = session.calls[-1].text
        assert "too long" not in stitched
        for word in ("word0", "word15", "word29"):
            assert word in stitched
        assert len(stitched) > len(long_text)  # translated in several chunks

        # 4. Upstream failure notifies owner and leaves original message intact
        translator.fail = True
        fail_msg = msg_no_perm.model_copy(
            update={"business_connection_id": "bc-err", "text": "> Тест"}
        )
        await dp.feed_update(bot, Update(update_id=23, business_message=fail_msg))
        assert isinstance(session.calls[-1], SendMessage)
        assert "temporarily unavailable" in session.calls[-1].text

        # 5. Rate-limit exceeded (burst=1 was consumed on attempt #23!) notifies owner
        translator.fail = False
        rl_msg = msg_no_perm.model_copy(
            update={"business_connection_id": "bc-err", "text": "> Второй"}
        )
        await dp.feed_update(bot, Update(update_id=24, business_message=rl_msg))
        assert isinstance(session.calls[-1], SendMessage)
        assert "rate limit reached" in session.calls[-1].text

        # 6. TelegramBadRequest("message is not modified") on EditMessageText is swallowed safely
        await db.upsert_business_connection(
            "bc-fresh",
            user_id=201,
            user_chat_id=201,
            can_reply=True,
            is_enabled=True,
        )
        session.edit_error = TelegramBadRequest(
            method=EditMessageText(chat_id=888, message_id=25, text="x"),
            message="Bad Request: message is not modified",
        )
        ok_msg = Message(
            message_id=25,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=888, type="private"),
            from_user=_owner(201, "ru"),
            business_connection_id="bc-fresh",
            text="> Привет",
        )
        await dp.feed_update(bot, Update(update_id=25, business_message=ok_msg))
    finally:
        await db.close()
        await bot.session.close()
