"""Форум-ночь п.6 (D-25, `.planning/FORUM-CHECKIN.md`, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`
идея №14): фильтры рассылки «Отметка на форуме» (`checkin_entry`) и «Сессия программы»
(`checkin_session`).

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path` (`tests/_dbtpl.fast_init_db`).
"""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
from database.db import _build_filter_clause
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, FakeMessage, _fresh_state

ADMIN_ID = 900924


def _ready(tmp_path, name="checkin_broadcast_filters.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _add_user(tid, *, status="approved", city=None, season=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, event_city, season) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, f"Делегат {tid}", status, city, season),
        )
        await conn.commit()


# ── двойная регистрация + сентинелы (прецедент Фазы 5, D-19) ────────────────────────────────

def test_checkin_entry_double_registration():
    from handlers.comms import admin_broadcasts
    assert "checkin_entry" in admin_broadcasts._PICKER_FIELDS
    assert "checkin_entry" in db._FILTER_COLUMNS
    assert "checkin_entry" in db._FILTER_VIRTUAL_FIELDS


def test_checkin_session_registered_but_not_a_generic_picker():
    """`checkin_session` НЕ входит в `_PICKER_FIELDS` — у него собственный мастер (город → день
    → сессия, handlers/comms/admin_broadcast_session_filter.py), не generic-пикер значений."""
    from handlers.comms import admin_broadcasts
    assert "checkin_session" not in admin_broadcasts._PICKER_FIELDS
    assert "checkin_session" in db._FILTER_COLUMNS
    assert "checkin_session" in db._FILTER_VIRTUAL_FIELDS


def test_filter_field_label_checkin_entry():
    from handlers.comms import admin_broadcasts
    assert admin_broadcasts._FILTER_FIELD_LABELS["checkin_entry"] == "Отметка на форуме"


def test_entry_point_literal_matches_service():
    """`db.CHECKIN_ENTRY_POINT` ОБЯЗАН побайтово совпадать с `services.checkin.ENTRY_POINT` —
    два независимых литерала одного и того же `checkins.point`, синхронизация только этим
    тестом (db.py не может импортировать services.checkin — см. докстринг константы)."""
    from services.checkin import ENTRY_POINT
    assert db.CHECKIN_ENTRY_POINT == ENTRY_POINT == "entry"


# ── SQL-слой: checkin_entry ──────────────────────────────────────────────────────────────────

def test_checkin_entry_yes_is_plain_exists(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved"))
    _run(_add_user(2, status="approved"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": db.CHECKIN_YES}]))
    assert ids == [1]


def test_checkin_entry_no_requires_approved_current_season(tmp_path):
    """«Не пришли» — НЕ просто NOT EXISTS: обязано отсечь неодобренных и делегатов прошлого
    сезона (482 импортированных approved 26/1 без QR — им нельзя писать «мы тебя не видим»)."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    _run(_add_user(1, status="approved", season="YL26"))  # не пришёл, текущий сезон -> "no"
    _run(_add_user(2, status="pending", season="YL26"))  # не одобрен -> НЕ "no"
    _run(_add_user(3, status="approved", season="YL25"))  # прошлый сезон -> НЕ "no"
    _run(_add_user(4, status="approved", season=None))  # легаси без сезона -> считается current
    ids = set(_run(db.count_and_list_filtered([{"field": "checkin_entry", "value": db.CHECKIN_NO}])))
    assert ids == {1, 4}, ids


def test_checkin_entry_no_recomputes_event_season_on_every_call(tmp_path):
    """Требование 1 (D-25): аудитория «не пришли» резолвится ЗАНОВО на каждый вызов
    `count_and_list_filtered` — фильтр, построенный ДО смены `event_season`, обязан читать
    актуальное значение, а не замороженный снимок момента выбора в мастере."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    _run(_add_user(1, status="approved", season="YL26"))
    _run(_add_user(2, status="approved", season="YL27"))
    spec = [{"field": "checkin_entry", "value": db.CHECKIN_NO}]  # без event_season внутри записи
    assert _run(db.count_and_list_filtered(spec)) == [1]
    _run(db.set_setting("event_season", "YL27"))  # сезон сменился МЕЖДУ вызовами
    assert _run(db.count_and_list_filtered(spec)) == [2]


def test_checkin_entry_no_unknown_event_season_means_no_restriction(tmp_path):
    """`event_season` не задан вовсе — тот же fail-soft приём, что у
    `count_approved_current_season`: без настройки сезон не фильтруется (любой approved без
    отметки считается «не пришёл»)."""
    _ready(tmp_path)
    _run(_add_user(1, status="approved", season="YL25"))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": db.CHECKIN_NO}]))
    assert ids == [1]


def test_checkin_entry_combines_with_city_filter_as_and(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    _run(_add_user(2, status="approved", city="spb"))
    ids = _run(db.count_and_list_filtered([
        {"field": "checkin_entry", "value": db.CHECKIN_NO},
        {"field": "event_city", "value": "spb", "exclude": ["msk", "tyumen"]},
    ]))
    assert ids == [2]


def test_checkin_entry_auto_session_counts_as_arrived(tmp_path):
    """D-18: отметка на СЕССИИ сама подтверждает вход (`source="auto_session"`, `services.
    checkin.record_arrival`) — «пришли на форум» обязано видеть такого делегата тоже, не
    только тех, кто буквально отсканирован на точке «Вход»."""
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))

    from services.checkin import record_arrival

    user = {"telegram_id": 1, "event_city": "msk"}
    _run(record_arrival(
        user, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:05:00",
    ))

    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": db.CHECKIN_YES}]))
    assert ids == [1]
    # запись входа реально появилась под auto_session, не подделана SQL-условием
    async def _source():
        async with db._connect() as conn:
            cur = await conn.execute(
                "SELECT source FROM checkins WHERE telegram_id = 1 AND point = ?",
                (db.CHECKIN_ENTRY_POINT,),
            )
            return (await cur.fetchone())[0]
    assert _run(_source()) == "auto_session"


def test_checkin_entry_unknown_value_fails_closed(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved"))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": "garbage"}]))
    assert ids == []


