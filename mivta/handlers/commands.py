"""Command and settings callback handlers (localized EN / RU persistent preferences)."""

from __future__ import annotations

import contextlib
import re
import time
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from mivta import __version__
from mivta.config import MAX_INLINE_EXTRA_LANGS, Config
from mivta.database import Database, DatabaseStats, UserSettings
from mivta.service import (
    RestartController,
    ServiceMarker,
    UpdateInfo,
    UpdatePlan,
    apply_self_update,
    check_update,
    plan_update,
    run_detached,
    write_marker,
    write_update_trigger,
)
from mivta.translator.base import ProviderStatus, TranslationStyle, Translator, parse_style
from mivta.utils.cache import TranslationCache
from mivta.utils.i18n import (
    parse_ui_lang,
    resolve_ui_lang,
    style_label,
    suggest_swap_lang,
)
from mivta.utils.languages import (
    AUTO_LANG,
    QUICK_LANGUAGES_PRIMARY,
    QUICK_LANGUAGES_SECONDARY,
    language_label,
    language_name,
    resolve_language,
)
from mivta.utils.metrics import RuntimeMetrics

_CLEAR_TOKENS = frozenset({"off", "clear", "none", "reset", "выкл", "сброс", "-"})


def _ui_for(user: User, settings: UserSettings, config: Config) -> str:
    return resolve_ui_lang(
        settings.ui_lang,
        user.language_code,
        default_ui_lang=config.default_ui_lang,
    )


def _off_label(ui_lang: str) -> str:
    return "off" if ui_lang == "en" else "выкл"


def _format_extra_langs(extra_langs: tuple[str, ...], ui_lang: str) -> str:
    if not extra_langs:
        return _off_label(ui_lang)
    return ", ".join(code.upper() for code in extra_langs)


def _format_swap_lang(swap_lang: str, ui_lang: str) -> str:
    if not swap_lang:
        return _off_label(ui_lang)
    return language_label(swap_lang, ui_lang=ui_lang)


def _format_settings_text(
    settings: UserSettings,
    bot_username: str,
    ui_lang: str,
) -> str:
    src_label = language_label(settings.source_lang, ui_lang=ui_lang)
    tgt_label = language_label(settings.target_lang, ui_lang=ui_lang)
    swap_str = _format_swap_lang(settings.swap_lang, ui_lang)
    st_label = style_label(settings.style, ui_lang)
    extra_str = _format_extra_langs(settings.extra_langs, ui_lang)

    if ui_lang == "en":
        return (
            "mivta Translation Settings\n\n"
            f"• Source language: {src_label}\n"
            f"• Target language: {tgt_label}\n"
            f"• Auto-swap pair (⇄): {swap_str}\n"
            f"• Extra Inline cards: {extra_str}\n"
            f"• Style: {st_label}\n\n"
            "Inline Mode (any chat):\n"
            f"• @{bot_username} Hello, how are you?\n"
            f"• @{bot_username} >de text · en>pl text · !formal text\n\n"
            "Secretary Mode / Chat Automation (no 'via @bot' label):\n"
            "• > Hello, how are you? (messages & media captions)\n"
            "• >de text · en>pl !formal text"
        )

    return (
        "Настройки перевода mivta\n\n"
        f"• Исходный язык: {src_label}\n"
        f"• Язык перевода: {tgt_label}\n"
        f"• Авто-смена (⇄): {swap_str}\n"
        f"• Доп. языки Inline: {extra_str}\n"
        f"• Стиль: {st_label}\n\n"
        "Inline Mode (любой чат):\n"
        f"• @{bot_username} Привет, как дела?\n"
        f"• @{bot_username} >de текст · ru>en текст · !formal текст\n\n"
        "Secretary Mode / Автоматизация чатов (без via @bot):\n"
        "• > Привет, как дела? (текст и подписи к фото/файлам)\n"
        "• >de текст · ru>pl !formal текст"
    )


def _format_privacy_text(ui_lang: str) -> str:
    if ui_lang == "en":
        return (
            "mivta Privacy Policy\n\n"
            "1. Zero message logging: mivta never stores message text, inline queries, "
            "or translated strings on disk or in server logs.\n"
            "2. Minimal SQLite preferences: only numeric Telegram user_id / chat_id, chosen "
            "language/style codes, numeric group ignore-list IDs, and Chat Automation flags "
            "are saved.\n"
            "3. Transient RAM cache: identical translations are held only in memory by "
            "short BLAKE2b digest until TTL expiry and are never written to disk.\n"
            "4. Upstream translation: text you explicitly submit via @bot or '>' is sent "
            "to the configured translation API solely to return the translation.\n"
            "5. Data erasure: run /reset (or tap the button below) at any time to delete "
            "your stored preferences immediately."
        )
    return (
        "Политика конфиденциальности mivta\n\n"
        "1. Без хранения переписки: mivta никогда не записывает тексты сообщений, "
        "inline-запросы и переводы на диск или в логи сервера.\n"
        "2. Минимум метаданных в SQLite: хранятся только числовой user_id, выбранные "
        "коды языков/стиля и технический статус подключения Автоматизации чатов.\n"
        "3. Временный кэш в ОЗУ: кэш переводов хранится только в оперативной памяти по "
        "BLAKE2b-хэшу до истечения TTL и очищается фоновым процессом.\n"
        "4. Внешний провайдер перевода: текст, явно отправленный через @bot или «>», "
        "передаётся выбранному API переводчика исключительно для получения перевода.\n"
        "5. Удаление данных: команда /reset (или кнопка ниже) мгновенно удаляет ваши "
        "сохранённые настройки из базы."
    )


