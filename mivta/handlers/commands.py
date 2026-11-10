"""Command and settings callback handlers (persistent user preferences only)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from mivta.database import Database, UserSettings
from mivta.translator.base import STYLE_LABELS_RU, TranslationStyle, parse_style
from mivta.utils.languages import (
    AUTO_LANG,
    LANGUAGE_BY_CODE,
    QUICK_LANGUAGES_PRIMARY,
    QUICK_LANGUAGES_SECONDARY,
    language_label,
    resolve_language,
)


def _format_settings_text(settings: UserSettings, bot_username: str) -> str:
    src_label = language_label(settings.source_lang)
    tgt_label = language_label(settings.target_lang)
    style_label = STYLE_LABELS_RU[settings.style]
    return (
        "Настройки перевода mivta\n\n"
        f"• Исходный язык: {src_label}\n"
        f"• Язык перевода: {tgt_label}\n"
        f"• Стиль: {style_label}\n\n"
        "Inline Mode (любой чат):\n"
        f"• @{bot_username} Привет, как дела?\n"
        f"• @{bot_username} >de текст · ru>en текст · !formal текст\n\n"
        "Telegram Business (личные чаты 1-на-1, без метки via @bot):\n"
        "• > Привет, как дела?\n"
        "• >de текст · ru>pl !formal текст"
    )


def _main_settings_keyboard(settings: UserSettings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Quick target language row (most common 6 languages for 1-tap switching)
    quick_targets = ("en", "ru", "de", "fr", "es", "pl")
    quick_row: list[InlineKeyboardButton] = []
    for code in quick_targets:
        prefix = "• " if settings.target_lang == code else ""
        quick_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{code.upper()}",
                callback_data=f"cfg:set_tgt:{code}",
            )
        )
    rows.append(quick_row)

    # Style selector row
    style_row: list[InlineKeyboardButton] = []
    for style in (
        TranslationStyle.NEUTRAL,
        TranslationStyle.CASUAL,
        TranslationStyle.FORMAL,
    ):
        prefix = "• " if settings.style == style else ""
        style_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{STYLE_LABELS_RU[style].capitalize()}",
                callback_data=f"cfg:set_style:{style.value}",
            )
        )
    rows.append(style_row)

    # Full target & source language pickers
    src_short = "Авто" if settings.source_lang == AUTO_LANG else settings.source_lang.upper()
    rows.append(
        [
            InlineKeyboardButton(
                text=f"Язык перевода ({settings.target_lang.upper()}) ▸",
                callback_data="cfg:tgt_menu:1",
            ),
            InlineKeyboardButton(
                text=f"Исходный ({src_short}) ▸",
                callback_data="cfg:src_menu:1",
            ),
        ]
    )

    # Jump straight back to inline input in any chat
    rows.append(
        [
            InlineKeyboardButton(
                text="Перейти к переводу в чате",
                switch_inline_query="",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _language_grid_keyboard(
    *,
    mode: str,
    active_code: str,
    page: int,
) -> InlineKeyboardMarkup:
    codes = QUICK_LANGUAGES_PRIMARY if page == 1 else QUICK_LANGUAGES_SECONDARY
    action = "set_tgt" if mode == "tgt" else "set_src"
    menu = "tgt_menu" if mode == "tgt" else "src_menu"

    rows: list[list[InlineKeyboardButton]] = []
    if mode == "src":
        auto_prefix = "• " if active_code == AUTO_LANG else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{auto_prefix}Автоопределение (auto)",
                    callback_data="cfg:set_src:auto",
                )
            ]
        )

    current_row: list[InlineKeyboardButton] = []
    for code in codes:
        info = LANGUAGE_BY_CODE[code]
        prefix = "• " if active_code == code else ""
        current_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{info.russian_name} ({code})",
                callback_data=f"cfg:{action}:{code}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    other_page = 2 if page == 1 else 1
    other_label = "Ещё языки ▸" if page == 1 else "◂ Основные языки"
    rows.append(
        [
            InlineKeyboardButton(text="◂ Назад", callback_data="cfg:main"),
            InlineKeyboardButton(text=other_label, callback_data=f"cfg:{menu}:{other_page}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_safely(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text=text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def cmd_start(
    message: Message,
    command: CommandObject,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    settings = await db.get_user_settings(message.from_user.id)
    if command.args and command.args.strip().lower() == "settings":
        await message.answer(
            _format_settings_text(settings, bot_username),
            reply_markup=_main_settings_keyboard(settings),
        )
        return

    text = (
        "mivta — переводчик для Telegram Inline Mode.\n\n"
        "Бот не переводит пересланные или личные сообщения здесь. "
        "Чтобы перевести текст, вызовите бота прямо в поле ввода любого чата:\n\n"
        f"@{bot_username} Привет, как дела?\n\n"
        "Выберите готовый перевод во всплывающем окне — и он отправится от вашего имени.\n\n"
        "Ниже можно настроить язык по умолчанию, исходный язык и стиль перевода."
    )
    await message.answer(text, reply_markup=_main_settings_keyboard(settings))


async def cmd_settings(
    message: Message,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    settings = await db.get_user_settings(message.from_user.id)
    await message.answer(
        _format_settings_text(settings, bot_username),
        reply_markup=_main_settings_keyboard(settings),
    )


async def cmd_lang(
    message: Message,
    command: CommandObject,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if not command.args or not command.args.strip():
        settings = await db.get_user_settings(user_id)
        await message.answer(
            "Выберите язык перевода по умолчанию (или укажите код: /lang en, /lang de):",
            reply_markup=_language_grid_keyboard(
                mode="tgt",
                active_code=settings.target_lang,
                page=1,
            ),
        )
        return

    resolved = resolve_language(command.args, allow_auto=False)
    if resolved is None:
        await message.answer(
            f"Неизвестный язык назначения: «{command.args.strip()}».\n"
            "Пример: /lang en, /lang de, /lang pl, /lang ja"
        )
        return

    settings = await db.update_user_settings(user_id, target_lang=resolved)
    await message.answer(
        _format_settings_text(settings, bot_username),
        reply_markup=_main_settings_keyboard(settings),
    )


async def cmd_source(
    message: Message,
    command: CommandObject,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if not command.args or not command.args.strip():
        settings = await db.get_user_settings(user_id)
        await message.answer(
            "Выберите исходный язык (или укажите /source auto либо /source ru):",
            reply_markup=_language_grid_keyboard(
                mode="src",
                active_code=settings.source_lang,
                page=1,
            ),
        )
        return

    resolved = resolve_language(command.args, allow_auto=True)
    if resolved is None:
        await message.answer(
            f"Неизвестный исходный язык: «{command.args.strip()}».\n"
            "Пример: /source auto, /source ru, /source en"
        )
        return

    settings = await db.update_user_settings(user_id, source_lang=resolved)
    await message.answer(
        _format_settings_text(settings, bot_username),
        reply_markup=_main_settings_keyboard(settings),
    )


async def cmd_style(
    message: Message,
    command: CommandObject,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if not command.args or not command.args.strip():
        settings = await db.get_user_settings(user_id)
        await message.answer(
            _format_settings_text(settings, bot_username),
            reply_markup=_main_settings_keyboard(settings),
        )
        return

    parsed = parse_style(command.args)
    if parsed is None:
        await message.answer(
            f"Неизвестный стиль: «{command.args.strip()}».\n"
            "Доступные варианты: /style neutral, /style casual, /style formal"
        )
        return

    settings = await db.update_user_settings(user_id, style=parsed)
    await message.answer(
        _format_settings_text(settings, bot_username),
        reply_markup=_main_settings_keyboard(settings),
    )


async def handle_settings_callback(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if callback.data is None:
        await callback.answer()
        return

    user_id = callback.from_user.id
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else "main"
    arg = parts[2] if len(parts) > 2 else ""

    if action == "main":
        settings = await db.get_user_settings(user_id)
        await _edit_safely(
            callback,
            _format_settings_text(settings, bot_username),
            _main_settings_keyboard(settings),
        )
        await callback.answer()
        return

    if action in ("tgt_menu", "src_menu"):
        page = 2 if arg == "2" else 1
        mode = "tgt" if action == "tgt_menu" else "src"
        settings = await db.get_user_settings(user_id)
        active = settings.target_lang if mode == "tgt" else settings.source_lang
        title = (
            "Выберите язык перевода по умолчанию:"
            if mode == "tgt"
            else "Выберите исходный язык (или «Автоопределение»):"
        )
        await _edit_safely(
            callback,
            title,
            _language_grid_keyboard(mode=mode, active_code=active, page=page),
        )
        await callback.answer()
        return

    if action == "set_tgt":
        resolved = resolve_language(arg, allow_auto=False)
        if resolved is None:
            await callback.answer("Неизвестный язык", show_alert=False)
            return
        settings = await db.update_user_settings(user_id, target_lang=resolved)
        await _edit_safely(
            callback,
            _format_settings_text(settings, bot_username),
            _main_settings_keyboard(settings),
        )
        await callback.answer(f"Язык перевода: {language_label(resolved)}")
        return

    if action == "set_src":
        resolved = resolve_language(arg, allow_auto=True)
        if resolved is None:
            await callback.answer("Неизвестный язык", show_alert=False)
            return
        settings = await db.update_user_settings(user_id, source_lang=resolved)
        await _edit_safely(
            callback,
            _format_settings_text(settings, bot_username),
            _main_settings_keyboard(settings),
        )
        await callback.answer(f"Исходный язык: {language_label(resolved)}")
        return

    if action == "set_style":
        style = parse_style(arg)
        if style is None:
            await callback.answer("Неизвестный стиль", show_alert=False)
            return
        settings = await db.update_user_settings(user_id, style=style)
        await _edit_safely(
            callback,
            _format_settings_text(settings, bot_username),
            _main_settings_keyboard(settings),
        )
        await callback.answer(f"Стиль: {STYLE_LABELS_RU[style]}")
        return

    await callback.answer()


async def reject_direct_messages(
    message: Message,
    bot_username: str = "BotUsername",
) -> None:
    """Explicitly decline DM/forwarded/voice translation — Inline Mode is the sole flow."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перевести в чате (Inline Mode)",
                    switch_inline_query="",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Настройки перевода",
                    callback_data="cfg:main",
                )
            ],
        ]
    )
    await message.answer(
        "mivta работает только в Inline Mode — пересылать сообщения "
        "или отправлять аудио не нужно.\n\n"
        f"Введите в поле ввода любого чата:\n@{bot_username} ваш текст\n\n"
        "Для изменения языка или стиля используйте /settings.",
        reply_markup=keyboard,
    )


def build_commands_router() -> Router:
    """Create a fresh Router bound to command and settings callback handlers."""
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_settings, Command("help", "settings"))
    router.message.register(cmd_lang, Command("lang"))
    router.message.register(cmd_source, Command("source"))
    router.message.register(cmd_style, Command("style"))
    router.callback_query.register(handle_settings_callback, F.data.startswith("cfg:"))
    router.message.register(reject_direct_messages, F.chat.type == "private")
    return router
