"""UI-фиксы экрана «✅ Отметки на форуме» (24.09): строка «Рассылка QR» объясняет, почему
автоматическая рассылка не стоит (выключена / нет даты / дата с ошибкой / форум прошёл), а кнопки
городов подписаны словами, а не одними иконками.

Фикстуры — тот же приём, что `tests/test_admin_checkin_260924.py` (`_FakeCallback`,
`asyncio.run`, БД — шаблонная копия `tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import cities
from config import config
from database import db
from database.db import _connect
from handlers import admin_checkin
from tests._dbtpl import fast_init_db

ADMIN_ID = 910301


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin_checkin_labels_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeCallbackMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeCallbackMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _screen():
    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    return cb.message.sent[0]


def _fix_today(monkeypatch, day="01.10.2026"):
    monkeypatch.setattr(admin_checkin, "msk_now", lambda: datetime.strptime(day, "%d.%m.%Y"))


# ── Причина, почему рассылка QR не поставлена ────────────────────────────────────────────────

def test_qr_line_explains_disabled_broadcast(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _fix_today(monkeypatch)
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))
    asyncio.run(db.set_setting("checkin_qr_broadcast_enabled", "off"))
    text, _kb = _screen()
    assert "автоматическая рассылка выключена" in text
    assert "⚙️ Настройки QR" in text


def test_qr_line_explains_missing_date(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _fix_today(monkeypatch)
    text, _kb = _screen()
    assert "дата форума не задана — рассылка не поставлена" in text


def test_qr_line_explains_past_forum(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _fix_today(monkeypatch, "05.10.2026")
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))
    text, _kb = _screen()
    assert "форум уже прошёл" in text


def test_qr_line_silent_when_broadcast_scheduled(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _fix_today(monkeypatch)
    asyncio.run(db.set_setting("forum_date", "03.10.2026"))
    text, _kb = _screen()
    assert "QR получили 0 · подтвердили 0" in text
    for label in admin_checkin._QR_NOT_SCHEDULED_LABELS.values():
        assert label not in text


def test_every_schedule_reason_has_human_label():
    for reason in ("no_date", "disabled", "bad_date", "past"):
        assert admin_checkin._QR_NOT_SCHEDULED_LABELS[reason]


# ── Кнопки городов подписаны словами ─────────────────────────────────────────────────────────

async def _insert_approved(telegram_id, city):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, event_city) VALUES (?, ?, ?, ?)",
            (telegram_id, f"Тест {telegram_id}", "approved", city),
        )
        await conn.commit()


def test_all_cities_buttons_have_words_and_city_name(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _fix_today(monkeypatch)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__msk", "Москва"))
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    asyncio.run(_insert_approved(1, "msk"))

    _text, kb = _screen()
    by_cb = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    msk = admin_checkin._encode_city("msk")
    assert by_cb[f"checkinqr_send:{msk}"] == "📤 Разослать QR сейчас — Москва"
    assert by_cb[f"checkinqr_cfg:{msk}"] == "⚙️ Настройки QR — Москва"
    assert by_cb[f"cna_send:{msk}"] == "📨 Написать не пришедшим — Москва"
    for row in kb.inline_keyboard:
        assert len(row) == 1  # длинные подписи — по одной кнопке в ряд
        for b in row:
            assert len(b.callback_data.encode("utf-8")) <= 64
