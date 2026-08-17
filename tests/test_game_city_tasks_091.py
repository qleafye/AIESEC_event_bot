"""Phase 09.1 Plan 02 (GAME-06) — tasks by city.

`game_tasks.event_city` (NULL = all cities), optional `city_scope` kwargs on task/queue
accessors, the "Кому задание?" wizard step (city module gated), the delegate-facing task
list filter, and the moderation-queue city scope + "Город" column on the gamification
sheet tabs.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_scope_phase72.py / tests/test_gamification_data_phase9.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import cities
from handlers import admin as admin_mod
from handlers import user_actions as ua_mod
from handlers.states import GameTaskCreate


ADMIN_ID = 940901
DELEGATE_ID = 940902


def _new_state(uid: int = ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.markup = None
        self.answers_sent = []
        self.answer_markups = []
        self.answers = []  # (text, parse_mode, markup) tuples -- user_actions.py convention

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answers.append((text, parse_mode, reply_markup))
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_city_tasks_091.db")
    asyncio.run(db.init_db())


# ── Task 1: game_tasks.event_city + city_scope kwarg on tasks/queue ─────────────────────────

def test_city_clause_no_new_args_matches_old_pair_byte_for_byte():
    """Regression: `_city_clause` without the new args returns the exact same pair the
    pre-09.1 applications/receipts queries depend on."""
    assert db._city_clause(("msk", ("spb", "tyumen"))) == (
        "(event_city IS NULL OR event_city NOT IN (?, ?))", ["spb", "tyumen"],
    )
    assert db._city_clause(("spb", ())) == ("event_city = ?", ["spb"])
    assert db._city_clause(None) == ("", [])


def test_city_clause_column_kwarg_qualifies_both_branches():
    frag, params = db._city_clause(("msk", ("spb",)), "t.event_city")
    assert frag == "(t.event_city IS NULL OR t.event_city NOT IN (?))"
    assert params == ["spb"]
    frag2, params2 = db._city_clause(("spb", ()), "t.event_city")
    assert frag2 == "t.event_city = ?"
    assert params2 == ["spb"]


def test_city_clause_include_null_equality_branch():
    frag, params = db._city_clause(("spb", ()), "t.event_city", include_null=True)
    assert frag == "(t.event_city IS NULL OR t.event_city = ?)"
    assert params == ["spb"]


def test_city_clause_include_null_is_noop_for_exclusion_branch():
    frag, params = db._city_clause(("msk", ("spb",)), "event_city", include_null=True)
    assert frag == "(event_city IS NULL OR event_city NOT IN (?))"
    assert params == ["spb"]


def test_create_task_with_event_city_saves_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "spb task", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb",
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["event_city"] == "spb"


def test_create_task_without_kwarg_saves_null(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "old style", "Light", 10, "text", "2099-01-01 00:00:00", None,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["event_city"] is None


def test_list_active_tasks_city_scope_includes_null_excludes_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    tasks = asyncio.run(db.list_active_tasks(city_scope=cities.city_scope("spb"), include_null=True))
    texts = {t["text"] for t in tasks}
    assert texts == {"all", "spb"}


def test_list_active_tasks_default_city_scope_includes_null(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    tasks = asyncio.run(db.list_active_tasks(city_scope=cities.city_scope("msk"), include_null=True))
    texts = {t["text"] for t in tasks}
    assert texts == {"all", "msk"}


def test_list_active_tasks_no_kwarg_returns_everything(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    tasks = asyncio.run(db.list_active_tasks())
    assert len(tasks) == 3


def _seed_submission(task_id, user_id, user_city):
    asyncio.run(db.add_user({
        "telegram_id": user_id,
        "full_name": f"User {user_id}",
        "registration_date": "2026-01-01 09:00:00",
        "event_city": user_city,
    }))
    return asyncio.run(db.create_submission(
        task_id, user_id, "text", "готово", "2026-08-20 10:00:00",
    ))


def test_get_pending_submissions_city_scope_filters_by_delegate_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    _seed_submission(task_id, 103, None)
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0, city_scope=cities.city_scope("spb")))
    assert {r["user_id"] for r in rows} == {101}


def test_get_pending_submissions_count_consistent_with_list(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    _seed_submission(task_id, 103, None)
    scope = cities.city_scope("msk")
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0, city_scope=scope))
    count = asyncio.run(db.get_pending_submissions_count(city_scope=scope))
    assert count == len(rows) == 2  # msk delegate + no-city delegate (exclusion form)


def test_list_all_submissions_exposes_user_event_city(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    rows = asyncio.run(db.list_all_submissions())
    assert rows[0]["user_event_city"] == "spb"


def test_get_pending_submissions_no_scope_matches_today(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 101, "spb")
    _seed_submission(task_id, 102, "msk")
    rows = asyncio.run(db.get_pending_submissions(limit=50, offset=0))
    assert len(rows) == 2


# ── Task 2: "Кому задание?" wizard step + delegate-facing task-list filter ──────────────────

def _drive_to_city_step(state, *, text="t", category="Light", coins="10", proof="photo"):
    asyncio.run(admin_mod.game_task_text_step(FakeMessage(text=text), state))
    asyncio.run(admin_mod.game_task_category_step(FakeCallback(f"gtcat:{category}"), state))
    asyncio.run(admin_mod.game_task_coins_step(FakeMessage(text=coins), state))
    if proof is not None:
        asyncio.run(admin_mod.game_task_proof_step(FakeCallback(f"gtproof:{proof}"), state))
    callback = FakeCallback("gtproof_done")
    asyncio.run(admin_mod.game_task_proof_done(callback, state))
    return callback


def test_module_off_wizard_skips_city_step_entirely(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    callback = _drive_to_city_step(state)
    # straight to the deadline prompt -- no city keyboard sent at all
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert "Кому" not in callback.message.answers_sent[-1]
    data = asyncio.run(state.get_data())
    assert data.get("gt_event_city") is None
    assert data.get("gt_city_step_shown") is False


def test_module_on_wizard_shows_city_step_with_all_and_enabled_cities(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    callback = _drive_to_city_step(state)
    assert asyncio.run(state.get_state()) == GameTaskCreate.city
    kb = callback.message.answer_markups[-1]
    codes = _flat_callback_data(kb)
    assert codes[0] == "gttcity:all"
    assert set(codes[1:]) == {f"gttcity:{c}" for c in cities.city_codes()}


def test_gttcity_all_sets_null_event_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_to_city_step(state)
    callback = FakeCallback("gttcity:all")
    asyncio.run(admin_mod.game_task_city_step(callback, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert asyncio.run(state.get_data())["gt_event_city"] is None


def test_gttcity_known_code_sets_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_to_city_step(state)
    callback = FakeCallback("gttcity:spb")
    asyncio.run(admin_mod.game_task_city_step(callback, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert asyncio.run(state.get_data())["gt_event_city"] == "spb"


def test_gttcity_unknown_code_alerts_and_keeps_state(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_to_city_step(state)
    callback = FakeCallback("gttcity:bogus")
    asyncio.run(admin_mod.game_task_city_step(callback, state))
    assert callback.answers == [("Неизвестный город", True)]
    assert asyncio.run(state.get_state()) == GameTaskCreate.city  # unchanged


def _drive_full_wizard_with_city(state, gttcity="gttcity:spb"):
    _drive_to_city_step(state)
    asyncio.run(admin_mod.game_task_city_step(FakeCallback(gttcity), state))
    deadline_message = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_mod.game_task_deadline_step(deadline_message, state))
    return deadline_message


def test_confirm_card_prints_komu_line_only_when_module_on(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = _drive_full_wizard_with_city(state, "gttcity:spb")
    card_text = message.answers_sent[-1]
    assert "Кому:" in card_text
    assert "Санкт-Петербург" in card_text or "СПб" in card_text or "spb" in card_text.lower()


def test_confirm_card_all_cities_label(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = _drive_full_wizard_with_city(state, "gttcity:all")
    card_text = message.answers_sent[-1]
    assert "Кому: 🌍 Все города" in card_text


def test_confirm_card_no_komu_line_when_module_off(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    callback = _drive_to_city_step(state)  # goes straight to deadline (module off)
    deadline_message = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_mod.game_task_deadline_step(deadline_message, state))
    card_text = deadline_message.answers_sent[-1]
    assert "Кому:" not in card_text


def test_game_task_confirm_passes_event_city_to_create_task(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_full_wizard_with_city(state, "gttcity:spb")
    callback = FakeCallback("gtconfirm")
    asyncio.run(admin_mod.game_task_confirm(callback, state))
    tasks = asyncio.run(db.list_all_tasks())
    assert tasks[0]["event_city"] == "spb"


def _seed_delegate(uid, event_city):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
        "event_city": event_city,
    }))
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = 'approved' WHERE telegram_id = ?", (uid,))
    conn.commit()
    conn.close()


def test_delegate_sees_own_city_and_null_not_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_delegate(DELEGATE_ID, "spb")
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_game_tasks(message))
    text = message.answers[-1][0]
    assert "all" in text and "spb" in text and "msk" not in text


def test_delegate_without_city_sees_default_city_and_null(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_delegate(DELEGATE_ID, None)
    asyncio.run(db.create_task("all", "Light", 10, "text", "2099-01-01 00:00:00", None))
    asyncio.run(db.create_task("msk", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="msk"))
    asyncio.run(db.create_task("spb", "Light", 10, "text", "2099-01-01 00:00:00", None, event_city="spb"))
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_game_tasks(message))
    text = message.answers[-1][0]
    assert "all" in text and "msk" in text and "spb" not in text


def test_module_off_show_game_tasks_calls_list_active_tasks_without_kwargs(tmp_path, monkeypatch):
    """Module off -> byte-identical to pre-09.1: no city_scope kwarg is ever passed."""
    _db_ready(tmp_path)
    _seed_delegate(DELEGATE_ID, "spb")
    asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))

    calls = []
    real = db.list_active_tasks

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(ua_mod, "list_active_tasks", spy)
    message = FakeMessage(user_id=DELEGATE_ID)
    asyncio.run(ua_mod.show_game_tasks(message))
    assert calls == [((), {})]


# ── Task 3: moderation-queue city scope + card city lines + sheet "Город" column ────────────

def _seed_pending_two_cities(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    config.ADMIN_IDS = [ADMIN_ID]
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 201, "spb")
    _seed_submission(task_id, 202, "spb")
    _seed_submission(task_id, 203, "msk")
    return task_id


def test_show_current_submission_city_scoped_spb_excludes_msk(tmp_path):
    _seed_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target, state))
    assert "1/2" in target.answers_sent[-1]  # only the 2 spb submissions are in scope


def test_show_current_submission_module_off_sees_everything(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    config.ADMIN_IDS = [ADMIN_ID]
    _seed_submission(task_id, 201, "spb")
    _seed_submission(task_id, 202, "msk")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target, state))
    assert "1/2" in target.answers_sent[-1]  # module off -- unfiltered, same as pre-09.1


def test_show_current_submission_card_prints_both_cities_when_module_on(tmp_path):
    task_id = _seed_pending_two_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target, state))
    card_text = target.answers_sent[-1]
    assert "🏙 Город делегата:" in card_text
    assert "🎯 Кому задание:" in card_text
    assert "Все города" in card_text  # this task has event_city=None


def test_show_current_submission_card_no_city_lines_when_module_off(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    config.ADMIN_IDS = [ADMIN_ID]
    _seed_submission(task_id, 201, "spb")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_submission(target, state))
    card_text = target.answers_sent[-1]
    assert "🏙 Город делегата:" not in card_text
    assert "🎯 Кому задание:" not in card_text


def test_render_submission_card_city_labels_none_is_byte_identical():
    """Regression: the pre-09.1 call shape (no city_labels arg) renders no new lines."""
    row = {
        "task_text": "t", "task_category": "Light", "task_coins": 10,
        "user_full_name": "X", "user_username": None, "task_proof_type": "text",
        "content_type": "text", "content": "готово",
    }
    text = admin_mod._render_submission_card(row, 1, 1)
    assert "🏙" not in text
    assert "🎯" not in text


def test_render_submission_card_with_city_labels():
    row = {
        "task_text": "t", "task_category": "Light", "task_coins": 10,
        "user_full_name": "X", "user_username": None, "task_proof_type": "text",
        "content_type": "text", "content": "готово",
    }
    text = admin_mod._render_submission_card(row, 1, 1, city_labels=("Санкт-Петербург", "🌍 Все города"))
    assert "🏙 Город делегата: Санкт-Петербург" in text
    assert "🎯 Кому задание: 🌍 Все города" in text


def test_sync_game_sheets_matrix_and_history_carry_city_column(tmp_path):
    """End-to-end: list_all_submissions() (Task 1) feeds straight into the "Город" column
    (Task 3) without the sync handler itself needing any change."""
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    task_id = asyncio.run(db.create_task("t", "Light", 10, "text", "2099-01-01 00:00:00", None))
    _seed_submission(task_id, 201, "spb")
    tasks = asyncio.run(db.list_all_tasks())
    submissions = asyncio.run(db.list_all_submissions())
    m_headers, m_rows = admin_mod._build_game_matrix(tasks, submissions)
    h_headers, h_rows = admin_mod._build_game_history(submissions)
    assert m_headers[:3] == ["telegram_id", "ФИО", "Город"]
    assert m_rows[0][2] == "spb"
    assert h_headers[:5] == ["ID сдачи", "Задание", "Категория", "Участник", "Город"]
    assert h_rows[0][4] == "spb"
