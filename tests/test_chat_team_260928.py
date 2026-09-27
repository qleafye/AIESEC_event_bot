"""Кто «команда» в рейтинге чата.

Команда = суперадмины (ADMIN_IDS) + сотрудники бота с любой ролью, кроме волонтёра форума
(его выдают по ссылке-приглашению, часто делегатам) + админы группы, у которых есть реальные
права модерации: владелец или право удалять сообщения / ограничивать участников. Админ «ради
подписи» без прав — обычный участник: иначе он пропадает из рейтинга, а в режиме правил
каждое его сообщение становится «постом», на который можно набивать комментарии.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from database import db
from services import chat_tracking
from tests._dbtpl import fast_init_db

CHAT = -1009284001
BOT_ID = 777928401
OWNER, MOD, BANNER, TITLE_ONLY = 900928411, 900928412, 900928413, 900928414
REG_MANAGER, VOLUNTEER, SUPER = 900928421, 900928422, 900928423


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    path = str(tmp_path / "team.db")
    config.DB_PATH = path
    config.ADMIN_IDS = [SUPER]
    fast_init_db()
    return path


def _adm(uid, *, status="administrator", can_delete=False, can_restrict=False, is_bot=False):
    return SimpleNamespace(status=status, can_delete_messages=can_delete,
                           can_restrict_members=can_restrict,
                           user=SimpleNamespace(id=uid, is_bot=is_bot))


class _Bot:
    id = BOT_ID

    def __init__(self, admins):
        self.admins = admins

    async def get_chat_administrators(self, chat_id):
        return self.admins


def test_only_moderating_group_admins_are_stored(tmp_path):
    _ready(tmp_path)
    bot = _Bot([
        _adm(OWNER, status="creator"),
        _adm(MOD, can_delete=True),
        _adm(BANNER, can_restrict=True),
        _adm(TITLE_ONLY),  # админка ради кастомной подписи
        _adm(BOT_ID, can_delete=True, is_bot=True),
    ])
    _run(chat_tracking.refresh_chat_admins(bot, CHAT))

    async def _ids():
        async with db._connect() as conn:
            async with conn.execute("SELECT telegram_id FROM chat_admins WHERE chat_id = ?", (CHAT,)) as cur:
                return sorted(r[0] for r in await cur.fetchall())

    assert _run(_ids()) == sorted([OWNER, MOD, BANNER])
    assert _run(db.get_chat_bot_state(CHAT))["can_delete"] == 1


def test_team_excludes_volunteers_but_keeps_managers(tmp_path):
    path = _ready(tmp_path)
    conn = sqlite3.connect(path)
    for uid, role in ((REG_MANAGER, "reg_manager"), (VOLUNTEER, "volunteer"),
                      (MOD, "volunteer"), (MOD, "game_manager")):
        conn.execute("INSERT INTO staff (telegram_id, role, added_at) VALUES (?, ?, '2026-09-01')",
                     (uid, role))
    conn.commit()
    conn.close()
    with dash_db.read_conn(path) as ro:
        team = chat_rating.team_ids(ro, CHAT, {SUPER})
    assert SUPER in team and REG_MANAGER in team
    assert MOD in team  # волонтёр, но ещё и менеджер геймы
    assert VOLUNTEER not in team
