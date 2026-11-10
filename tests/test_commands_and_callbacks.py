"""Comprehensive tests for all slash commands, interactive menus, pagination, and callbacks."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from mivta.bot import create_dispatcher
from mivta.config import Config
from mivta.database import Database
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
)


class CmdSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.edit_not_modified: bool = False

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, EditMessageText) and self.edit_not_modified:
            raise TelegramBadRequest(
                method=method,
                message="Bad Request: message is not modified",
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=10,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=1, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class NoopTranslator(Translator):
    name = "noop"

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        return TranslationResult(
            translated_text=request.text,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )


def _msg(update_id: int, text: str, uid: int = 10, lang_code: str = "en") -> Update:
    entities = None
    if text.startswith("/"):
        cmd_tok = text.split()[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(cmd_tok)}]
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name="Test", language_code=lang_code),
            text=text,
            entities=entities,
        ),
    )


def _cb(update_id: int, data: str | None, uid: int = 10, lang_code: str = "en") -> Update:
    base_msg = Message(
        message_id=77,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=1, is_bot=True, first_name="mivta"),
        text="menu",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=User(id=uid, is_bot=False, first_name="Test", language_code=lang_code),
            chat_instance="ci-cmd",
            message=base_msg,
            data=data,
        ),
    )


async def test_all_commands_empty_valid_and_invalid_arguments_in_en_and_ru() -> None:
    config = Config(bot_token="123:TOK")
    db = Database(":memory:")
    await db.connect()
    session = CmdSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(
        config=config,
        db=db,
        translator=NoopTranslator(),
        bot_username="mivta_bot",
    )

    try:
        # /start, /start settings, /help in EN and RU
        await dp.feed_update(bot, _msg(1, "/start", uid=11, lang_code="en"))
        assert "mivta" in session.calls[-1].text

        await dp.feed_update(bot, _msg(2, "/start settings", uid=11, lang_code="en"))
        assert "mivta Translation Settings" in session.calls[-1].text

        await dp.feed_update(bot, _msg(3, "/help", uid=11, lang_code="en"))
        assert "Inline Mode" in session.calls[-1].text
        await dp.feed_update(bot, _msg(4, "/help", uid=12, lang_code="ru"))
        assert "Secretary Mode" in session.calls[-1].text

        # /lang: no args -> picker; native name -> sets code; invalid -> localized error
        await dp.feed_update(bot, _msg(5, "/lang", uid=11, lang_code="en"))
        assert session.calls[-1].reply_markup is not None
        await dp.feed_update(bot, _msg(6, "/lang polski", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).target_lang == "pl"
        await dp.feed_update(bot, _msg(7, "/lang nope", uid=11, lang_code="en"))
        assert "Unknown" in session.calls[-1].text
        await dp.feed_update(bot, _msg(8, "/lang nope", uid=12, lang_code="ru"))
        assert "Неизвестный" in session.calls[-1].text

        # /swap: no args -> swap picker; set; clear; invalid in EN & RU
        await dp.feed_update(bot, _msg(9, "/swap", uid=11, lang_code="en"))
        assert session.calls[-1].reply_markup is not None
        await dp.feed_update(bot, _msg(10, "/swap ru", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).swap_lang == "ru"
        await dp.feed_update(bot, _msg(11, "/swap off", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).swap_lang == ""
        await dp.feed_update(bot, _msg(12, "/swap bad", uid=11, lang_code="en"))
        assert "Unknown language code" in session.calls[-1].text
        await dp.feed_update(bot, _msg(13, "/swap bad", uid=12, lang_code="ru"))
        assert "Неизвестный код языка" in session.calls[-1].text

        # /langs: no args -> extra picker; valid list; >3 rejected; invalid code; clear
        await dp.feed_update(bot, _msg(14, "/langs", uid=11, lang_code="en"))
        assert session.calls[-1].reply_markup is not None
        await dp.feed_update(bot, _msg(15, "/langs de, fr; es", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).extra_langs == ("de", "fr", "es")
        await dp.feed_update(bot, _msg(16, "/langs de fr es it", uid=11, lang_code="en"))
        assert "at most 3" in session.calls[-1].text
        await dp.feed_update(bot, _msg(17, "/langs de fr es it", uid=12, lang_code="ru"))
        assert "не более 3" in session.calls[-1].text
        await dp.feed_update(bot, _msg(18, "/langs de badcode", uid=11, lang_code="en"))
        assert "Unknown language code" in session.calls[-1].text
        await dp.feed_update(bot, _msg(19, "/langs off", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).extra_langs == ()

        # /source: no args -> source picker; auto; language; invalid in EN & RU
        await dp.feed_update(bot, _msg(20, "/source", uid=11, lang_code="en"))
        assert "Choose source language" in session.calls[-1].text
        await dp.feed_update(bot, _msg(21, "/source fr", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).source_lang == "fr"
        await dp.feed_update(bot, _msg(22, "/source авто", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).source_lang == "auto"
        await dp.feed_update(bot, _msg(23, "/source bad", uid=11, lang_code="en"))
        assert "Unknown source language" in session.calls[-1].text
        await dp.feed_update(bot, _msg(24, "/source bad", uid=12, lang_code="ru"))
        assert "Неизвестный исходный язык" in session.calls[-1].text

        # /style: no args -> menu; valid alias; invalid in EN & RU
        await dp.feed_update(bot, _msg(25, "/style", uid=11, lang_code="en"))
        assert "mivta Translation Settings" in session.calls[-1].text
        await dp.feed_update(bot, _msg(26, "/style разговорный", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).style == TranslationStyle.CASUAL
        await dp.feed_update(bot, _msg(27, "/style poetic", uid=11, lang_code="en"))
        assert "Unknown style" in session.calls[-1].text
        await dp.feed_update(bot, _msg(28, "/style poetic", uid=12, lang_code="ru"))
        assert "Неизвестный стиль" in session.calls[-1].text

        # /ui: no args toggles EN <-> RU; invalid arg prints usage; /privacy & /reset in RU
        await dp.feed_update(bot, _msg(29, "/ui", uid=11, lang_code="en"))
        assert (await db.get_user_settings(11)).ui_lang == "ru"
        assert "Настройки перевода mivta" in session.calls[-1].text
        await dp.feed_update(bot, _msg(30, "/ui invalid", uid=11, lang_code="en"))
        assert "Использование: /ui" in session.calls[-1].text

        await dp.feed_update(bot, _msg(31, "/privacy", uid=12, lang_code="ru"))
        assert "Политика конфиденциальности mivta" in session.calls[-1].text
        await dp.feed_update(bot, _msg(32, "/reset", uid=12, lang_code="ru"))
        assert "Ваши сохранённые настройки удалены" in session.calls[-1].text

        # Plain text in private chat declines DM translation in user's UI language
        await dp.feed_update(bot, _msg(33, "Translate this please", uid=13, lang_code="de"))
        assert "Inline Mode" in session.calls[-1].text
    finally:
        await db.close()
        await bot.session.close()


async def test_all_settings_callbacks_pagination_toggle_limits_and_safe_edits() -> None:
    config = Config(bot_token="123:TOK")
    db = Database(":memory:")
    await db.connect()
    session = CmdSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=NoopTranslator())

    try:
        # Sub-menus & page 1 / page 2 pagination
        for idx, cb_data in enumerate(
            [
                "cfg:main",
                "cfg:tgt_menu:1",
                "cfg:tgt_menu:2",
                "cfg:src_menu:1",
                "cfg:src_menu:2",
                "cfg:swap_menu:1",
                "cfg:swap_menu:2",
                "cfg:extra_menu:1",
                "cfg:extra_menu:2",
            ],
            start=100,
        ):
            await dp.feed_update(bot, _cb(idx, cb_data, uid=50, lang_code="en"))
            assert isinstance(session.calls[-1], AnswerCallbackQuery)

        # Toggle extra languages up to max 3, verify 4th triggers max toast, then remove & clear
        await dp.feed_update(bot, _cb(110, "cfg:toggle_extra:de:1", uid=50))
        await dp.feed_update(bot, _cb(111, "cfg:toggle_extra:fr:1", uid=50))
        await dp.feed_update(bot, _cb(112, "cfg:toggle_extra:pl:1", uid=50))
        assert (await db.get_user_settings(50)).extra_langs == ("de", "fr", "pl")

        # 4th language blocked
        await dp.feed_update(bot, _cb(113, "cfg:toggle_extra:es:1", uid=50))
        ans_max = session.calls[-1]
        assert isinstance(ans_max, AnswerCallbackQuery)
        assert "Max 3" in (ans_max.text or "")

        # Toggle off 'fr' and then clear all
        await dp.feed_update(bot, _cb(114, "cfg:toggle_extra:fr:1", uid=50))
        assert (await db.get_user_settings(50)).extra_langs == ("de", "pl")
        await dp.feed_update(bot, _cb(115, "cfg:clear_extra:1", uid=50))
        assert (await db.get_user_settings(50)).extra_langs == ()

        # Set target, source, swap, style, UI via callbacks + invalid callback payloads
        await dp.feed_update(bot, _cb(116, "cfg:set_tgt:it", uid=50))
        await dp.feed_update(bot, _cb(117, "cfg:set_src:es", uid=50))
        await dp.feed_update(bot, _cb(118, "cfg:set_swap:pl", uid=50))
        await dp.feed_update(bot, _cb(119, "cfg:set_style:formal", uid=50))
        await dp.feed_update(bot, _cb(120, "cfg:set_ui:ru", uid=50))
        st = await db.get_user_settings(50)
        assert (st.target_lang, st.source_lang, st.swap_lang, st.style, st.ui_lang) == (
            "it",
            "es",
            "pl",
            TranslationStyle.FORMAL,
            "ru",
        )
        await dp.feed_update(bot, _cb(121, "cfg:set_swap:off", uid=50))
        assert (await db.get_user_settings(50)).swap_lang == ""

        # Invalid/unknown callback arguments do not crash
        for idx, bad_cb in enumerate(
            [
                "cfg:toggle_extra:zz:1",
                "cfg:set_tgt:zz",
                "cfg:set_src:zz",
                "cfg:set_swap:zz",
                "cfg:set_style:zz",
                "cfg:set_ui:zz",
                "cfg:nonexistent_action",
            ],
            start=130,
        ):
            await dp.feed_update(bot, _cb(idx, bad_cb, uid=50))

        # TelegramBadRequest("message is not modified") inside _edit_safely is ignored cleanly
        session.edit_not_modified = True
        await dp.feed_update(bot, _cb(140, "cfg:main", uid=50))
    finally:
        await db.close()
        await bot.session.close()
