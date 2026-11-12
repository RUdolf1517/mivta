"""Tests for owner `/status` dashboard (`OWNER_ID`) and automatic bot profile sync."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import (
    BotCommandScopeChat,
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    Update,
    User,
)

from mivta import __version__
from mivta.bot import (
    _register_localized_commands,
    _register_localized_profile,
    build_bot_descriptions,
    create_dispatcher,
)
from mivta.config import Config, ConfigError
from mivta.database import Database
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorRateLimitError,
)
from mivta.translator.fallback import FallbackTranslator
from mivta.utils.cache import TranslationCache
from mivta.utils.metrics import RuntimeMetrics


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
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True


class StubProvider(Translator):
    def __init__(self, name: str, *, fail_with: Exception | None = None) -> None:
        self.name = name
        self.fail_with = fail_with

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        if self.fail_with is not None:
            raise self.fail_with
        return TranslationResult(
            translated_text=f"[{self.name}:{request.target_lang}] {request.text}",
            source_lang="ru" if request.source_lang == "auto" else request.source_lang,
            target_lang=request.target_lang,
            provider=self.name,
            style=request.style,
        )


def _private_msg_update(
    update_id: int,
    *,
    user_id: int,
    text: str,
    lang_code: str = "ru",
) -> Update:
    user = User(id=user_id, is_bot=False, first_name="User", language_code=lang_code)
    msg = Message(
        message_id=100 + update_id,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=msg)


def _callback_update(
    update_id: int,
    *,
    user_id: int,
    data: str,
    lang_code: str = "ru",
) -> Update:
    user = User(id=user_id, is_bot=False, first_name="User", language_code=lang_code)
    msg = Message(
        message_id=501,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=123456, is_bot=True, first_name="mivta"),
        text="status placeholder",
    )
    cb = CallbackQuery(
        id=f"cb-{update_id}",
        from_user=user,
        chat_instance="ci-status",
        message=msg,
        data=data,
    )
    return Update(update_id=update_id, callback_query=cb)


def test_config_parses_owner_ids_and_sync_bot_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("OWNER_ID", " 777001, 888002 ; 777001 ")
    monkeypatch.setenv("SYNC_BOT_PROFILE", "false")
    cfg = Config.from_env(dotenv_path=tmp_path / "missing.env")
    assert cfg.owner_ids == (777001, 888002)
    assert cfg.sync_bot_profile is False

    monkeypatch.setenv("OWNER_ID", "not_a_number")
    with pytest.raises(ConfigError, match="OWNER_ID"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")

    monkeypatch.setenv("OWNER_ID", "-5")
    with pytest.raises(ConfigError, match="OWNER_ID"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")

    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("SYNC_BOT_PROFILE", "maybe")
    with pytest.raises(ConfigError, match="SYNC_BOT_PROFILE"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")


def test_bot_descriptions_respect_telegram_limits_for_max_username() -> None:
    for handle in ("mivtabot", "a_very_long_telegram_bot_name_32"):
        short_en, short_ru, desc_en, desc_ru = build_bot_descriptions(handle)
        assert 20 <= len(short_en) <= 120
        assert 20 <= len(short_ru) <= 120
        assert 80 <= len(desc_en) <= 512
        assert 80 <= len(desc_ru) <= 512
        assert f"@{handle}" in short_en
        assert f"@{handle}" in short_ru
        assert f"@{handle}" in desc_en
        assert f"@{handle}" in desc_ru


@pytest.mark.asyncio
async def test_register_localized_profile_and_owner_scoped_commands() -> None:
    class ProfileInspectBot:
        def __init__(self) -> None:
            self.short_descriptions: list[tuple[str | None, str]] = []
            self.descriptions: list[tuple[str | None, str]] = []
            self.scoped_commands: list[tuple[Any, str | None, list[str]]] = []

        async def set_my_short_description(
            self,
            short_description: str | None = None,
            language_code: str | None = None,
        ) -> bool:
            self.short_descriptions.append((language_code, short_description or ""))
            return True

        async def set_my_description(
            self,
            description: str | None = None,
            language_code: str | None = None,
        ) -> bool:
            self.descriptions.append((language_code, description or ""))
            return True

        async def set_my_commands(
            self,
            commands: list[Any],
            scope: Any = None,
            language_code: str | None = None,
        ) -> bool:
            self.scoped_commands.append((scope, language_code, [c.command for c in commands]))
            return True

    fake = ProfileInspectBot()
    await _register_localized_profile(fake, "mivtabot")  # type: ignore[arg-type]
    assert [lc for lc, _ in fake.short_descriptions] == [None, "ru", "be", "kk"]
    assert [lc for lc, _ in fake.descriptions] == [None, "ru", "be", "kk"]

    await _register_localized_commands(fake, owner_ids=(900001,))  # type: ignore[arg-type]
    owner_Entries = [
        (lc, cmds)
        for scope, lc, cmds in fake.scoped_commands
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == 900001
    ]
    assert [lc for lc, _ in owner_Entries] == [None, "ru", "be", "kk"]
    assert all("status" in cmds for _, cmds in owner_Entries)
    # Non-owner scopes never expose `/status` in menu
    other_entries = [
        cmds
        for scope, _, cmds in fake.scoped_commands
        if not isinstance(scope, BotCommandScopeChat)
    ]
    assert all("status" not in cmds for cmds in other_entries)


def test_runtime_metrics_hashes_users_without_storing_raw_ids() -> None:
    now = [100.0]
    metrics = RuntimeMetrics(clock=lambda: now[0])
    metrics.record_activity(777123456)
    metrics.record_activity(777123456)
    metrics.record_activity(888654321)
    metrics.record_activity(None)
    now[0] = 225.0

    assert metrics.session_unique_users == 2
    assert metrics.total_updates == 4
    assert metrics.uptime_seconds == 125.0
    # Verify raw integer IDs are not stored anywhere inside RuntimeMetrics
    raw_bytes_1 = (777123456).to_bytes(8, "big")
    for digest in metrics._seen_digests:
        assert len(digest) == 8
        assert digest != raw_bytes_1


@pytest.mark.asyncio
async def test_status_unconfigured_denies_stranger_and_renders_owner_dashboard(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    groq = StubProvider("groq", fail_with=TranslatorRateLimitError("429 quota"))
    google = StubProvider("google")
    mymemory = StubProvider("mymemory")
    chain = FallbackTranslator(
        [groq, google, mymemory],
        unavailable_cooldown=30.0,
        rate_limit_cooldown=60.0,
        clock=lambda: now[0],
    )

    # Trigger failover so groq enters 60s cooldown and google serves 1 translation
    req1 = TranslationRequest(text="Привет", target_lang="en")
    res = await chain.translate(req1)
    assert res.provider == "google"

    db = Database(tmp_path / "status.db")
    await db.connect()
    try:
        # Seed DB stats: 2 saved users, 1 active + 1 revoked business, 1 active group
        await db.update_user_settings(9001, target_lang="en")
        await db.update_user_settings(9002, target_lang="de")
        await db.upsert_business_connection(
            "bc-1",
            user_id=9001,
            user_chat_id=9001,
            can_reply=True,
            is_enabled=True,
        )
        await db.upsert_business_connection(
            "bc-2",
            user_id=9002,
            user_chat_id=9002,
            can_reply=False,
            is_enabled=False,
        )
        await db.update_group_settings(-100555, is_enabled=True, target_lang="en")
        await db.add_group_ignored_user(-100555, 4040)

        cache = TranslationCache(max_size=256, ttl_seconds=300, clock=lambda: now[0])
        cache.set(chain.name, req1, res)
        assert cache.get(chain.name, req1) is not None  # 1 hit
        assert (
            cache.get(chain.name, TranslationRequest(text="нет", target_lang="en")) is None
        )  # 1 miss

        metrics = RuntimeMetrics(clock=lambda: now[0])
        bot = CapturingBot()

        # 1) When OWNER_ID is empty -> shows setup hint with caller's ID only, zero server internals
        cfg_no_owner = Config(bot_token="123456:ABCDEF", owner_ids=())
        dp_no_owner = create_dispatcher(
            config=cfg_no_owner,
            db=db,
            translator=chain,
            cache=cache,
            metrics=metrics,
            bot_username="mivtabot",
        )
        await dp_no_owner.feed_update(
            bot,
            _private_msg_update(1, user_id=555111, text="/status", lang_code="ru"),
        )
        hint_msg = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "OWNER_ID=555111" in hint_msg.text
        assert "groq" not in hint_msg.text
        assert "google" not in hint_msg.text

        # 2) When OWNER_ID=(9001,): stranger (555111) is denied; group `/status` is ignored
        bot.calls.clear()
        cfg_owner = Config(bot_token="123456:ABCDEF", owner_ids=(9001,))
        dp = create_dispatcher(
            config=cfg_owner,
            db=db,
            translator=chain,
            cache=cache,
            metrics=metrics,
            bot_username="mivtabot",
        )
        await dp.feed_update(
            bot,
            _private_msg_update(2, user_id=555111, text="/status", lang_code="en"),
        )
        deny_msg = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Access denied" in deny_msg.text
        assert "groq" not in deny_msg.text

        # Stranger callback attempt is rejected with alert and does not reset cooldowns
        await dp.feed_update(
            bot,
            _callback_update(3, user_id=555111, data="ost:reset_cd", lang_code="en"),
        )
        cb_ans = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery)][-1]
        assert cb_ans.show_alert is True
        assert chain.get_provider_statuses()[0].cooldown_remaining == 60.0

        # Group `/status` produces zero replies
        bot.calls.clear()
        grp_msg = Message(
            message_id=777,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=-100555, type="supergroup", title="Team"),
            from_user=User(id=9001, is_bot=False, first_name="Owner"),
            text="/status",
        )
        await dp.feed_update(bot, Update(update_id=4, message=grp_msg))
        assert [c for c in bot.calls if isinstance(c, SendMessage)] == []

        # Also simulate an inline query from another user so session_unique_users grows
        iq = InlineQuery(
            id="iq-1",
            from_user=User(id=333222, is_bot=False, first_name="InlineUser", language_code="ru"),
            query="",
            offset="",
        )
        await dp.feed_update(bot, Update(update_id=5, inline_query=iq))

        # 3) Owner (9001) runs `/status` after 95 seconds of uptime
        now[0] = 1_095.0
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _private_msg_update(6, user_id=9001, text="/status", lang_code="ru"),
        )
        owner_msg = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        txt = owner_msg.text
        assert f"v{__version__}" in txt
        assert "1м 35с" in txt
        # Trigger another failover at t=1020 -> groq cools down until t=1080
        now[0] = 1_020.0
        groq.fail_with = TranslatorRateLimitError("429 again")
        await chain.translate(TranslationRequest(text="Ещё", target_lang="en"))
        now[0] = 1_035.0

        bot.calls.clear()
        await dp.feed_update(
            bot,
            _private_msg_update(7, user_id=9001, text="/status", lang_code="ru"),
        )
        owner_msg = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        txt = owner_msg.text
        assert "активен сейчас: <code>google</code>" in txt
        assert "<code>groq</code> — ⏳ cooldown 45с, TranslatorRateLimitError" in txt
        assert "<code>google</code> — 🟢 активен · ok: 2 / err: 0" in txt
        assert "<code>mymemory</code> — ⚪️ в резерве · ok: 0 / err: 0" in txt
        assert "Профилей в БД: <b>2</b>" in txt
        assert "Личка (Secretary / Business): <b>1</b> активн. (всего 2)" in txt
        assert "Группы (режим «&gt;»): <b>1</b> активн. (всего 1, игнор: 1)" in txt
        assert "Заполнено: <b>1 / 256</b>" in txt
        assert "<b>1</b> hit / <b>1</b> miss (50%)" in txt

        # 4) Owner presses `ost:reset_cd` -> clears cooldown so `groq` becomes active immediately
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _callback_update(8, user_id=9001, data="ost:reset_cd", lang_code="ru"),
        )
        edited = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "активен сейчас: <code>groq</code>" in edited.text
        assert "<code>groq</code> — 🟢 активен" in edited.text

        # 5) Owner presses `ost:clear_cache` -> empties RAM cache
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _callback_update(9, user_id=9001, data="ost:clear_cache", lang_code="ru"),
        )
        assert len(cache) == 0
        edited_after_clear = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Заполнено: <b>0 / 256</b>" in edited_after_clear.text
    finally:
        await db.close()
