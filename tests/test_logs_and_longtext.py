"""Owner `/logs` command and chunked long-text translation."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import Chat, Message, MessageEntity, User

from mivta.config import Config
from mivta.database import Database
from mivta.handlers.commands import cmd_logs, cmd_status, handle_owner_service_callback
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
)
from mivta.translator.base import (
    Translator as BaseTranslator,
)
from mivta.utils.cache import TranslationCache
from mivta.utils.formatting import (
    join_translated_chunks,
    prepare_text_for_translation,
    split_for_translation,
)
from mivta.utils.logtail import LogTail, install_log_tail
from mivta.utils.ratelimit import RateLimiter

# --------------------------------------------------------------------------- log tail


def test_log_tail_keeps_only_recent_warning_plus() -> None:
    tail = LogTail(capacity=5)
    logger = logging.getLogger("test.tail.records")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(tail)
    try:
        logger.debug("debug noise")
        logger.info("info noise")
        logger.warning("first warning")
        logger.error("an error")
        logger.critical("boom")
    finally:
        logger.removeHandler(tail)

    entries = tail.entries(limit=10)
    assert [entry.level for entry in entries] == ["WARNING", "ERROR", "CRITICAL"]
    assert entries[-1].message == "boom"
    assert entries[0].render() == entries[0].render()  # rendering is stable
    assert "WARNING test.tail.records: first warning" in entries[0].render()


def test_log_tail_honours_capacity_and_clear() -> None:
    tail = LogTail(capacity=3)
    logger = logging.getLogger("test.tail.capacity")
    logger.propagate = False
    logger.addHandler(tail)
    try:
        for index in range(10):
            logger.warning("w%d", index)
    finally:
        logger.removeHandler(tail)

    assert len(tail) == 3
    assert [entry.message for entry in tail.entries(limit=3)] == ["w7", "w8", "w9"]
    assert tail.entries(limit=1)[0].message == "w9"
    assert [entry.message for entry in tail.entries(limit=10, min_level="CRITICAL")] == []
    tail.clear()
    assert len(tail) == 0


def test_log_tail_truncates_long_lines() -> None:
    tail = LogTail(capacity=2)
    logger = logging.getLogger("test.tail.truncate")
    logger.propagate = False
    logger.addHandler(tail)
    try:
        logger.warning("x" * 500)
    finally:
        logger.removeHandler(tail)

    message = tail.entries(limit=1)[0].message
    assert len(message) == 300
    assert message.endswith("…")


def test_install_log_tail_captures_root_records(tmp_path) -> None:
    root = logging.getLogger()
    tail = install_log_tail(root, capacity=10)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("mivta.some.module").warning("captured through root")
    finally:
        root.setLevel(previous)
        root.removeHandler(tail)

    assert any(entry.message == "captured through root" for entry in tail.entries(limit=5))


# --------------------------------------------------------------------------- /logs


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


def _message(bot: CapturingBot, uid: int, lang_code: str = "ru") -> Message:
    message = Message(
        message_id=1,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Owner", language_code=lang_code),
        text="/logs",
    )
    return message.as_(bot)


def _tail_with_records(count: int = 4) -> LogTail:
    tail = LogTail(capacity=50)
    logger = logging.getLogger("test.logs.handler")
    logger.propagate = False
    logger.addHandler(tail)
    try:
        for index in range(count):
            logger.warning("record %d", index)
        logger.error("boom")
    finally:
        logger.removeHandler(tail)
    return tail


async def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "logs.db")
    await db.connect()
    return db


def _sent_text(bot: CapturingBot) -> str:
    texts = [m.text for m in bot.calls if isinstance(m, (SendMessage, EditMessageText)) and m.text]
    assert texts, "no outgoing message captured"
    return texts[-1]


@pytest.mark.asyncio
async def test_cmd_logs_requires_owner_id_or_shows_id_only_hint(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=())
        await cmd_logs(_message(bot, 555), db=db, config=config, log_tail=_tail_with_records(0))
    finally:
        await db.close()
    text = _sent_text(bot)
    assert "555" in text
    # No server layout leaks: neither the env file name nor its directory
    assert ".env" not in text
    assert "/etc/mivta" not in text
    assert "/opt/mivta" not in text


@pytest.mark.asyncio
async def test_cmd_logs_denies_non_owner(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(1,))
        await cmd_logs(_message(bot, 999), db=db, config=config, log_tail=_tail_with_records())
    finally:
        await db.close()
    assert "record 0" not in _sent_text(bot)


@pytest.mark.asyncio
async def test_cmd_logs_shows_tail_for_owner(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(555,))
        await cmd_logs(_message(bot, 555), db=db, config=config, log_tail=_tail_with_records())
    finally:
        await db.close()
    text = _sent_text(bot)
    assert "record 0" in text
    assert "boom" in text
    assert "Логи" in text


@pytest.mark.asyncio
async def test_cmd_logs_says_empty_when_clean(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(555,))
        await cmd_logs(_message(bot, 555), db=db, config=config, log_tail=LogTail())
    finally:
        await db.close()
    assert "пусты" in _sent_text(bot)


@pytest.mark.asyncio
async def test_cmd_logs_without_tail_runtime(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(555,))
        await cmd_logs(_message(bot, 555), db=db, config=config, log_tail=None)
    finally:
        await db.close()
    assert "недоступны" in _sent_text(bot)


@pytest.mark.asyncio
async def test_logs_callback_refreshes_and_clears(tmp_path) -> None:
    from aiogram.types import CallbackQuery

    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(555,))
        tail = _tail_with_records(3)
        message = _message(bot, 555)
        callback = CallbackQuery(
            id="cb1",
            from_user=User(id=555, is_bot=False, first_name="Owner"),
            chat_instance="ci",
            data="svc:logs",
            message=message,
        ).as_(bot)
        await handle_owner_service_callback(callback, db=db, config=config, log_tail=tail)
        assert "record 0" in _sent_text(bot)

        clear = callback.model_copy(update={"id": "cb2", "data": "svc:logs_clear"})
        await handle_owner_service_callback(clear, db=db, config=config, log_tail=tail)
        assert len(tail) == 0
        assert "пусты" in _sent_text(bot)
    finally:
        await db.close()


class StubTranslator(BaseTranslator):
    """Minimal translator for the owner dashboard."""

    name = "google"

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        return TranslationResult(
            translated_text=request.text,
            source_lang="ru",
            target_lang=request.target_lang,
            provider=self.name,
            style=request.style,
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_owner_status_panel_has_logs_button(tmp_path) -> None:
    db = await _make_db(tmp_path)
    bot = CapturingBot()
    try:
        config = Config(bot_token="token", owner_ids=(555,))
        await cmd_status(
            _message(bot, 555),
            db=db,
            config=config,
            translator=StubTranslator(),
        )
    finally:
        await db.close()
    markup = bot.calls[-1].reply_markup
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("Логи" in label for label in labels)


# --------------------------------------------------------------------------- chunking


def test_split_never_breaks_verbatim_tokens() -> None:
    text = "Visit https://example.com/a/very/long/path?q=1 and 42 apples " * 6
    prepared = prepare_text_for_translation(text)
    chunks = split_for_translation(prepared.encoded_text, 60)
    assert not chunks.exceeds
    assert len(chunks.chunks) > 1
    for piece in chunks.chunks:
        # every placeholder that starts in a chunk also ends in it
        assert piece.count("MIVTAK") == piece.count("X")
    assert join_translated_chunks(chunks.chunks, chunks.separators) == prepared.encoded_text


def test_split_keeps_style_spans_intact() -> None:
    text = " ".join(["обычный текст про погоду и дела на сегодня"] * 8)
    entities = [
        MessageEntity(type="bold", offset=0, length=7),
        MessageEntity(type="italic", offset=100, length=10),
    ]
    prepared = prepare_text_for_translation(text, entities=entities)
    chunks = split_for_translation(prepared.encoded_text, 80)
    assert not chunks.exceeds
    assert len(chunks.chunks) > 1
    for piece in chunks.chunks:
        assert piece.count("<s") == piece.count("</s")
    assert join_translated_chunks(chunks.chunks, chunks.separators) == prepared.encoded_text


def test_split_hard_cuts_unbreakable_atoms() -> None:
    long_atom = "a" * 130
    chunks = split_for_translation(long_atom, 50)
    assert not chunks.exceeds
    assert chunks.chunks == ("a" * 50, "a" * 50, "a" * 30)
    assert chunks.separators == ("", "")


def test_split_respects_chunk_limit_and_flags_overflow() -> None:
    words = [f"w{index}" for index in range(400)]
    chunks = split_for_translation(" ".join(words), 40, max_chunks=5)
    assert chunks.exceeds
    assert chunks.chunks == ()

    ok = split_for_translation(" ".join(words), 400, max_chunks=5)
    assert not ok.exceeds
    assert len(ok.chunks) <= 5


def test_split_is_lossless_on_random_text() -> None:
    """Chunking never loses or invents characters, whatever the text looks like."""
    alphabet = "абвгд .,!?:\nhttps://x.y/z<>s0/s0\t—«»)"
    rng = random.Random(2024)
    for _ in range(400):
        source = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 300)))
        for max_length in (7, 23, 90, 400):
            chunks = split_for_translation(source, max_length)
            if chunks.exceeds or not chunks.chunks:
                continue
            assert join_translated_chunks(chunks.chunks, chunks.separators) == source
            assert len(chunks.separators) == max(0, len(chunks.chunks) - 1)


def test_split_short_text_is_single_chunk() -> None:
    chunks = split_for_translation("hello world", 500)
    assert chunks.chunks == ("hello world",)
    assert chunks.separators == ()
    assert split_for_translation("", 500).chunks == ()


# --------------------------------------------------------------------- chunked pipeline


@dataclass
class _FakeTranslator:
    """Fake provider that prefixes every chunk so we can verify the stitching."""

    name: str = "fake"
    uses_llm_glossary: bool = False
    calls: int = 0

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.calls += 1
        return TranslationResult(
            translated_text=f"[{request.text}]",
            source_lang="ru",
            target_lang=request.target_lang,
            provider=self.name,
            style=request.style,
        )

    async def aclose(self) -> None:
        return None


async def _run_chunked(text: str, max_length: int, max_chunks: int = 12):
    from mivta.handlers.pipeline import execute_chunked_translation

    translator = _FakeTranslator()
    cache = TranslationCache(max_size=256, ttl_seconds=60)
    limiter = RateLimiter(rate_per_minute=6000.0, burst=200)
    outcome = await execute_chunked_translation(
        user_id=1,
        request=TranslationRequest(text=text, source_lang="ru", target_lang="en"),
        translator=translator,
        cache=cache,
        rate_limiter=limiter,
        concurrency_sem=asyncio.Semaphore(4),
        inflight={},
        max_length=max_length,
        max_chunks=max_chunks,
    )
    return outcome, translator


@pytest.mark.asyncio
async def test_chunked_translation_stitches_pieces() -> None:
    text = " ".join(f"word{index}" for index in range(60))
    outcome, translator = await _run_chunked(text, 50)
    assert translator.calls > 1
    for word in ("word0", "word30", "word59"):
        assert word in outcome.translated_text
    assert outcome.translated_text.startswith("[word0 ")
    assert outcome.translated_text.endswith("word59]")
    assert translator.name in outcome.provider
    assert "×" in outcome.provider


@pytest.mark.asyncio
async def test_chunked_translation_short_text_uses_one_call() -> None:
    outcome, translator = await _run_chunked("короткий текст", 500)
    assert translator.calls == 1
    assert outcome.translated_text == "[короткий текст]"


@pytest.mark.asyncio
async def test_chunked_translation_reports_too_long() -> None:
    from mivta.handlers.pipeline import TextTooLong

    text = " ".join(f"word{index}" for index in range(400))
    outcome, translator = await _run_chunked(text, 40, max_chunks=5)
    assert isinstance(outcome, TextTooLong)
    assert outcome.length == len(text)
    assert outcome.max_chunks == 5
