"""Tests for the compact `/settings` panel, separate `/help` and unified owner panel."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from mivta import __version__
from mivta.bot import create_dispatcher
from mivta.config import Config
from mivta.database import Database
from mivta.service import RestartController
from mivta.translator.base import ProviderStatus, TranslationRequest, TranslationResult
from mivta.translator.base import Translator as BaseTranslator


class CapturingBot(Bot):
    """Offline Bot stub capturing outgoing API methods."""

    def __init__(self) -> None:
        super().__init__(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.calls: list[TelegramMethod[Any]] = []

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = int(method.chat_id) if method.chat_id is not None else 1
            return Message(
                message_id=500 + len(self.calls),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
        return True


class StubTranslator(BaseTranslator):
    """Minimal translator with a fixed provider chain for the owner dashboard."""

    name = "google"

    def __init__(self) -> None:
        self.calls = 0

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.calls += 1
        return TranslationResult(
            translated_text=request.text,
            source_lang="ru",
            target_lang=request.target_lang,
            provider="google",
            style=request.style,
        )

    def get_provider_statuses(self) -> tuple[ProviderStatus, ...]:
        return (ProviderStatus(name="google", is_active=True),)

    def reset_cooldowns(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _msg_update(update_id: int, user_id: int, text: str, lang_code: str = "ru") -> Update:
    user = User(id=user_id, is_bot=False, first_name="User", language_code=lang_code)
    return Update(
        update_id=update_id,
        message=Message(
            message_id=100 + update_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text=text,
        ),
    )


def _cb_update(update_id: int, user_id: int, data: str, lang_code: str = "ru") -> Update:
    user = User(id=user_id, is_bot=False, first_name="User", language_code=lang_code)
    msg = Message(
        message_id=700,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=123456, is_bot=True, first_name="mivta"),
        text="panel placeholder",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=user,
            chat_instance="ci-ui",
            message=msg,
            data=data,
        ),
    )


def _button_rows(markup: Any) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_settings_panel_is_one_screen_and_help_is_separate(tmp_path: Path) -> None:
    db = Database(tmp_path / "ui.db")
    await db.connect()
    try:
        cfg = Config(bot_token="123456:ABCDEF", default_ui_lang="ru")
        dp = create_dispatcher(
            config=cfg,
            db=db,
            translator=StubTranslator(),
            bot_username="mivtabot",
        )
        bot = CapturingBot()

        # 1. /settings: compact state line (no swap yet -> АВТО → EN)
        await dp.feed_update(bot, _msg_update(1, user_id=555, text="/settings"))
        panel = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Настройки перевода mivta" in panel.text
        assert "🌍 Авто → EN · Нейтральный" in panel.text
        assert "Доп. языки карточек: выкл" in panel.text
        assert "cfg:help" in _button_rows(panel.reply_markup)
        # The instruction dump no longer lives in the settings message
        assert "Автоматизация чатов (личка" not in panel.text

        # 2. «Как пользоваться» swaps the same message into the full guide
        bot.calls.clear()
        await dp.feed_update(bot, _cb_update(2, user_id=555, data="cfg:help"))
        guide = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "mivta — как пользоваться" in guide.text
        assert "Инлайн-режим" in guide.text
        assert "Автоматизация чатов" in guide.text
        assert "cfg:main" in _button_rows(guide.reply_markup)

        # 3. «◂ Настройки» returns to the compact panel
        bot.calls.clear()
        await dp.feed_update(bot, _cb_update(3, user_id=555, data="cfg:main"))
        back = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Настройки перевода mivta" in back.text

        # 4. /help is its own command with the guide + a jump into settings
        bot.calls.clear()
        await dp.feed_update(bot, _msg_update(4, user_id=555, text="/help"))
        help_msg = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "как пользоваться" in help_msg.text
        assert "cfg:main" in _button_rows(help_msg.reply_markup)

        # 5. With an auto-swap pair the state line shows the ⇄ direction
        await db.update_user_settings(555, target_lang="en", swap_lang="ru")
        bot.calls.clear()
        await dp.feed_update(bot, _msg_update(5, user_id=555, text="/settings"))
        swapped = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "🌍 EN ⇄ RU · Нейтральный" in swapped.text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_logs_command_is_wired_into_the_dispatcher(tmp_path: Path) -> None:
    """`/logs` reaches the handler through workflow data, including its tail buffer."""
    from mivta.utils.logtail import LogTail

    db = Database(tmp_path / "logs-ui.db")
    await db.connect()
    try:
        cfg = Config(bot_token="123456:ABCDEF", default_ui_lang="ru", owner_ids=(555,))
        tail = LogTail(capacity=10)
        dp = create_dispatcher(
            config=cfg,
            db=db,
            translator=StubTranslator(),
            bot_username="mivtabot",
            log_tail=tail,
        )
        bot = CapturingBot()

        # 1. Empty buffer -> the "all clean" message
        await dp.feed_update(bot, _msg_update(1, user_id=555, text="/logs"))
        clean = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "пусты" in clean.text

        # 2. A warning shows up in the owner's chat, with refresh/clear buttons
        import logging

        logger = logging.getLogger("mivta.test.tail")
        logger.propagate = False
        logger.addHandler(tail)
        try:
            logger.warning("provider timeout")
        finally:
            logger.removeHandler(tail)

        bot.calls.clear()
        await dp.feed_update(bot, _msg_update(2, user_id=555, text="/logs"))
        logs = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "provider timeout" in logs.text
        rows = _button_rows(logs.reply_markup)
        assert "svc:logs" in rows and "svc:logs_clear" in rows

        # 3. «🧹 Очистить» empties the buffer and re-renders the same message
        bot.calls.clear()
        await dp.feed_update(bot, _cb_update(3, user_id=555, data="svc:logs_clear"))
        cleared = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "пусты" in cleared.text
        assert len(tail) == 0

        # 4. A non-owner gets nothing but the denial text
        bot.calls.clear()
        await dp.feed_update(bot, _msg_update(4, user_id=777, text="/logs"))
        denied = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Доступ запрещён" in denied.text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_panel_unifies_status_and_service(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "owner.db")
    await db.connect()
    try:
        cfg = Config(
            bot_token="123456:ABCDEF",
            owner_ids=(9001,),
            database_path=str(data_dir / "owner.db"),
        )
        dp = create_dispatcher(
            config=cfg,
            db=db,
            translator=StubTranslator(),
            restart=RestartController(),
            bot_username="mivtabot",
        )
        bot = CapturingBot()

        # 1. /status now carries the service section and navigation buttons
        await dp.feed_update(bot, _msg_update(1, user_id=9001, text="/status"))
        status = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert f"v{__version__}" in status.text
        assert "🛠 <b>Обслуживание</b>" in status.text
        assert "Обновление из Telegram: <b>вкл</b>" in status.text
        rows = _button_rows(status.reply_markup)
        assert "svc:check" in rows
        assert "svc:restart" in rows

        # 2. «Обслуживание ▸» opens the update screen in the same message
        bot.calls.clear()
        await dp.feed_update(bot, _cb_update(2, user_id=9001, data="svc:check"))
        service = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        service_rows = _button_rows(service.reply_markup)
        assert "svc:status" in service_rows
        assert "svc:restart" in service_rows

        # 3. «📊 Статус» returns to the dashboard in the same message
        bot.calls.clear()
        await dp.feed_update(bot, _cb_update(3, user_id=9001, data="svc:status"))
        back = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert f"v{__version__}" in back.text
        assert "🛠 <b>Обслуживание</b>" in back.text
        assert "svc:check" in _button_rows(back.reply_markup)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_panel_hides_service_buttons_when_disabled(tmp_path: Path) -> None:
    data_dir = tmp_path / "data2"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "owner.db")
    await db.connect()
    try:
        cfg = Config(
            bot_token="123456:ABCDEF",
            owner_ids=(9001,),
            database_path=str(data_dir / "owner.db"),
            self_update=False,
            self_restart=False,
        )
        dp = create_dispatcher(
            config=cfg,
            db=db,
            translator=StubTranslator(),
            bot_username="mivtabot",
        )
        bot = CapturingBot()
        await dp.feed_update(bot, _msg_update(1, user_id=9001, text="/status"))
        status = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Обновление из Telegram: <b>выкл</b>" in status.text
        rows = _button_rows(status.reply_markup)
        assert "svc:check" not in rows
        assert "svc:restart" not in rows
    finally:
        await db.close()
