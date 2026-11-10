"""Aiogram routers for Inline Mode and user preference commands."""

from aiogram import Router

from mivta.handlers.commands import build_commands_router
from mivta.handlers.inline import build_inline_router


def build_root_router() -> Router:
    """Assemble a fresh root Router containing inline and command routers."""
    root = Router(name="root")
    root.include_router(build_inline_router())
    root.include_router(build_commands_router())
    return root


__all__ = ["build_commands_router", "build_inline_router", "build_root_router"]
