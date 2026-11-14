"""Tests for owner-only `/update` and `/restart` self-service (OWNER_ID gated)."""

from __future__ import annotations

import datetime as dt
import time
import types
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

from mivta import __version__
from mivta.bot import _register_localized_commands, create_dispatcher
from mivta.config import Config, ConfigError
from mivta.database import Database
from mivta.handlers import commands as commands_module
from mivta.service import (
    TRIGGER_FILENAME_PREFIX,
    CommandResult,
    RestartController,
    ServiceMarker,
    UpdateInfo,
    UpdatePlan,
    announce_pending_marker,
    apply_self_update,
    check_update,
    clear_update_triggers,
    detect_install_root,
    install_is_writable,
    marker_path,
    perform_restart,
    plan_update,
    read_marker,
    render_marker_result,
    run_detached,
    write_marker,
    write_update_trigger,
)


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
        if isinstance(method, EditMessageReplyMarkup):
            return Message(
                message_id=600,
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=1, type="private"),
                text="edited",
            )
        return True


class StubTranslator:
    """Minimal Translator duck-type sufficient for dispatcher wiring."""

    name = "google"

    def get_provider_statuses(self) -> tuple[Any, ...]:
        return ()

    def reset_cooldowns(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


SHA_A = "aaa1111abcdef1234567890abcdef1234567890ab"


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
        text="service placeholder",
    )
    cb = CallbackQuery(
        id=f"cb-{update_id}",
        from_user=user,
        chat_instance="ci-service",
        message=msg,
        data=data,
    )
    return Update(update_id=update_id, callback_query=cb)


