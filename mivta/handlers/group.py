"""Group & Supergroup '>' translation mode, admin-only settings, and user ignore list."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import OrderedDict
from collections.abc import Sequence

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReplyParameters,
    User,
)

from mivta.config import Config
from mivta.database import Database, GroupSettings
from mivta.handlers.pipeline import (
    TELEGRAM_MAX_CAPTION_LEN,
    TELEGRAM_MAX_MESSAGE_LEN,
    InflightEntry,
    RateLimitedOutcome,
    TextTooLong,
    execute_chunked_translation,
)
from mivta.translator.base import (
    TranslationRequest,
    TranslationStyle,
    Translator,
    TranslatorError,
    parse_style,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.formatting import (
    prepare_text_for_translation,
    restore_formatted_text,
    utf16_len,
)
from mivta.utils.i18n import parse_ui_lang, resolve_ui_lang, style_label
from mivta.utils.languages import (
    QUICK_LANGUAGES_PRIMARY,
    QUICK_LANGUAGES_SECONDARY,
    language_label,
    language_name,
    resolve_auto_swap_target,
    resolve_language,
)
from mivta.utils.metrics import RuntimeMetrics
from mivta.utils.parser import has_translatable_text, parse_business_query
from mivta.utils.ratelimit import RateLimiter
from mivta.utils.telegram import call_with_retry

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES: frozenset[str] = frozenset({"group", "supergroup"})
_PERSONAL_TOKENS: frozenset[str] = frozenset(
    {"personal", "user", "default", "off", "clear", "none", "reset", "выкл", "сброс", "личн", "-"}
)
_ADMIN_STATUSES: frozenset[str] = frozenset({"creator", "administrator"})
_CLEAN_NAME_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")

MAX_RAM_USERNAMES = 2048
MAX_MISSING_DELETE_HINTS = 1024
IGNORED_PAGE_SIZE = 8


class GroupRuntimeState:
    """Transient RAM-only state for `@username` resolution and one-time permission hints."""

    def __init__(self) -> None:
        self._usernames: OrderedDict[tuple[int, str], int] = OrderedDict()
        self._warned_delete_chats: OrderedDict[int, None] = OrderedDict()

    def remember_user(self, chat_id: int, user: User | None) -> None:
        if user is None or user.is_bot or not user.username:
            return
        key = (chat_id, user.username.strip().lstrip("@").lower())
        if not key[1]:
            return
        self._usernames[key] = user.id
        self._usernames.move_to_end(key)
        while len(self._usernames) > MAX_RAM_USERNAMES:
            self._usernames.popitem(last=False)

    def resolve_username(self, chat_id: int, username: str) -> int | None:
        clean = username.strip().lstrip("@").lower()
        if not clean:
            return None
        return self._usernames.get((chat_id, clean))

    def mark_delete_hint_shown(self, chat_id: int) -> bool:
        """Return True the first time `chat_id` lacks delete permission, False afterwards."""
        if chat_id in self._warned_delete_chats:
            return False
        self._warned_delete_chats[chat_id] = None
        while len(self._warned_delete_chats) > MAX_MISSING_DELETE_HINTS:
            self._warned_delete_chats.popitem(last=False)
        return True


def _is_anonymous_group_admin(message: Message) -> bool:
    return (
        message.sender_chat is not None
        and message.sender_chat.id == message.chat.id
        and not bool(getattr(message, "is_automatic_forward", False))
    )


async def _is_user_group_admin(bot: Bot, chat_id: int, user: User | None) -> bool:
    if user is None or user.is_bot:
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user.id)
    except TelegramAPIError:
        return False
    status = getattr(member, "status", "")
    status_val = str(getattr(status, "value", status)).lower()
    return status_val in _ADMIN_STATUSES


async def _check_message_admin(message: Message, bot: Bot) -> bool:
    if _is_anonymous_group_admin(message):
        return True
    return await _is_user_group_admin(bot, message.chat.id, message.from_user)


def _group_ui(
    g_settings: GroupSettings,
    user: User | None,
    config: Config,
) -> str:
    tg_code = user.language_code if user is not None else None
    return resolve_ui_lang(
        g_settings.ui_lang,
        tg_code,
        default_ui_lang=config.default_ui_lang,
    )


def _non_admin_notice(ui: str, bot_username: str) -> str:
    if ui == "en":
        return (
            "Only group administrators can change group bot settings or the ignore list. "
            f"For personal translation preferences, message @{bot_username} privately."
        )
    return (
        "Менять настройки бота и игнор-лист в группе могут только администраторы. "
        f"Личные настройки перевода доступны в ЛС @{bot_username}."
    )


def _clean_display_name(raw: str) -> str:
    cleaned = _CLEAN_NAME_RE.sub(" ", raw).strip()
    if not cleaned:
        return "User"
    return cleaned[:64]


async def _resolve_member_label(bot: Bot, chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        u = getattr(member, "user", None)
        if u is not None:
            name = _clean_display_name(u.full_name or u.username or "")
            if name and name != "User":
                return f"{name} ({user_id})"
    except TelegramAPIError:
        pass
    return f"ID {user_id}"


def _extract_target_from_reply(reply: Message, bot_id: int | None) -> int | None:
    """Extract target human user_id from a replied-to message (including bot translations)."""
    # 1. If replying to a translated message sent by this bot, inspect leading text_mention!
    ents = list(reply.entities or ()) + list(reply.caption_entities or ())
    if (
        reply.from_user is not None
        and bot_id is not None
        and reply.from_user.id == bot_id
    ):
        for ent in ents:
            if ent.offset == 0 and ent.type == "text_mention" and ent.user is not None:
                return ent.user.id
        return None

    if reply.from_user is not None and not reply.from_user.is_bot:
        return reply.from_user.id
    return None


def _resolve_command_target_user(
    message: Message,
    command: CommandObject,
    state: GroupRuntimeState,
    bot_id: int | None,
) -> tuple[int | None, str | None]:
    """Resolve `(user_id, missing_username)` from reply, text_mention, numeric ID, or @username."""
    # 1. Explicit text_mention entity in command arguments
    for ent in message.entities or ():
        if ent.type == "text_mention" and ent.user is not None and not ent.user.is_bot:
            return ent.user.id, None

    raw_arg = (command.args or "").strip()
    if raw_arg:
        first_tok = raw_arg.split()[0].strip()
        if first_tok.isdigit():
            uid = int(first_tok)
            if uid > 0:
                return uid, None
        if first_tok.startswith("@") and len(first_tok) > 1:
            found = state.resolve_username(message.chat.id, first_tok)
            if found is not None:
                return found, None
            return None, first_tok

    # 2. Reply to a user's message (or to a bot-translated message with author header)
    if (
        message.reply_to_message is not None
        and getattr(message.reply_to_message, "forum_topic_created", None) is None
    ):
        uid = _extract_target_from_reply(message.reply_to_message, bot_id)
        if uid is not None:
            return uid, None

    return None, None


def _format_group_panel_text(
    g_settings: GroupSettings,
    ignored_count: int,
    config: Config,
    ui: str,
) -> str:
    is_en = ui == "en"
    default_tgt = language_name(config.default_target_lang, ui_lang=ui)

    if is_en:
        status_str = "Active ✅" if g_settings.is_enabled else "Paused ⏸"
        tgt_str = (
            language_label(g_settings.target_lang, ui_lang="en")
            if g_settings.target_lang
            else f"Member's setting (default: {default_tgt})"
        )
        swap_str = (
            language_label(g_settings.swap_lang, ui_lang="en")
            if g_settings.swap_lang
            else "off"
        )
        st_str = (
            style_label(g_settings.style, "en")
            if g_settings.style is not None
            else "Member's setting"
        )
        del_str = "On" if g_settings.show_delete_btn else "Off"
        return (
            "mivta — Group Admin Settings\n\n"
            f"• '>' translation in group: {status_str}\n"
            f"• Group language: {tgt_str}\n"
            f"• Group auto-swap (⇄): {swap_str}\n"
            f"• Group style: {st_str}\n"
            f"• Author delete button (🗑): {del_str}\n"
            f"• Ignored users: {ignored_count}\n\n"
            "How members translate in this group:\n"
            "• > Hello everyone! (bot deletes draft & posts translation)\n"
            "• >de text · en>pl !formal text\n"
            "(Requires 'Delete messages' admin permission for the bot).\n\n"
            "Admin moderation:\n"
            "• Reply /ignore or /unignore to a member's message (or pass @username / ID)"
        )

    status_str = "Активен ✅" if g_settings.is_enabled else "На паузе ⏸"
    tgt_str = (
        language_label(g_settings.target_lang, ui_lang="ru")
        if g_settings.target_lang
        else f"По настройкам участника (по умолч. {default_tgt})"
    )
    swap_str = (
        language_label(g_settings.swap_lang, ui_lang="ru")
        if g_settings.swap_lang
        else "выкл"
    )
    st_str = (
        style_label(g_settings.style, "ru")
        if g_settings.style is not None
        else "По настройкам участника"
    )
    del_str = "Вкл" if g_settings.show_delete_btn else "Выкл"
    return (
        "mivta — Настройки группы (только для админов)\n\n"
        f"• Перевод по «>» в группе: {status_str}\n"
        f"• Язык группы: {tgt_str}\n"
        f"• Авто-смена (⇄): {swap_str}\n"
        f"• Стиль: {st_str}\n"
        f"• Кнопка удаления у автора (🗑): {del_str}\n"
        f"• В игнор-листе: {ignored_count} чел.\n\n"
        "Как участники переводят в группе:\n"
        "• > Привет всем! (бот удаляет черновик и публикует перевод)\n"
        "• >de текст · ru>en !formal текст\n"
        "(Боту нужно одно право админа: «Удаление сообщений»).\n\n"
        "Управление игнор-листом:\n"
        "• Ответьте /ignore или /unignore на сообщение участника (или укажите @username / ID)"
    )


def _group_main_keyboard(
    g_settings: GroupSettings,
    ignored_count: int,
    ui: str,
) -> InlineKeyboardMarkup:
    is_en = ui == "en"
    toggle_lbl = (
        ("⏸ Pause '>'" if g_settings.is_enabled else "▶️ Resume '>'")
        if is_en
        else ("⏸ Пауза «>»" if g_settings.is_enabled else "▶️ Включить «>»")
    )
    del_lbl = (
        f"🗑 Delete btn: {'On' if g_settings.show_delete_btn else 'Off'}"
        if is_en
        else f"🗑 Кнопка 🗑: {'Вкл' if g_settings.show_delete_btn else 'Выкл'}"
    )
    next_ui = "ru" if is_en else "en"
    ui_btn_lbl = "🌐 Русский" if is_en else "🌐 English"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=toggle_lbl, callback_data="gcfg:toggle_enable"),
                InlineKeyboardButton(
                    text="🌍 Group language" if is_en else "🌍 Язык группы",
                    callback_data="gcfg:lang:1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⇄ Auto-swap" if is_en else "⇄ Авто-смена",
                    callback_data="gcfg:swap:1",
                ),
                InlineKeyboardButton(
                    text="🎭 Style" if is_en else "🎭 Стиль",
                    callback_data="gcfg:style",
                ),
            ],
            [
                InlineKeyboardButton(text=del_lbl, callback_data="gcfg:toggle_del"),
                InlineKeyboardButton(
                    text=(
                        f"🚫 Ignore list ({ignored_count})"
                        if is_en
                        else f"🚫 Игнор-лист ({ignored_count})"
                    ),
                    callback_data="gcfg:ignored:0",
                ),
            ],
            [
                InlineKeyboardButton(text=ui_btn_lbl, callback_data=f"gcfg:set_ui:{next_ui}"),
                InlineKeyboardButton(
                    text="♻️ Reset" if is_en else "♻️ Сброс",
                    callback_data="gcfg:reset",
                ),
            ],
        ]
    )


def _group_lang_keyboard(
    *,
    action_prefix: str,
    current_code: str,
    clear_label: str,
    clear_value: str,
    page: int,
    page_prefix: str,
    ui: str,
) -> InlineKeyboardMarkup:
    is_en = ui == "en"
    codes = QUICK_LANGUAGES_SECONDARY if page == 2 else QUICK_LANGUAGES_PRIMARY
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"• {clear_label}" if not current_code else clear_label,
                callback_data=f"gcfg:{action_prefix}:{clear_value}",
            )
        ]
    ]
    row: list[InlineKeyboardButton] = []
    for code in codes:
        lbl = language_name(code, ui_lang=ui)
        mark = "• " if code == current_code else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{lbl} ({code})",
                callback_data=f"gcfg:{action_prefix}:{code}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    other_page = 2 if page == 1 else 1
    rows.append(
        [
            InlineKeyboardButton(
                text="More languages →" if (page == 1 and is_en) else (
                    "← Primary languages" if is_en else (
                        "Ещё языки →" if page == 1 else "← Основные языки"
                    )
                ),
                callback_data=f"gcfg:{page_prefix}:{other_page}",
            ),
            InlineKeyboardButton(
                text="← Back" if is_en else "← Назад",
                callback_data="gcfg:main",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_style_keyboard(current: TranslationStyle | None, ui: str) -> InlineKeyboardMarkup:
    is_en = ui == "en"
    pers_lbl = "Member's setting" if is_en else "По настройкам участника"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"• {pers_lbl}" if current is None else pers_lbl,
                callback_data="gcfg:set_style:personal",
            )
        ]
    ]
    for st in (TranslationStyle.NEUTRAL, TranslationStyle.CASUAL, TranslationStyle.FORMAL):
        lbl = style_label(st, ui).capitalize()
        prefix = "• " if current == st else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{lbl}",
                    callback_data=f"gcfg:set_style:{st.value}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← Back" if is_en else "← Назад", callback_data="gcfg:main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_ignored_view(
    bot: Bot,
    chat_id: int,
    ignored_ids: Sequence[int],
    page: int,
    ui: str,
) -> tuple[str, InlineKeyboardMarkup]:
    is_en = ui == "en"
    total = len(ignored_ids)
    max_page = max(0, (total - 1) // IGNORED_PAGE_SIZE) if total else 0
    clamped_page = max(0, min(page, max_page))
    start = clamped_page * IGNORED_PAGE_SIZE
    slice_ids = ignored_ids[start : start + IGNORED_PAGE_SIZE]

    labels = await asyncio.gather(
        *(_resolve_member_label(bot, chat_id, uid) for uid in slice_ids)
    )

    if total == 0:
        text = (
            "🚫 Group Ignore List (empty)\n\n"
            "Ignored members' '> ...' messages will not be translated or deleted by the bot.\n\n"
            "To add a user:\n"
            "• Reply /ignore to any of their messages\n"
            "• Or send /ignore @username or /ignore <user_id>"
            if is_en
            else "🚫 Игнор-лист группы (пуст)\n\n"
            "Сообщения участников из этого списка бот не переводит и не удаляет.\n\n"
            "Как добавить участника:\n"
            "• Ответьте командой /ignore на любое его сообщение\n"
            "• Или отправьте /ignore @username либо /ignore <user_id>"
        )
    else:
        lines = [f"• {lbl}" for lbl in labels]
        header = (
            f"🚫 Group Ignore List ({total})\n\n"
            if is_en
            else f"🚫 Игнор-лист группы ({total} чел.)\n\n"
        )
        footer = (
            "\n\nTap a button below to unblock, or reply /ignore to a message to add."
            if is_en
            else "\n\nНажмите на участника ниже, чтобы снять игнор, "
            "или ответьте /ignore на сообщение, чтобы добавить."
        )
        text = header + "\n".join(lines) + footer

    rows: list[list[InlineKeyboardButton]] = []
    for uid, lbl in zip(slice_ids, labels, strict=True):
        btn_prefix = "✖ Unignore: " if is_en else "✖ Снять игнор: "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{btn_prefix}{lbl}"[:64],
                    callback_data=f"gcfg:unig:{uid}:{clamped_page}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if clamped_page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="← Prev" if is_en else "← Назад",
                callback_data=f"gcfg:ignored:{clamped_page - 1}",
            )
        )
    if clamped_page < max_page:
        nav_row.append(
            InlineKeyboardButton(
                text="Next →" if is_en else "Далее →",
                callback_data=f"gcfg:ignored:{clamped_page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    if total > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 Clear entire list" if is_en else "🧹 Очистить весь список",
                    callback_data="gcfg:clear_ig",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Group settings" if is_en else "← К настройкам группы",
                callback_data="gcfg:main",
            )
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_cb_safely(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def build_group_router() -> Router:
    """Create a fresh Router handling group '>' translation, admin panel, and ignore list."""
    router = Router(name="group")
    state = GroupRuntimeState()
    is_group_msg = F.chat.type.in_(GROUP_CHAT_TYPES)

    async def cmd_group_settings(
        message: Message,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return
        ignored = await db.list_group_ignored_users(message.chat.id)
        await message.reply(
            _format_group_panel_text(g_settings, len(ignored), config, ui),
            reply_markup=_group_main_keyboard(g_settings, len(ignored), ui),
        )

    async def cmd_grouplang(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        arg = (command.args or "").strip()
        if not arg:
            await message.reply(
                "Usage: /grouplang <code|personal> (e.g. /grouplang en or /grouplang personal)"
                if is_en
                else "Использование: /grouplang <код|personal> "
                "(например: /grouplang en или /grouplang personal)"
            )
            return

        if arg.lower() in _PERSONAL_TOKENS:
            await db.update_group_settings(message.chat.id, target_lang="")
            await message.reply(
                "Group language reset: each member's own /settings will be used."
                if is_en
                else "Язык группы сброшен: используются личные настройки каждого участника."
            )
            return

        resolved = resolve_language(arg, allow_auto=False)
        if resolved is None:
            await message.reply(
                f"Unknown language: {arg!r}." if is_en else f"Неизвестный язык: {arg!r}."
            )
            return
        await db.update_group_settings(message.chat.id, target_lang=resolved)
        lbl = language_label(resolved, ui_lang=ui)
        await message.reply(
            f"Group target language set to {lbl}."
            if is_en
            else f"Язык перевода группы установлен: {lbl}."
        )

    async def cmd_groupswap(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        arg = (command.args or "").strip()
        if not arg:
            await message.reply(
                "Usage: /groupswap <code|off> (e.g. /groupswap ru)"
                if is_en
                else "Использование: /groupswap <код|off> (например: /groupswap ru)"
            )
            return

        if arg.lower() in _PERSONAL_TOKENS:
            await db.update_group_settings(message.chat.id, swap_lang="")
            await message.reply(
                "Group auto-swap disabled." if is_en else "Авто-смена языка в группе выключена."
            )
            return

        resolved = resolve_language(arg, allow_auto=False)
        if resolved is None:
            await message.reply(
                f"Unknown language: {arg!r}." if is_en else f"Неизвестный язык: {arg!r}."
            )
            return
        await db.update_group_settings(message.chat.id, swap_lang=resolved)
        lbl = language_label(resolved, ui_lang=ui)
        await message.reply(
            f"Group auto-swap language set to {lbl}."
            if is_en
            else f"Парный язык авто-смены (⇄) в группе: {lbl}."
        )

    async def cmd_groupstyle(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        arg = (command.args or "").strip()
        if not arg:
            await message.reply(
                "Usage: /groupstyle <neutral|casual|formal|personal>"
                if is_en
                else "Использование: /groupstyle <neutral|casual|formal|personal>"
            )
            return

        if arg.lower() in _PERSONAL_TOKENS:
            await db.update_group_settings(message.chat.id, clear_style=True)
            await message.reply(
                "Group style reset to each member's personal setting."
                if is_en
                else "Стиль группы сброшен на личные настройки участников."
            )
            return

        parsed_st = parse_style(arg)
        if parsed_st is None:
            await message.reply(
                "Expected neutral, casual, formal, or personal."
                if is_en
                else "Ожидается neutral, casual, formal или personal."
            )
            return
        await db.update_group_settings(message.chat.id, style=parsed_st)
        await message.reply(
            f"Group style set to {style_label(parsed_st, ui)}."
            if is_en
            else f"Стиль перевода в группе: {style_label(parsed_st, ui)}."
        )

    async def cmd_grouppause(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        arg = (command.args or "").strip().lower()
        if arg in ("on", "enable", "resume", "вкл"):
            next_enabled = True
        elif arg in ("off", "pause", "disable", "выкл", "пауза"):
            next_enabled = False
        else:
            next_enabled = not g_settings.is_enabled

        updated = await db.update_group_settings(message.chat.id, is_enabled=next_enabled)
        if updated.is_enabled:
            await message.reply(
                "Group '>' translation is now active ✅"
                if is_en
                else "Перевод по «>» в группе включён ✅"
            )
        else:
            await message.reply(
                "Group '>' translation is paused ⏸"
                if is_en
                else "Перевод по «>» в группе поставлен на паузу ⏸"
            )

    async def cmd_ignore(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        if message.reply_to_message is not None:
            state.remember_user(message.chat.id, message.reply_to_message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        target_uid, missing_handle = _resolve_command_target_user(
            message, command, state, getattr(bot, "id", None)
        )
        if missing_handle is not None:
            await message.reply(
                f"I haven't seen messages from {missing_handle} yet — reply /ignore directly "
                "to one of their messages or pass their numeric user ID."
                if is_en
                else f"Я ещё не видел сообщений от {missing_handle} — ответьте командой /ignore "
                "реплаем на его сообщение или укажите числовой ID."
            )
            return

        if target_uid is None:
            await message.reply(
                "Reply /ignore to a member's message, or use /ignore @username or /ignore <id>."
                if is_en
                else "Ответьте командой /ignore на сообщение участника "
                "(или укажите /ignore @username либо /ignore <id>)."
            )
            return

        if getattr(bot, "id", None) == target_uid:
            await message.reply(
                "Cannot ignore the bot itself — use /grouppause to pause group translation."
                if is_en
                else "Нельзя добавить самого бота в игнор — используйте /grouppause для паузы."
            )
            return

        try:
            added = await db.add_group_ignored_user(message.chat.id, target_uid)
        except ValueError as exc:
            await message.reply(str(exc))
            return

        lbl = await _resolve_member_label(bot, message.chat.id, target_uid)
        if added:
            await message.reply(
                f"🚫 Added to ignore list: {lbl}. The bot will no longer translate their messages."
                if is_en
                else f"🚫 Добавлен в игнор-лист: {lbl}. Бот больше не переводит его сообщения."
            )
        else:
            await message.reply(
                f"{lbl} is already in the ignore list."
                if is_en
                else f"{lbl} уже находится в игнор-листе."
            )

    async def cmd_unignore(
        message: Message,
        command: CommandObject,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        if message.reply_to_message is not None:
            state.remember_user(message.chat.id, message.reply_to_message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        is_en = ui == "en"
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return

        target_uid, missing_handle = _resolve_command_target_user(
            message, command, state, getattr(bot, "id", None)
        )
        if missing_handle is not None:
            await message.reply(
                f"Unknown handle {missing_handle} — reply /unignore to their message, "
                "pass their numeric ID, or use /ignored buttons."
                if is_en
                else f"Не удалось найти {missing_handle} — ответьте /unignore на его сообщение, "
                "укажите числовой ID или нажмите кнопку в /ignored."
            )
            return

        if target_uid is None:
            await message.reply(
                "Reply /unignore to a member's message, pass @username / ID, or open /ignored."
                if is_en
                else "Ответьте /unignore на сообщение участника, укажите @username / ID "
                "или откройте список /ignored."
            )
            return

        removed = await db.remove_group_ignored_user(message.chat.id, target_uid)
        lbl = await _resolve_member_label(bot, message.chat.id, target_uid)
        if removed:
            await message.reply(
                f"✅ Removed from ignore list: {lbl}."
                if is_en
                else f"✅ Убран из игнор-листа: {lbl}."
            )
        else:
            await message.reply(
                f"{lbl} was not in the ignore list."
                if is_en
                else f"{lbl} не числится в игнор-листе."
            )

    async def cmd_ignored(
        message: Message,
        bot: Bot,
        config: Config,
        db: Database,
        bot_username: str = "BotUsername",
    ) -> None:
        state.remember_user(message.chat.id, message.from_user)
        g_settings = await db.get_group_settings(message.chat.id)
        ui = _group_ui(g_settings, message.from_user, config)
        if not await _check_message_admin(message, bot):
            await message.reply(_non_admin_notice(ui, bot_username))
            return
        ignored_ids = await db.list_group_ignored_users(message.chat.id)
        text, markup = await _build_ignored_view(bot, message.chat.id, ignored_ids, 0, ui)
        await message.reply(text, reply_markup=markup)

    async def handle_group_callback(
        callback: CallbackQuery,
        bot: Bot,
        config: Config,
        db: Database,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        chat = callback.message.chat
        if chat.type not in GROUP_CHAT_TYPES:
            await callback.answer()
            return

        state.remember_user(chat.id, callback.from_user)
        g_settings = await db.get_group_settings(chat.id)
        ui = _group_ui(g_settings, callback.from_user, config)
        is_en = ui == "en"

        if not await _is_user_group_admin(bot, chat.id, callback.from_user):
            await callback.answer(
                "Only group administrators can change bot settings."
                if is_en
                else "Только администраторы группы могут менять настройки бота.",
                show_alert=True,
            )
            return

        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else "main"
        arg = parts[2] if len(parts) > 2 else ""

        if action == "main":
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(g_settings, len(ignored), config, ui),
                _group_main_keyboard(g_settings, len(ignored), ui),
            )
            await callback.answer()
            return

        if action == "toggle_enable":
            updated = await db.update_group_settings(chat.id, is_enabled=not g_settings.is_enabled)
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, ui),
                _group_main_keyboard(updated, len(ignored), ui),
            )
            await callback.answer(
                ("Active" if updated.is_enabled else "Paused")
                if is_en
                else ("Перевод включён" if updated.is_enabled else "На паузе")
            )
            return

        if action == "toggle_del":
            updated = await db.update_group_settings(
                chat.id, show_delete_btn=not g_settings.show_delete_btn
            )
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, ui),
                _group_main_keyboard(updated, len(ignored), ui),
            )
            await callback.answer()
            return

        if action == "lang":
            page = 2 if arg == "2" else 1
            await _edit_cb_safely(
                callback,
                "Choose default target language for this group:"
                if is_en
                else "Выберите единый язык перевода для группы (или личные настройки участников):",
                _group_lang_keyboard(
                    action_prefix="set_lang",
                    current_code=g_settings.target_lang,
                    clear_label=(
                        "👤 Member's personal setting"
                        if is_en
                        else "👤 По настройкам участника"
                    ),
                    clear_value="personal",
                    page=page,
                    page_prefix="lang",
                    ui=ui,
                ),
            )
            await callback.answer()
            return

        if action == "set_lang":
            if arg.lower() in _PERSONAL_TOKENS:
                updated = await db.update_group_settings(chat.id, target_lang="")
                toast = "Using members' personal settings" if is_en else "По настройкам участников"
            else:
                resolved = resolve_language(arg, allow_auto=False)
                if resolved is None:
                    await callback.answer("Unknown language" if is_en else "Неизвестный язык")
                    return
                updated = await db.update_group_settings(chat.id, target_lang=resolved)
                toast = language_label(resolved, ui_lang=ui)
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, ui),
                _group_main_keyboard(updated, len(ignored), ui),
            )
            await callback.answer(toast)
            return

        if action == "swap":
            page = 2 if arg == "2" else 1
            await _edit_cb_safely(
                callback,
                "Choose bidirectional auto-swap language (⇄) for this group:"
                if is_en
                else "Выберите парный язык двусторонней авто-смены (⇄) для группы:",
                _group_lang_keyboard(
                    action_prefix="set_swap",
                    current_code=g_settings.swap_lang,
                    clear_label="Disable auto-swap (off)" if is_en else "Выключить авто-смену",
                    clear_value="off",
                    page=page,
                    page_prefix="swap",
                    ui=ui,
                ),
            )
            await callback.answer()
            return

        if action == "set_swap":
            if arg.lower() in _PERSONAL_TOKENS:
                updated = await db.update_group_settings(chat.id, swap_lang="")
                toast = "Auto-swap off" if is_en else "Авто-смена выключена"
            else:
                resolved = resolve_language(arg, allow_auto=False)
                if resolved is None:
                    await callback.answer("Unknown language" if is_en else "Неизвестный язык")
                    return
                updated = await db.update_group_settings(chat.id, swap_lang=resolved)
                toast = f"⇄ {language_label(resolved, ui_lang=ui)}"
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, ui),
                _group_main_keyboard(updated, len(ignored), ui),
            )
            await callback.answer(toast)
            return

        if action == "style":
            await _edit_cb_safely(
                callback,
                "Choose translation style for this group:"
                if is_en
                else "Выберите регистр (стиль) перевода в группе:",
                _group_style_keyboard(g_settings.style, ui),
            )
            await callback.answer()
            return

        if action == "set_style":
            if arg.lower() in _PERSONAL_TOKENS:
                updated = await db.update_group_settings(chat.id, clear_style=True)
                toast = "Member's setting" if is_en else "По настройкам участников"
            else:
                parsed_st = parse_style(arg)
                if parsed_st is None:
                    await callback.answer()
                    return
                updated = await db.update_group_settings(chat.id, style=parsed_st)
                toast = style_label(parsed_st, ui)
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, ui),
                _group_main_keyboard(updated, len(ignored), ui),
            )
            await callback.answer(toast)
            return

        if action == "set_ui":
            parsed_ui = parse_ui_lang(arg)
            if parsed_ui is None:
                await callback.answer()
                return
            updated = await db.update_group_settings(chat.id, ui_lang=parsed_ui)
            new_ui = _group_ui(updated, callback.from_user, config)
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, new_ui),
                _group_main_keyboard(updated, len(ignored), new_ui),
            )
            await callback.answer("English" if new_ui == "en" else "Русский")
            return

        if action == "ignored":
            page = int(arg) if arg.isdigit() else 0
            ignored = await db.list_group_ignored_users(chat.id)
            text, markup = await _build_ignored_view(bot, chat.id, ignored, page, ui)
            await _edit_cb_safely(callback, text, markup)
            await callback.answer()
            return

        if action == "unig":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            if arg.isdigit():
                await db.remove_group_ignored_user(chat.id, int(arg))
            ignored = await db.list_group_ignored_users(chat.id)
            text, markup = await _build_ignored_view(bot, chat.id, ignored, page, ui)
            await _edit_cb_safely(callback, text, markup)
            await callback.answer("Unignored" if is_en else "Убран из игнора")
            return

        if action == "clear_ig":
            await db.clear_group_ignored_users(chat.id)
            text, markup = await _build_ignored_view(bot, chat.id, (), 0, ui)
            await _edit_cb_safely(callback, text, markup)
            await callback.answer("Ignore list cleared" if is_en else "Игнор-лист очищен")
            return

        if action == "reset":
            updated = await db.reset_group_settings(chat.id)
            new_ui = _group_ui(updated, callback.from_user, config)
            ignored = await db.list_group_ignored_users(chat.id)
            await _edit_cb_safely(
                callback,
                _format_group_panel_text(updated, len(ignored), config, new_ui),
                _group_main_keyboard(updated, len(ignored), new_ui),
            )
            await callback.answer(
                "Group settings reset" if new_ui == "en" else "Настройки группы сброшены"
            )
            return

        await callback.answer()

    async def handle_author_delete_callback(
        callback: CallbackQuery,
        bot: Bot,
        config: Config,
        db: Database,
    ) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        chat = callback.message.chat
        g_settings = await db.get_group_settings(chat.id)
        ui = _group_ui(g_settings, callback.from_user, config)
        is_en = ui == "en"

        parts = (callback.data or "").split(":")
        owner_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        caller = callback.from_user
        is_owner = caller is not None and caller.id == owner_id
        is_admin = await _is_user_group_admin(bot, chat.id, caller)
        if not is_owner and not is_admin:
            await callback.answer(
                "Only the author or a group admin can delete this message."
                if is_en
                else "Удалить это сообщение может только его автор или админ группы.",
                show_alert=True,
            )
            return

        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(chat_id=chat.id, message_id=callback.message.message_id)
        await callback.answer()

    async def handle_group_translation(
        message: Message,
        bot: Bot,
        config: Config,
        db: Database,
        translator: Translator,
        cache: TranslationCache,
        rate_limiter: RateLimiter,
        concurrency_sem: asyncio.Semaphore,
        inflight: dict[bytes, InflightEntry],
        bot_username: str = "BotUsername",
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        """Translate group messages starting with '>', repost with author header, delete draft."""
        if getattr(message, "is_automatic_forward", False):
            return
        state.remember_user(message.chat.id, message.from_user)

        is_caption = message.text is None and message.caption is not None
        raw_content = message.text if not is_caption else message.caption
        raw_entities = message.entities if not is_caption else message.caption_entities
        if not raw_content:
            return

        # Multi-item media albums cannot be atomically resent from a single Message update
        if is_caption and message.media_group_id is not None:
            return
        if is_caption and not any(
            (
                message.photo,
                message.video,
                message.document,
                message.animation,
                message.audio,
                message.voice,
            )
        ):
            return

        anon_admin = _is_anonymous_group_admin(message)
        if not anon_admin and (message.from_user is None or message.from_user.is_bot):
            return

        prelim = parse_business_query(raw_content, entities=raw_entities)
        if prelim is None or prelim.error is not None or not prelim.text:
            return

        chat_id = message.chat.id
        g_settings = await db.get_group_settings(chat_id)
        if not g_settings.is_enabled:
            return

        if not anon_admin and message.from_user is not None:
            sender_uid = message.from_user.id
            if await db.is_user_ignored_in_group(chat_id, sender_uid):
                return
            u_settings = await db.get_user_settings(sender_uid)
            author_name = _clean_display_name(
                message.from_user.full_name or message.from_user.username or str(sender_uid)
            )
            mention_user: User | None = message.from_user
        else:
            sender_uid = abs(chat_id)
            u_settings = await db.get_user_settings(0)
            title = message.sender_chat.title if message.sender_chat is not None else "Admin"
            author_name = _clean_display_name(title or "Admin")
            mention_user = None

        telegram_limit = TELEGRAM_MAX_CAPTION_LEN if is_caption else TELEGRAM_MAX_MESSAGE_LEN
        sep = ":\n" if "\n" in prelim.text else ": "
        header_prefix = f"{author_name}{sep}"
        body_budget = telegram_limit - len(header_prefix)
        # Long messages are split into `MAX_TEXT_LENGTH` chunks by the pipeline and
        # stitched back together, so the only hard limit is Telegram's own length.
        max_allowed = body_budget
        if max_allowed <= 0 or len(prelim.text) > max_allowed:
            return

        effective_source = (
            prelim.source_lang
            or (g_settings.source_lang if g_settings.source_lang != "auto" else "")
            or u_settings.source_lang
        )
        effective_target = prelim.target_lang or g_settings.target_lang or u_settings.target_lang
        effective_swap = (
            g_settings.swap_lang
            if g_settings.target_lang
            else (g_settings.swap_lang or u_settings.swap_lang)
        )
        effective_style = prelim.style or g_settings.style or u_settings.style
        explicit_override = prelim.source_lang is not None or prelim.target_lang is not None

        prepared = prepare_text_for_translation(
            prelim.text,
            entities=prelim.entities,
            glossary=config.translator_glossary,
            protect_glossary_tokens=not translator.uses_llm_glossary,
            parse_inline_code_markdown=False,
        )

        if not has_translatable_text(prepared.translatable_probe):
            restored = restore_formatted_text(
                prepared.encoded_text,
                prepared,
                max_length=body_budget,
                emit_entities_only_if_rich=False,
            )
        else:
            resolved_target, provider_swap = resolve_auto_swap_target(
                prepared.translatable_probe,
                source_lang=effective_source,
                target_lang=effective_target,
                swap_lang=effective_swap,
                explicit_override=explicit_override,
            )
            request = TranslationRequest(
                text=prepared.encoded_text,
                source_lang=effective_source,
                target_lang=resolved_target,
                style=effective_style,
                swap_lang=provider_swap,
            )
            try:
                outcome = await execute_chunked_translation(
                    user_id=sender_uid,
                    request=request,
                    translator=translator,
                    cache=cache,
                    rate_limiter=rate_limiter,
                    concurrency_sem=concurrency_sem,
                    inflight=inflight,
                    max_length=config.max_text_length,
                    metrics=metrics,
                )
            except TranslatorError as exc:
                logger.warning(
                    "Group translation failed (provider=%s, err=%s)",
                    translator.name,
                    exc,
                )
                return

            if isinstance(outcome, (RateLimitedOutcome, TextTooLong)):
                return

            restored = restore_formatted_text(
                outcome.translated_text,
                prepared,
                max_length=body_budget,
                emit_entities_only_if_rich=False,
            )

        if not restored.text:
            return

        full_text = f"{header_prefix}{restored.text}"
        name_u16 = utf16_len(author_name)
        prefix_u16 = utf16_len(header_prefix)

        header_entities: list[MessageEntity] = [
            MessageEntity(type="bold", offset=0, length=name_u16),
        ]
        if mention_user is not None:
            header_entities.append(
                MessageEntity(
                    type="text_mention",
                    offset=0,
                    length=name_u16,
                    user=mention_user,
                )
            )
        shifted_body_entities = [
            ent.model_copy(update={"offset": ent.offset + prefix_u16})
            for ent in (restored.entities or ())
        ]
        all_entities = header_entities + shifted_body_entities

        thread_id = message.message_thread_id
        reply_params: ReplyParameters | None = None
        if (
            message.reply_to_message is not None
            and getattr(message.reply_to_message, "forum_topic_created", None) is None
        ):
            reply_params = ReplyParameters(
                message_id=message.reply_to_message.message_id,
                allow_sending_without_reply=True,
            )

        del_markup: InlineKeyboardMarkup | None = None
        if g_settings.show_delete_btn and mention_user is not None:
            del_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🗑", callback_data=f"gdel:{mention_user.id}")]
                ]
            )

        # Golden rule: send translated message FIRST so user text is NEVER lost on send failure!
        try:
            if not is_caption:
                await call_with_retry(
                    lambda: bot.send_message(
                        chat_id=chat_id,
                        text=full_text,
                        entities=all_entities,
                        parse_mode=None,
                        message_thread_id=thread_id,
                        reply_parameters=reply_params,
                        reply_markup=del_markup,
                        disable_web_page_preview=True,
                    ),
                    label="group repost",
                )
            elif message.photo:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=message.photo[-1].file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
            elif message.video:
                await bot.send_video(
                    chat_id=chat_id,
                    video=message.video.file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
            elif message.document:
                await bot.send_document(
                    chat_id=chat_id,
                    document=message.document.file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
            elif message.animation:
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=message.animation.file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
            elif message.audio:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=message.audio.file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
            elif message.voice:
                await bot.send_voice(
                    chat_id=chat_id,
                    voice=message.voice.file_id,
                    caption=full_text,
                    caption_entities=all_entities,
                    parse_mode=None,
                    message_thread_id=thread_id,
                    reply_parameters=reply_params,
                    reply_markup=del_markup,
                )
        except TelegramAPIError as exc:
            logger.warning("Failed to send group translation in chat %s: %s", chat_id, exc)
            return

        try:
            await call_with_retry(
                lambda: bot.delete_message(chat_id=chat_id, message_id=message.message_id),
                label="group draft delete",
            )
        except TelegramRetryAfter:
            logger.warning(
                "Group draft delete postponed by flood control in chat %s", chat_id
            )
        except TelegramBadRequest:
            if state.mark_delete_hint_shown(chat_id):
                ui = _group_ui(g_settings, message.from_user, config)
                hint = (
                    f"Tip: grant @{bot_username} the 'Delete messages' admin permission "
                    "so '> ...' drafts are deleted automatically."
                    if ui == "en"
                    else f"Подсказка: выдайте @{bot_username} право администратора "
                    "«Удаление сообщений», чтобы черновики с «>» удалялись автоматически."
                )
                with contextlib.suppress(TelegramAPIError):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=hint,
                        message_thread_id=thread_id,
                    )
        except TelegramAPIError as exc:
            logger.warning("Could not delete group draft in chat %s: %s", chat_id, exc)

    router.message.register(
        cmd_group_settings,
        Command("settings", "groupsettings", "help"),
        is_group_msg,
    )
    router.message.register(cmd_grouplang, Command("grouplang"), is_group_msg)
    router.message.register(cmd_groupswap, Command("groupswap"), is_group_msg)
    router.message.register(cmd_groupstyle, Command("groupstyle"), is_group_msg)
    router.message.register(cmd_grouppause, Command("grouppause"), is_group_msg)
    router.message.register(cmd_ignore, Command("ignore"), is_group_msg)
    router.message.register(cmd_unignore, Command("unignore"), is_group_msg)
    router.message.register(cmd_ignored, Command("ignored"), is_group_msg)
    router.callback_query.register(handle_group_callback, F.data.startswith("gcfg:"))
    router.callback_query.register(handle_author_delete_callback, F.data.startswith("gdel:"))
    router.message.register(handle_group_translation, is_group_msg)
    return router