def test_checkin_entry_filter_spec_survives_json_roundtrip(tmp_path):
    """Отложенная рассылка хранит спеку как JSON (`services/scheduler.py`) — `checkin_entry`
    обязан пережить круговорот без потери значения/label."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    _run(_add_user(1, status="approved", season="YL26"))
    spec = [{"field": "checkin_entry", "value": db.CHECKIN_NO, "label": "не пришли"}]
    restored = json.loads(json.dumps(spec))
    assert restored == spec
    assert _run(db.count_and_list_filtered(restored)) == [1]


# ── SQL-слой: checkin_session ────────────────────────────────────────────────────────────────

def test_checkin_session_attended_and_not_attended(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    _run(_add_user(2, status="approved", city="msk"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    _run(db.record_checkin(1, f"session:{sid}", source="miniapp"))

    attended = _run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_ATTENDED, "session_id": sid}]
    ))
    not_attended = _run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_NOT_ATTENDED, "session_id": sid}]
    ))
    assert attended == [1]
    assert not_attended == [2]


def test_checkin_session_not_attended_requires_approved_current_season(tmp_path):
    """Тот же баг, что чинили у `checkin_entry`=`CHECKIN_NO`: «🚫 Не были на сессии X» без
    гарда `status='approved' AND (season IS NULL OR season = event_season)` ловит
    pending/rejected и делегатов прошлого сезона (482 импортированных 26/1) — никто из них
    отметиться на сессии физически не мог, но раньше "не были" совпадало с любым из них."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    _run(_add_user(1, status="approved", city="msk", season="YL26"))  # не был, текущий сезон
    _run(_add_user(2, status="pending", city="msk", season="YL26"))  # не одобрен
    _run(_add_user(3, status="rejected", city="msk", season="YL26"))  # отклонён
    _run(_add_user(4, status="approved", city="msk", season="YL25"))  # прошлый сезон
    _run(_add_user(5, status="approved", city="msk", season=None))  # легаси без сезона -> current
    ids = set(_run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_NOT_ATTENDED, "session_id": sid}]
    )))
    assert ids == {1, 5}, ids


