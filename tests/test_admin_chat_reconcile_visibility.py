"""Пункт «🔄 Сверить состав чата» в разделе «🔧 Управление» показывается только при
привязанном чате делегатов (тот же критерий `chat_tracking.bound_chats`, что у хендлера)."""
from __future__ import annotations

import asyncio

from handlers.settings.admin_sections import build_section_keyboard
from services import chat_tracking
from tests.test_chat_refresh_admin_260914 import ADMIN_ID, _ready

CHAT_ID = -1004489658418
CALLBACK = "admin_chat_reconcile"


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_hidden_without_bound_chat(tmp_path):
    _ready(tmp_path)
    kb = asyncio.run(build_section_keyboard("manage", ADMIN_ID))
    cbs = _callbacks(kb)
    assert CALLBACK not in cbs
    assert "admin_menu" in cbs  # раздел отрисован, пропал только этот пункт


def test_shown_with_bound_chat(tmp_path):
    _ready(tmp_path)

    async def go():
        await chat_tracking.bind_chat(None, CHAT_ID, "Чат делегатов", None)
        return await build_section_keyboard("manage", ADMIN_ID)

    kb = asyncio.run(go())
    texts = [b.text for row in kb.inline_keyboard for b in row if b.callback_data == CALLBACK]
    assert texts == ["🔄 Сверить состав чата"]
