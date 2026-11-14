"""Owner-only self-service: update checks, update triggers and restart requests.

Everything here is `OWNER_ID`-gated at the handler level (see `handlers.commands`):
strangers never trigger a single subprocess, and nothing here runs unless
`SELF_UPDATE` / `SELF_RESTART` are enabled (both default to `true`).

Update strategies, tried in this order:

1. `MIVTA_UPDATE_CMD`   — an explicit argv (no shell) for exotic setups (Docker, k8s).
2. `self`               — the bot owns its install dir, so it runs `git fetch` itself
                          and re-execs (only when dependency files are untouched).
3. `trigger`            — the classic `install.sh` layout where code is root-owned:
                          the bot drops a marker into `data/`, and the privileged
                          `mivta-update.path` systemd unit runs `install.sh --self-update`.
4. `manual`             — nothing available; the bot prints the exact command to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from mivta import __version__
from mivta.config import Config

logger = logging.getLogger(__name__)

DEFAULT_UPDATE_REPO = "RUdolf1517/mivta"
GIT_TIMEOUT_SECONDS = 25.0
GIT_QUICK_TIMEOUT_SECONDS = 10.0
RELEASE_TIMEOUT_SECONDS = 8.0

TRIGGER_FILENAME_PREFIX = ".update-request-"
MARKER_FILENAME = ".self-service.json"
MARKER_MAX_AGE_SECONDS = 3_600.0
DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml")


def _now() -> float:
    return time.time()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of an allowlisted subprocess call (argv only, never a shell)."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str = ""

    @property
    def detail(self) -> str:
        return (self.stderr or self.error or self.stdout).strip()[:300]


async def run_command(
    argv: Sequence[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> CommandResult:
    """Run an allowlisted command in a worker thread and captured output."""

    def _run() -> CommandResult:
        try:
                proc = subprocess.run(  # argv list only, never shell=True
                [str(part) for part in argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=None if cwd is None else str(cwd),
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(ok=False, error=f"timeout after {int(timeout)}s")
        except FileNotFoundError as exc:
            return CommandResult(ok=False, error=f"command not found: {exc}")
        except OSError as exc:
            return CommandResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return CommandResult(
            ok=proc.returncode == 0,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            returncode=proc.returncode,
        )

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Install layout detection
# ---------------------------------------------------------------------------


def detect_install_root(config: Config) -> Path | None:
    """Best-effort location of the mivta checkout (`install.sh` + `.git`)."""
    candidates: list[Path] = []
    override = os.environ.get("MIVTA_INSTALL_DIR", "").strip()
    if override:
        candidates.append(Path(override))
    with contextlib.suppress(OSError):
        candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parent.parent)
    for candidate in candidates:
        with contextlib.suppress(OSError):
            if (candidate / "install.sh").is_file() or (candidate / ".git").exists():
                return candidate
    return None


def install_is_writable(root: Path | None) -> bool:
    """True when the bot process may rewrite its own code (dev/Docker layouts)."""
    if root is None:
        return False
    with contextlib.suppress(OSError):
        return os.access(root, os.W_OK) and os.access(root / "mivta", os.W_OK)
    return False


def data_dir(config: Config) -> Path | None:
    """Writable directory holding the SQLite DB, markers and update triggers."""
    raw = (config.database_path or "").strip()
    if not raw or raw.startswith("file:"):
        return None
    path = Path(raw).expanduser()
    directory = path if path.is_dir() else path.parent
    with contextlib.suppress(OSError):
        if directory.is_dir() and os.access(directory, os.W_OK):
            return directory
    return None


def systemd_update_path_unit(config: Config) -> Path | None:
    """Path of the privileged `mivta-update.path` trigger unit, if installed."""
    if not Path("/run/systemd/system").is_dir():
        return None
    service = os.environ.get("MIVTA_SERVICE_NAME", "").strip() or "mivta"
    unit = Path("/etc/systemd/system") / f"{service}-update.path"
    return unit if unit.is_file() else None


def update_trigger_available(config: Config) -> bool:
    """True when the bot may request a privileged update via a marker file."""
    raw = os.environ.get("MIVTA_UPDATE_TRIGGER", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return data_dir(config) is not None
    if raw in ("0", "false", "no", "off"):
        return False
    return data_dir(config) is not None and systemd_update_path_unit(config) is not None


def write_update_trigger(config: Config) -> Path | None:
    """Drop a one-shot trigger file consumed by the root `*-update.path` unit."""
    directory = data_dir(config)
    if directory is None:
        return None
    name = f"{TRIGGER_FILENAME_PREFIX}{int(_now())}-{secrets.token_hex(3)}"
    path = directory / name
    try:
        path.write_text(f"update requested at {_now():.0f}\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        return None
    return path


def clear_update_triggers(config: Config) -> int:
    """Remove pending trigger files (used after a successful update)."""
    directory = data_dir(config)
    if directory is None:
        return 0
    removed = 0
    with contextlib.suppress(OSError):
        for path in directory.glob(f"{TRIGGER_FILENAME_PREFIX}*"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# Update availability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """Result of an update check (git-based when possible, GitHub release otherwise)."""

    available: bool = False
    current_version: str = __version__
    latest_version: str | None = None
    release_url: str | None = None
    published_at: str | None = None
    current_rev: str | None = None
    latest_rev: str | None = None
    commits: tuple[str, ...] = ()
    source: str = "none"
    error: str | None = None

    @property
    def checked(self) -> bool:
        return self.source in ("git", "release") and self.error is None


def _normalize_version(raw: str) -> tuple[int, ...]:
    cleaned = (raw or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.replace("-", ".").replace("+", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits[:9]))
    return tuple(parts)


def _is_newer(candidate: tuple[int, ...], current: str) -> bool:
    if not candidate:
        return False
    cur = _normalize_version(current)
    width = max(len(candidate), len(cur))
    padded = candidate + (0,) * (width - len(candidate))
    current_padded = cur + (0,) * (width - len(cur))
    return padded > current_padded


async def check_update(
    config: Config,
    *,
    root: Path | None = None,
    runner: Callable[..., Any] = run_command,
    fetch_release: Callable[..., Any] | None = None,
) -> UpdateInfo:
    """Check for a newer revision locally (git) or a newer GitHub release."""
    resolved_root = root if root is not None else detect_install_root(config)
    git_info = await _git_update_info(config, resolved_root, runner)
    if git_info is not None:
        return git_info
    return await _release_update_info(config, fetch_release)


async def _git_update_info(
    config: Config,
    root: Path | None,
    runner: Callable[..., Any],
) -> UpdateInfo | None:
    if root is None or shutil.which("git") is None:
        return None
    if not (root / ".git").exists():
        return None

    base = ["git", "-c", f"safe.directory={root}", "-C", str(root)]
    branch = config.update_branch or "main"
    fetch = await runner(
        [*base, "fetch", "--depth", "1", "origin", branch],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    head = await runner([*base, "rev-parse", "HEAD"], timeout=GIT_QUICK_TIMEOUT_SECONDS)
    if not head.ok:
        return None
    if not fetch.ok:
        return UpdateInfo(
            current_rev=(head.stdout.strip()[:12] or None),
            source="git",
            error=(fetch.stderr or fetch.error or "git fetch failed")[:200],
        )

    latest = await runner(
        [*base, "rev-parse", "FETCH_HEAD"], timeout=GIT_QUICK_TIMEOUT_SECONDS
    )
    log = await runner(
        [*base, "log", "--oneline", "-n", "8", "HEAD..FETCH_HEAD"],
        timeout=GIT_QUICK_TIMEOUT_SECONDS,
    )
    current_rev = head.stdout.strip()[:12] or None
    latest_rev = latest.stdout.strip()[:12] or None
    commits = tuple(line.strip() for line in log.stdout.splitlines() if line.strip())
    return UpdateInfo(
        available=bool(current_rev and latest_rev and current_rev != latest_rev),
        current_rev=current_rev,
        latest_rev=latest_rev,
        commits=commits[:8],
        source="git",
    )


async def _release_update_info(
    config: Config,
    fetch_release: Callable[..., Any] | None = None,
) -> UpdateInfo:
    fetch = fetch_release or fetch_latest_release
    try:
        payload = await fetch(config.update_repo, RELEASE_TIMEOUT_SECONDS)
    except Exception as exc:  # network errors must never break /update
        return UpdateInfo(source="release", error=f"{type(exc).__name__}: {exc}"[:200])
    if not isinstance(payload, dict) or not payload:
        return UpdateInfo(source="release", error="empty release response")

    tag = str(payload.get("tag_name") or "").strip()
    latest = _normalize_version(tag)
    url = payload.get("html_url")
    published = payload.get("published_at")
    return UpdateInfo(
        available=_is_newer(latest, __version__),
        latest_version=".".join(str(p) for p in latest) or tag.lstrip("vV") or None,
        release_url=str(url) if url else None,
        published_at=str(published)[:10] if published else None,
        source="release",
    )


async def fetch_latest_release(repo: str, timeout: float) -> dict[str, Any] | None:
    """Fetch the latest GitHub release payload (imported lazily to keep startup lean)."""
    import httpx  # optional network path, only used by /update

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"mivta/{__version__}"}
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Applying an update
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """How `/update` will be carried out: `self`, `trigger`, `command` or `manual`."""

    method: str
    reason: str = ""


async def plan_update(
    config: Config,
    *,
    root: Path | None = None,
    runner: Callable[..., Any] = run_command,
) -> UpdatePlan:
    """Pick the safest available update strategy for this installation."""
    if config.update_cmd:
        return UpdatePlan("command")
    resolved_root = root if root is not None else detect_install_root(config)
    if (
        resolved_root is not None
        and install_is_writable(resolved_root)
        and shutil.which("git") is not None
        and (resolved_root / ".git").exists()
    ):
        changed = await _dependency_files_changed(config, resolved_root, runner)
        if changed:
            return UpdatePlan("manual", reason="dependencies")
        return UpdatePlan("self")
    if update_trigger_available(config):
        return UpdatePlan("trigger")
    return UpdatePlan("manual")


async def _dependency_files_changed(
    config: Config,
    root: Path,
    runner: Callable[..., Any],
) -> bool:
    base = ["git", "-c", f"safe.directory={root}", "-C", str(root)]
    fetch = await runner(
        [*base, "fetch", "--depth", "1", "origin", config.update_branch or "main"],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if not fetch.ok:
        return True
    diff = await runner(
        [*base, "diff", "--name-only", "HEAD", "FETCH_HEAD", "--", *DEPENDENCY_FILES],
        timeout=GIT_QUICK_TIMEOUT_SECONDS,
    )
    return bool(diff.stdout.strip())


async def apply_self_update(
    config: Config,
    *,
    root: Path | None = None,
    runner: Callable[..., Any] = run_command,
) -> CommandResult:
    """Fast-forward the checkout we own (never called on root-owned installs)."""
    resolved_root = root if root is not None else detect_install_root(config)
    if resolved_root is None or not install_is_writable(resolved_root):
        return CommandResult(ok=False, error="install directory is not writable")
    base = ["git", "-c", f"safe.directory={resolved_root}", "-C", str(resolved_root)]
    fetch = await runner(
        [*base, "fetch", "--depth", "1", "origin", config.update_branch or "main"],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if not fetch.ok:
        return fetch
    ancestor = await runner(
        [*base, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"],
        timeout=GIT_QUICK_TIMEOUT_SECONDS,
    )
    if not ancestor.ok:
        return CommandResult(ok=False, error="remote history is not a fast-forward")
    return await runner(
        [*base, "checkout", "-f", "FETCH_HEAD"], timeout=GIT_TIMEOUT_SECONDS
    )


def run_detached(argv: Sequence[str]) -> CommandResult:
    """Fire-and-forget an allowlisted restart/update command (no shell, no pipes)."""
    try:
        subprocess.Popen(  # argv list only, never shell=True
            [str(part) for part in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return CommandResult(ok=True, returncode=0)


# ---------------------------------------------------------------------------
# Restart marker (survives the restart so the bot can confirm it came back)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceMarker:
    """A pending `restart` / `update` request recorded before the process dies."""

    action: str
    chat_id: int
    message_id: int
    from_version: str
    requested_at: float
    from_rev: str | None = None


def marker_path(config: Config) -> Path | None:
    directory = data_dir(config)
    return None if directory is None else directory / MARKER_FILENAME


def write_marker(config: Config, marker: ServiceMarker) -> Path | None:
    """Persist a restart/update request atomically inside the writable data dir."""
    path = marker_path(config)
    if path is None:
        return None
    payload = {
        "action": marker.action,
        "chat_id": marker.chat_id,
        "message_id": marker.message_id,
        "from_version": marker.from_version,
        "requested_at": marker.requested_at,
        "from_rev": marker.from_rev,
    }
    tmp = path.with_name(f".{MARKER_FILENAME}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return None
    return path


def read_marker(
    config: Config,
    *,
    now: float | None = None,
    max_age: float = MARKER_MAX_AGE_SECONDS,
) -> ServiceMarker | None:
    """Read (and discard) the pending marker; stale markers are dropped silently."""
    path = marker_path(config)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
        marker = ServiceMarker(
            action=str(payload["action"])[:16],
            chat_id=int(payload["chat_id"]),
            message_id=int(payload["message_id"]),
            from_version=str(payload.get("from_version") or "")[:32],
            requested_at=float(payload["requested_at"]),
            from_rev=(
                str(payload["from_rev"])[:40]
                if payload.get("from_rev") is not None
                else None
            ),
        )
    except (ValueError, TypeError, KeyError):
        clear_marker(config)
        return None

    stamp = _now() if now is None else now
    if max_age > 0 and (stamp - marker.requested_at) > max_age:
        clear_marker(config)
        return None
    return marker


def clear_marker(config: Config) -> None:
    path = marker_path(config)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def render_marker_result(marker: ServiceMarker, lang: str) -> str:
    """Short post-restart report sent to the owner's original message."""
    en = lang == "en"
    if marker.action == "update":
        if marker.from_version and marker.from_version != __version__:
            return (
                f"✅ Update applied: <code>v{_esc(marker.from_version)}</code> → "
                f"<code>v{_esc(__version__)}</code>."
                if en
                else f"✅ Обновление применено: <code>v{_esc(marker.from_version)}</code> → "
                f"<code>v{_esc(__version__)}</code>."
            )
        return (
            f"⚠️ Restarted after an update request, but the version is still "
            f"<code>v{_esc(__version__)}</code> — check the update unit logs."
            if en
            else f"⚠️ Перезапустился после запроса обновления, но версия осталась "
            f"<code>v{_esc(__version__)}</code> — проверьте логи юнита обновления."
        )
    return (
        f"✅ Restarted · <code>v{_esc(__version__)}</code> is back online."
        if en
        else f"✅ Перезапущен · снова в сети, версия <code>v{_esc(__version__)}</code>."
    )