def test_checkin_session_attended_also_guarded_by_status_and_season(tmp_path):
    """Требование ночи: гард применён и к «🎤 Были на сессии» — отмеченный неодобренный/чужого
    сезона делегат аномален (checkins физически не мог появиться без QR), но фильтр обязан
    оставаться согласованным с «не были», а не полагаться на то, что аномалии не бывает."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    _run(_add_user(1, status="approved", city="msk", season="YL26"))
    _run(_add_user(2, status="pending", city="msk", season="YL26"))
    _run(_add_user(3, status="approved", city="msk", season="YL25"))
    _run(db.record_checkin(1, f"session:{sid}", source="miniapp"))
    _run(db.record_checkin(2, f"session:{sid}", source="miniapp"))  # аномалия: pending с отметкой
    _run(db.record_checkin(3, f"session:{sid}", source="miniapp"))  # аномалия: чужой сезон
    ids = set(_run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_ATTENDED, "session_id": sid}]
    )))
    assert ids == {1}, ids


def test_checkin_session_recomputes_event_season_on_every_call(tmp_path):
    """Тот же приём D-25, что у `checkin_entry`: сезон резолвится ЗАНОВО на каждый вызов
    `count_and_list_filtered`, не замораживается на момент выбора сессии в мастере."""
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL26"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    _run(_add_user(1, status="approved", city="msk", season="YL26"))
    _run(_add_user(2, status="approved", city="msk", season="YL27"))
    spec = [{"field": "checkin_session", "value": db.SESSION_NOT_ATTENDED, "session_id": sid}]
    assert _run(db.count_and_list_filtered(spec)) == [1]
    _run(db.set_setting("event_season", "YL27"))
    assert _run(db.count_and_list_filtered(spec)) == [2]


def test_checkin_session_deleted_session_fails_closed_not_everyone(tmp_path):
    """WR-01: `session_id`, удалённый между планированием и отправкой, НЕ должен молча
    превратить «не были на сессии» во «всех» (NOT EXISTS на никогда не существовавшую точку
    иначе совпадает с любым делегатом)."""
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    ids = _run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_NOT_ATTENDED, "session_id": 99999}]
    ))
    assert ids == []


def test_checkin_session_survives_json_roundtrip(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    _run(db.record_checkin(1, f"session:{sid}", source="miniapp"))
    spec = [{
        "field": "checkin_session", "value": db.SESSION_ATTENDED, "session_id": sid,
        "label": "были на «Sess» (30.10.2026)",
    }]
    restored = json.loads(json.dumps(spec))
    assert restored == spec
    assert _run(db.count_and_list_filtered(restored)) == [1]


def test_checkin_session_bad_value_or_missing_id_fails_closed(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    assert _run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": "garbage", "session_id": sid}]
    )) == []
    assert _run(db.count_and_list_filtered(
        [{"field": "checkin_session", "value": db.SESSION_ATTENDED}]
    )) == []


# ── get_checkin_entry_filter_options / any_program_sessions_exist (пороги показа кнопок) ────

def test_get_checkin_entry_filter_options_thresholds_on_both_sides(tmp_path):
    _ready(tmp_path)
    assert _run(db.get_checkin_entry_filter_options()) == []

    _run(_add_user(1, status="approved"))
    # никто не пришёл -> только "no"
    assert _run(db.get_checkin_entry_filter_options()) == [db.CHECKIN_NO]

    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    # теперь единственный approved уже пришёл -> только "yes"
    assert _run(db.get_checkin_entry_filter_options()) == [db.CHECKIN_YES]

    _run(_add_user(2, status="approved"))
    options = _run(db.get_checkin_entry_filter_options())
    assert set(options) == {db.CHECKIN_YES, db.CHECKIN_NO}


def test_any_program_sessions_exist(tmp_path):
    _ready(tmp_path)
    assert _run(db.any_program_sessions_exist()) is False
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    assert _run(db.any_program_sessions_exist()) is True


# ── UI-слой: меню/пикер ──────────────────────────────────────────────────────────────────────

def test_checkin_button_hidden_when_no_data(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    msg = FakeMessage()
    _run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    kb = msg.answers[-1][2]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "filter_f_checkin_entry" not in flat
    assert "cksf_start:attended" not in flat


def test_checkin_button_shown_when_both_sides_present(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    _run(_add_user(1, status="approved"))
    _run(_add_user(2, status="approved"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))

    msg = FakeMessage()
    _run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    kb = msg.answers[-1][2]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "filter_f_checkin_entry" in flat


def test_session_buttons_shown_only_when_program_exists(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    msg = FakeMessage()
    _run(admin_broadcasts._render_filter_menu(msg, [], edit=False))
    flat = [btn.callback_data for row in msg.answers[-1][2].inline_keyboard for btn in row]
    assert "cksf_start:attended" not in flat

    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Sess"))
    msg2 = FakeMessage()
    _run(admin_broadcasts._render_filter_menu(msg2, [], edit=False))
    flat2 = [btn.callback_data for row in msg2.answers[-1][2].inline_keyboard for btn in row]
    assert "cksf_start:attended" in flat2
    assert "cksf_start:not_attended" in flat2


def test_checkin_entry_picker_shows_human_labels_not_codes(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    _run(_add_user(1, status="approved"))
    _run(_add_user(2, status="approved"))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))

    cb = FakeCallback("filter_f_checkin_entry", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    _run(admin_broadcasts._show_value_picker(cb, state, "checkin_entry", "Выберите значение:"))

    data = _run(state.get_data())
    labels = data.get("filter_option_labels") or {}
    assert labels.get(db.CHECKIN_YES) == "пришли на форум"
    assert labels.get(db.CHECKIN_NO) == "не пришли ни разу"
    assert labels.get(f"{db.CHECKIN_NO}@{db.CHECKIN_DAY_TODAY}") == "не пришли сегодня"


def test_checkin_entry_picker_alerts_when_all_on_one_side(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    _run(_add_user(1, status="approved"))  # ещё никто не пришёл -> одна сторона

    cb = FakeCallback("filter_f_checkin_entry", ADMIN_ID)
    state = _fresh_state(ADMIN_ID)
    _run(admin_broadcasts._show_value_picker(cb, state, "checkin_entry", "Выберите значение:"))
    assert cb.answers and cb.answers[0][1] is True  # show_alert


def test_filter_pick_value_checkin_entry_attaches_label(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(
        filter_pending_field="checkin_entry", filters=[],
        filter_options=[db.CHECKIN_YES, db.CHECKIN_NO],
        filter_option_labels={db.CHECKIN_YES: "пришли на форум", db.CHECKIN_NO: "не пришли"},
    ))
    cb = FakeCallback("filter_opt:1", ADMIN_ID)  # index 1 -> CHECKIN_NO
    _run(admin_broadcasts.filter_pick_value(cb, state))
    data = _run(state.get_data())
    assert data["filters"] == [{"field": "checkin_entry", "value": db.CHECKIN_NO, "label": "не пришли"}]


# ── UI-слой: мастер сессии (handlers/comms/admin_broadcast_session_filter.py) ─────────────────────

def test_session_wizard_full_flow_module_off(tmp_path):
    """Модуль городов выключен -> сразу день (дефолтный город), без экрана выбора города."""
    from handlers.comms import admin_broadcast_session_filter as sf
    _ready(tmp_path)
    _run(_add_user(1, status="approved", city="msk"))
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Keynote"))

    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[]))
    cb = FakeCallback("cksf_start:attended", ADMIN_ID)
    _run(sf.cksf_start(cb, state))
    assert "день" in cb.message.text.lower()

    cb2 = FakeCallback(f"cksf_day:2026-10-30", ADMIN_ID)
    _run(sf.cksf_day_pick(cb2, state))
    assert "Keynote" in cb2.message.text or any(
        "Keynote" in (btn.text or "") for row in cb2.message.markup.inline_keyboard for btn in row
    )

    cb3 = FakeCallback(f"cksf_pick:{sid}", ADMIN_ID)
    _run(sf.cksf_session_pick(cb3, state))
    data = _run(state.get_data())
    assert len(data["filters"]) == 1
    entry = data["filters"][0]
    assert entry["field"] == "checkin_session"
    assert entry["value"] == db.SESSION_ATTENDED
    assert entry["session_id"] == sid
    assert "были" in entry["label"]


def test_session_wizard_empty_city_alerts_and_stays(tmp_path):
    from handlers.comms import admin_broadcast_session_filter as sf
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[]))
    cb = FakeCallback("cksf_start:not_attended", ADMIN_ID)
    _run(sf.cksf_start(cb, state))
    assert cb.answers and cb.answers[-1][1] is True  # show_alert -- программы вообще нет


def test_session_wizard_picking_deleted_session_alerts(tmp_path):
    from handlers.comms import admin_broadcast_session_filter as sf
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[], cksf_mode=db.SESSION_ATTENDED, cksf_city="msk"))
    cb = FakeCallback("cksf_pick:99999", ADMIN_ID)
    _run(sf.cksf_session_pick(cb, state))
    assert cb.answers and cb.answers[0][1] is True


# ── capability (deny-by-default, D-02) ───────────────────────────────────────────────────────

def test_required_capability_checkin_entry_and_session_wizard_is_broadcast():
    from handlers.access import admin_caps
    assert admin_caps.required_capability(callback_data="filter_f_checkin_entry") == "broadcast"
    assert admin_caps.required_capability(callback_data="cksf_start:attended") == "broadcast"
    assert admin_caps.required_capability(callback_data="cksf_city:msk") == "broadcast"
    assert admin_caps.required_capability(callback_data="cksf_day:2026-10-30") == "broadcast"
    assert admin_caps.required_capability(callback_data="cksf_pick:1") == "broadcast"
    assert admin_caps.required_capability(callback_data="cksf_cancel") == "broadcast"


# ── напоминание о пересчёте на шаге «когда» (Требование 1) ─────────────────────────────────

def test_filter_schedule_warns_about_recompute_when_filters_present(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[{"field": "checkin_entry", "value": db.CHECKIN_NO}]))
    cb = FakeCallback("filter_schedule", ADMIN_ID)
    _run(admin_broadcasts.filter_schedule(cb, state))
    assert "пересчитается" in cb.message.text


def test_filter_schedule_no_warning_without_filters(tmp_path):
    from handlers.comms import admin_broadcasts
    _ready(tmp_path)
    state = _fresh_state(ADMIN_ID)
    _run(state.update_data(filters=[]))
    cb = FakeCallback("filter_schedule", ADMIN_ID)
    _run(admin_broadcasts.filter_schedule(cb, state))
    assert "пересчитается" not in cb.message.text
