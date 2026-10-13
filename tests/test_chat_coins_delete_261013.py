"""13.10: разобранный «+N» гейм-менеджера бот убирает из общего чата (подтверждение делегату —
в личку), реакция 👌 — только когда убрать нельзя."""
from __future__ import annotations

import sqlite3

from config import config
from database import db
from handlers.chat import group_chat
from tests.test_chat_coins_261009 import (  # noqa: F401 — env — фикстура соседнего модуля
    CHAT, DELEGATE, MANAGER, _Bot, _coins, _msg, _run, env,
)


class _DelBot(_Bot):
    def __init__(self, delete_error: Exception | None = None, **kw):
        super().__init__(**kw)
        self.deleted: list[tuple[int, int]] = []
        self.delete_error = delete_error

    async def delete_message(self, chat_id, message_id, **kw):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append((chat_id, message_id))

    async def get_chat_member(self, chat_id, user_id):
        raise RuntimeError("не спрашивали")


def _award(bot, text="+5 за мем"):
    target = _msg(10, DELEGATE, "мой мем", username="delegate_one")
    _run(group_chat.on_group_message(_msg(11, MANAGER, text, reply_to=target), bot))


def test_award_removes_plus_from_chat_without_reaction(env):
    bot = _DelBot()
    _award(bot)
    assert _coins() == [(DELEGATE, 5, "за мем — в чате", MANAGER, "chat")]
    assert bot.deleted == [(CHAT, 11)]
    assert bot.reactions == []
    assert any(cid == DELEGATE for cid, _ in bot.sent)  # делегат узнал в личке
    assert all(cid != CHAT for cid, _ in bot.sent)


def test_toggle_off_keeps_plus_and_reacts(env):
    _run(db.set_setting("chat_coins_delete_command", "off"))
    bot = _DelBot()
    _award(bot)
    assert bot.deleted == []
    assert bot.reactions == [(CHAT, 11, "👌")]


def test_no_delete_right_falls_back_to_reaction_without_api_call(env):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_bot_state (chat_id, bot_status, can_delete, checked_at) "
            "VALUES (?, 'member', 0, '2026-10-13 20:00:00')", (CHAT,),
        )
    bot = _DelBot()
    _award(bot)
    assert bot.deleted == []
    assert bot.reactions == [(CHAT, 11, "👌")]


def test_delete_failure_falls_back_to_reaction(env):
    bot = _DelBot(delete_error=RuntimeError("Bad Request: message can't be deleted"))
    _award(bot)
    assert [r[1] for r in _coins()] == [5]
    assert bot.reactions == [(CHAT, 11, "👌")]


def test_refused_plus_is_removed_too(env):
    bot = _DelBot()
    _run(group_chat.on_group_message(_msg(11, MANAGER, "+5000", reply_to=_msg(10, DELEGATE, "x")), bot))
    assert _coins() == []
    assert bot.deleted == [(CHAT, 11)]
    assert any(cid == MANAGER and "от 1 до 1000" in t for cid, t in bot.sent)
