"""Раздел «🤝 Амбассадоры» → экран «🙋 Кандидаты и команда» и общая сборка карточки заявки.

- карточка заявки собирается одной функцией (`admin_modcard_render.build_card_text`) для очереди
  «📋 Заявки» и для кнопки «🧾 Анкета» у кандидата;
- список пагинирован, фильтры кнопками, счётчик мест, «привёл / прошли отбор»;
- «✅ Взять» / «⏸ Не сейчас» / «🎁 Пакет выдан» / «🎟 Дать место» / «🚪 Вывести из команды»;
- выгрузка CSV без «@» и формул; права (анкета — moderate_reg).

pytest-asyncio нет — async через `asyncio.run()`; хендлеры зовутся напрямую с фейковыми
Message/CallbackQuery (приём `tests/test_amb_section_34.py`).
"""
from __future__ import annotations

import asyncio
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from tests._dbtpl import fast_init_db

SEASON = "RT26"
ADMIN_ID = 934101


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_candidates_34.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("event_season", SEASON))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, *, status="approved", amb_status=None, slot=False, pack=False, reserve=False,
          name=None, username=None, city=None, referrer=None, language=None):
    data = {
        "telegram_id": tid,
        "full_name": name or f"Delegate {tid}",
        "registration_date": "2026-09-01 00:00:00",
        "season": SEASON,
    }
    if username:
        data["username"] = username
    _run(db.add_user(data))
    if status:
        _run(db.set_user_status(tid, status))
    _sql(
        "UPDATE users SET ambassador_status = ?, is_ambassador = ?, ambassador_slot_at = ?, "
        "ambassador_pack_at = ?, ambassador_reserve_at = ?, ambassador_status_at = ?, "
        "event_city = COALESCE(?, event_city), referrer_id = ? WHERE telegram_id = ?",
        (amb_status, 1 if amb_status == "active" else 0,
         "2026-09-02 10:00:00" if slot else None,
         "2026-09-03 10:00:00" if pack else None,
         "2026-09-04 10:00:00" if reserve else None,
         f"2026-09-01 00:{tid % 60:02d}:00", city, referrer, tid),
    )
    if language:
        _sql("UPDATE users SET language = ? WHERE telegram_id = ?", (language, tid))


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.caption = None
        self.from_user = FakeUser(user_id)
        self.answers = []
        self.edits = []
        self.documents = []

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))
        return self

    async def answer_document(self, document, caption=None, **kw):
        self.documents.append((document, caption))
        return self


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _buttons(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


# ── общая сборка карточки заявки ────────────────────────────────────────────────────────

def test_build_card_text_matches_queue_card(tmp_path):
    from handlers import admin_moderation
    from handlers.admin_modcard_render import build_card_text
    _ready(tmp_path)
    _seed(500, status="pending", name="Иван <Петров>", username="ivan_p")
    msg = FakeMessage()
    _run(admin_moderation._show_current_card(msg, _new_state()))
    queue_text = msg.answers[-1][0]
    assert "Заявка 1/1" in queue_text

    user = _run(db.get_user(500))
    built = _run(build_card_text(user, position=1, total=1))
    assert built.text == queue_text
    assert built.overflow is False and built.has_history is False


def test_build_card_text_without_position_has_no_counter(tmp_path):
    from handlers.admin_modcard_render import build_card_text
    _ready(tmp_path)
    _seed(501, status="approved", name="Мария")
    user = _run(db.get_user(501))
    text = _run(build_card_text(user)).text
    assert text.startswith("📋 <b>Заявка</b>\n")
    assert text.split("\n", 1)[0] == "📋 <b>Заявка</b>"
    with_pos = _run(build_card_text(user, position=2, total=5)).text
    assert with_pos.startswith("📋 <b>Заявка 2/5</b>\n")
    assert with_pos.split("\n", 1)[1] == text.split("\n", 1)[1]
