"""In-memory ring buffer of recent log records for the owner-only `/logs` command.

mivta never logs message text (that is a hard product rule), so this buffer only
ever holds our own operational records: provider errors, restarts, config
warnings. Records are captured already redacted (the secret-scrubbing filter is
attached to the root logger before this handler is added) and are truncated to a
short line so a single log storm cannot flood the owner's chat.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_CAPACITY = 200
DEFAULT_MIN_LEVEL = logging.WARNING
MAX_LINE_LENGTH = 300

_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One captured log record, rendered as a single short line."""

    timestamp: str
    level: str
    logger: str
    message: str

    def render(self) -> str:
        return f"{self.timestamp} {self.level} {self.logger}: {self.message}"


class LogTail(logging.Handler):
    """Logging handler keeping the last `capacity` records of `min_level` and above."""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        min_level: int = DEFAULT_MIN_LEVEL,
    ) -> None:
        super().__init__(level=min_level)
        self._capacity = max(1, int(capacity))
        self._records: deque[LogEntry] = deque(maxlen=self._capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a broken %-format must never raise in logging
            message = "<unrenderable log record>"
        message = " ".join(message.split())
        if len(message) > MAX_LINE_LENGTH:
            message = message[: MAX_LINE_LENGTH - 1] + "…"
        self._records.append(
            LogEntry(
                timestamp=time.strftime("%H:%M:%S", time.localtime(record.created)),
                level=record.levelname,
                logger=record.name,
                message=message,
            )
        )

    def entries(
        self,
        limit: int = 30,
        *,
        min_level: str | None = None,
    ) -> tuple[LogEntry, ...]:
        """Most recent `limit` entries, optionally filtered by level name."""
        threshold = _LEVEL_ORDER.get((min_level or "").upper(), 0)
        selected = [
            entry for entry in self._records if _LEVEL_ORDER.get(entry.level, 0) >= threshold
        ]
        if limit <= 0:
            return tuple(selected)
        return tuple(selected[-limit:])

    def clear(self) -> None:
        self._records.clear()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._records)


def install_log_tail(
    logger: logging.Logger,
    *,
    capacity: int = DEFAULT_CAPACITY,
    min_level: int = DEFAULT_MIN_LEVEL,
) -> LogTail:
    """Attach a `LogTail` handler to `logger` and return it."""
    tail = LogTail(capacity=capacity, min_level=min_level)
    logger.addHandler(tail)
    return tail


def render_entries(entries: Sequence[LogEntry]) -> str:
    """Render entries as plain text lines (escaping happens in the handler)."""
    return "\n".join(entry.render() for entry in entries)
