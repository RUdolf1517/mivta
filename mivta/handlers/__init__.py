"""Aiogram routers for Inline Mode, Secretary Mode, Group Mode, and preference commands."""

from aiogram import Router

from mivta.handlers.business import build_business_router
from mivta.handlers.commands import build_commands_router
from mivta.handlers.group import build_group_router
from mivta.handlers.inline import build_inline_router


def build_root_router() -> Router:
    """Assemble a fresh root Router containing inline, business, group, and command routers."""
    root = Router(name="root")
    root.include_router(build_inline_router())
    root.include_router(build_business_router())
    root.include_router(build_group_router())
    root.include_router(build_commands_router())
    return root


__all__ = [
    "build_business_router",
    "build_commands_router",
    "build_group_router",
    "build_inline_router",
    "build_root_router",
]
