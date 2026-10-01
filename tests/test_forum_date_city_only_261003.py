"""Дата форума — только своя у города (форумы 03.10): общий `forum_date`, записанный с шапкой
«🌍 Все города», больше не достаётся городам без своей даты. Иначе Москва получала QR
«Завтра форум!» и опрос неявившихся за чужой региональный форум.

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
import services.checkin_broadcast as cb
import services.scheduler as sched
from services.reject_rules import forum_date_for
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, _fresh_state

ADMIN_ID = 261003001


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, cities_on=True):
    config.DB_PATH = str(tmp_path / "forum_date_city_only.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()
    if cities_on:
        _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("event_season", "YL 26/2"))


def _seed(tid, city):
    _run(db.add_user({
        "telegram_id": tid, "full_name": f"D{tid}",
        "registration_date": "2026-09-01 00:00:00", "event_city": city,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status='approved', season='YL 26/2' WHERE telegram_id=?", (tid,))
    conn.commit()
    conn.close()


class _Sched:
    def __init__(self):
        self.jobs = {}

    def get_job(self, jid):
        return self.jobs.get(jid)

    def add_job(self, fn, trigger, run_date=None, args=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = (fn, run_date, args)

    def remove_job(self, jid):
        self.jobs.pop(jid, None)

    def get_jobs(self):
        return []


def test_city_without_own_date_ignores_common_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    assert _run(forum_date_for("msk")) is None
    assert _run(forum_date_for(None)) is None  # делегат без города = город по умолчанию
    assert _run(forum_date_for("spb")) == "03.10.2026"
    # Возрастное правило автоотказа по-прежнему видит общую дату.
    assert _run(forum_date_for("msk", inherit_common=True)) == "03.10.2026"


def test_cities_off_common_date_is_the_date(tmp_path):
    _ready(tmp_path, cities_on=False)
    _run(db.set_setting("forum_date", "03.10.2026"))
    assert _run(forum_date_for(None)) == "03.10.2026"


def test_qr_broadcast_not_scheduled_for_city_without_own_date(tmp_path, monkeypatch):
    """Город без своей даты + общая дата -> QR ему не ставится и не уходит."""
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _seed(1, "msk")
    _seed(2, "spb")
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    from datetime import datetime
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 1, 12, 0))
    res = _run(cb.schedule_city_jobs("msk"))
    assert res == {"scheduled": False, "reason": "no_date"}
    assert cb.evening_job_id("msk") not in fake.jobs
    assert _run(cb.schedule_city_jobs("spb"))["scheduled"] is True
    assert cb.evening_job_id("spb") in fake.jobs
    # Даже если джоба Москвы каким-то образом сработает — «не тот день», ничего не уходит.
    sent = []

    async def _send(city):
        sent.append(city)
        return {"sent": 1}

    monkeypatch.setattr(cb, "send_broadcast", _send)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    out = _run(cb._run_evening_job("msk"))
    assert out.get("skipped") == "wrong_day" and sent == []


def test_all_cities_header_asks_for_city_instead_of_writing_common(tmp_path):
    from cities import ALL_CITIES, set_admin_city
    from handlers.admin_settings import EditSetting, settings_edit_start

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, ALL_CITIES))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    state = _fresh_state(ADMIN_ID)
    cbq = FakeCallback("settings_edit:forum_date", user_id=ADMIN_ID)
    _run(settings_edit_start(cbq, state))
    # Ввод не ляжет в общий ключ: состояние только отвечает «сначала выберите город».
    assert _run(state.get_data()) == {"forum_date_pick_city": True}
    assert "для какого города" in cbq.message.text
    cbs = [b.callback_data for row in cbq.message.markup.inline_keyboard for b in row]
    assert "settings_edit_city:forum_date@spb" in cbs and "settings_edit_city:forum_date@msk" in cbs
    labels = [b.text for row in cbq.message.markup.inline_keyboard for b in row]
    assert any("03.10.2026" in t for t in labels)
    assert EditSetting.waiting_for_value is not None


def test_city_pick_switches_header_and_edits_city_key(tmp_path):
    from cities import ALL_CITIES, admin_selected_city, set_admin_city
    from handlers.admin_settings import settings_edit_city as forum_city_key_edit

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, ALL_CITIES))
    state = _fresh_state(ADMIN_ID)
    cbq = FakeCallback("settings_edit_city:forum_date@tyumen", user_id=ADMIN_ID)
    _run(forum_city_key_edit(cbq, state))
    assert _run(admin_selected_city(ADMIN_ID)) == "tyumen"
    assert _run(state.get_data())["setting_key"] == "forum_date__city__tyumen"


def test_city_pick_rejects_unknown_key_and_foreign_city(tmp_path):
    from handlers.admin_settings import settings_edit_city as forum_city_key_edit

    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    bad = FakeCallback("settings_edit_city:event_season@spb", user_id=ADMIN_ID)
    _run(forum_city_key_edit(bad, state))
    assert bad.answers and bad.answers[0][1] is True
    other = 261003999  # менеджер, привязанный к СПб, не правит Тюмень
    _run(db.add_staff(other, "manager", ADMIN_ID))
    assert _run(db.set_staff_city(other, "spb"))
    foreign = FakeCallback("settings_edit_city:forum_date@tyumen", user_id=other)
    other_state = _fresh_state(other)
    _run(forum_city_key_edit(foreign, other_state))
    assert foreign.answers[0][1] is True
    assert _run(other_state.get_state()) is None


def test_ready_screen_buttons_edit_the_traffic_light_city(tmp_path):
    """Светофор Тюмени при шапке «СПб» правит Тюмень — город в callback, не из шапки."""
    from handlers import admin_forum_ready as afr

    _ready(tmp_path)
    row = _run(afr._row_forum_date("tyumen"))
    assert row["fix"][1] == "settings_edit_city:forum_date@tyumen"
    _run(db.set_setting("forum_date__city__tyumen", "03.10.2099"))
    _run(db.set_setting("sos_active_days__city__tyumen", "2"))
    row = _run(afr._row_forum_date("tyumen"))
    cbs = [c for _label, c in row["fix"]]
    assert cbs == ["settings_edit_city:forum_date@tyumen", "settings_edit_city:sos_active_days@tyumen"]
    assert row["light"] == afr.YELLOW and "дольше одного дня" in row["text"]
    _run(db.set_setting("sos_active_days__city__tyumen", "1"))
    assert _run(afr._row_forum_date("tyumen"))["light"] == afr.GREEN


def test_city_buttons_need_settings_right():
    """Кнопки с городом идут тем же маршрутом прав, что «✏️ Изменить для города»."""
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="settings_edit_city:forum_date@spb") == "settings"


def test_date_typed_instead_of_city_button_gets_hint(tmp_path):
    from cities import ALL_CITIES, set_admin_city
    from handlers.admin_settings import settings_edit_start, settings_edit_value
    from tests.test_roles_phase8 import FakeMessage

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, ALL_CITIES))
    state = _fresh_state(ADMIN_ID)
    _run(settings_edit_start(FakeCallback("settings_edit:forum_date", user_id=ADMIN_ID), state))
    msg = FakeMessage("05.10.2026", user_id=ADMIN_ID)
    _run(settings_edit_value(msg, state))
    assert "Сначала выберите город" in msg.answers[0][0]
    assert _run(db.get_setting("forum_date")) is None


def _screen(code, key="forum_date"):
    from handlers.admin_settings import _settings_edit_screen
    text, kb = _run(_settings_edit_screen(key, code))
    return text, [b.text for row in kb.inline_keyboard for b in row]


def test_city_screen_without_own_date_does_not_promise_common_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "03.10.2026"))  # старый общий ключ — ничего не даёт
    text, buttons = _screen("spb")
    assert "Как везде" not in text and "03.10.2026" not in text
    assert "Своей даты нет" in text
    assert not any("Как везде" in b for b in buttons)
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _text, buttons = _screen("spb")
    assert "🗑 Стереть дату города" in buttons and not any("Как везде" in b for b in buttons)
    # У обычной городской настройки «↩️ Как везде» на месте.
    _run(db.set_setting("start_text__city__spb", "свой"))
    assert any("Как везде" in b for b in _screen("spb", "start_text")[1])


def test_clear_city_date_confirm_names_what_turns_off(tmp_path):
    from cities import set_admin_city
    from handlers.admin_settings import settings_reset_city

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, "spb"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    cbq = FakeCallback("settings_reset_city:forum_date", user_id=ADMIN_ID)
    _run(settings_reset_city(cbq))
    assert "Стереть дату форума" in cbq.message.text and "SOS" in cbq.message.text
    assert "как везде" not in cbq.message.text.lower()
    cbs = [b.callback_data for row in cbq.message.markup.inline_keyboard for b in row]
    assert "settings_reset_city_go:forum_date:spb" in cbs


def test_clear_city_date_go_says_date_erased_not_as_everywhere(tmp_path):
    """После «🗑 Да, стереть дату» ответ «дата стёрта», а не «как везде» — общей даты у
    города нет. У обычной городской настройки — по-прежнему «как везде»."""
    from cities import set_admin_city
    from handlers.admin_settings import settings_reset_city_go

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, "spb"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    cbq = FakeCallback("settings_reset_city_go:forum_date:spb", user_id=ADMIN_ID)
    _run(settings_reset_city_go(cbq))
    answer = cbq.answers[-1][0]
    assert "дата стёрта" in answer and "как везде" not in answer
    assert _run(db.get_setting("forum_date__city__spb")) in (None, "")
    _run(db.set_setting("start_text__city__spb", "свой"))
    cbq = FakeCallback("settings_reset_city_go:start_text:spb", user_id=ADMIN_ID)
    _run(settings_reset_city_go(cbq))
    assert "как везде" in cbq.answers[-1][0]


def test_dash_on_city_date_asks_confirmation_instead_of_erasing(tmp_path):
    """«-» на экране даты города ведёт на то же подтверждение, что «🗑 Стереть дату города»,
    — дата остаётся на месте до «Да, стереть». У обычной городской настройки «-» — сброс."""
    from cities import set_admin_city
    from handlers.admin_settings import settings_edit_city, settings_edit_value
    from tests.test_roles_phase8 import FakeMessage

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, "spb"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    state = _fresh_state(ADMIN_ID)
    cbq = FakeCallback("settings_edit_city:forum_date", user_id=ADMIN_ID)
    _run(settings_edit_city(cbq, state))
    assert "Чтобы очистить поле" not in cbq.message.text and "переспросит" in cbq.message.text
    msg = FakeMessage("-", user_id=ADMIN_ID)
    _run(settings_edit_value(msg, state))
    text, _mode, kb = msg.answers[-1]
    assert "Стереть дату форума" in text
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings_reset_city_go:forum_date:spb" in cbs
    assert _run(db.get_setting("forum_date__city__spb")) == "03.10.2026"
    assert _run(state.get_state()) is None
    # Даты нет — «-» объясняет, что стирать нечего, и ничего не пишет.
    _run(db.delete_setting("forum_date__city__spb"))
    state = _fresh_state(ADMIN_ID)
    _run(settings_edit_city(FakeCallback("settings_edit_city:forum_date", user_id=ADMIN_ID), state))
    msg = FakeMessage("-", user_id=ADMIN_ID)
    _run(settings_edit_value(msg, state))
    assert "стирать нечего" in msg.answers[-1][0]
    # Обычная городская настройка: «-» по-прежнему сбрасывает сразу.
    _run(db.set_setting("start_text__city__spb", "свой"))
    state = _fresh_state(ADMIN_ID)
    _run(settings_edit_city(FakeCallback("settings_edit_city:start_text", user_id=ADMIN_ID), state))
    _run(settings_edit_value(FakeMessage("-", user_id=ADMIN_ID), state))
    assert _run(db.get_setting("start_text__city__spb")) in (None, "")


def test_city_date_save_offers_way_back_to_readiness(tmp_path):
    """Текст поля объясняет, что дата включает QR/SOS/меню дня форума; после сохранения —
    «🚦 К готовности форума» этого города (только тому, у кого есть право светофора)."""
    from cities import set_admin_city
    from handlers.admin_caps import required_capability
    from handlers.admin_settings import settings_edit_city, settings_edit_value
    from tests.test_roles_phase8 import FakeMessage

    _ready(tmp_path)
    _run(set_admin_city(ADMIN_ID, "spb"))
    state = _fresh_state(ADMIN_ID)
    cbq = FakeCallback("settings_edit_city:forum_date", user_id=ADMIN_ID)
    _run(settings_edit_city(cbq, state))
    assert "QR" in cbq.message.text and "SOS" in cbq.message.text and "меню дня форума" in cbq.message.text
    msg = FakeMessage("05.10.2099", user_id=ADMIN_ID)
    _run(settings_edit_value(msg, state))
    assert _run(db.get_setting("forum_date__city__spb")) == "05.10.2099"
    kb = msg.answers[-1][2]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "forum_ready:spb" in cbs
    assert required_capability(callback_data="forum_ready:spb") == "moderate_reg"

    # Менеджер с правом «Настройки», но без права светофора — кнопки нет.
    other = 261003777
    _run(db.add_staff(other, "stats_manager", ADMIN_ID))
    _run(db.set_setting("role_caps_stats_manager", "settings"))
    _run(set_admin_city(other, "spb"))
    state = _fresh_state(other)
    _run(settings_edit_city(FakeCallback("settings_edit_city:forum_date", user_id=other), state))
    msg = FakeMessage("06.10.2099", user_id=other)
    _run(settings_edit_value(msg, state))
    assert _run(db.get_setting("forum_date__city__spb")) == "06.10.2099"
    cbs = [b.callback_data for row in msg.answers[-1][2].inline_keyboard for b in row]
    assert "forum_ready:spb" not in cbs