def _main_settings_keyboard(
    settings: UserSettings,
    *,
    ui_lang: str,
    telegram_lang_code: str | None = None,
) -> InlineKeyboardMarkup:
    is_en = ui_lang == "en"
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
        lbl = style_label(style, ui_lang).capitalize()
        style_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{lbl}",
                callback_data=f"cfg:set_style:{style.value}",
            )
        )
    rows.append(style_row)

    # Full target & source language pickers
    src_short = (
        ("Auto" if is_en else "Авто")
        if settings.source_lang == AUTO_LANG
        else settings.source_lang.upper()
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    f"Target ({settings.target_lang.upper()}) ▸"
                    if is_en
                    else f"Язык перевода ({settings.target_lang.upper()}) ▸"
                ),
                callback_data="cfg:tgt_menu:1",
            ),
            InlineKeyboardButton(
                text=(
                    f"Source ({src_short}) ▸"
                    if is_en
                    else f"Исходный ({src_short}) ▸"
                ),
                callback_data="cfg:src_menu:1",
            ),
        ]
    )

    # Bidirectional Auto-Swap + Extra Inline cards row
    swap_short = settings.swap_lang.upper() if settings.swap_lang else _off_label(ui_lang)
    extra_short = _format_extra_langs(settings.extra_langs, ui_lang)
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    f"⇄ Auto-swap ({swap_short}) ▸"
                    if is_en
                    else f"⇄ Авто-смена ({swap_short}) ▸"
                ),
                callback_data="cfg:swap_menu:1",
            ),
            InlineKeyboardButton(
                text=(
                    f"Extra cards ({extra_short}) ▸"
                    if is_en
                    else f"Доп. карточки ({extra_short}) ▸"
                ),
                callback_data="cfg:extra_menu:1",
            ),
        ]
    )

    # 1-tap smart auto-swap suggestion based on Telegram client locale when not yet set
    suggested_swap = suggest_swap_lang(telegram_lang_code, settings.target_lang)
    if not settings.swap_lang and suggested_swap:
        pair_label = f"{settings.target_lang.upper()} ⇄ {suggested_swap.upper()}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"Enable {pair_label} auto-swap"
                        if is_en
                        else f"Включить авто-смену {pair_label}"
                    ),
                    callback_data=f"cfg:set_swap:{suggested_swap}",
                )
            ]
        )

    # UI language toggle + Jump straight back to inline input in any chat
    next_ui = "ru" if is_en else "en"
    ui_btn_text = "🌐 Русский UI" if is_en else "🌐 English UI"
    rows.append(
        [
            InlineKeyboardButton(
                text=ui_btn_text,
                callback_data=f"cfg:set_ui:{next_ui}",
            ),
            InlineKeyboardButton(
                text=(
                    "Translate in chat ▸"
                    if is_en
                    else "Перевести в чате ▸"
                ),
                switch_inline_query="",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _language_grid_keyboard(
    *,
    mode: str,
    active_code: str,
    page: int,
    ui_lang: str,
) -> InlineKeyboardMarkup:
    is_en = ui_lang == "en"
    codes = QUICK_LANGUAGES_PRIMARY if page == 1 else QUICK_LANGUAGES_SECONDARY
    action_map = {"tgt": "set_tgt", "src": "set_src", "swap": "set_swap"}
    menu_map = {"tgt": "tgt_menu", "src": "src_menu", "swap": "swap_menu"}
    action = action_map[mode]
    menu = menu_map[mode]

    rows: list[list[InlineKeyboardButton]] = []
    if mode == "src":
        auto_prefix = "• " if active_code == AUTO_LANG else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{auto_prefix}Auto-detect (auto)"
                        if is_en
                        else f"{auto_prefix}Автоопределение (auto)"
                    ),
                    callback_data="cfg:set_src:auto",
                )
            ]
        )
    elif mode == "swap" and active_code:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "✕ Disable auto-swap"
                        if is_en
                        else "✕ Выключить авто-смену"
                    ),
                    callback_data="cfg:set_swap:off",
                )
            ]
        )

    current_row: list[InlineKeyboardButton] = []
    for code in codes:
        prefix = "• " if active_code == code else ""
        disp = language_name(code, ui_lang=ui_lang)
        current_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{disp} ({code})",
                callback_data=f"cfg:{action}:{code}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    other_page = 2 if page == 1 else 1
    if is_en:
        other_label = "More languages ▸" if page == 1 else "◂ Main languages"
        back_label = "◂ Back"
    else:
        other_label = "Ещё языки ▸" if page == 1 else "◂ Основные языки"
        back_label = "◂ Назад"
    rows.append(
        [
            InlineKeyboardButton(text=back_label, callback_data="cfg:main"),
            InlineKeyboardButton(text=other_label, callback_data=f"cfg:{menu}:{other_page}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _extra_languages_keyboard(
    *,
    extra_langs: tuple[str, ...],
    page: int,
    ui_lang: str,
) -> InlineKeyboardMarkup:
    is_en = ui_lang == "en"
    codes = QUICK_LANGUAGES_PRIMARY if page == 1 else QUICK_LANGUAGES_SECONDARY
    rows: list[list[InlineKeyboardButton]] = []

    if extra_langs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "✕ Clear extra languages"
                        if is_en
                        else "✕ Сбросить доп. языки (оставить один)"
                    ),
                    callback_data=f"cfg:clear_extra:{page}",
                )
            ]
        )

    current_row: list[InlineKeyboardButton] = []
    for code in codes:
        prefix = "✓ " if code in extra_langs else ""
        disp = language_name(code, ui_lang=ui_lang)
        current_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{disp} ({code})",
                callback_data=f"cfg:toggle_extra:{code}:{page}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    other_page = 2 if page == 1 else 1
    if is_en:
        other_label = "More languages ▸" if page == 1 else "◂ Main languages"
        done_label = "◂ Done"
    else:
        other_label = "Ещё языки ▸" if page == 1 else "◂ Основные языки"
        done_label = "◂ Готово"
    rows.append(
        [
            InlineKeyboardButton(text=done_label, callback_data="cfg:main"),
            InlineKeyboardButton(
                text=other_label,
                callback_data=f"cfg:extra_menu:{other_page}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _extra_menu_title(settings: UserSettings, ui_lang: str) -> str:
    extra_str = _format_extra_langs(settings.extra_langs, ui_lang)
    if ui_lang == "en":
        return (
            f"Extra Inline Mode card languages (up to {MAX_INLINE_EXTRA_LANGS}):\n"
            f"Primary target: {settings.target_lang.upper()} · Extra: {extra_str}"
        )
    return (
        f"Дополнительные языки для карточек Inline Mode (до {MAX_INLINE_EXTRA_LANGS}):\n"
        f"Основной язык: {settings.target_lang.upper()} · Доп.: {extra_str}"
    )


def _swap_menu_title(settings: UserSettings, ui_lang: str) -> str:
    swap_str = _format_swap_lang(settings.swap_lang, ui_lang)
    if ui_lang == "en":
        return (
            "Bidirectional Auto-Swap (⇄):\n"
            f"When input is already in {settings.target_lang.upper()}, automatically "
            f"translate into the paired language instead.\n"
            f"Current: {swap_str}"
        )
    return (
        "Двусторонняя авто-смена языка (⇄):\n"
        f"Если исходный текст уже на {settings.target_lang.upper()}, бот автоматически "
        "переведёт его на выбранный парный язык.\n"
        f"Сейчас: {swap_str}"
    )


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
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    settings = await db.get_user_settings(message.from_user.id)
    ui = _ui_for(message.from_user, settings, config)
    kb = _main_settings_keyboard(
        settings,
        ui_lang=ui,
        telegram_lang_code=message.from_user.language_code,
    )
    if command.args and command.args.strip().lower() == "settings":
        await message.answer(_format_settings_text(settings, bot_username, ui), reply_markup=kb)
        return

    if ui == "en":
        text = (
            "mivta — Telegram Inline & Secretary Mode translator.\n\n"
            "Direct or forwarded messages here are not translated. "
            "Invoke the bot straight from the input field of any chat:\n\n"
            f"@{bot_username} Hello, how are you?\n\n"
            "Tap a translation card in the popup to send it under your own name.\n\n"
            "Configure your target language, ⇄ auto-swap pair, extra cards, and style below."
        )
    else:
        text = (
            "mivta — переводчик для Telegram Inline & Secretary Mode.\n\n"
            "Бот не переводит пересланные или личные сообщения здесь. "
            "Чтобы перевести текст, вызовите бота прямо в поле ввода любого чата:\n\n"
            f"@{bot_username} Привет, как дела?\n\n"
            "Выберите готовый перевод во всплывающем окне — и он отправится от вашего имени.\n\n"
            "Ниже можно настроить язык по умолчанию, ⇄ авто-смену, доп. карточки и стиль."
        )
    await message.answer(text, reply_markup=kb)


async def cmd_settings(
    message: Message,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    settings = await db.get_user_settings(message.from_user.id)
    ui = _ui_for(message.from_user, settings, config)
    await message.answer(
        _format_settings_text(settings, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            settings,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_lang(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"

    if not command.args or not command.args.strip():
        await message.answer(
            (
                "Choose your default target language (or pass a code: /lang en, /lang de):"
                if is_en
                else "Выберите язык перевода по умолчанию (или укажите код: /lang en, /lang de):"
            ),
            reply_markup=_language_grid_keyboard(
                mode="tgt",
                active_code=settings.target_lang,
                page=1,
                ui_lang=ui,
            ),
        )
        return

    resolved = resolve_language(command.args, allow_auto=False)
    if resolved is None:
        raw = command.args.strip()
        await message.answer(
            f"Unknown target language: '{raw}'.\nExample: /lang en, /lang de, /lang pl, /lang ja"
            if is_en
            else f"Неизвестный язык назначения: «{raw}».\nПример: /lang en, /lang de, /lang pl"
        )
        return

    updated = await db.update_user_settings(user_id, target_lang=resolved)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_swap(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    """Configure bidirectional auto-swap language (`/swap ru`, `/swap pl`, `/swap off`)."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"
    raw_args = (command.args or "").strip()

    if not raw_args:
        await message.answer(
            _swap_menu_title(settings, ui),
            reply_markup=_language_grid_keyboard(
                mode="swap",
                active_code=settings.swap_lang,
                page=1,
                ui_lang=ui,
            ),
        )
        return

    if raw_args.lower() in _CLEAR_TOKENS:
        updated = await db.update_user_settings(user_id, swap_lang="")
        await message.answer(
            _format_settings_text(updated, bot_username, ui),
            reply_markup=_main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=message.from_user.language_code,
            ),
        )
        return

    resolved = resolve_language(raw_args, allow_auto=False)
    if resolved is None:
        await message.answer(
            (
                f"Unknown language code: '{raw_args}'.\n"
                "Example: /swap pl, /swap ru · disable: /swap off"
            )
            if is_en
            else (
                f"Неизвестный код языка: «{raw_args}».\n"
                "Пример: /swap ru, /swap pl · выкл: /swap off"
            )
        )
        return

    updated = await db.update_user_settings(user_id, swap_lang=resolved)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_langs(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    """Configure 1-3 extra inline card target languages (`/langs pl de` or `/langs off`)."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"
    raw_args = (command.args or "").strip()

    if not raw_args:
        await message.answer(
            _extra_menu_title(settings, ui),
            reply_markup=_extra_languages_keyboard(
                extra_langs=settings.extra_langs,
                page=1,
                ui_lang=ui,
            ),
        )
        return

    if raw_args.lower() in _CLEAR_TOKENS:
        updated = await db.update_user_settings(user_id, extra_langs=())
        await message.answer(
            _format_settings_text(updated, bot_username, ui),
            reply_markup=_main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=message.from_user.language_code,
            ),
        )
        return

    tokens = [tok for tok in re.split(r"[\s,;]+", raw_args) if tok]
    resolved_codes: list[str] = []
    for tok in tokens:
        code = resolve_language(tok, allow_auto=False)
        if code is None:
            await message.answer(
                (
                    f"Unknown language code: '{tok}'.\n"
                    "Example: /langs pl de · disable: /langs off"
                )
                if is_en
                else (
                    f"Неизвестный код языка: «{tok}».\n"
                    "Пример: /langs pl de · отключить: /langs off"
                )
            )
            return
        if code not in resolved_codes:
            resolved_codes.append(code)

    if len(resolved_codes) > MAX_INLINE_EXTRA_LANGS:
        await message.answer(
            (
                f"You can select at most {MAX_INLINE_EXTRA_LANGS} extra languages.\n"
                "Example: /langs pl de"
            )
            if is_en
            else f"Можно выбрать не более {MAX_INLINE_EXTRA_LANGS} дополнительных языков.\n"
            "Пример: /langs pl de"
        )
        return

    updated = await db.update_user_settings(user_id, extra_langs=resolved_codes)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_source(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"

    if not command.args or not command.args.strip():
        await message.answer(
            (
                "Choose source language (or pass /source auto or /source en):"
                if is_en
                else "Выберите исходный язык (или укажите /source auto либо /source ru):"
            ),
            reply_markup=_language_grid_keyboard(
                mode="src",
                active_code=settings.source_lang,
                page=1,
                ui_lang=ui,
            ),
        )
        return

    resolved = resolve_language(command.args, allow_auto=True)
    if resolved is None:
        raw = command.args.strip()
        await message.answer(
            f"Unknown source language: '{raw}'.\nExample: /source auto, /source en, /source ru"
            if is_en
            else (
                f"Неизвестный исходный язык: «{raw}».\n"
                "Пример: /source auto, /source ru, /source en"
            )
        )
        return

    updated = await db.update_user_settings(user_id, source_lang=resolved)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_style(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"

    if not command.args or not command.args.strip():
        await message.answer(
            _format_settings_text(settings, bot_username, ui),
            reply_markup=_main_settings_keyboard(
                settings,
                ui_lang=ui,
                telegram_lang_code=message.from_user.language_code,
            ),
        )
        return

    parsed = parse_style(command.args)
    if parsed is None:
        raw = command.args.strip()
        await message.answer(
            (
                f"Unknown style: '{raw}'.\n"
                "Available: /style neutral, /style casual, /style formal"
            )
            if is_en
            else f"Неизвестный стиль: «{raw}».\n"
            "Доступные варианты: /style neutral, /style casual, /style formal"
        )
        return

    updated = await db.update_user_settings(user_id, style=parsed)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_ui(
    message: Message,
    command: CommandObject,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    """Switch bot interface language (`/ui en`, `/ui ru`, or `/ui auto`)."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    settings = await db.get_user_settings(user_id)
    current_ui = _ui_for(message.from_user, settings, config)
    raw_args = (command.args or "").strip()

    if not raw_args:
        toggled = "ru" if current_ui == "en" else "en"
        updated = await db.update_user_settings(user_id, ui_lang=toggled)
        ui = _ui_for(message.from_user, updated, config)
        await message.answer(
            _format_settings_text(updated, bot_username, ui),
            reply_markup=_main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=message.from_user.language_code,
            ),
        )
        return

    parsed = parse_ui_lang(raw_args)
    if parsed is None:
        await message.answer(
            "Usage: /ui en, /ui ru, or /ui auto"
            if current_ui == "en"
            else "Использование: /ui en, /ui ru или /ui auto"
        )
        return

    updated = await db.update_user_settings(user_id, ui_lang=parsed)
    ui = _ui_for(message.from_user, updated, config)
    await message.answer(
        _format_settings_text(updated, bot_username, ui),
        reply_markup=_main_settings_keyboard(
            updated,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def cmd_privacy(
    message: Message,
    config: Config,
    db: Database,
) -> None:
    """Display transparent privacy policy and self-service erasure button."""
    if message.from_user is None:
        return
    settings = await db.get_user_settings(message.from_user.id)
    ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "Reset my saved settings"
                        if is_en
                        else "Удалить мои настройки (/reset)"
                    ),
                    callback_data="cfg:reset",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◂ Settings" if is_en else "◂ Настройки",
                    callback_data="cfg:main",
                )
            ],
        ]
    )
    await message.answer(_format_privacy_text(ui), reply_markup=keyboard)


async def cmd_reset(
    message: Message,
    config: Config,
    db: Database,
) -> None:
    """Delete caller's stored preferences from SQLite immediately."""
    if message.from_user is None:
        return
    prev_settings = await db.get_user_settings(message.from_user.id)
    ui = _ui_for(message.from_user, prev_settings, config)
    await db.delete_user_data(message.from_user.id)
    fresh = await db.get_user_settings(message.from_user.id)
    await message.answer(
        (
            "Your saved preferences have been deleted and reset to defaults."
            if ui == "en"
            else "Ваши сохранённые настройки удалены из базы и сброшены к значениям по умолчанию."
        ),
        reply_markup=_main_settings_keyboard(
            fresh,
            ui_lang=ui,
            telegram_lang_code=message.from_user.language_code,
        ),
    )


async def handle_settings_callback(
    callback: CallbackQuery,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    if callback.data is None:
        await callback.answer()
        return

    user = callback.from_user
    user_id = user.id
    settings = await db.get_user_settings(user_id)
    ui = _ui_for(user, settings, config)
    is_en = ui == "en"

    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else "main"
    arg = parts[2] if len(parts) > 2 else ""

    if action == "main":
        await _edit_safely(
            callback,
            _format_settings_text(settings, bot_username, ui),
            _main_settings_keyboard(
                settings,
                ui_lang=ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer()
        return

    if action in ("tgt_menu", "src_menu", "swap_menu"):
        page = 2 if arg == "2" else 1
        if action == "tgt_menu":
            mode = "tgt"
            active = settings.target_lang
            title = (
                "Choose default target language:"
                if is_en
                else "Выберите язык перевода по умолчанию:"
            )
        elif action == "src_menu":
            mode = "src"
            active = settings.source_lang
            title = (
                "Choose source language (or Auto-detect):"
                if is_en
                else "Выберите исходный язык (или «Автоопределение»):"
            )
        else:
            mode = "swap"
            active = settings.swap_lang
            title = _swap_menu_title(settings, ui)

        await _edit_safely(
            callback,
            title,
            _language_grid_keyboard(
                mode=mode,
                active_code=active,
                page=page,
                ui_lang=ui,
            ),
        )
        await callback.answer()
        return

    if action == "extra_menu":
        page = 2 if arg == "2" else 1
        await _edit_safely(
            callback,
            _extra_menu_title(settings, ui),
            _extra_languages_keyboard(
                extra_langs=settings.extra_langs,
                page=page,
                ui_lang=ui,
            ),
        )
        await callback.answer()
        return

    if action == "toggle_extra":
        page = 2 if (len(parts) > 3 and parts[3] == "2") else 1
        resolved = resolve_language(arg, allow_auto=False)
        if resolved is None:
            await callback.answer("Unknown language" if is_en else "Неизвестный язык")
            return
        current = list(settings.extra_langs)
        lbl = language_label(resolved, ui_lang=ui)
        if resolved in current:
            current.remove(resolved)
            note = f"Removed: {lbl}" if is_en else f"Убран: {lbl}"
        else:
            if len(current) >= MAX_INLINE_EXTRA_LANGS:
                await callback.answer(
                    f"Max {MAX_INLINE_EXTRA_LANGS} extra languages"
                    if is_en
                    else f"Максимум {MAX_INLINE_EXTRA_LANGS} доп. языка"
                )
                return
            current.append(resolved)
            note = f"Added: {lbl}" if is_en else f"Добавлен: {lbl}"
        updated = await db.update_user_settings(user_id, extra_langs=current)
        await _edit_safely(
            callback,
            _extra_menu_title(updated, ui),
            _extra_languages_keyboard(
                extra_langs=updated.extra_langs,
                page=page,
                ui_lang=ui,
            ),
        )
        await callback.answer(note)
        return

    if action == "clear_extra":
        page = 2 if arg == "2" else 1
        updated = await db.update_user_settings(user_id, extra_langs=())
        await _edit_safely(
            callback,
            _extra_menu_title(updated, ui),
            _extra_languages_keyboard(
                extra_langs=updated.extra_langs,
                page=page,
                ui_lang=ui,
            ),
        )
        await callback.answer(
            "Extra languages cleared" if is_en else "Дополнительные языки сброшены"
        )
        return

    if action == "set_tgt":
        resolved = resolve_language(arg, allow_auto=False)
        if resolved is None:
            await callback.answer("Unknown language" if is_en else "Неизвестный язык")
            return
        updated = await db.update_user_settings(user_id, target_lang=resolved)
        lbl = language_label(resolved, ui_lang=ui)
        await _edit_safely(
            callback,
            _format_settings_text(updated, bot_username, ui),
            _main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer(f"Target: {lbl}" if is_en else f"Язык перевода: {lbl}")
        return

    if action == "set_src":
        resolved = resolve_language(arg, allow_auto=True)
        if resolved is None:
            await callback.answer("Unknown language" if is_en else "Неизвестный язык")
            return
        updated = await db.update_user_settings(user_id, source_lang=resolved)
        lbl = language_label(resolved, ui_lang=ui)
        await _edit_safely(
            callback,
            _format_settings_text(updated, bot_username, ui),
            _main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer(f"Source: {lbl}" if is_en else f"Исходный язык: {lbl}")
        return

    if action == "set_swap":
        if arg.lower() in _CLEAR_TOKENS:
            updated = await db.update_user_settings(user_id, swap_lang="")
            toast = "Auto-swap disabled" if is_en else "Авто-смена выключена"
        else:
            resolved = resolve_language(arg, allow_auto=False)
            if resolved is None:
                await callback.answer("Unknown language" if is_en else "Неизвестный язык")
                return
            updated = await db.update_user_settings(user_id, swap_lang=resolved)
            lbl = language_label(resolved, ui_lang=ui)
            toast = f"Auto-swap: {lbl}" if is_en else f"Авто-смена: {lbl}"
        await _edit_safely(
            callback,
            _format_settings_text(updated, bot_username, ui),
            _main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer(toast)
        return

    if action == "set_style":
        style = parse_style(arg)
        if style is None:
            await callback.answer("Unknown style" if is_en else "Неизвестный стиль")
            return
        updated = await db.update_user_settings(user_id, style=style)
        st_lbl = style_label(style, ui)
        await _edit_safely(
            callback,
            _format_settings_text(updated, bot_username, ui),
            _main_settings_keyboard(
                updated,
                ui_lang=ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer(f"Style: {st_lbl}" if is_en else f"Стиль: {st_lbl}")
        return

    if action == "set_ui":
        parsed_ui = parse_ui_lang(arg)
        if parsed_ui is None:
            await callback.answer()
            return
        updated = await db.update_user_settings(user_id, ui_lang=parsed_ui)
        new_ui = _ui_for(user, updated, config)
        await _edit_safely(
            callback,
            _format_settings_text(updated, bot_username, new_ui),
            _main_settings_keyboard(
                updated,
                ui_lang=new_ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer("Language: English" if new_ui == "en" else "Язык интерфейса: Русский")
        return

    if action == "reset":
        await db.delete_user_data(user_id)
        fresh = await db.get_user_settings(user_id)
        new_ui = _ui_for(user, fresh, config)
        await _edit_safely(
            callback,
            _format_settings_text(fresh, bot_username, new_ui),
            _main_settings_keyboard(
                fresh,
                ui_lang=new_ui,
                telegram_lang_code=user.language_code,
            ),
        )
        await callback.answer(
            "Settings reset to defaults" if is_en else "Настройки сброшены"
        )
        return

    await callback.answer()


async def reject_direct_messages(
    message: Message,
    config: Config,
    db: Database,
    bot_username: str = "BotUsername",
) -> None:
    """Explicitly decline DM/forwarded/voice translation — Inline & Secretary are the sole flows."""
    ui = config.default_ui_lang
    if message.from_user is not None:
        settings = await db.get_user_settings(message.from_user.id)
        ui = _ui_for(message.from_user, settings, config)
    is_en = ui == "en"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "Translate in chat (Inline Mode)"
                        if is_en
                        else "Перевести в чате (Inline Mode)"
                    ),
                    switch_inline_query="",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Translation settings" if is_en else "Настройки перевода",
                    callback_data="cfg:main",
                )
            ],
        ]
    )
    await message.answer(
        (
            "mivta works in Inline Mode — no need to forward messages or send audio here.\n\n"
            f"Type in the input field of any chat:\n@{bot_username} your text\n\n"
            "Use /settings to change languages or style."
            if is_en
            else "mivta работает только в Inline Mode — пересылать сообщения "
            "или отправлять аудио не нужно.\n\n"
            f"Введите в поле ввода любого чата:\n@{bot_username} ваш текст\n\n"
            "Для изменения языка или стиля используйте /settings."
        ),
        reply_markup=keyboard,
    )


def _format_uptime(seconds: float, lang: str) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if lang == "en":
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        if minutes or hours or days:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        return " ".join(parts)
    parts_ru: list[str] = []
    if days:
        parts_ru.append(f"{days}д")
    if hours or days:
        parts_ru.append(f"{hours}ч")
    if minutes or hours or days:
        parts_ru.append(f"{minutes}м")
    parts_ru.append(f"{secs}с")
    return " ".join(parts_ru)


def _select_active_provider_index(statuses: tuple[ProviderStatus, ...]) -> int:
    for idx, st in enumerate(statuses):
        if st.cooldown_remaining <= 0.0:
            return idx
    if not statuses:
        return -1
    return min(range(len(statuses)), key=lambda idx: statuses[idx].cooldown_remaining)


def _build_owner_status_keyboard(
    lang: str,
    *,
    has_cooldowns: bool,
    cache_enabled: bool,
) -> InlineKeyboardMarkup:
    refresh_lbl = "🔄 Refresh" if lang == "en" else "🔄 Обновить"
    reset_cd_lbl = "⚡️ Reset cooldowns" if lang == "en" else "⚡️ Сбросить cooldown"
    clear_cache_lbl = "🧹 Clear cache" if lang == "en" else "🧹 Очистить кэш"

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=refresh_lbl, callback_data="ost:refresh")],
    ]
    action_row: list[InlineKeyboardButton] = []
    if has_cooldowns:
        action_row.append(InlineKeyboardButton(text=reset_cd_lbl, callback_data="ost:reset_cd"))
    if cache_enabled:
        action_row.append(
            InlineKeyboardButton(text=clear_cache_lbl, callback_data="ost:clear_cache")
        )
    if action_row:
        rows.append(action_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_owner_status_html(
    *,
    lang: str,
    bot_username: str,
    config: Config,
    statuses: tuple[ProviderStatus, ...],
    db_stats: DatabaseStats,
    cache: TranslationCache | None,
    metrics: RuntimeMetrics | None,
    inflight_count: int,
) -> str:
    handle = escape(f"@{bot_username.lstrip('@') or 'mivtabot'}")
    mode = "webhook" if config.webhook_url else "polling"
    uptime_str = _format_uptime(metrics.uptime_seconds if metrics is not None else 0.0, lang)
    session_users = metrics.session_unique_users if metrics is not None else 0
    total_updates = metrics.total_updates if metrics is not None else 0

    active_idx = _select_active_provider_index(statuses)
    active_name = escape(statuses[active_idx].name) if active_idx >= 0 else "—"
    show_counts = len(statuses) > 1 or any(
        (st.success_count > 0 or st.failure_count > 0) for st in statuses
    )

    provider_lines: list[str] = []
    for idx, st in enumerate(statuses):
        p_name = escape(st.name)
        if st.cooldown_remaining > 0.0:
            cd_secs = max(1, int(round(st.cooldown_remaining)))
            err_suffix = f", {escape(st.last_error_type)}" if st.last_error_type else ""
            state_txt = (
                f"⏳ cooldown {cd_secs}s{err_suffix}"
                if lang == "en"
                else f"⏳ cooldown {cd_secs}с{err_suffix}"
            )
        elif idx == active_idx:
            state_txt = "🟢 active" if lang == "en" else "🟢 активен"
        else:
            state_txt = "⚪️ standby" if lang == "en" else "⚪️ в резерве"

        counts_txt = (
            f" · ok: {st.success_count} / err: {st.failure_count}" if show_counts else ""
        )
        provider_lines.append(f"  {idx + 1}. <code>{p_name}</code> — {state_txt}{counts_txt}")

    if cache is not None and cache.enabled:
        fill = len(cache)
        cap = cache.max_size
        pct = int(round((fill / cap) * 100)) if cap > 0 else 0
        hits = cache.hits
        misses = cache.misses
        total_lookups = hits + misses
        hit_rate = int(round((hits / total_lookups) * 100)) if total_lookups > 0 else 0
        if lang == "en":
            cache_line = (
                f"• Fill: <b>{fill} / {cap}</b> ({pct}%) · TTL {int(cache.ttl_seconds)}s\n"
                f"• Hits: <b>{hits}</b> hit / <b>{misses}</b> miss ({hit_rate}%)"
            )
        else:
            cache_line = (
                f"• Заполнено: <b>{fill} / {cap}</b> ({pct}%) · TTL {int(cache.ttl_seconds)}с\n"
                f"• Попадания: <b>{hits}</b> hit / <b>{misses}</b> miss ({hit_rate}%)"
            )
    else:
        cache_line = "• Disabled" if lang == "en" else "• Отключён"

    providers_block = "\n".join(provider_lines) or "  —"
    if lang == "en":
        return (
            f"📊 <b>Status {handle}</b> · <code>v{escape(__version__)}</code>\n"
            f"Mode: <code>{mode}</code> · Uptime: <b>{uptime_str}</b>\n\n"
            f"🔌 <b>Translation chain</b> (now serving: <code>{active_name}</code>)\n"
            f"{providers_block}\n\n"
            "👥 <b>Users &amp; chats</b>\n"
            f"• Active users (session): <b>{session_users}</b>\n"
            f"• Saved user profiles (DB): <b>{db_stats.saved_users}</b>\n"
            f"• Secretary / Business DMs: <b>{db_stats.business_active}</b> active "
            f"({db_stats.business_total} total)\n"
            f"• Groups ('&gt;' mode): <b>{db_stats.groups_active}</b> active "
            f"({db_stats.groups_total} total, "
            f"{db_stats.ignored_users_total} ignored)\n"
            f"• Updates processed (session): <b>{total_updates}</b>\n\n"
            "🧠 <b>RAM cache &amp; concurrency</b>\n"
            f"{cache_line}\n"
            f"• In-flight translations: <b>{inflight_count} / "
            f"{config.max_concurrent_translations}</b>"
        )

    return (
        f"📊 <b>Статус {handle}</b> · <code>v{escape(__version__)}</code>\n"
        f"Режим: <code>{mode}</code> · Аптайм: <b>{uptime_str}</b>\n\n"
        f"🔌 <b>Цепочка перевода</b> (активен сейчас: <code>{active_name}</code>)\n"
        f"{providers_block}\n\n"
        "👥 <b>Аудитория и чаты</b>\n"
        f"• Уникальных юзеров за сессию: <b>{session_users}</b>\n"
        f"• Профилей в БД: <b>{db_stats.saved_users}</b>\n"
        f"• Личка (Secretary / Business): <b>{db_stats.business_active}</b> активн. "
        f"(всего {db_stats.business_total})\n"
        f"• Группы (режим «&gt;»): <b>{db_stats.groups_active}</b> активн. "
        f"(всего {db_stats.groups_total}, игнор: {db_stats.ignored_users_total})\n"
        f"• Апдейтов за сессию: <b>{total_updates}</b>\n\n"
        "🧠 <b>RAM-кэш и нагрузка</b>\n"
        f"{cache_line}\n"
        f"• Переводов в полёте: <b>{inflight_count} / "
        f"{config.max_concurrent_translations}</b>"
    )


def _owner_setup_hint_html(lang: str, uid: int, command: str) -> str:
    """Tell an unconfigured caller their own Telegram ID (and nothing else)."""
    if lang == "en":
        return (
            f"🔒 <b>{escape(command)} is not configured yet.</b>\n\n"
            f"Your numeric Telegram ID: <code>{uid}</code>\n"
            f"Add <code>OWNER_ID={uid}</code> to <code>.env</code> "
            "(or <code>/etc/mivta/mivta.env</code>) and restart the service."
        )
    return (
        f"🔒 <b>Команда {escape(command)} ещё не настроена.</b>\n\n"
        f"Ваш числовой Telegram ID: <code>{uid}</code>\n"
        f"Добавьте <code>OWNER_ID={uid}</code> в <code>.env</code> "
        "(или <code>/etc/mivta/mivta.env</code>) и перезапустите сервис."
    )


def _owner_denied_text(lang: str) -> str:
    return (
        "Access denied: this command is available only to the bot owner."
        if lang == "en"
        else "Доступ запрещён: команда доступна только владельцу бота."
    )


async def cmd_status(
    message: Message,
    db: Database,
    config: Config,
    translator: Translator,
    cache: TranslationCache | None = None,
    metrics: RuntimeMetrics | None = None,
    inflight: dict[bytes, Any] | None = None,
    bot_username: str = "BotUsername",
) -> None:
    """Owner-only `/status` dashboard in private chat."""
    if message.from_user is None:
        return
    uid = message.from_user.id
    settings = await db.get_user_settings(uid)
    lang = _ui_for(message.from_user, settings, config)

    if not config.owner_ids:
        await message.answer(
            _owner_setup_hint_html(lang, uid, "/status"),
            parse_mode=ParseMode.HTML,
        )
        return

    if uid not in config.owner_ids:
        await message.answer(_owner_denied_text(lang))
        return

    statuses = translator.get_provider_statuses()
    db_stats = await db.get_stats()
    has_cd = any(st.cooldown_remaining > 0.0 for st in statuses)
    text = _render_owner_status_html(
        lang=lang,
        bot_username=bot_username,
        config=config,
        statuses=statuses,
        db_stats=db_stats,
        cache=cache,
        metrics=metrics,
        inflight_count=len(inflight) if inflight is not None else 0,
    )
    kb = _build_owner_status_keyboard(
        lang,
        has_cooldowns=has_cd,
        cache_enabled=bool(cache is not None and cache.enabled),
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_owner_status_callback(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    translator: Translator,
    cache: TranslationCache | None = None,
    metrics: RuntimeMetrics | None = None,
    inflight: dict[bytes, Any] | None = None,
    bot_username: str = "BotUsername",
) -> None:
    """Handle owner `/status` inline actions (`ost:refresh`, `ost:reset_cd`, `ost:clear_cache`)."""
    uid = callback.from_user.id
    settings = await db.get_user_settings(uid)
    lang = _ui_for(callback.from_user, settings, config)

    if not config.owner_ids or uid not in config.owner_ids:
        await callback.answer(
            "Owner only." if lang == "en" else "Доступно только владельцу бота.",
            show_alert=True,
        )
        return

    action = (callback.data or "").partition(":")[2]
    toast = "Updated" if lang == "en" else "Обновлено"
    if action == "reset_cd":
        translator.reset_cooldowns()
        toast = "Cooldowns reset" if lang == "en" else "Cooldown сброшен"
    elif action == "clear_cache":
        if cache is not None:
            cache.clear()
        toast = "RAM cache cleared" if lang == "en" else "RAM-кэш очищен"

    if isinstance(callback.message, Message):
        statuses = translator.get_provider_statuses()
        db_stats = await db.get_stats()
        has_cd = any(st.cooldown_remaining > 0.0 for st in statuses)
        text = _render_owner_status_html(
            lang=lang,
            bot_username=bot_username,
            config=config,
            statuses=statuses,
            db_stats=db_stats,
            cache=cache,
            metrics=metrics,
            inflight_count=len(inflight) if inflight is not None else 0,
        )
        kb = _build_owner_status_keyboard(
            lang,
            has_cooldowns=has_cd,
            cache_enabled=bool(cache is not None and cache.enabled),
        )
        try:
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    await callback.answer(toast)


async def _edit_service_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit an owner service message, tolerating Telegram's "not modified" error."""
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _build_service_keyboard(
    lang: str,
    *,
    update_available: bool,
    confirm_restart: bool = False,
) -> InlineKeyboardMarkup:
    if lang == "en":
        update_lbl = "⬆️ Update & restart"
        restart_lbl = "🔄 Restart"
        recheck_lbl = "🔎 Check again"
        confirm_lbl = "✅ Yes, restart"
        cancel_lbl = "✖️ Close"
    else:
        update_lbl = "⬆️ Обновить и перезапустить"
        restart_lbl = "🔄 Перезапустить"
        recheck_lbl = "🔎 Проверить снова"
        confirm_lbl = "✅ Да, перезапустить"
        cancel_lbl = "✖️ Закрыть"

    rows: list[list[InlineKeyboardButton]] = []
    if confirm_restart:
        rows.append([InlineKeyboardButton(text=confirm_lbl, callback_data="svc:restart")])
    else:
        if update_available:
            rows.append([InlineKeyboardButton(text=update_lbl, callback_data="svc:update")])
        rows.append(
            [
                InlineKeyboardButton(text=restart_lbl, callback_data="svc:restart"),
                InlineKeyboardButton(text=recheck_lbl, callback_data="svc:check"),
            ]
        )
    rows.append([InlineKeyboardButton(text=cancel_lbl, callback_data="svc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_update_html(lang: str, info: UpdateInfo, config: Config) -> str:
    """Human-readable result of an update check (EN / RU)."""
    en = lang == "en"
    if info.error is not None and info.source == "release":
        return (
            f"⚠️ <b>Could not check for updates.</b>\n"
            f"<code>{escape(info.error[:200])}</code>\n\n"
            "Update manually on the server:\n"
            "<code>sudo /opt/mivta/install.sh --self-update</code>"
            if en
            else f"⚠️ <b>Не удалось проверить обновления.</b>\n"
            f"<code>{escape(info.error[:200])}</code>\n\n"
            "Обновите вручную на сервере:\n"
            "<code>sudo /opt/mivta/install.sh --self-update</code>"
        )
    if info.error is not None:
        return (
            f"⚠️ <b>Could not reach the update source.</b>\n"
            f"<code>{escape(info.error[:200])}</code>\n\n"
            f"Installed: <code>v{escape(__version__)}</code>"
            + (f" · rev <code>{escape(info.current_rev)}</code>" if info.current_rev else "")
            if en
            else f"⚠️ <b>Не удалось получить список обновлений.</b>\n"
            f"<code>{escape(info.error[:200])}</code>\n\n"
            f"Установлено: <code>v{escape(__version__)}</code>"
            + (f" · rev <code>{escape(info.current_rev)}</code>" if info.current_rev else "")
        )

    lines: list[str] = []
    if info.available:
        lines.append("🆕 <b>Update available</b>" if en else "🆕 <b>Доступно обновление</b>")
    else:
        lines.append(
            "✅ <b>Already up to date</b>" if en else "✅ <b>Обновлений нет — актуальная версия</b>"
        )
    lines.append(
        f"• {'Installed' if en else 'Установлено'}: <code>v{escape(__version__)}</code>"
        + (f" · rev <code>{escape(info.current_rev)}</code>" if info.current_rev else "")
    )

    if info.source == "git" and info.latest_rev:
        available_lbl = "Available" if en else "Доступно"
        if info.commits:
            lines.append(
                f"• {available_lbl}: rev <code>{escape(info.latest_rev)}</code> · "
                f"{len(info.commits)} " + ("commits" if en else "коммитов")
            )
        else:
            lines.append(f"• {available_lbl}: rev <code>{escape(info.latest_rev)}</code>")
        if info.commits:
            lines.append("")
            for commit in info.commits[:5]:
                lines.append(f"    <code>{escape(commit[:88])}</code>")
    elif info.latest_version:
        published = f" ({escape(info.published_at)})" if info.published_at else ""
        lines.append(
            f"• {'Latest release' if en else 'Последний релиз'}: "
            f"<code>v{escape(info.latest_version)}</code>{published}"
        )
        if info.release_url:
            lines.append(f"• <a href=\"{escape(info.release_url)}\">"
                         f"{'Release notes on GitHub' if en else 'Что нового на GitHub'}</a>")

    note = ""
    if info.available:
        note = (
            "\n\nОбновление выполнит <code>install.sh --self-update</code> "
            "(git pull, переустановка зависимостей, перезапуск сервиса)."
            if not en
            else "\n\nThe update runs <code>install.sh --self-update</code> "
            "(git pull, dependency refresh, service restart)."
        )
    else:
        note = (
            "\n\n<i>Проверка идёт по ветке "
            f"<code>{escape(config.update_branch)}</code> и релизам "
            f"<code>{escape(config.update_repo)}</code>.</i>"
            if not en
            else f"\n\n<i>Checked against branch <code>{escape(config.update_branch)}</code> "
            f"and releases of <code>{escape(config.update_repo)}</code>.</i>"
        )
    return "\n".join(lines) + note


def _manual_update_text(lang: str, plan: UpdatePlan) -> str:
    en = lang == "en"
    if plan.reason == "dependencies":
        return (
            "⚙️ <b>Manual step required.</b>\n\n"
            "The new revision changes <code>requirements.txt</code> / "
            "<code>pyproject.toml</code>, so dependencies must be reinstalled by root:\n\n"
            "<code>sudo /opt/mivta/install.sh --self-update</code>"
            if en
            else "⚙️ <b>Нужен ручной шаг.</b>\n\n"
            "В новой ревизии изменились <code>requirements.txt</code> / "
            "<code>pyproject.toml</code>, поэтому зависимости должен переустановить root:\n\n"
            "<code>sudo /opt/mivta/install.sh --self-update</code>"
        )
    return (
        "⚙️ <b>Automatic update is not available here.</b>\n\n"
        "Run on the server:\n<code>sudo /opt/mivta/install.sh --self-update</code>\n\n"
        "For Docker/other setups set <code>MIVTA_UPDATE_CMD</code> "
        "(for example <code>docker compose pull &amp;&amp; docker compose up -d</code>)."
        if en
        else "⚙️ <b>Автообновление в этой установке недоступно.</b>\n\n"
        "Выполните на сервере:\n<code>sudo /opt/mivta/install.sh --self-update</code>\n\n"
        "Для Docker и других схем задайте <code>MIVTA_UPDATE_CMD</code> "
        "(например <code>docker compose pull &amp;&amp; docker compose up -d</code>)."
    )


async def cmd_update(
    message: Message,
    db: Database,
    config: Config,
    restart: RestartController | None = None,
) -> None:
    """Owner-only `/update`: check for a newer revision and offer to apply it."""
    if message.from_user is None:
        return
    uid = message.from_user.id
    settings = await db.get_user_settings(uid)
    lang = _ui_for(message.from_user, settings, config)

    if not config.owner_ids:
        await message.answer(
            _owner_setup_hint_html(lang, uid, "/update"),
            parse_mode=ParseMode.HTML,
        )
        return
    if uid not in config.owner_ids:
        await message.answer(_owner_denied_text(lang))
        return
    if not config.self_update:
        await message.answer(
            "Updates from Telegram are disabled (SELF_UPDATE=false)."
            if lang == "en"
            else "Обновление из Telegram отключено (SELF_UPDATE=false)."
        )
        return

    info = await check_update(config)
    await message.answer(
        _render_update_html(lang, info, config),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_service_keyboard(lang, update_available=info.available),
    )


async def cmd_restart(
    message: Message,
    db: Database,
    config: Config,
    restart: RestartController | None = None,
) -> None:
    """Owner-only `/restart`: ask for confirmation before bouncing the service."""
    if message.from_user is None:
        return
    uid = message.from_user.id
    settings = await db.get_user_settings(uid)
    lang = _ui_for(message.from_user, settings, config)

    if not config.owner_ids:
        await message.answer(
            _owner_setup_hint_html(lang, uid, "/restart"),
            parse_mode=ParseMode.HTML,
        )
        return
    if uid not in config.owner_ids:
        await message.answer(_owner_denied_text(lang))
        return
    if not config.self_restart:
        await message.answer(
            "Restarts from Telegram are disabled (SELF_RESTART=false)."
            if lang == "en"
            else "Перезапуск из Telegram отключён (SELF_RESTART=false)."
        )
        return
    if restart is None:
        await message.answer(
            "Restart is not available in this runtime."
            if lang == "en"
            else "Перезапуск недоступен в этом режиме запуска."
        )
        return

    text = (
        "🔄 <b>Restart the bot?</b>\n\n"
        f"The process will be replaced with a fresh <code>v{escape(__version__)}</code> "
        "in a couple of seconds. In-flight translations may be lost."
        if lang == "en"
        else "🔄 <b>Перезапустить бота?</b>\n\n"
        f"Процесс будет поднят заново на <code>v{escape(__version__)}</code> "
        "через пару секунд. Переводы, выполняемые прямо сейчас, могут не дождаться ответа."
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_service_keyboard(lang, update_available=False, confirm_restart=True),
    )


async def handle_owner_service_callback(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    restart: RestartController | None = None,
) -> None:
    """Owner-only `/update` & `/restart` actions (`svc:update`, `svc:restart`, ...)."""
    uid = callback.from_user.id
    settings = await db.get_user_settings(uid)
    lang = _ui_for(callback.from_user, settings, config)

    if not config.owner_ids or uid not in config.owner_ids:
        await callback.answer(
            "Owner only." if lang == "en" else "Доступно только владельцу бота.",
            show_alert=True,
        )
        return

    action = (callback.data or "").partition(":")[2]
    message = callback.message if isinstance(callback.message, Message) else None

    if action == "cancel":
        if message is not None:
            with contextlib.suppress(TelegramBadRequest):
                await message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ok" if lang == "en" else "Ок")
        return

    if action == "check":
        if message is None:
            await callback.answer()
            return
        info = await check_update(config)
        await _edit_service_message(
            message,
            _render_update_html(lang, info, config),
            reply_markup=_build_service_keyboard(lang, update_available=info.available),
        )
        await callback.answer("Updated" if lang == "en" else "Обновил информацию")
        return

    if action == "restart":
        if not config.self_restart or restart is None or message is None:
            await callback.answer(
                "Restart is unavailable." if lang == "en" else "Перезапуск недоступен.",
                show_alert=True,
            )
            return
        marker = ServiceMarker(
            action="restart",
            chat_id=message.chat.id,
            message_id=message.message_id,
            from_version=__version__,
            requested_at=time.time(),
        )
        write_marker(config, marker)
        await _edit_service_message(
            message,
            "⏳ Restarting the service…"
            if lang == "en"
            else "⏳ Перезапускаю сервис…",
        )
        await callback.answer()
        restart.request(marker)
        return

    if action == "update":
        if not config.self_update or message is None:
            await callback.answer(
                "Updates are disabled." if lang == "en" else "Обновления отключены.",
                show_alert=True,
            )
            return
        plan = await plan_update(config)
        marker = ServiceMarker(
            action="update",
            chat_id=message.chat.id,
            message_id=message.message_id,
            from_version=__version__,
            requested_at=time.time(),
        )
        if plan.method == "manual":
            await _edit_service_message(message, _manual_update_text(lang, plan))
            await callback.answer()
            return
        if plan.method == "command":
            write_marker(config, marker)
            run_detached(config.update_cmd)
            await _edit_service_message(
                message,
                (
                    "⏳ Update command started:\n"
                    f"<code>{escape(' '.join(config.update_cmd))}</code>"
                    if lang == "en"
                    else "⏳ Команда обновления запущена:\n"
                    f"<code>{escape(' '.join(config.update_cmd))}</code>"
                ),
            )
            await callback.answer()
            return
        if plan.method == "trigger":
            write_marker(config, marker)
            trigger = write_update_trigger(config)
            if trigger is None:
                await callback.answer(
                    "Cannot request an update: data directory is not writable."
                    if lang == "en"
                    else "Не могу запросить обновление: каталог данных недоступен для записи.",
                    show_alert=True,
                )
                return
            await _edit_service_message(
                message,
                (
                    "⏳ Update requested from systemd.\n\n"
                    "The service pulls the new revision and restarts within a minute or two; "
                    "I'll report back in this message as soon as I'm online again."
                    if lang == "en"
                    else "⏳ Запрос на обновление передан в systemd.\n\n"
                    "Сервис подтянет новую ревизию и перезапустится в течение минуты-двух; "
                    "я напишу в этом сообщении, как только снова буду в сети."
                ),
            )
            await callback.answer()
            return
        if plan.method == "self":
            result = await apply_self_update(config)
            if not result.ok:
                await _edit_service_message(
                    message,
                    (
                        "❌ <b>Self-update failed.</b>\n"
                        f"<code>{escape(result.detail[:200])}</code>\n\n"
                        "Run manually:\n<code>sudo /opt/mivta/install.sh --self-update</code>"
                        if lang == "en"
                        else "❌ <b>Не удалось обновить код.</b>\n"
                        f"<code>{escape(result.detail[:200])}</code>\n\n"
                        "Запустите вручную:\n"
                        "<code>sudo /opt/mivta/install.sh --self-update</code>"
                    ),
                )
                await callback.answer()
                return
            if restart is None:
                await _edit_service_message(
                    message,
                    (
                        "✅ Code updated — restart the service to apply it."
                        if lang == "en"
                        else "✅ Код обновлён — перезапустите сервис, чтобы применить."
                    ),
                )
                await callback.answer()
                return
            write_marker(config, marker)
            await _edit_service_message(
                message,
                "⏳ Code updated — restarting…"
                if lang == "en"
                else "⏳ Код обновлён — перезапускаюсь…",
            )
            await callback.answer()
            restart.request(marker)
            return

    await callback.answer()


def build_commands_router() -> Router:
    """Create a fresh Router bound to command and settings callback handlers."""
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_settings, Command("help", "settings"))
    router.message.register(cmd_status, Command("status"), F.chat.type == "private")
    router.message.register(cmd_update, Command("update", "upgrade"), F.chat.type == "private")
    router.message.register(cmd_restart, Command("restart"), F.chat.type == "private")
    router.message.register(cmd_lang, Command("lang"))
    router.message.register(cmd_swap, Command("swap"))
    router.message.register(cmd_langs, Command("langs"))
    router.message.register(cmd_source, Command("source"))
    router.message.register(cmd_style, Command("style"))
    router.message.register(cmd_ui, Command("ui"))
    router.message.register(cmd_privacy, Command("privacy"))
    router.message.register(cmd_reset, Command("reset"))
    router.callback_query.register(handle_settings_callback, F.data.startswith("cfg:"))
    router.callback_query.register(handle_owner_status_callback, F.data.startswith("ost:"))
    router.callback_query.register(handle_owner_service_callback, F.data.startswith("svc:"))
    router.message.register(reject_direct_messages, F.chat.type == "private")
    return router