def _config(tmp_path: Path, **overrides: Any) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "bot_token": "123456:ABCDEF",
        "owner_ids": (9001,),
        "database_path": str(data_dir / "mivta.db"),
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_parses_self_service_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:ABCDEF")
    cfg = Config.from_env(dotenv_path=tmp_path / "missing.env")
    assert cfg.self_update is True
    assert cfg.self_restart is True
    assert cfg.update_repo == "RUdolf1517/mivta"
    assert cfg.update_branch == "main"
    assert cfg.update_cmd == ()
    assert cfg.restart_cmd == ()

    monkeypatch.setenv("SELF_UPDATE", "false")
    monkeypatch.setenv("SELF_RESTART", "no")
    monkeypatch.setenv("MIVTA_UPDATE_CMD", "/usr/local/bin/mivta-update --quiet")
    monkeypatch.setenv("MIVTA_RESTART_CMD", "systemctl --user restart mivta")
    monkeypatch.setenv("MIVTA_UPDATE_REPO", "RUdolf1517/mivta-fork")
    monkeypatch.setenv("MIVTA_BRANCH", "release/0.7")
    cfg = Config.from_env(dotenv_path=tmp_path / "missing.env")
    assert cfg.self_update is False
    assert cfg.self_restart is False
    assert cfg.update_cmd == ("/usr/local/bin/mivta-update", "--quiet")
    assert cfg.restart_cmd == ("systemctl", "--user", "restart", "mivta")
    assert cfg.update_repo == "RUdolf1517/mivta-fork"
    assert cfg.update_branch == "release/0.7"

    monkeypatch.setenv("MIVTA_UPDATE_REPO", "not-a-slug")
    with pytest.raises(ConfigError, match="MIVTA_UPDATE_REPO"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")

    monkeypatch.setenv("MIVTA_UPDATE_REPO", "RUdolf1517/mivta")
    monkeypatch.setenv("MIVTA_BRANCH", "bad branch")
    with pytest.raises(ConfigError, match="MIVTA_BRANCH"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")

    monkeypatch.setenv("MIVTA_BRANCH", "main")
    monkeypatch.setenv("MIVTA_RESTART_CMD", 'unbalanced "quote')
    with pytest.raises(ConfigError, match="MIVTA_RESTART_CMD"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")

    # Shell pipelines are rejected: the command is executed without a shell
    monkeypatch.setenv("MIVTA_RESTART_CMD", "docker compose pull && docker compose up -d")
    with pytest.raises(ConfigError, match="shell operators"):
        Config.from_env(dotenv_path=tmp_path / "missing.env")


# ---------------------------------------------------------------------------
# Update checks
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records argv lists and returns canned CommandResults."""

    def __init__(self, mapping: dict[tuple[str, ...], CommandResult]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Any, **kwargs: Any) -> CommandResult:
        self.calls.append(list(argv))
        return self.mapping.get(tuple(argv), CommandResult(ok=False, error="unexpected argv"))


def _git_result(stdout: str = "", ok: bool = True) -> CommandResult:
    return CommandResult(ok=ok, stdout=stdout, returncode=0 if ok else 1)


@pytest.mark.asyncio
async def test_check_update_via_git_detects_new_commits(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    runner = FakeRunner(
        {
            (
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "fetch",
                "--depth",
                "1",
                "origin",
                "main",
            ): _git_result(),
            (
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "rev-parse",
                "HEAD",
            ): _git_result("aaa1111111111111111111111111111111111111"),
            (
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "rev-parse",
                "FETCH_HEAD",
            ): _git_result("bbb2222222222222222222222222222222222222"),
            (
                "git",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                "log",
                "--oneline",
                "-n",
                "8",
                "HEAD..FETCH_HEAD",
            ): _git_result("bbb2222 Add /update command\nbbb2221 Bump version"),
        }
    )

    info = await check_update(cfg, root=root, runner=runner)
    assert info.available is True
    assert info.source == "git"
    assert info.current_rev == "aaa111111111"
    assert info.latest_rev == "bbb222222222"
    assert info.commits == ("bbb2222 Add /update command", "bbb2221 Bump version")
    assert info.error is None


@pytest.mark.asyncio
async def test_check_update_git_up_to_date(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    base = ["git", "-c", f"safe.directory={root}", "-C", str(root)]
    runner = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(),
            (*base, "rev-parse", "HEAD"): _git_result(SHA_A),
            (*base, "rev-parse", "FETCH_HEAD"): _git_result(SHA_A),
            (*base, "log", "--oneline", "-n", "8", "HEAD..FETCH_HEAD"): _git_result(""),
        }
    )
    info = await check_update(cfg, root=root, runner=runner)
    assert info.available is False
    assert info.checked is True

    # A failed fetch still reports the local revision instead of raising
    broken = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(ok=False),
            (*base, "rev-parse", "HEAD"): _git_result("aaa1111abcdef1234567890abcdef1234567890ab"),
        }
    )
    info = await check_update(cfg, root=root, runner=broken)
    assert info.current_rev == "aaa1111abcde"
    assert info.error
    assert info.checked is False


@pytest.mark.asyncio
class _FlakyBot:
    """Bot stub that fails the first `failures` `get_me()` calls."""

    def __init__(self, failures: int, calls: list[int]) -> None:
        self.failures = failures
        self.calls = calls

    async def get_me(self) -> types.SimpleNamespace:
        self.calls.append(1)
        if len(self.calls) <= self.failures:
            raise OSError("telegram is down")
        return types.SimpleNamespace(username="mivtabot")


async def test_get_me_retries_then_succeeds() -> None:
    """A briefly unreachable Telegram must not kill startup."""
    import mivta.bot as bot_module

    calls: list[int] = []
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    original_sleep = bot_module.asyncio.sleep
    bot_module.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        me = await bot_module._get_me_with_retry(_FlakyBot(failures=2, calls=calls))
    finally:
        bot_module.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert me.username == "mivtabot"
    assert len(calls) == 3
    assert slept == [bot_module.STARTUP_BASE_DELAY, bot_module.STARTUP_BASE_DELAY * 2]


async def test_get_me_gives_up_after_budget() -> None:
    import mivta.bot as bot_module

    calls: list[int] = []

    async def _fake_sleep(_seconds: float) -> None:
        return None

    original_sleep = bot_module.asyncio.sleep
    bot_module.asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="telegram is down"):
            await bot_module._get_me_with_retry(
                _FlakyBot(failures=bot_module.STARTUP_ATTEMPTS, calls=calls)
            )
    finally:
        bot_module.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert len(calls) == bot_module.STARTUP_ATTEMPTS


async def test_parse_version_treats_prerelease_as_older() -> None:
    """`v7.3-rc1` must never advertise itself as newer than the `7.3` final release."""
    from mivta.service import _is_newer, _normalize_version, _parse_version

    assert _normalize_version("v7.3-rc1") == (7, 3)
    assert _parse_version("v7.3-rc1").is_prerelease is True
    assert _parse_version("v7.3").is_prerelease is False

    # Same numbers: the final release wins, the pre-release does not
    assert _is_newer(_parse_version("v7.3-rc1"), "7.3") is False
    assert _is_newer(_parse_version("v7.3"), "7.3-rc1") is True
    # A pre-release of the *next* version is still an update
    assert _is_newer(_parse_version("v7.4-rc1"), "7.3") is True
    assert _is_newer(_parse_version("v8.0-rc2"), "7.3") is True
    # Ordinary ordering is untouched
    assert _is_newer(_parse_version("v7.10"), "7.3") is True
    assert _is_newer(_parse_version("v7.2"), "7.3") is False
    assert _is_newer(_parse_version("v7.3"), "7.3") is False
    assert _is_newer(_parse_version("nonsense"), "7.3") is False


async def test_release_update_info_ignores_prerelease_suffix(tmp_path: Path) -> None:
    """A `-rc` tag must not flip `/update` into 'new version available'."""
    cfg = _config(tmp_path)

    async def fake_prerelease(repo: str, timeout: float) -> dict[str, Any] | None:
        return {"tag_name": "v7.3-rc1", "html_url": "https://example.invalid/rc1"}

    info = await check_update(cfg, root=tmp_path, fetch_release=fake_prerelease)
    assert info.source == "release"
    assert info.latest_version == "7.3"
    assert info.available is False

    async def fake_next_rc(repo: str, timeout: float) -> dict[str, Any] | None:
        return {"tag_name": "v99.1-rc1", "html_url": "https://example.invalid/next"}

    info = await check_update(cfg, root=tmp_path, fetch_release=fake_next_rc)
    assert info.latest_version == "99.1"
    assert info.available is True


async def test_check_update_falls_back_to_github_release(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    newer = {"tag_name": "v99.1.0", "html_url": "https://github.com/x/y/releases/tag/v99.1.0"}
    older = {"tag_name": "v0.0.1", "html_url": "https://github.com/x/y/releases/tag/v0.0.1"}

    async def fake_fetch(repo: str, timeout: float) -> dict[str, Any] | None:
        assert repo == "RUdolf1517/mivta"
        return newer

    info = await check_update(cfg, root=tmp_path, fetch_release=fake_fetch)
    assert info.source == "release"
    assert info.available is True
    assert info.latest_version == "99.1.0"
    assert info.release_url == "https://github.com/x/y/releases/tag/v99.1.0"

    async def fake_older(repo: str, timeout: float) -> dict[str, Any] | None:
        return older

    info = await check_update(cfg, root=tmp_path, fetch_release=fake_older)
    assert info.available is False

    async def fake_error(repo: str, timeout: float) -> dict[str, Any] | None:
        raise RuntimeError("network is down")

    info = await check_update(cfg, root=tmp_path, fetch_release=fake_error)
    assert info.error is not None
    assert info.available is False


# ---------------------------------------------------------------------------
# Update strategies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_update_prefers_command_then_self_then_trigger_then_manual(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _config(tmp_path, update_cmd=("docker", "compose", "up", "-d"))
    assert (await plan_update(cfg)).method == "command"

    # Root-owned install (the `install.sh` layout) + systemd path unit -> trigger
    cfg = _config(tmp_path)
    root = tmp_path / "opt"
    (root / ".git").mkdir(parents=True)
    (root / "mivta").mkdir(parents=True)
    monkeypatch.setattr("mivta.service.install_is_writable", lambda *_a, **_k: False)
    monkeypatch.setenv("MIVTA_UPDATE_TRIGGER", "1")
    assert (await plan_update(cfg, root=root)).method == "trigger"

    monkeypatch.setenv("MIVTA_UPDATE_TRIGGER", "0")
    assert (await plan_update(cfg, root=root)).method == "manual"

    # Writable checkout without dependency changes -> self-update
    monkeypatch.delenv("MIVTA_UPDATE_TRIGGER", raising=False)
    monkeypatch.setattr("mivta.service.install_is_writable", lambda *_a, **_k: True)
    base = ["git", "-c", f"safe.directory={root}", "-C", str(root)]
    runner = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(),
            (
                *base,
                "diff",
                "--name-only",
                "HEAD",
                "FETCH_HEAD",
                "--",
                "requirements.txt",
                "pyproject.toml",
            ): _git_result(""),
        }
    )
    assert (await plan_update(cfg, root=root, runner=runner)).method == "self"

    # Dependency files changed -> refuse to touch the venv, ask for root
    runner = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(),
            (
                *base,
                "diff",
                "--name-only",
                "HEAD",
                "FETCH_HEAD",
                "--",
                "requirements.txt",
                "pyproject.toml",
            ): _git_result("requirements.txt"),
        }
    )
    plan = await plan_update(cfg, root=root, runner=runner)
    assert plan.method == "manual"
    assert plan.reason == "dependencies"


@pytest.mark.asyncio
async def test_apply_self_update_requires_fast_forward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    base = ["git", "-c", f"safe.directory={root}", "-C", str(root)]
    monkeypatch.setattr("mivta.service.install_is_writable", lambda *_a, **_k: True)

    runner = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(),
            (*base, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"): _git_result(ok=False),
        }
    )
    result = await apply_self_update(cfg, root=root, runner=runner)
    assert result.ok is False
    assert "fast-forward" in (result.error or "")

    runner = FakeRunner(
        {
            (*base, "fetch", "--depth", "1", "origin", "main"): _git_result(),
            (*base, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"): _git_result(),
            (*base, "checkout", "-f", "FETCH_HEAD"): _git_result("ok"),
        }
    )
    result = await apply_self_update(cfg, root=root, runner=runner)
    assert result.ok is True


def test_update_trigger_matches_systemd_glob(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    trigger = write_update_trigger(cfg)
    assert trigger is not None
    assert trigger.name.startswith(TRIGGER_FILENAME_PREFIX)
    assert trigger.match(f"{TRIGGER_FILENAME_PREFIX}*")
    assert trigger.parent == Path(cfg.database_path).parent
    assert trigger.read_text(encoding="utf-8").startswith("update requested")

    assert clear_update_triggers(cfg) == 1
    assert list(Path(cfg.database_path).parent.glob(f"{TRIGGER_FILENAME_PREFIX}*")) == []


def test_install_root_detection_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)
    target = tmp_path / "opt" / "mivta"
    (target / ".git").mkdir(parents=True)
    (target / "mivta").mkdir(parents=True)
    monkeypatch.setenv("MIVTA_INSTALL_DIR", str(target))
    assert detect_install_root(cfg) == target
    assert install_is_writable(target) is True
    assert install_is_writable(None) is False

    monkeypatch.delenv("MIVTA_INSTALL_DIR", raising=False)
    assert detect_install_root(cfg) is not None  # dev checkout or cwd


# ---------------------------------------------------------------------------
# Restart marker & controller
# ---------------------------------------------------------------------------


def test_marker_roundtrip_and_stale_marker_is_dropped(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    path = marker_path(cfg)
    assert path is not None

    marker = ServiceMarker(
        action="update",
        chat_id=9001,
        message_id=555,
        from_version="0.6.0",
        requested_at=time.time(),
        from_rev="aaa1111",
    )
    assert write_marker(cfg, marker) == path
    loaded = read_marker(cfg)
    assert loaded == marker

    stale = ServiceMarker(
        action="restart",
        chat_id=9001,
        message_id=556,
        from_version=__version__,
        requested_at=time.time() - 10_000,
    )
    write_marker(cfg, stale)
    assert read_marker(cfg) is None
    assert not path.exists()

    # Corrupted payloads are discarded instead of crashing startup
    path.write_text("{not json", encoding="utf-8")
    assert read_marker(cfg) is None


def test_render_marker_result_reports_version_change() -> None:
    marker = ServiceMarker(
        action="update",
        chat_id=9001,
        message_id=1,
        from_version="0.6.0",
        requested_at=time.time(),
    )
    text = render_marker_result(marker, "ru")
    assert "Обновление применено" in text
    assert "0.6.0" in text and f"v{__version__}" in text

    same = ServiceMarker(
        action="update",
        chat_id=9001,
        message_id=1,
        from_version=__version__,
        requested_at=time.time(),
    )
    assert "версия осталась" in render_marker_result(same, "ru")

    restart = ServiceMarker(
        action="restart",
        chat_id=9001,
        message_id=1,
        from_version=__version__,
        requested_at=time.time(),
    )
    assert "Restarted" in render_marker_result(restart, "en")
    assert "Перезапущен" in render_marker_result(restart, "ru")


def test_restart_controller_invokes_stop_hook_once(tmp_path: Path) -> None:
    stops: list[str] = []
    controller = RestartController()
    controller.bind_stop(lambda: stops.append("stop"))
    assert controller.pending is False

    marker = ServiceMarker(
        action="restart",
        chat_id=9001,
        message_id=1,
        from_version=__version__,
        requested_at=time.time(),
    )
    controller.request(marker)
    assert stops == ["stop"]
    assert controller.pending is True
    assert controller.take() == marker
    assert controller.take() is None
    assert controller.pending is False


@pytest.mark.asyncio
async def test_announce_pending_marker_edits_owner_message(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    marker = ServiceMarker(
        action="update",
        chat_id=9001,
        message_id=777,
        from_version="0.6.0",
        requested_at=time.time(),
    )
    write_marker(cfg, marker)

    class EditingBot:
        def __init__(self) -> None:
            self.edits: list[dict[str, Any]] = []

        async def edit_message_text(self, **kwargs: Any) -> bool:
            self.edits.append(kwargs)
            return True

    bot = EditingBot()
    assert await announce_pending_marker(bot, cfg) is True
    assert len(bot.edits) == 1
    assert bot.edits[0]["chat_id"] == 9001
    assert bot.edits[0]["message_id"] == 777
    assert "Обновление применено" in bot.edits[0]["text"]
    # Marker is consumed: a second startup does not re-notify
    assert await announce_pending_marker(bot, cfg) is False
    assert len(bot.edits) == 1


def test_run_detached_uses_argv_without_shell() -> None:
    assert run_detached(["/bin/true"]).ok is True
    missing = run_detached(["definitely-not-a-binary-xyz42"])
    assert missing.ok is False
    assert "definitely-not-a-binary-xyz42" in (missing.error or "")


def test_perform_restart_prefers_external_command_then_re_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executed: list[str] = []

    def fake_sleep(_seconds: float) -> None:
        executed.append("slept")

    def fake_self_exec() -> None:
        executed.append("exec")

    monkeypatch.setattr("mivta.service.time.sleep", fake_sleep)
    monkeypatch.setattr("mivta.service._self_exec", fake_self_exec)

    spawned: list[tuple[str, ...]] = []

    def fake_run_detached(argv: Any) -> CommandResult:
        spawned.append(tuple(argv))
        return CommandResult(ok=True, returncode=0)

    monkeypatch.setattr("mivta.service.run_detached", fake_run_detached)

    # With an external restart command we hand over and only fall back after a timeout
    perform_restart(_config(tmp_path, restart_cmd=("systemctl", "restart", "mivta")))
    assert spawned == [("systemctl", "restart", "mivta")]
    assert executed == ["slept", "exec"]

    executed.clear()
    spawned.clear()
    perform_restart(_config(tmp_path))
    assert spawned == []
    assert executed == ["exec"]


# ---------------------------------------------------------------------------
# Dispatcher wiring: `/update`, `/restart` and `svc:*` callbacks
# ---------------------------------------------------------------------------


def _make_dispatcher(
    tmp_path: Path,
    db: Database,
    config: Config,
    restart: RestartController | None = None,
) -> Any:
    return create_dispatcher(
        config=config,
        db=db,
        translator=StubTranslator(),  # type: ignore[arg-type]
        restart=restart,
        bot_username="mivtabot",
    )


@pytest.mark.asyncio
async def test_update_and_restart_are_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(tmp_path / "data" / "svc.db")
    await db.connect()
    try:
        async def fake_check(config: Config) -> UpdateInfo:
            return UpdateInfo(available=True, latest_version="99.0.0", source="release")

        monkeypatch.setattr(commands_module, "check_update", fake_check)

        # 1) No OWNER_ID configured -> caller only learns their own Telegram ID
        cfg_no_owner = _config(tmp_path, owner_ids=())
        dp = _make_dispatcher(tmp_path, db, cfg_no_owner)
        bot = CapturingBot()
        await dp.feed_update(bot, _private_msg_update(1, user_id=555111, text="/update"))
        hint = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "OWNER_ID=555111" in hint.text
        assert "99.0.0" not in hint.text

        bot.calls.clear()
        await dp.feed_update(bot, _private_msg_update(2, user_id=555111, text="/restart"))
        hint = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "OWNER_ID=555111" in hint.text

        # 2) Stranger is denied and never reaches the update check
        cfg = _config(tmp_path, owner_ids=(9001,))
        dp = _make_dispatcher(tmp_path, db, cfg)
        bot.calls.clear()
        await dp.feed_update(bot, _private_msg_update(3, user_id=555111, text="/update"))
        deny = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Доступ запрещён" in deny.text

        bot.calls.clear()
        await dp.feed_update(
            bot, _callback_update(4, user_id=555111, data="svc:restart", lang_code="en")
        )
        alert = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery)][-1]
        assert alert.show_alert is True
        assert marker_path(cfg) is not None
        assert not marker_path(cfg).exists()  # type: ignore[union-attr]

        # 3) Owner sees the update offer with an apply button
        bot.calls.clear()
        await dp.feed_update(bot, _private_msg_update(5, user_id=9001, text="/update"))
        offer = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Доступно обновление" in offer.text
        assert offer.reply_markup is not None
        button_data = [
            btn.callback_data
            for row in offer.reply_markup.inline_keyboard
            for btn in row
        ]
        assert "svc:update" in button_data
        assert "svc:restart" in button_data
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_confirmation_hands_request_to_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(tmp_path / "data" / "svc2.db")
    await db.connect()
    try:
        cfg = _config(tmp_path, owner_ids=(9001,))
        controller = RestartController()
        stopped: list[bool] = []
        controller.bind_stop(lambda: stopped.append(True))
        dp = _make_dispatcher(tmp_path, db, cfg, restart=controller)
        bot = CapturingBot()

        await dp.feed_update(bot, _private_msg_update(1, user_id=9001, text="/restart"))
        prompt = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Перезапустить" in prompt.text
        assert controller.pending is False  # /restart only asks for confirmation

        bot.calls.clear()
        await dp.feed_update(
            bot, _callback_update(2, user_id=9001, data="svc:restart", lang_code="ru")
        )
        assert stopped == [True]
        assert controller.pending is True
        marker = controller.take()
        assert marker is not None
        assert marker.action == "restart"
        assert marker.chat_id == 9001
        assert marker.from_version == __version__
        persisted = read_marker(cfg)
        assert persisted is not None and persisted.action == "restart"

        edited = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Перезапускаю" in edited.text

        # Cancel simply removes the keyboard
        bot.calls.clear()
        await dp.feed_update(
            bot, _callback_update(3, user_id=9001, data="svc:cancel", lang_code="ru")
        )
        assert any(isinstance(c, EditMessageReplyMarkup) for c in bot.calls)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_callback_uses_systemd_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(tmp_path / "data" / "svc3.db")
    await db.connect()
    try:
        cfg = _config(tmp_path, owner_ids=(9001,))
        controller = RestartController()
        dp = _make_dispatcher(tmp_path, db, cfg, restart=controller)
        bot = CapturingBot()

        async def fake_plan(config: Config) -> UpdatePlan:
            return UpdatePlan("trigger")

        monkeypatch.setattr(commands_module, "plan_update", fake_plan)

        await dp.feed_update(
            bot, _callback_update(1, user_id=9001, data="svc:update", lang_code="ru")
        )
        triggers = list(Path(cfg.database_path).parent.glob(f"{TRIGGER_FILENAME_PREFIX}*"))
        assert len(triggers) == 1
        assert triggers[0].stat().st_mode & 0o777 == 0o600

        marker = read_marker(cfg)
        assert marker is not None and marker.action == "update"
        edited = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "systemd" in edited.text
        # The process is restarted externally, so the loop is never stopped by us
        assert controller.pending is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_callback_self_method_applies_and_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(tmp_path / "data" / "svc4.db")
    await db.connect()
    try:
        cfg = _config(tmp_path, owner_ids=(9001,))
        controller = RestartController()
        stopped: list[bool] = []
        controller.bind_stop(lambda: stopped.append(True))
        dp = _make_dispatcher(tmp_path, db, cfg, restart=controller)
        bot = CapturingBot()

        async def fake_plan(config: Config) -> UpdatePlan:
            return UpdatePlan("self")

        applied: list[Config] = []

        async def fake_apply(config: Config) -> CommandResult:
            applied.append(config)
            return CommandResult(ok=True, stdout="HEAD is now at bbb2222")

        monkeypatch.setattr(commands_module, "plan_update", fake_plan)
        monkeypatch.setattr(commands_module, "apply_self_update", fake_apply)

        await dp.feed_update(
            bot, _callback_update(1, user_id=9001, data="svc:update", lang_code="en")
        )
        assert applied == [cfg]
        assert stopped == [True]
        assert controller.pending is True
        marker = controller.take()
        assert marker is not None and marker.action == "update"
        edited = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Code updated" in edited.text

        # A failing self-update keeps the service running and explains the manual path
        bot.calls.clear()

        async def failing_apply(config: Config) -> CommandResult:
            return CommandResult(ok=False, error="permission denied")

        monkeypatch.setattr(commands_module, "apply_self_update", failing_apply)
        await dp.feed_update(
            bot, _callback_update(2, user_id=9001, data="svc:update", lang_code="en")
        )
        failure = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Self-update failed" in failure.text
        assert "install.sh --self-update" in failure.text
        assert controller.pending is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_recheck_button_and_group_chat_are_handled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(tmp_path / "data" / "svc6.db")
    await db.connect()
    try:
        cfg = _config(tmp_path, owner_ids=(9001,))
        dp = _make_dispatcher(tmp_path, db, cfg, restart=RestartController())
        bot = CapturingBot()

        checks: list[int] = []

        async def fake_check(config: Config) -> UpdateInfo:
            checks.append(1)
            return UpdateInfo(
                available=True,
                latest_version="99.0.0",
                source="release",
                release_url="https://github.com/RUdolf1517/mivta/releases/tag/v99.0.0",
            )

        monkeypatch.setattr(commands_module, "check_update", fake_check)

        # `svc:check` re-runs the check and refreshes the same message
        await dp.feed_update(bot, _callback_update(1, user_id=9001, data="svc:check"))
        assert checks == [1]
        edited = [c for c in bot.calls if isinstance(c, EditMessageText)][-1]
        assert "Доступно обновление" in edited.text
        assert "v99.0.0" in edited.text

        # A failed check explains the manual upgrade path instead of failing silently
        async def failing_check(config: Config) -> UpdateInfo:
            return UpdateInfo(source="release", error="ConnectError: no network")

        monkeypatch.setattr(commands_module, "check_update", failing_check)
        bot.calls.clear()
        await dp.feed_update(bot, _private_msg_update(2, user_id=9001, text="/update"))
        failed = [c for c in bot.calls if isinstance(c, SendMessage)][-1]
        assert "Не удалось проверить обновления" in failed.text
        assert "install.sh --self-update" in failed.text

        # `/update` and `/restart` are private-chat only: a group message is ignored
        bot.calls.clear()
        group_msg = Message(
            message_id=900,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=-100777, type="supergroup", title="Team"),
            from_user=User(id=9001, is_bot=False, first_name="Owner"),
            text="/update",
        )
        await dp.feed_update(bot, Update(update_id=3, message=group_msg))
        assert [c for c in bot.calls if isinstance(c, SendMessage)] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_self_service_disabled_by_env_flags(tmp_path: Path) -> None:
    db = Database(tmp_path / "data" / "svc5.db")
    await db.connect()
    try:
        cfg = _config(tmp_path, owner_ids=(9001,), self_update=False, self_restart=False)
        dp = _make_dispatcher(tmp_path, db, cfg, restart=RestartController())
        bot = CapturingBot()

        await dp.feed_update(bot, _private_msg_update(1, user_id=9001, text="/update"))
        await dp.feed_update(bot, _private_msg_update(2, user_id=9001, text="/restart"))
        texts = [c.text for c in bot.calls if isinstance(c, SendMessage)]
        assert any("SELF_UPDATE=false" in t for t in texts)
        assert any("SELF_RESTART=false" in t for t in texts)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_command_menu_exposes_update_and_restart() -> None:
    class MenuBot:
        def __init__(self) -> None:
            self.scoped: list[tuple[Any, list[str]]] = []

        async def set_my_commands(
            self,
            commands: list[Any],
            scope: Any = None,
            language_code: str | None = None,
        ) -> bool:
            self.scoped.append((scope, [c.command for c in commands]))
            return True

    fake = MenuBot()
    await _register_localized_commands(fake, owner_ids=(9001,))  # type: ignore[arg-type]
    owner_lists = [cmds for scope, cmds in fake.scoped if getattr(scope, "chat_id", None) == 9001]
    assert owner_lists
    for cmds in owner_lists:
        assert {"status", "update", "restart"} <= set(cmds)
    other = [cmds for scope, cmds in fake.scoped if getattr(scope, "chat_id", None) != 9001]
    for cmds in other:
        assert "update" not in cmds
        assert "restart" not in cmds