def _esc(text: str) -> str:
    return escape(text, quote=False)


async def announce_pending_marker(bot: Any, config: Config) -> bool:
    """After a restart/update, confirm the outcome in the owner's original message."""
    marker = read_marker(config)
    if marker is None:
        return False
    clear_marker(config)
    text = render_marker_result(marker, config.default_ui_lang)
    try:
        await bot.edit_message_text(
            chat_id=marker.chat_id,
            message_id=marker.message_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception:  # a report must never break startup
        logger.warning(
            "could not report %s result to chat %s",
            marker.action,
            marker.chat_id,
            exc_info=True,
        )
        return False
    logger.info("reported %s result to chat %s", marker.action, marker.chat_id)
    return True


class RestartController:
    """Hands a restart request from a handler back to the runner loop."""

    def __init__(self) -> None:
        self._pending: ServiceMarker | None = None
        self._stop: Callable[[], None] | None = None

    def bind_stop(self, stop: Callable[[], None]) -> None:
        """Register the graceful-stop hook (polling stop or webhook stop_event)."""
        self._stop = stop

    def request(self, marker: ServiceMarker) -> None:
        """Record the request and gracefully stop the update loop."""
        self._pending = marker
        if self._stop is not None:
            with contextlib.suppress(Exception):
                self._stop()

    def take(self) -> ServiceMarker | None:
        marker, self._pending = self._pending, None
        return marker

    @property
    def pending(self) -> bool:
        return self._pending is not None


def perform_restart(config: Config) -> None:
    """Restart in place: an external command if configured, otherwise re-exec self."""
    if config.restart_cmd:
        result = run_detached(config.restart_cmd)
        if result.ok:
            time.sleep(10.0)
    _self_exec()


def _self_exec() -> None:
    """Replace the current process with a fresh one (PID and systemd unit survive)."""
    argv = [sys.executable, "-m", "mivta", *sys.argv[1:]]
    try:
        os.execv(sys.executable, argv)  # re-exec, no shell involved
    except OSError:
        os.execv(sys.argv[0], [sys.argv[0], *sys.argv[1:]])  # last resort



