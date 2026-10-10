"""Идеи №31/№32: экраны бота «📓 Журнал площадки» и снятие отметки менеджером
(`handlers/forum/admin_venue.py`) + врезки журнала в `handlers/forum/admin_checkin.py` (перевыпуск QR,
загрузка CSV, кнопка входа). Фейковая обвязка — та же, что `tests/test_checkin_reissue_260924.py`
(хендлеры зовутся функцией, без Dispatcher)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.forum import admin_checkin, admin_venue
from services.forum.checkin import ENTRY_POINT, record_arrival
from tests._dbtpl import fast_init_db
from tests.test_checkin_reissue_260924 import _FakeCallback, _FakeMessage, _insert_user

ADMIN_ID = 910301
VOLUNTEER_ID = 910302
DELEGATE_ID = 910303
DAY = "2026-10-30"


class _FakeState:
    def __init__(self):
        self.state = None
        self.data = {}

    async def set_state(self, value):
        self.state = value

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_data(self, data):
        self.data = dict(data)


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin_venue_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_marked(tmp_path, *, session=False):
    _ready(tmp_path)
    _run(_insert_user(DELEGATE_ID, full_name="Иванов Иван"))
    user = _run(db.get_user(DELEGATE_ID))
    # Вход в день сессии: снятие входа предупреждает только о сессиях ЭТОГО дня.
    _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=VOLUNTEER_ID, staff_name="Анна (@anna)",
                        scanned_at=f"{DAY} 09:00:00" if session else None))
    if session:
        sid = _run(db.create_program_session("msk", DAY, "10:00", "11:00", "Открытие"))
        _run(record_arrival(user, f"session:{sid}", source="miniapp", by_staff_id=VOLUNTEER_ID,
                            scanned_at=f"{DAY} 10:05:00"))
        return sid
    return None


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _last_text(cb):
    if cb.message.edits:
        return cb.message.edits[-1]
    return cb.message.sent[-1][0]


# ── журнал ───────────────────────────────────────────────────────────────────────────────────

def test_log_screen_lists_human_lines(tmp_path):
    _seed_marked(tmp_path)
    text, kb = _run(admin_venue.render_log_screen(ADMIN_ID))
    assert "Журнал площадки" in text
    assert "Анна (@anna)" in text and "Иванов Иван" in text and "Вход" in text
    assert str(DELEGATE_ID) not in text
    data = [d for _t, d in _buttons(kb)]
    assert "vlogst" in data and "vrv_find" in data and "admin_checkin" in data


def test_log_screen_paginates(tmp_path):
    _ready(tmp_path)
    for i in range(12):
        _run(db.venue_log_add({"action": "reissue_qr", "staff_id": VOLUNTEER_ID, "staff_name": "Анна"}))
    text, kb = _run(admin_venue.render_log_screen(ADMIN_ID))
    assert "страница 1 из 2" in text
    assert ("Старше ➡️", "vlog:0:10") in _buttons(kb)
    text2, kb2 = _run(admin_venue.render_log_screen(ADMIN_ID, offset=10))
    assert "страница 2 из 2" in text2
    assert ("⬅️ Новее", "vlog:0:0") in _buttons(kb2)


def test_staff_filter_buttons_show_names(tmp_path):
    _seed_marked(tmp_path)
    cb = _FakeCallback("vlogst", ADMIN_ID)
    _run(admin_venue.venue_log_staff_pick(cb))
    assert "Чьи действия" in _last_text(cb)
    text, kb = _run(admin_venue.render_log_screen(ADMIN_ID, staff_id=VOLUNTEER_ID))
    assert "Волонтёр: Анна (@anna)" in text
    assert ("👥 Все волонтёры", "vlog:0:0") in _buttons(kb)


def test_log_needs_moderate_reg(tmp_path):
    _ready(tmp_path)
    cb = _FakeCallback("admin_venue_log", VOLUNTEER_ID)
    _run(admin_venue.venue_log_open(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert _run(admin_venue.venue_entry_rows(VOLUNTEER_ID)) == []
    assert _run(admin_venue.venue_entry_rows(ADMIN_ID))[0][0].callback_data == "admin_venue_log"


def test_checkin_screen_has_journal_button_for_manager(tmp_path):
    _ready(tmp_path)
    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    _run(admin_checkin.show_admin_checkin(cb))
    _text, kb = cb.message.sent[-1]
    assert ("📓 Журнал площадки", "admin_venue_log") in _buttons(kb)


# ── снятие отметки менеджером ────────────────────────────────────────────────────────────────

def test_search_then_marks_then_confirm_then_revoke(tmp_path):
    _seed_marked(tmp_path)
    state = _FakeState()
    _run(admin_venue.venue_revoke_find(_FakeCallback("vrv_find", ADMIN_ID), state))
    assert state.state is not None

    msg = _FakeMessage("Иванов", ADMIN_ID)
    _run(admin_venue.venue_revoke_find_step(msg, state))
    assert state.state is None
    kb = msg.answers[-1][2]
    assert ("Иванов Иван", f"vrv_u:{DELEGATE_ID}") in [(t.split(" · ")[0], d) for t, d in _buttons(kb)]

    cb = _FakeCallback(f"vrv_u:{DELEGATE_ID}", ADMIN_ID)
    _run(admin_venue.venue_revoke_user(cb))
    mark = _run(db.list_checkins_for_user(DELEGATE_ID))[0]
    assert "Отметки делегата" in _last_text(cb)

    cb2 = _FakeCallback(f"vrv_p:{mark['id']}", ADMIN_ID)
    _run(admin_venue.venue_revoke_confirm(cb2))
    confirm = _last_text(cb2)
    assert "Снять отметку?" in confirm and "не пришедшим" in confirm and "Пришёл" in confirm
    assert _run(db.list_checkins_for_user(DELEGATE_ID))  # подтверждение само ничего не снимает

    cb3 = _FakeCallback(f"vrv_go:{mark['id']}", ADMIN_ID)
    _run(admin_venue.venue_revoke_go(cb3))
    assert _run(db.list_checkins_for_user(DELEGATE_ID)) == []
    assert "Отметка снята" in _last_text(cb3)
    rows, _total = _run(db.venue_log_page())
    assert rows[0]["action"] == "revoke" and rows[0]["staff_id"] == ADMIN_ID


def test_revoke_entry_confirm_warns_about_sessions(tmp_path):
    _seed_marked(tmp_path, session=True)
    entry = next(r for r in _run(db.list_checkins_for_user(DELEGATE_ID)) if r["point"] == ENTRY_POINT)
    cb = _FakeCallback(f"vrv_p:{entry['id']}", ADMIN_ID)
    _run(admin_venue.venue_revoke_confirm(cb))
    assert "Отметки на сессиях (1) останутся" in _last_text(cb)


def test_revoke_session_confirm_mentions_feedback(tmp_path):
    sid = _seed_marked(tmp_path, session=True)
    mark = next(r for r in _run(db.list_checkins_for_user(DELEGATE_ID)) if r["point"] == f"session:{sid}")
    cb = _FakeCallback(f"vrv_p:{mark['id']}", ADMIN_ID)
    _run(admin_venue.venue_revoke_confirm(cb))
    text = _last_text(cb)
    assert "отзыв" in text and "Отметка на входе останется" in text


def test_revoke_twice_is_friendly(tmp_path):
    _seed_marked(tmp_path)
    mark = _run(db.list_checkins_for_user(DELEGATE_ID))[0]
    _run(admin_venue.venue_revoke_go(_FakeCallback(f"vrv_go:{mark['id']}", ADMIN_ID)))
    cb = _FakeCallback(f"vrv_go:{mark['id']}", ADMIN_ID)
    _run(admin_venue.venue_revoke_go(cb))
    assert cb.answers[-1] == ("Этой отметки уже нет — ничего не снято.", True)


def test_search_miss_explains_what_to_do(tmp_path):
    _ready(tmp_path)
    state = _FakeState()
    msg = _FakeMessage("Несуществующий", ADMIN_ID)
    _run(admin_venue.venue_revoke_find_step(msg, state))
    assert "Никого не нашёл" in msg.answers[-1][0]


# ── врезки журнала в admin_checkin ───────────────────────────────────────────────────────────

def test_reissue_qr_is_logged(tmp_path):
    _ready(tmp_path)
    _run(_insert_user(DELEGATE_ID, full_name="Иванов Иван"))
    _run(admin_checkin.checkin_reissue_go(_FakeCallback(f"checkin_reissue_yes:{DELEGATE_ID}", ADMIN_ID)))
    rows, total = _run(db.venue_log_page())
    assert total == 1
    assert rows[0]["action"] == "reissue_qr" and rows[0]["telegram_id"] == DELEGATE_ID
    assert rows[0]["staff_id"] == ADMIN_ID


def test_csv_upload_is_one_log_line(tmp_path):
    _ready(tmp_path)
    _run(_insert_user(DELEGATE_ID, full_name="Иванов Иван"))
    token = _run(db.get_or_create_checkin_token(DELEGATE_ID))
    from services.forum.checkin import build_payload, current_event_tag
    qr = build_payload(_run(current_event_tag()), "Иванов Иван", "Москва", token)
    state = _FakeState()
    # Второй скан — тот же день (вход каждый день: скан без времени лёг бы на день загрузки).
    state.data = {"checkin_records": [{"qr": qr, "scanned_at": "2026-10-30 09:10:00"}, {"qr": qr, "scanned_at": "2026-10-30 11:00:00"}]}
    _run(admin_checkin.checkin_point_pick(_FakeCallback(f"checkin_point:{ENTRY_POINT}", ADMIN_ID), state))
    rows, total = _run(db.venue_log_page())
    assert total == 1
    assert rows[0]["action"] == "csv_upload"
    assert rows[0]["details"]["new"] == 1 and rows[0]["details"]["duplicate"] == 1


def test_broken_callback_says_screen_is_stale(tmp_path):
    _ready(tmp_path)
    for handler, data in (
        (admin_venue.venue_revoke_user, "vrv_u:abc"),
        (admin_venue.venue_revoke_confirm, "vrv_p:"),
        (admin_venue.venue_revoke_go, "vrv_go"),
        (admin_venue.venue_log_page_cb, "vlog:1"),
    ):
        cb = _FakeCallback(data, ADMIN_ID)
        _run(handler(cb))
        assert cb.answers[-1] == ("Экран устарел — откройте журнал площадки заново.", True), data
