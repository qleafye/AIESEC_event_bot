"""Phase 32 План 10 (D-06/D-09/D-10/D-11/D-13): админка волн в чат-боте — правила сервиса
(даты/права/копия/редактируемые поля, задача 1), экран списка + визард создания (задача 2),
карточка волны — правка/копия/активация/удаление (задача 3).

pytest-asyncio недоступен — async через `asyncio.run()`, фикстура временной БД — тот же
приём, что `tests/test_ambassador_waves_db_32.py::_ready`.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from services import ambassador_waves as aw


ADMIN_ID = 921001
MSK_MANAGER_ID = 921002
SPB_MANAGER_ID = 921003
STRANGER_MANAGER_ID = 921004


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.html_text = text
        self.from_user = FakeUser(user_id)
        self.answers = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return self


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, text=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(text)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _last(msg: FakeMessage):
    """(text, kb) последнего message.answer(...)."""
    return msg.answers[-1] if msg.answers else (None, None)


def _kb_callbacks(kb):
    if kb is None:
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _ready(tmp_path, name="test_ambassador_waves_crud_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _codes():
    codes = cities.city_codes()
    assert len(codes) >= 2, "нужно хотя бы два города в тестовом реестре"
    return codes[0], codes[1]


def _bind_manager(manager_id: int, city: str):
    _run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(manager_id, city))


def _dt(day: str) -> str:
    """«01.10.2026» -> «2026-10-01 00:00:00» (начало дня, соглашение этого модуля)."""
    from datetime import datetime
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 00:00:00")


def _dt_end(day: str) -> str:
    """«21.10.2026» -> «2026-10-21 23:59:59» (конец дня)."""
    from datetime import datetime
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 23:59:59")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: validate_wave_dates / can_edit_wave / editable_city_codes / copy_wave /
# wave_editable_fields
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_editable_fields_four_states(tmp_path):
    _ready(tmp_path)
    draft = {"state": "draft"}
    active_fresh = {"state": "active", "started_notified_at": None}
    active_sent = {"state": "active", "started_notified_at": "2026-10-01 09:00:00"}
    closing = {"state": "closing"}
    announced = {"state": "announced"}
    assert aw.wave_editable_fields(draft) == {"dates", "intro_text", "prize_places", "event_city", "tasks"}
    assert aw.wave_editable_fields(active_fresh) == aw.wave_editable_fields(draft)
    assert aw.wave_editable_fields(active_sent) == {"intro_text", "prize_places"}
    assert aw.wave_editable_fields(closing) == {"prize_places"}
    assert aw.wave_editable_fields(announced) == set()


def test_validate_wave_dates_end_before_start(tmp_path):
    _ready(tmp_path)
    msg = _run(aw.validate_wave_dates(_dt_end("21.10.2026"), _dt("01.10.2026"), None))
    assert msg is not None
    assert "дат" in msg.lower()


def test_validate_wave_dates_zero_length(tmp_path):
    _ready(tmp_path)
    same = "2026-10-01 12:00:00"
    msg = _run(aw.validate_wave_dates(same, same, None))
    assert msg is not None


def test_validate_wave_dates_ok_no_conflict(tmp_path):
    _ready(tmp_path)
    msg = _run(aw.validate_wave_dates(_dt("01.10.2026"), _dt_end("10.10.2026"), None))
    assert msg is None


def test_validate_wave_dates_edge_overlap_caught_adjacent_not(tmp_path):
    _ready(tmp_path)
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    # Перекрытие ровно по краю (новая начинается в день конца старой) — считается пересечением.
    msg_edge = _run(aw.validate_wave_dates(_dt("10.10.2026"), _dt_end("15.10.2026"), None))
    assert msg_edge is not None
    assert str(wid) not in "".join([]) and "id" not in msg_edge.lower()
    # Соседний отрезок (новая начинается на следующий день после конца старой) — не пересекается.
    msg_adjacent = _run(aw.validate_wave_dates(_dt("11.10.2026"), _dt_end("15.10.2026"), None))
    assert msg_adjacent is None


def test_validate_wave_dates_message_has_no_db_ids(tmp_path):
    _ready(tmp_path)
    _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    msg = _run(aw.validate_wave_dates(_dt("05.10.2026"), _dt_end("15.10.2026"), None))
    assert msg is not None
    for forbidden in ("id", "wave_id", "None"):
        assert forbidden not in msg


def test_validate_wave_dates_exclude_id_allows_self(tmp_path):
    _ready(tmp_path)
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    # Правка ТОЙ ЖЕ волны на те же даты не должна конфликтовать сама с собой.
    msg = _run(aw.validate_wave_dates(_dt("01.10.2026"), _dt_end("10.10.2026"), None, exclude_id=wid))
    assert msg is None


def test_can_edit_wave_bound_manager_own_city_only(tmp_path):
    _ready(tmp_path)
    city_a, city_b = _codes()
    _bind_manager(MSK_MANAGER_ID, city_a)
    wave_own = {"event_city": city_a}
    wave_other = {"event_city": city_b}
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_own)) is True
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_other)) is False


def test_can_edit_wave_bound_manager_cannot_touch_all_cities_wave(tmp_path):
    _ready(tmp_path)
    city_a, _ = _codes()
    _bind_manager(MSK_MANAGER_ID, city_a)
    wave_all = {"event_city": None}
    assert _run(aw.can_edit_wave(MSK_MANAGER_ID, wave_all)) is False


def test_can_edit_wave_superadmin_sees_everything(tmp_path):
    _ready(tmp_path)
    city_a, city_b = _codes()
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": city_a})) is True
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": city_b})) is True
    assert _run(aw.can_edit_wave(ADMIN_ID, {"event_city": None})) is True


def test_copy_wave_shifts_deadlines_by_wave_start_delta(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("10.10.2026"), intro_text="Стартуем!", prize_places=5,
        created_by=ADMIN_ID,
    ))
    t1 = _run(db.create_task(
        "Пост в сторис", "Light", 10, "photo", "2026-10-05 12:00:00", ADMIN_ID,
        wave_id=src_id, audience="ambassadors",
    ))
    t2 = _run(db.create_task(
        "Без срока", "Hard", 50, "text", db.NO_DEADLINE_AT, ADMIN_ID,
        wave_id=src_id, audience="all",
    ))
    new_starts = _dt("15.11.2026")
    new_ends = _dt_end("24.11.2026")
    new_id = _run(aw.copy_wave(src_id, new_starts, new_ends, created_by=ADMIN_ID))

    new_wave = _run(db.get_wave(new_id))
    assert new_wave["intro_text"] == "Стартуем!"
    assert new_wave["prize_places"] == 5
    assert new_wave["state"] == "draft"

    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert len(tasks) == 2

    from datetime import datetime
    old_start = datetime.strptime("2026-10-01 00:00:00", "%Y-%m-%d %H:%M:%S")
    new_start = datetime.strptime(new_starts, "%Y-%m-%d %H:%M:%S")
    shift = new_start - old_start
    old_deadline = datetime.strptime("2026-10-05 12:00:00", "%Y-%m-%d %H:%M:%S")
    expected = (old_deadline + shift).strftime("%Y-%m-%d %H:%M:%S")

    by_text = {t["text"]: t for t in tasks}
    assert by_text["Пост в сторис"]["deadline_at"] == expected
    assert by_text["Без срока"]["deadline_at"] == db.NO_DEADLINE_AT


def test_copy_wave_clamps_deadline_to_new_wave_end(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("31.10.2026"), created_by=ADMIN_ID))
    # Дедлайн задания в последний день старой волны -> после сдвига на короткую новую волну
    # вылезет за её конец и должен быть подрезан ровно до конца новой волны.
    _run(db.create_task(
        "Позднее задание", "Medium", 20, "text", "2026-10-31 20:00:00", ADMIN_ID, wave_id=src_id,
    ))
    new_starts = _dt("01.11.2026")
    new_ends = _dt_end("03.11.2026")
    new_id = _run(aw.copy_wave(src_id, new_starts, new_ends, created_by=ADMIN_ID))
    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert tasks[0]["deadline_at"] == new_ends


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: экран списка волн и визард создания (handlers/admin_game_waves.py)
# ══════════════════════════════════════════════════════════════════════════════════════════

def _enable_cities():
    _run(db.set_setting("event_city_enabled", "on"))


def test_wave_list_screen_has_no_state_codes(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    _run(db.set_wave_state(wid, "announced"))
    text, kb = _run(w._wave_list_screen(ADMIN_ID))
    for code in ("draft", "active", "closing", "announced"):
        assert code not in text
    assert "итоги объявлены" in text


def test_wave_create_dates_both_via_semicolon(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.dates))
    msg = FakeMessage("01.10.2026; 21.10.2026")
    _run(w.wave_create_dates_step(msg, state))
    data = _run(state.get_data())
    assert data["wc_starts"] == "2026-10-01 00:00:00"
    assert data["wc_ends"] == "2026-10-21 23:59:59"
    current = _run(state.get_state())
    assert current == WaveCreate.intro.state


def test_wave_create_single_date_asks_second(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.dates))
    msg1 = FakeMessage("01.10.2026")
    _run(w.wave_create_dates_step(msg1, state))
    assert _run(state.get_state()) == WaveCreate.dates.state
    assert (_run(state.get_data())).get("wc_start") == "01.10.2026"
    text1, _ = _last(msg1)
    assert "конца" in text1.lower()

    msg2 = FakeMessage("21.10.2026")
    _run(w.wave_create_dates_step(msg2, state))
    data = _run(state.get_data())
    assert data["wc_starts"] == "2026-10-01 00:00:00"
    assert data["wc_ends"] == "2026-10-21 23:59:59"


def test_wave_create_garbage_input_stays_in_state_with_example(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.dates))
    msg = FakeMessage("когда-нибудь потом")
    _run(w.wave_create_dates_step(msg, state))
    assert _run(state.get_state()) == WaveCreate.dates.state
    text, _ = _last(msg)
    assert "ДД.ММ.ГГГГ" in text or "дд.мм.гггг" in text.lower()


def test_wave_create_overlap_rejected_names_conflicting_wave(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.dates))
    msg = FakeMessage("05.10.2026; 15.10.2026")
    _run(w.wave_create_dates_step(msg, state))
    text, _ = _last(msg)
    assert "Волна 1" in text
    # Не продвинулись дальше шага дат — подтверждения ещё не показали.
    assert _run(state.get_state()) == WaveCreate.dates.state


def test_wave_card_stale_button_wrong_city_manager_gets_explanation(tmp_path):
    """T-32-10-01: устаревшая кнопка `wave:{id}` на волну чужого города — объяснение, не
    карточка (менеджер привязан к городу ПОСЛЕ того, как кнопка могла быть отрисована)."""
    _ready(tmp_path)
    _enable_cities()
    city_a, city_b = _codes()
    wave_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), event_city=city_b, created_by=ADMIN_ID))
    _bind_manager(MSK_MANAGER_ID, city_a)
    from handlers import admin_game_waves as w
    state = _new_state(MSK_MANAGER_ID)
    cb = FakeCallback(f"wave:{wave_id}", user_id=MSK_MANAGER_ID)
    _run(w.show_wave_card(cb, state))
    assert cb.answers
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert not cb.message.edits  # экран НЕ перерисован


def test_wave_create_go_creates_draft_with_continuing_number(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    # Первая волна города — номер 1, уже существует.
    _run(db.create_wave(_dt("01.01.2026"), _dt_end("05.01.2026"), created_by=ADMIN_ID))
    state = _new_state(ADMIN_ID)
    _run(state.set_data({
        "wc_mode": "create", "wc_city": None,
        "wc_starts": _dt("01.11.2026"), "wc_ends": _dt_end("10.11.2026"), "wc_intro": None,
    }))
    _run(state.set_state(WaveCreate.confirm))
    cb = FakeCallback("wccreate_go", user_id=ADMIN_ID)
    _run(w.wave_create_go(cb, state))
    waves = _run(db.list_waves())
    new_wave = next(x for x in waves if x["starts_at"] == _dt("01.11.2026"))
    assert new_wave["state"] == "draft"
    assert new_wave["number"] == 2


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: карточка волны — правка / активация / удаление / копия
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_card_shows_intro_html_as_stored_without_second_escape(tmp_path):
    """Вводный текст хранится готовым HTML (`message.html_text`): «&» уже лежит как «&amp;»,
    жирный — тегом. Второй escape на карточке показывал менеджеру «&amp;amp;» и «&lt;b&gt;»."""
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID,
        intro_text="<b>Старт</b> &amp; вперёд",
    ))
    text, _kb = _run(w._wave_card_screen(ADMIN_ID, _run(db.get_wave(wid))))
    assert "<b>Старт</b> &amp; вперёд" in text
    assert "&amp;amp;" not in text and "&lt;b&gt;" not in text


def test_card_locked_fields_after_start_sent_shows_explanation(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    _run(db.set_wave_state(wid, "active"))
    _run(db.mark_wave_started(wid, "2026-09-30 09:00:00"))
    wave = _run(db.get_wave(wid))
    text, kb = _run(w._wave_card_screen(ADMIN_ID, wave))
    calls = _kb_callbacks(kb)
    assert not any(c and c.startswith("waveedit:") and c.endswith(":dates") for c in calls)
    assert "нельзя" in text.lower()
    assert any(c and c.startswith("waveedit:") and c.endswith(":intro_text") for c in calls)


def test_activation_arms_jobs_and_flips_state_once(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(_dt("01.11.2026"), _dt_end("10.11.2026"), created_by=ADMIN_ID))
    # WR-16 (остаток, ревизия 32-FIX-common-2): `wave_activate_go` теперь тоже отказывает
    # волне без заданий — этот тест проверяет саму расстановку джоб, а не запрет пустой волны
    # (тот сюжет покрыт tests/test_wave_activate_guard_wr16_260922.py), поэтому у волны есть
    # задание.
    _run(db.create_task("Задание", "Light", 10, "text", _dt_end("10.11.2026"), ADMIN_ID, wave_id=wid))
    calls = {"start_for_all": 0, "wave_end": [], "reminders": []}

    async def fake_start_for_all(wave_id):
        calls["start_for_all"] += 1
        return 0

    def fake_wave_end(wave_id, ends_at):
        calls["wave_end"].append((wave_id, ends_at))

    def fake_reminder(task_id, deadline):
        calls["reminders"].append((task_id, deadline))
        return True

    monkeypatch.setattr(w, "schedule_wave_start_for_all", fake_start_for_all)
    monkeypatch.setattr(w, "schedule_wave_end", fake_wave_end)
    monkeypatch.setattr(w, "schedule_task_deadline_reminder", fake_reminder)

    cb = FakeCallback(f"waveactivate_go:{wid}", user_id=ADMIN_ID)
    state = _new_state(ADMIN_ID)
    _run(w.wave_activate_go(cb, state))

    assert calls["start_for_all"] == 1
    assert len(calls["wave_end"]) == 1
    wave = _run(db.get_wave(wid))
    assert wave["state"] == "active"

    # Повторная активация — состояние уже не 'draft', джоб больше не ставим.
    cb2 = FakeCallback(f"waveactivate_go:{wid}", user_id=ADMIN_ID)
    _run(w.wave_activate_go(cb2, state))
    assert calls["start_for_all"] == 1
    assert len(calls["wave_end"]) == 1
    assert cb2.answers and cb2.answers[-1][1] is True


def test_delete_requires_confirm_and_clears_wave_id_and_jobs(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    tid = _run(db.create_task("Задание", "Light", 10, "text", "2026-10-10 23:59:59", ADMIN_ID, wave_id=wid))

    cancelled = {}

    def fake_cancel_wave_jobs(wave_id):
        cancelled["wave"] = wave_id

    def fake_cancel_task_reminder(task_id):
        cancelled.setdefault("tasks", []).append(task_id)

    monkeypatch.setattr(w, "cancel_wave_jobs", fake_cancel_wave_jobs)
    monkeypatch.setattr("services.scheduler.cancel_task_deadline_reminder", fake_cancel_task_reminder)

    # Шаг подтверждения не удаляет.
    confirm_cb = FakeCallback(f"wavedel:{wid}", user_id=ADMIN_ID)
    state = _new_state(ADMIN_ID)
    _run(w.wave_delete_confirm(confirm_cb, state))
    assert _run(db.get_wave(wid)) is not None
    edit_text = confirm_cb.message.edits[-1][0]
    assert "пропадёт" in edit_text.lower()
    assert "останется" in edit_text.lower() or "останутся" in edit_text.lower()

    go_cb = FakeCallback(f"wavedel_go:{wid}", user_id=ADMIN_ID)
    _run(w.wave_delete_go(go_cb, state))
    assert _run(db.get_wave(wid)) is None
    task = _run(db.get_task(tid))
    assert task["wave_id"] is None
    assert "wave" in cancelled
    # WR-13: задание остаётся жить вне волн со своим сроком — его напоминание о дедлайне
    # НЕ снимается при удалении волны (снимаются только волновые джобы старта/конца/итогов).
    assert "tasks" not in cancelled


def test_delete_announced_wave_rejected(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_waves as w
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    _run(db.set_wave_state(wid, "announced"))
    cb = FakeCallback(f"wavedel:{wid}", user_id=ADMIN_ID)
    state = _new_state(ADMIN_ID)
    _run(w.wave_delete_confirm(cb, state))
    assert cb.answers and cb.answers[-1][1] is True
    assert _run(db.get_wave(wid)) is not None


def test_copy_from_card_opens_new_wave_card(tmp_path):
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    src_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    cb = FakeCallback(f"wavecopy:{src_id}", user_id=ADMIN_ID)
    state = _new_state(ADMIN_ID)
    _run(w.wave_copy_from_card(cb, state))
    assert _run(state.get_state()) == WaveCreate.dates.state

    _run(state.update_data(wc_starts=_dt("01.12.2026"), wc_ends=_dt_end("10.12.2026")))
    msg = FakeMessage("")
    _run(w._show_copy_confirm(msg, state))
    _, kb = _last(msg)
    go_callback = next(c for c in _kb_callbacks(kb) if c.startswith("wavecopy_go:"))

    go_cb = FakeCallback(go_callback, user_id=ADMIN_ID, text="")
    _run(w.wave_copy_go(go_cb, state))
    assert len(go_cb.message.answers) == 2
    card_text = go_cb.message.answers[-1][0]
    assert "Волна 2" in card_text


def test_wrong_city_manager_blocked_on_every_mutating_callback(tmp_path):
    """T-32-10-01: право на город проверяется на КАЖДОМ изменяющем обработчике карточки."""
    _ready(tmp_path)
    _enable_cities()
    city_a, city_b = _codes()
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), event_city=city_b, created_by=ADMIN_ID))
    _bind_manager(MSK_MANAGER_ID, city_a)
    from handlers import admin_game_waves as w
    from handlers import admin_game_wave_wizard as ww

    checks = [
        (ww.wave_edit_field_start, f"waveedit:{wid}:dates"),
        (w.wave_activate_confirm, f"waveactivate:{wid}"),
        (w.wave_activate_go, f"waveactivate_go:{wid}"),
        (w.wave_delete_confirm, f"wavedel:{wid}"),
        (w.wave_delete_go, f"wavedel_go:{wid}"),
        (ww.wave_copy_from_card, f"wavecopy:{wid}"),
    ]
    for handler, data in checks:
        cb = FakeCallback(data, user_id=MSK_MANAGER_ID)
        state = _new_state(MSK_MANAGER_ID)
        _run(handler(cb, state))
        assert cb.answers, f"{handler.__name__} не ответил"
        assert cb.answers[-1][1] is True, f"{handler.__name__} не показал alert"
    # Волна не изменилась ни одним из вызовов.
    wave = _run(db.get_wave(wid))
    assert wave is not None
    assert wave["event_city"] == city_b


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревизия 32-FIX: CR-04 (отмена визарда) / WR-06 (право и состав перепроверяются на шаге
# правки) / WR-13 частично — handlers/admin_game_wave_wizard.py
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_wizard_cancel_on_create_intro_step_does_not_save_text(tmp_path):
    """CR-04, сценарий 1: раньше «Отмена» на шаге вводного текста сохранялась как сам текст."""
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.intro))
    msg = FakeMessage("Отмена")
    _run(w.wave_wizard_cancel(msg, state))
    assert _run(state.get_data()) == {}
    assert _run(state.get_state()) is None
    assert "Отменено" in msg.answers[0][0]


def test_wave_wizard_cancel_via_slash_command_on_dates_step(tmp_path):
    """CR-04, сценарий 2: раньше любая команда посреди шага дат вешала в бесконечном «не понял»."""
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"wc_mode": "create", "wc_city": None}))
    _run(state.set_state(WaveCreate.dates))
    msg = FakeMessage("/start")
    _run(w.wave_wizard_cancel(msg, state))
    assert _run(state.get_state()) is None


def test_wave_wizard_cancel_on_edit_intro_step_returns_to_card_without_saving(tmp_path):
    """CR-04: «Отмена» на шаге правки вводного текста волны не портит прежний текст и
    возвращает на карточку (не в список — есть куда вернуться, we_wave_id известен)."""
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveEdit
    wid = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("10.10.2026"), intro_text="Старый текст", created_by=ADMIN_ID,
    ))
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"we_wave_id": wid}))
    _run(state.set_state(WaveEdit.intro_text))
    msg = FakeMessage("Отмена")
    _run(w.wave_wizard_cancel(msg, state))
    wave = _run(db.get_wave(wid))
    assert wave["intro_text"] == "Старый текст"
    assert _run(state.get_state()) is None
    card_text = msg.answers[-1][0]
    assert "Старый текст" in card_text


def test_wave_edit_dates_step_rejects_when_wave_deleted_mid_edit(tmp_path):
    """WR-06: волну удалили, пока менеджер вводил новые даты — правка не падает и не пишет
    в несуществующую волну."""
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveEdit
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"we_wave_id": wid}))
    _run(state.set_state(WaveEdit.dates))
    _run(db.delete_wave(wid))
    msg = FakeMessage("05.10.2026; 15.10.2026")
    _run(w.wave_edit_dates_step(msg, state))
    assert _run(state.get_state()) is None
    assert "не найдена" in msg.answers[0][0].lower()


def test_wave_edit_dates_step_rejects_field_locked_after_start_notified(tmp_path):
    """WR-06, сценарий 1: стартовая рассылка ушла, пока менеджер вводил новые даты — даты
    заперты `wave_editable_fields`, правка отклоняется, а не молча сдвигает дедлайны."""
    _ready(tmp_path)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveEdit
    wid = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    _run(db.set_wave_state(wid, "active"))
    state = _new_state(ADMIN_ID)
    _run(state.set_data({"we_wave_id": wid}))
    _run(state.set_state(WaveEdit.dates))
    _run(db.mark_wave_started(wid, "2026-09-30 09:00:00"))
    msg = FakeMessage("05.10.2026; 20.10.2026")
    _run(w.wave_edit_dates_step(msg, state))
    wave = _run(db.get_wave(wid))
    assert wave["starts_at"] == _dt("01.10.2026")
    assert wave["ends_at"] == _dt_end("10.10.2026")
    assert _run(state.get_state()) is None


def test_wave_edit_intro_step_rejects_when_city_access_lost(tmp_path):
    """WR-06, сценарий 2: менеджера перепривязали к другому городу, пока он вводил текст —
    право перепроверяется заново, правка не сохраняется."""
    _ready(tmp_path)
    _enable_cities()
    city_a, city_b = _codes()
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveEdit
    wid = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("10.10.2026"), event_city=city_a, intro_text="Было",
        created_by=ADMIN_ID,
    ))
    _bind_manager(MSK_MANAGER_ID, city_a)
    state = _new_state(MSK_MANAGER_ID)
    _run(state.set_data({"we_wave_id": wid}))
    _run(state.set_state(WaveEdit.intro_text))
    _run(db.set_staff_city(MSK_MANAGER_ID, city_b))
    msg = FakeMessage("Новый текст", user_id=MSK_MANAGER_ID)
    _run(w.wave_edit_intro_step(msg, state))
    wave = _run(db.get_wave(wid))
    assert wave["intro_text"] == "Было"
    assert _run(state.get_state()) is None


def test_copy_wave_skips_archived_tasks(tmp_path):
    _ready(tmp_path)
    src_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    tid = _run(db.create_task(
        "В архиве", "Light", 5, "text", "2026-10-05 12:00:00", ADMIN_ID, wave_id=src_id,
    ))
    _run(db.archive_task(tid))
    new_id = _run(aw.copy_wave(src_id, _dt("01.11.2026"), _dt_end("10.11.2026"), created_by=ADMIN_ID))
    tasks = _run(db.list_wave_tasks(new_id, active_only=False))
    assert tasks == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревизия 32-FIX-common-2: IN-05а/б — общая волна vs чужой город, «Скопировать прошлую»
# ищет СВОЮ волну, а не самую свежую видимую
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_general_wave_alert_differs_from_wrong_city_alert(tmp_path):
    """IN-05а: тап по общей волне («все города») и по волне другого города дают РАЗНЫЕ
    объяснения — раньше оба говорили «Эта волна другого города», хотя у общей волны никакого
    «правильного» города вообще нет (её правит главный менеджер)."""
    _ready(tmp_path)
    _enable_cities()
    city_a, city_b = _codes()
    general_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))
    other_city_id = _run(db.create_wave(
        _dt("01.11.2026"), _dt_end("10.11.2026"), event_city=city_b, created_by=ADMIN_ID,
    ))
    _bind_manager(MSK_MANAGER_ID, city_a)
    from handlers import admin_game_waves as w

    cb_general = FakeCallback(f"wave:{general_id}", user_id=MSK_MANAGER_ID)
    _run(w.show_wave_card(cb_general, _new_state(MSK_MANAGER_ID)))
    general_text = cb_general.answers[-1][0]
    assert "общая волна" in general_text.lower()
    assert "главный менеджер" in general_text.lower()

    cb_other = FakeCallback(f"wave:{other_city_id}", user_id=MSK_MANAGER_ID)
    _run(w.show_wave_card(cb_other, _new_state(MSK_MANAGER_ID)))
    other_text = cb_other.answers[-1][0]
    assert "другого города" in other_text.lower()
    assert other_text != general_text


def test_wave_copy_last_picks_most_recent_editable_wave(tmp_path):
    """IN-05б: «Скопировать прошлую» ищет самую свежую волну СРЕДИ ТЕХ, что менеджер может
    редактировать — более свежая, но недоступная (общая) волна раньше побеждала и отвечала
    «Нет прав на эту волну», хотя своя волна для копии у менеджера была."""
    _ready(tmp_path)
    _enable_cities()
    city_a, _city_b = _codes()
    own_id = _run(db.create_wave(
        _dt("01.09.2026"), _dt_end("10.09.2026"), event_city=city_a, created_by=ADMIN_ID,
    ))
    _run(db.create_wave(_dt("01.11.2026"), _dt_end("10.11.2026"), created_by=ADMIN_ID))  # общая, свежее
    _bind_manager(MSK_MANAGER_ID, city_a)
    from handlers import admin_game_wave_wizard as w
    from handlers.states import WaveCreate
    cb = FakeCallback("wavecopy", user_id=MSK_MANAGER_ID)
    state = _new_state(MSK_MANAGER_ID)
    _run(w.wave_copy_last_start(cb, state))
    assert _run(state.get_state()) == WaveCreate.dates.state
    assert (_run(state.get_data())).get("wc_copy_src") == own_id


def test_wave_copy_last_explains_when_nothing_editable(tmp_path):
    """IN-05б: единственная видимая волна — общая, менеджер её не редактирует. Раньше это
    давало «Нет прав на эту волну»; теперь — понятное объяснение и кнопка «Новая волна»."""
    _ready(tmp_path)
    _enable_cities()
    city_a, _city_b = _codes()
    _run(db.create_wave(_dt("01.10.2026"), _dt_end("10.10.2026"), created_by=ADMIN_ID))  # только общая
    _bind_manager(MSK_MANAGER_ID, city_a)
    from handlers import admin_game_wave_wizard as w
    cb = FakeCallback("wavecopy", user_id=MSK_MANAGER_ID)
    state = _new_state(MSK_MANAGER_ID)
    _run(w.wave_copy_last_start(cb, state))
    assert _run(state.get_state()) is None
    text, kb = cb.message.answers[-1]
    assert "нечего" in text.lower()
    assert "wavenew" in _kb_callbacks(kb)
