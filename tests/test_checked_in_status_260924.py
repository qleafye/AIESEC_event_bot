"""Идея №4 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): «✅ Ты отмечен» в
«🎟 Мой QR» (`handlers/user_actions.py::show_my_checkin_qr`) и в хабе Mini App
(`miniapp/routers/hub.py`, `GET /app/api/hub`).

Координация владельца 24.09: ОДНА функция чтения (`database.db.get_checkin_status`) для обеих
поверхностей; строка видна ТОЛЬКО при `checkin_qr_enabled == "on"`; Mini App не пересобирался.

pytest-asyncio недоступна в этом окружении — каждый async-хелпер через `asyncio.run()`."""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
from handlers import user_actions as ua_mod
from tests._dbtpl import fast_init_db

UID = 924101


def _run(coro):
    return asyncio.run(coro)


def _use_tmp_db(tmp_path, name="test_checked_in_status_260924.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _seed_user(uid=UID, city="msk", full_name="Иванов Иван"):
    _run(db.add_user({
        "telegram_id": uid, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city,
    }))


def _set_status(uid, status):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, uid))
    conn.commit()
    conn.close()


async def _set_setting(key, value):
    await db.set_setting(key, value)


# ══════════════════════════════════════════════════════════════════════════════════════════
# database.db.get_checkin_status — единая функция чтения
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_get_checkin_status_none_when_no_entry(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    assert _run(db.get_checkin_status(UID)) is None


def test_get_checkin_status_returns_entry_time_and_zero_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    status = _run(db.get_checkin_status(UID))
    assert status == {"scanned_at": "2026-10-30 09:15:00", "sessions_count": 0}


def test_get_checkin_status_counts_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    sid1 = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    sid2 = _run(db.create_program_session("msk", "2026-10-30", "12:00", "13:00", "Трек"))
    _run(db.record_session_checkin(UID, sid1, [], source="miniapp", scanned_at="2026-10-30 10:05:00"))
    _run(db.record_session_checkin(UID, sid2, [], source="miniapp", scanned_at="2026-10-30 12:05:00"))
    status = _run(db.get_checkin_status(UID))
    assert status["sessions_count"] == 2


def test_get_checkin_status_none_for_unknown_user(tmp_path):
    _use_tmp_db(tmp_path)
    assert _run(db.get_checkin_status(999999)) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр: checked_in_status_text — не per_city, дефолт с плейсхолдерами
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_checked_in_status_text():
    from settings_schema import SETTINGS_SCHEMA

    entry = SETTINGS_SCHEMA["checked_in_status_text"]
    assert entry["type"] == "text"
    assert entry.get("per_city") is not True
    assert "{time}" in entry["default"]
    assert "{sessions}" in entry["default"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers.user_actions.show_my_checkin_qr — строка над подписью
# ══════════════════════════════════════════════════════════════════════════════════════════

class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, uid=UID):
        self.text = "🎟 Мой QR"
        self.chat = _FakeChat(uid)
        self.from_user = _FakeUser(uid)
        self.answers = []
        self.photos = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        return "sent"

    async def answer_photo(self, photo, caption=None, **kwargs):
        self.photos.append((photo, caption))
        return "sent-photo"


def test_qr_caption_has_no_status_line_when_not_checked_in(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(_set_setting("checkin_qr_enabled", "on"))
    message = _FakeMessage()
    _run(ua_mod.show_my_checkin_qr(message))
    assert message.photos
    _photo, caption = message.photos[0]
    assert "Отмечен" not in caption


def test_qr_caption_shows_status_line_when_checked_in(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    message = _FakeMessage()
    _run(ua_mod.show_my_checkin_qr(message))
    assert message.photos
    _photo, caption = message.photos[0]
    assert "09:15" in caption
    assert "сессий: 0" in caption


def test_qr_caption_status_line_reflects_sessions_count(tmp_path):
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _run(db.record_session_checkin(UID, sid, [], source="miniapp", scanned_at="2026-10-30 10:05:00"))
    message = _FakeMessage()
    _run(ua_mod.show_my_checkin_qr(message))
    _photo, caption = message.photos[0]
    assert "сессий: 1" in caption


def test_qr_status_line_absent_when_qr_module_off_even_if_checked_in(tmp_path):
    """checkin_qr_enabled выключен -- хендлер отдаёт текст «выключен», отметка вообще не
    читается (не только строка не показывается, а QR не выдаётся вовсе -- уже покрыто
    `tests/test_checkin_qr_260923.py`, здесь фиксируем отсутствие утечки в этой ветке)."""
    _use_tmp_db(tmp_path)
    _seed_user()
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    message = _FakeMessage()
    _run(ua_mod.show_my_checkin_qr(message))
    assert not message.photos
    assert message.answers
    assert "Отмечен" not in message.answers[0]


def test_qr_caption_translates_status_line_for_english_delegate(tmp_path):
    from services.i18n_form_manual import FORM_DEFAULT_EN, seed

    _use_tmp_db(tmp_path)
    _seed_user()
    _run(_set_setting("checkin_qr_enabled", "on"))
    _run(_set_setting("delegate_lang_enabled", "on"))
    _run(seed())
    _run(db.record_checkin(UID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET lang = 'en' WHERE telegram_id = ?", (UID,))
    conn.commit()
    conn.close()

    message = _FakeMessage()
    _run(ua_mod.show_my_checkin_qr(message))
    _photo, caption = message.photos[0]
    default_ru = "Отмечен на входе в {time} · сессий: {sessions}"
    expected_line = FORM_DEFAULT_EN[default_ru].replace("{time}", "09:15").replace("{sessions}", "0")
    assert expected_line in caption


# ══════════════════════════════════════════════════════════════════════════════════════════
# Mini App: GET /app/api/hub -> checkin_status_fact
# ══════════════════════════════════════════════════════════════════════════════════════════

from tests.test_miniapp_routes import (  # noqa: E402
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db as _use_tmp_dash_db,
)


def _hub_client(tmp_path, name="hub_checkin_status.db"):
    db_path = _use_tmp_dash_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def test_hub_checkin_status_fact_absent_by_default(tmp_path):
    client = _hub_client(tmp_path)
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["checkin_status_fact"] is None


def test_hub_checkin_status_fact_absent_when_qr_module_off_even_if_checked_in(tmp_path):
    client = _hub_client(tmp_path)
    _run(db.record_checkin(DELEGATE_ID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["checkin_status_fact"] is None


def test_hub_checkin_status_fact_present_when_qr_on_and_checked_in(tmp_path):
    client = _hub_client(tmp_path)
    _set("checkin_qr_enabled", "on")
    _run(db.record_checkin(DELEGATE_ID, db.CHECKIN_ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00"))
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["checkin_status_fact"]
    assert "09:15" in body["checkin_status_fact"]
    assert "0" in body["checkin_status_fact"]


def test_hub_checkin_status_fact_absent_when_qr_on_but_not_checked_in(tmp_path):
    client = _hub_client(tmp_path)
    _set("checkin_qr_enabled", "on")
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["checkin_status_fact"] is None


# ── Структурный сторож фронта: hub.js рисует новую строку из общего блока фактов ────────────

def test_hub_js_renders_checkin_status_fact():
    from tests.test_miniapp_frontend import MINIAPP_STATIC, _js_without_comments

    text = _js_without_comments(MINIAPP_STATIC / "js" / "screens" / "hub.js")
    assert "hub.checkin_status_fact" in text
