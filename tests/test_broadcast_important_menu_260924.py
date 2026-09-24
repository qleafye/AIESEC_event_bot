"""Форум-ночь п.7 (D-XX, «❗ Важное»): пункт главного меню со списком важных рассылок делегату
за сегодня — показывается ТОЛЬКО когда сегодня была хотя бы одна важная рассылка ему/его
городу (`database.db.has_important_today`, тот же приём, что у menu_faq/has_faq_for_city в
`keyboards/builders.py::get_main_menu_kb`).

pytest-asyncio недоступен — `asyncio.run()`, БД в `tmp_path` (конвенция `tests/_dbtpl.py`).
"""
import asyncio
from datetime import datetime

from config import config
from database import db
from handlers import user_actions as ua
from keyboards.builders import get_main_menu_kb, MENU_TEXTS
from tests._dbtpl import fast_init_db

ADMIN_ID = 900950
DELEGATE_ID = 900951


def _ready(tmp_path, name="important_menu.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


async def _add_delegate(uid):
    await db.add_user({
        "telegram_id": uid, "full_name": "Delegate",
        "registration_date": "2026-09-24 00:00:00",
    })


async def _seed_important_delivery(admin_id, chat_id, text, sent_at):
    bid = await db.create_broadcast(admin_id, text[:80], 1, important=True, full_text=text)
    await db.record_broadcast_delivery(bid, chat_id, 1)
    # record_broadcast_delivery стампит sent_at=msk_now() -- перезаписываем нужным временем
    # напрямую, тот же приём, что и у остальных сидов дат в проекте (см. _dbtpl миграции).
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE broadcast_deliveries SET sent_at = ? WHERE broadcast_id = ? AND chat_id = ?",
            (sent_at, bid, chat_id),
        )
        await conn.commit()
    return bid


# ── Гейт кнопки меню ─────────────────────────────────────────────────────────────────────

def test_menu_important_hidden_when_nothing_today(tmp_path):
    async def go():
        _ready(tmp_path)
        await _add_delegate(DELEGATE_ID)
        kb = await get_main_menu_kb(DELEGATE_ID)
        texts = [b.text for row in kb.keyboard for b in row]
        assert "❗ Важное" not in texts

    asyncio.run(go())


def test_menu_important_shown_after_important_broadcast_today(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add_delegate(DELEGATE_ID)
        await _seed_important_delivery(
            ADMIN_ID, DELEGATE_ID, "Важная новость", "2026-09-24 10:00:00",
        )
        monkeypatch.setattr(
            "keyboards.builders.msk_now", lambda: datetime(2026, 9, 24, 15, 0),
        )
        kb = await get_main_menu_kb(DELEGATE_ID)
        texts = [b.text for row in kb.keyboard for b in row]
        assert "❗ Важное" in texts

    asyncio.run(go())


def test_menu_important_hidden_for_yesterdays_broadcast(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add_delegate(DELEGATE_ID)
        await _seed_important_delivery(
            ADMIN_ID, DELEGATE_ID, "Вчерашняя новость", "2026-09-23 10:00:00",
        )
        monkeypatch.setattr(
            "keyboards.builders.msk_now", lambda: datetime(2026, 9, 24, 15, 0),
        )
        kb = await get_main_menu_kb(DELEGATE_ID)
        texts = [b.text for row in kb.keyboard for b in row]
        assert "❗ Важное" not in texts

    asyncio.run(go())


def test_menu_important_no_telegram_id_hidden(tmp_path):
    async def go():
        _ready(tmp_path)
        kb = await get_main_menu_kb()
        texts = [b.text for row in kb.keyboard for b in row]
        assert "❗ Важное" not in texts

    asyncio.run(go())


# ── Экран делегата: список важных за сегодня ────────────────────────────────────────────

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.chat = FakeChat(uid)
        self.answers = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(text)


def test_show_important_today_lists_texts_and_times(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add_delegate(DELEGATE_ID)
        await db.set_user_status(DELEGATE_ID, "approved")
        await _seed_important_delivery(
            ADMIN_ID, DELEGATE_ID, "Первая важная новость", "2026-09-24 09:15:00",
        )
        await _seed_important_delivery(
            ADMIN_ID, DELEGATE_ID, "Вторая важная новость", "2026-09-24 14:30:00",
        )
        monkeypatch.setattr(ua, "msk_now", lambda: datetime(2026, 9, 24, 15, 0))

        msg = FakeMessage(DELEGATE_ID)
        await ua.show_important_today(msg)

        assert len(msg.answers) == 1
        text = msg.answers[0]
        assert "Первая важная новость" in text
        assert "09:15" in text
        assert "Вторая важная новость" in text
        assert "14:30" in text

    asyncio.run(go())


def test_show_important_today_empty_state(tmp_path, monkeypatch):
    async def go():
        _ready(tmp_path)
        await _add_delegate(DELEGATE_ID)
        await db.set_user_status(DELEGATE_ID, "approved")
        monkeypatch.setattr(ua, "msk_now", lambda: datetime(2026, 9, 24, 15, 0))

        msg = FakeMessage(DELEGATE_ID)
        await ua.show_important_today(msg)

        assert msg.answers == ["Сегодня важных рассылок не было."]

    asyncio.run(go())


def test_menu_important_key_registered_for_route():
    assert "menu_important" in MENU_TEXTS
    assert "❗ Важное" in MENU_TEXTS["menu_important"]
