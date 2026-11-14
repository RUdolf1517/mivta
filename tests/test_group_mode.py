"""Tests for Group '>' translation mode, admin-only group panel, and per-group ignore list."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetChatMember,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberMember,
    ChatMemberOwner,
    Document,
    ForumTopicCreated,
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
from mivta.utils.formatting import utf16_len


class GroupMockSession(BaseSession):
    def __init__(self, *, admin_ids: set[int] | None = None) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.admin_ids: set[int] = set(admin_ids or {900})
        self.user_names: dict[int, tuple[str, str]] = {
            900: ("Admin Boss", "boss900"),
            501: ("👨‍💻 Рудольф", "rudolf501"),
            502: ("Мария", "maria502"),
            503: ("Вася", "vasya503"),
        }
        self.fail_delete: bool = False
        self.fail_send: bool = False

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, GetChatMember):
            uid = int(method.user_id)
            fname, uname = self.user_names.get(uid, (f"User{uid}", f"u{uid}"))
            u = User(id=uid, is_bot=False, first_name=fname, username=uname)
            if uid in self.admin_ids:
                return ChatMemberOwner(user=u, is_anonymous=False)
            return ChatMemberMember(user=u)

        if isinstance(method, DeleteMessage):
            if self.fail_delete:
                raise TelegramBadRequest(
                    method=method,
                    message="Bad Request: message can't be deleted",
                )
            return True

        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            if self.fail_send:
                raise TelegramBadRequest(method=method, message="Bad Request:TOPIC_CLOSED")
            return Message(
                message_id=7000 + len(self.calls),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(method.chat_id), type="supergroup", title="Dev Group"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )

        if isinstance(method, EditMessageText):
            return Message(
                message_id=int(method.message_id or 1),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(method.chat_id or -1001), type="supergroup", title="Dev Group"),
                text=method.text,
            )

        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class TagEchoTranslator(Translator):
    name = "mockllm"
    supports_styles = True
    uses_llm_glossary = True

    def __init__(self) -> None:
        self.fail: bool = False
        self.requests: list[TranslationRequest] = []

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.requests.append(request)
        if self.fail:
            raise TranslatorUnavailableError("upstream down")
        # Preserve XML formatting spans `<s0>...</s0>` while marking target/style
        return TranslationResult(
            translated_text=f"[{request.target_lang}:{request.style.value}] {request.text}",
            source_lang="ru",
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )


def _grp_chat(chat_id: int = -100555) -> Chat:
    return Chat(id=chat_id, type="supergroup", title="Dev Group")


def _user(
    uid: int,
    first_name: str = "User",
    username: str | None = None,
    lang: str = "ru",
) -> User:
    return User(
        id=uid,
        is_bot=False,
        first_name=first_name,
        username=username or f"user{uid}",
        language_code=lang,
    )


def _grp_msg(
    upd_id: int,
    text: str,
    *,
    uid: int = 501,
    first_name: str = "👨‍💻 Рудольф",
    username: str = "rudolf501",
    chat_id: int = -100555,
    msg_id: int | None = None,
    entities: list[MessageEntity] | None = None,
    thread_id: int | None = None,
    reply_to: Message | None = None,
    sender_chat: Chat | None = None,
) -> Update:
    from_user = (
        User(id=1087968824, is_bot=True, first_name="Group", username="GroupAnonymousBot")
        if sender_chat is not None
        else _user(uid, first_name=first_name, username=username)
    )
    return Update(
        update_id=upd_id,
        message=Message(
            message_id=msg_id or (1000 + upd_id),
            date=dt.datetime.now(dt.UTC),
            chat=_grp_chat(chat_id),
            from_user=from_user,
            sender_chat=sender_chat,
            message_thread_id=thread_id,
            reply_to_message=reply_to,
            text=text,
            entities=entities,
        ),
    )


def _grp_cb(
    upd_id: int,
    data: str,
    *,
    uid: int = 900,
    chat_id: int = -100555,
    msg_id: int = 8888,
    lang: str = "ru",
) -> Update:
    panel_msg = Message(
        message_id=msg_id,
        date=dt.datetime.now(dt.UTC),
        chat=_grp_chat(chat_id),
        from_user=User(id=123, is_bot=True, first_name="mivta", username="mivtabot"),
        text="Panel",
    )
    return Update(
        update_id=upd_id,
        callback_query=CallbackQuery(
            id=f"gcb-{upd_id}",
            from_user=_user(uid, first_name="Caller", lang=lang),
            chat_instance="grp-inst",
            message=panel_msg,
            data=data,
        ),
    )


async def test_group_translation_entities_topics_replies_media_and_zero_loss() -> None:
    config = Config(bot_token="123:TOK", inline_debounce_ms=0)
    db = Database(":memory:")
    await db.connect()
    translator = TagEchoTranslator()
    session = GroupMockSession(admin_ids={900})
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=translator, bot_username="mivtabot")

    try:
        # 1. Ordinary group message without '>' is completely ignored
        await dp.feed_update(bot, _grp_msg(1, "Всем привет без стрелочки"))
        assert len(translator.requests) == 0
        assert len(session.calls) == 0

        # 2. '> Привет, коллеги!' with bold entity on 'коллеги' + emoji in author name!
        # Raw: "> Привет, коллеги!" -> 'коллеги' starts at char 10 (UTF-16 offset 10, len 7)
        raw = "> Привет, коллеги!"
        bold_ent = MessageEntity(type="bold", offset=10, length=7)
        await dp.feed_update(bot, _grp_msg(2, raw, msg_id=2002, entities=[bold_ent]))

        sent = [c for c in session.calls if isinstance(c, SendMessage)]
        deleted = [c for c in session.calls if isinstance(c, DeleteMessage)]
        assert len(sent) == 1
        assert len(deleted) == 1
        assert deleted[0].message_id == 2002
        # Verify send happened BEFORE delete!
        assert session.calls.index(sent[0]) < session.calls.index(deleted[0])

        out_text = sent[0].text
        assert out_text.startswith("👨‍💻 Рудольф: [en:neutral] Привет, коллеги!")
        out_ents = list(sent[0].entities or ())
        author_u16 = utf16_len("👨‍💻 Рудольф")
        assert out_ents[0].type == "bold" and out_ents[0].offset == 0
        assert out_ents[0].length == author_u16
        assert out_ents[1].type == "text_mention" and out_ents[1].user is not None
        assert out_ents[1].user.id == 501
        # Verify shifted bold entity still lands on 'коллеги' in UTF-16!
        shifted_bold = [e for e in out_ents[2:] if e.type == "bold"]
        assert len(shifted_bold) == 1
        u16_slice = out_text.encode("utf-16-le")[
            shifted_bold[0].offset * 2 : (shifted_bold[0].offset + shifted_bold[0].length) * 2
        ].decode("utf-16-le")
        assert u16_slice == "коллеги"

        # 3. Forum topic root message (`forum_topic_created`) keeps thread_id WITHOUT self-reply
        session.calls.clear()
        topic_root = Message(
            message_id=42,
            date=dt.datetime.now(dt.UTC),
            chat=_grp_chat(),
            forum_topic_created=ForumTopicCreated(name="Releases", icon_color=7322096),
        )
        await dp.feed_update(
            bot,
            _grp_msg(3, "> Тема релиза", thread_id=42, reply_to=topic_root),
        )
        sent_topic = [c for c in session.calls if isinstance(c, SendMessage)][0]
        assert sent_topic.message_thread_id == 42
        assert sent_topic.reply_parameters is None

        # Genuine reply inside topic preserves BOTH thread_id AND reply_parameters!
        session.calls.clear()
        real_target_msg = Message(
            message_id=77,
            date=dt.datetime.now(dt.UTC),
            chat=_grp_chat(),
            from_user=_user(502, "Мария"),
            text="When?",
        )
        await dp.feed_update(
            bot,
            _grp_msg(4, "> Завтра утром", thread_id=42, reply_to=real_target_msg),
        )
        sent_reply = [c for c in session.calls if isinstance(c, SendMessage)][0]
        assert sent_reply.message_thread_id == 42
        assert sent_reply.reply_parameters is not None
        assert sent_reply.reply_parameters.message_id == 77

        # 4. Photo & Document with '> caption' resends by file_id and deletes draft; album ignored
        session.calls.clear()
        photo_upd = Update(
            update_id=5,
            message=Message(
                message_id=2005,
                date=dt.datetime.now(dt.UTC),
                chat=_grp_chat(),
                from_user=_user(501, "Рудольф"),
                photo=[PhotoSize(file_id="ph_123", file_unique_id="u_123", width=800, height=600)],
                caption=">de Скриншот стенда",
            ),
        )
        await dp.feed_update(bot, photo_upd)
        sent_photos = [c for c in session.calls if isinstance(c, SendPhoto)]
        assert len(sent_photos) == 1
        assert sent_photos[0].photo == "ph_123"
        assert "[de:neutral]" in (sent_photos[0].caption or "")
        assert any(isinstance(c, DeleteMessage) and c.message_id == 2005 for c in session.calls)

        session.calls.clear()
        doc_upd = Update(
            update_id=6,
            message=Message(
                message_id=2006,
                date=dt.datetime.now(dt.UTC),
                chat=_grp_chat(),
                from_user=_user(501, "Рудольф"),
                document=Document(file_id="doc_9", file_unique_id="ud_9", file_name="spec.pdf"),
                caption="> Отчёт",
            ),
        )
        await dp.feed_update(bot, doc_upd)
        assert any(isinstance(c, SendDocument) and c.document == "doc_9" for c in session.calls)

        # Multi-photo album is untouched
        session.calls.clear()
        album_upd = Update(
            update_id=7,
            message=Message(
                message_id=2007,
                date=dt.datetime.now(dt.UTC),
                chat=_grp_chat(),
                from_user=_user(501, "Рудольф"),
                media_group_id="album_xyz",
                photo=[PhotoSize(file_id="ph_a", file_unique_id="ua", width=100, height=100)],
                caption="> Альбом",
            ),
        )
        await dp.feed_update(bot, album_upd)
        assert len(session.calls) == 0

        # 5. Zero-loss guarantee: upstream failure OR send failure NEVER deletes user draft!
        session.calls.clear()
        translator.fail = True
        await dp.feed_update(bot, _grp_msg(8, "> Важный черновик", msg_id=2008))
        assert not any(isinstance(c, DeleteMessage) for c in session.calls)

        translator.fail = False
        session.fail_send = True
        await dp.feed_update(bot, _grp_msg(9, "> Ещё важный черновик", msg_id=2009))
        assert not any(isinstance(c, DeleteMessage) for c in session.calls)
        session.fail_send = False

        # 6. Missing 'Delete messages' right sends hint ONCE per group, never spams twice
        session.calls.clear()
        session.fail_delete = True
        await dp.feed_update(bot, _grp_msg(10, "> Первое", msg_id=2010))
        msgs_after_first = [c for c in session.calls if isinstance(c, SendMessage)]
        assert len(msgs_after_first) == 2  # Translation + 1-time hint
        assert "Удаление сообщений" in msgs_after_first[1].text

        session.calls.clear()
        await dp.feed_update(bot, _grp_msg(11, "> Второе", msg_id=2011))
        msgs_after_second = [c for c in session.calls if isinstance(c, SendMessage)]
        assert len(msgs_after_second) == 1  # Only translation, NO duplicate hint!
    finally:
        await db.close()
        await bot.session.close()


async def test_group_long_message_is_translated_in_chunks() -> None:
    """A message longer than MAX_TEXT_LENGTH is chunked, not silently dropped."""
    config = Config(bot_token="123:TOK", inline_debounce_ms=0, max_text_length=50)
    db = Database(":memory:")
    await db.connect()
    translator = TagEchoTranslator()
    session = GroupMockSession(admin_ids={900})
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=translator, bot_username="mivtabot")

    try:
        long_body = " ".join(f"слово{index}" for index in range(40))
        assert len(long_body) > config.max_text_length
        await dp.feed_update(bot, _grp_msg(1, f"> {long_body}", msg_id=3001))

        sent = [c for c in session.calls if isinstance(c, SendMessage)]
        assert len(sent) == 1
        text = sent[0].text
        for word in ("слово0", "слово20", "слово39"):
            assert word in text
        assert translator.requests, "no provider request was made"
        assert len(translator.requests) > 1  # several chunks, one rate-limit token
    finally:
        await db.close()
        await bot.session.close()
        await db.close()
        await bot.session.close()


async def test_group_admin_only_settings_ignore_list_and_callbacks() -> None:
    config = Config(bot_token="123:TOK", inline_debounce_ms=0)
    db = Database(":memory:")
    await db.connect()
    translator = TagEchoTranslator()
    session = GroupMockSession(admin_ids={900})
    bot = Bot(token=config.bot_token, session=session)
    dp = create_dispatcher(config=config, db=db, translator=translator, bot_username="mivtabot")

    try:
        # 1. Non-admin member (501) CANNOT change group settings or ignore list
        for idx, cmd_str in enumerate(
            [
                "/settings",
                "/grouplang de",
                "/groupswap ru",
                "/groupstyle formal",
                "/grouppause off",
                "/ignore 502",
                "/unignore 502",
                "/ignored",
            ],
            start=20,
        ):
            session.calls.clear()
            await dp.feed_update(bot, _grp_msg(idx, cmd_str, uid=501))
            replies = [c for c in session.calls if isinstance(c, SendMessage)]
            assert len(replies) == 1
            assert "администратор" in replies[0].text.lower()

        g_initial = await db.get_group_settings(-100555)
        assert g_initial.target_lang == "" and g_initial.is_enabled is True
        assert await db.list_group_ignored_users(-100555) == ()

        # Non-admin clicking `gcfg:*` gets alert and zero DB changes
        session.calls.clear()
        await dp.feed_update(bot, _grp_cb(30, "gcfg:set_lang:de", uid=501))
        cb_ans = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
        assert len(cb_ans) == 1 and cb_ans[0].show_alert is True
        assert (await db.get_group_settings(-100555)).target_lang == ""

        # 2. Admin (900) configures group settings & anonymous group admin works too
        await dp.feed_update(bot, _grp_msg(31, "/grouplang de", uid=900))
        await dp.feed_update(bot, _grp_msg(32, "/groupswap ru", uid=900))
        await dp.feed_update(bot, _grp_msg(33, "/groupstyle formal", uid=900))
        # Anonymous group admin (`sender_chat.id == chat.id`) can also manage settings!
        await dp.feed_update(
            bot,
            _grp_msg(34, "/settings", sender_chat=_grp_chat(-100555)),
        )
        # Anonymous admin can also send `> ...` translations!
        session.calls.clear()
        await dp.feed_update(
            bot,
            _grp_msg(35, "> Объявление от группы", sender_chat=_grp_chat(-100555)),
        )
        anon_sent = [c for c in session.calls if isinstance(c, SendMessage)]
        assert len(anon_sent) == 1
        assert anon_sent[0].text.startswith("Dev Group: [de:formal]")

        # 3. Ignore list: reply, bot-translated reply, @username, ID, UI buttons!
        # Let 503 (@vasya503) send a normal message so RAM remembers @vasya503
        await dp.feed_update(
            bot,
            _grp_msg(
                36,
                "Просто сообщение в чате",
                uid=503,
                first_name="Вася",
                username="vasya503",
            ),
        )

        # (a) Admin ignores 501 by replying `/ignore` to 501's message
        msg_from_501 = Message(
            message_id=3001,
            date=dt.datetime.now(dt.UTC),
            chat=_grp_chat(),
            from_user=_user(501, "Рудольф", "rudolf501"),
            text="Привет",
        )
        await dp.feed_update(bot, _grp_msg(37, "/ignore", uid=900, reply_to=msg_from_501))
        assert await db.is_user_ignored_in_group(-100555, 501) is True

        # Ignored user 501's `> ...` message is now completely ignored (0 translations, 0 deletes)!
        session.calls.clear()
        translator.requests.clear()
        await dp.feed_update(bot, _grp_msg(38, "> Меня игнорят?", uid=501))
        assert len(translator.requests) == 0
        assert len(session.calls) == 0

        # Non-ignored user 502 still translates fine!
        await dp.feed_update(
            bot,
            _grp_msg(39, "> А меня переводят", uid=502, first_name="Мария", username="maria502"),
        )
        assert len(translator.requests) == 1

        # (b) Admin replies `/ignore` to a BOT-TRANSLATED message of 502 (leading text_mention)!
        bot_translated_of_502 = Message(
            message_id=3002,
            date=dt.datetime.now(dt.UTC),
            chat=_grp_chat(),
            from_user=User(id=123, is_bot=True, first_name="mivta", username="mivtabot"),
            text="Мария: Hello",
            entities=[
                MessageEntity(type="bold", offset=0, length=5),
                MessageEntity(
                    type="text_mention",
                    offset=0,
                    length=5,
                    user=_user(502, "Мария", "maria502"),
                ),
            ],
        )
        await dp.feed_update(
            bot,
            _grp_msg(40, "/ignore", uid=900, reply_to=bot_translated_of_502),
        )
        assert await db.is_user_ignored_in_group(-100555, 502) is True

        # (c) Admin ignores 503 via `@vasya503`!
        await dp.feed_update(bot, _grp_msg(41, "/ignore @vasya503", uid=900))
        assert await db.is_user_ignored_in_group(-100555, 503) is True

        # Cannot ignore the bot itself (id=123)
        await dp.feed_update(bot, _grp_msg(42, "/ignore 123", uid=900))
        assert await db.is_user_ignored_in_group(-100555, 123) is False

        # (d) Unignore 503 via `/unignore 503`
        await dp.feed_update(bot, _grp_msg(43, "/unignore 503", uid=900))
        assert await db.is_user_ignored_in_group(-100555, 503) is False

        # (e) Interactive panel callbacks: delete btn, unignore, clear, pause, UI, reset
        await dp.feed_update(bot, _grp_cb(44, "gcfg:ignored:0", uid=900))
        await dp.feed_update(bot, _grp_cb(45, "gcfg:unig:501:0", uid=900))
        assert await db.is_user_ignored_in_group(-100555, 501) is False
        await dp.feed_update(bot, _grp_cb(46, "gcfg:clear_ig", uid=900))
        assert await db.list_group_ignored_users(-100555) == ()

        await dp.feed_update(bot, _grp_cb(47, "gcfg:toggle_del", uid=900))
        assert (await db.get_group_settings(-100555)).show_delete_btn is True

        # Verify `🗑` author delete callback: stranger (502) blocked, author (501) allowed!
        session.calls.clear()
        await dp.feed_update(bot, _grp_msg(48, "> Проверка кнопки удаления", uid=501))
        sent_with_btn = [c for c in session.calls if isinstance(c, SendMessage)][0]
        assert sent_with_btn.reply_markup is not None

        session.calls.clear()
        await dp.feed_update(bot, _grp_cb(49, "gdel:501", uid=502, msg_id=9100))
        assert not any(isinstance(c, DeleteMessage) for c in session.calls)
        cb_del_denied = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
        assert cb_del_denied[0].show_alert is True

        session.calls.clear()
        await dp.feed_update(bot, _grp_cb(50, "gdel:501", uid=501, msg_id=9100))
        assert any(isinstance(c, DeleteMessage) and c.message_id == 9100 for c in session.calls)

        # Pause via callback blocks translation; reset restores active state
        await dp.feed_update(bot, _grp_cb(51, "gcfg:toggle_enable", uid=900))
        assert (await db.get_group_settings(-100555)).is_enabled is False
        translator.requests.clear()
        await dp.feed_update(bot, _grp_msg(52, "> Во время паузы", uid=501))
        assert len(translator.requests) == 0

        await dp.feed_update(bot, _grp_cb(53, "gcfg:lang:1", uid=900))
        await dp.feed_update(bot, _grp_cb(54, "gcfg:set_lang:fr", uid=900))
        await dp.feed_update(bot, _grp_cb(55, "gcfg:swap:1", uid=900))
        await dp.feed_update(bot, _grp_cb(56, "gcfg:set_swap:ru", uid=900))
        await dp.feed_update(bot, _grp_cb(57, "gcfg:style", uid=900))
        await dp.feed_update(bot, _grp_cb(58, "gcfg:set_style:casual", uid=900))
        await dp.feed_update(bot, _grp_cb(59, "gcfg:set_ui:en", uid=900))
        g_after = await db.get_group_settings(-100555)
        assert (g_after.target_lang, g_after.swap_lang, g_after.style, g_after.ui_lang) == (
            "fr",
            "ru",
            TranslationStyle.CASUAL,
            "en",
        )

        await dp.feed_update(bot, _grp_cb(60, "gcfg:reset", uid=900))
        g_reset = await db.get_group_settings(-100555)
        assert g_reset.is_enabled is True and g_reset.target_lang == ""

        # 4. Edge cases: text_mention /ignore, unseen @username, empty /ignore, >8 pagination
        await dp.feed_update(bot, _grp_msg(61, "/ignore", uid=900))
        await dp.feed_update(bot, _grp_msg(62, "/ignore @never_seen_handle", uid=900))
        await dp.feed_update(bot, _grp_msg(63, "/unignore @never_seen_handle", uid=900))
        await dp.feed_update(bot, _grp_msg(64, "/unignore", uid=900))

        mention_ent = MessageEntity(
            type="text_mention",
            offset=8,
            length=4,
            user=_user(599, "Иван"),
        )
        await dp.feed_update(
            bot,
            _grp_msg(65, "/ignore Иван", uid=900, entities=[mention_ent]),
        )
        assert await db.is_user_ignored_in_group(-100555, 599) is True

        for extra_uid in range(601, 611):
            await db.add_group_ignored_user(-100555, extra_uid)
        await dp.feed_update(bot, _grp_msg(66, "/ignored", uid=900))
        await dp.feed_update(bot, _grp_cb(67, "gcfg:ignored:1", uid=900))
        await dp.feed_update(bot, _grp_cb(68, "gcfg:ignored:0", uid=900))

        # CLI reset flags: /grouplang personal, /groupswap off, /groupstyle personal, /grouppause
        await dp.feed_update(bot, _grp_msg(69, "/grouplang personal", uid=900))
        await dp.feed_update(bot, _grp_msg(70, "/groupswap off", uid=900))
        await dp.feed_update(bot, _grp_msg(71, "/groupstyle personal", uid=900))
        await dp.feed_update(bot, _grp_msg(72, "/grouppause off", uid=900))
        assert (await db.get_group_settings(-100555)).is_enabled is False
        await dp.feed_update(bot, _grp_msg(73, "/grouppause on", uid=900))
        assert (await db.get_group_settings(-100555)).is_enabled is True
    finally:
        await db.close()
        await bot.session.close()
