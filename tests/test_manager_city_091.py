"""Phase 09.1 Plan 03 (manager <-> city binding, ROLE-03) tests.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as tests/test_roles_phase8.py /
tests/test_city_admin_phase72.py.

Task 1: database/db.py `staff.city` migration + `get_staff_city`/`set_staff_city` + the
"right, not filter" guards in cities.py::set_admin_city/admin_selected_city.
Task 2: handlers/admin.py city step in «Роли и доступы» + the admin_city_switch lock.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
import cities
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability


ADMIN_ID = 930101
MANAGER_ID = 930102
MANAGER2_ID = 930103
STRANGER_ID = 930104


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_manager_city_091.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Stand-in for the aiogram Message the callback carries — captures edit_text/answer."""

    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)


class FakeCallback:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage()
        self.answered_texts = []
        self.answered_alerts = []

    async def answer(self, text=None, show_alert=False):
        self.answered_texts.append(text)
        self.answered_alerts.append(show_alert)


# ── Task 1: staff.city + get_staff_city/set_staff_city ─────────────────────────────────────

def test_get_staff_city_unknown_person_is_none(tmp_path):
    _admin_ready(tmp_path)
    assert asyncio.run(db.get_staff_city(999999)) is None


def test_set_staff_city_syncs_all_roles_of_one_person(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(MANAGER_ID, "game_manager", ADMIN_ID))

    ok = asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    assert ok is True
    assert asyncio.run(db.get_staff_city(MANAGER_ID)) == "spb"

    roster = asyncio.run(db.list_staff())
    rows = [r for r in roster if r["telegram_id"] == MANAGER_ID]
    assert len(rows) == 2
    assert all(r["city"] == "spb" for r in rows)


def test_set_staff_city_none_clears_binding(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    asyncio.run(db.set_staff_city(MANAGER_ID, None))
    assert asyncio.run(db.get_staff_city(MANAGER_ID)) is None


def test_set_staff_city_no_staff_row_returns_false_and_writes_nothing(tmp_path):
    _admin_ready(tmp_path)
    ok = asyncio.run(db.set_staff_city(STRANGER_ID, "spb"))
    assert ok is False
    assert asyncio.run(db.get_staff_city(STRANGER_ID)) is None


def test_list_staff_exposes_city_key(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    roster = asyncio.run(db.list_staff())
    assert "city" in roster[0]
    assert roster[0]["city"] is None


def test_init_db_on_pre_phase_db_adds_column_without_dropping_rows(tmp_path):
    """Simulate a pre-09.1 DB: create the OLD staff table shape (no `city`), insert a row,
    then run init_db() again -- the row must survive with city == NULL."""
    import aiosqlite

    _use_tmp_db(tmp_path)

    async def _pre_phase_setup():
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS staff (
                    telegram_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    added_by INTEGER,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, role)
                )
            ''')
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at) VALUES (?, ?, ?, ?)",
                (MANAGER_ID, "reg_manager", ADMIN_ID, "2026-01-01T00:00:00"),
            )
            await conn.commit()

    asyncio.run(_pre_phase_setup())
    asyncio.run(db.init_db())  # must not raise, must not drop the pre-existing row

    roster = asyncio.run(db.list_staff())
    assert len(roster) == 1
    assert roster[0]["telegram_id"] == MANAGER_ID
    assert roster[0]["city"] is None


# ── Task 1: cities.py set_admin_city / admin_selected_city — right, not filter ─────────────

def test_set_admin_city_bound_manager_rejects_other_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    ok = asyncio.run(cities.set_admin_city(MANAGER_ID, "tyumen"))
    assert ok is False
    assert asyncio.run(db.get_setting(f"admin_city__{MANAGER_ID}")) is None


def test_set_admin_city_bound_manager_accepts_own_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    ok = asyncio.run(cities.set_admin_city(MANAGER_ID, "spb"))
    assert ok is True
    assert asyncio.run(db.get_setting(f"admin_city__{MANAGER_ID}")) == "spb"


def test_set_admin_city_unbound_manager_keeps_old_behavior(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    ok = asyncio.run(cities.set_admin_city(MANAGER_ID, "tyumen"))
    assert ok is True
    assert asyncio.run(db.get_setting(f"admin_city__{MANAGER_ID}")) == "tyumen"


def test_set_admin_city_superadmin_ignores_own_binding(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    # Give the bootstrap superadmin an (unusual, but not impossible) staff.city binding.
    asyncio.run(db.add_staff(ADMIN_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(ADMIN_ID, "spb"))

    ok = asyncio.run(cities.set_admin_city(ADMIN_ID, "tyumen"))
    assert ok is True
    assert asyncio.run(db.get_setting(f"admin_city__{ADMIN_ID}")) == "tyumen"


def test_admin_selected_city_bound_manager_returns_bound_city_ignoring_bot_settings(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    # Simulate a choice saved BEFORE the binding existed (or a stale/foreign value).
    asyncio.run(db.set_setting(f"admin_city__{MANAGER_ID}", "tyumen"))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    assert asyncio.run(cities.admin_selected_city(MANAGER_ID)) == "spb"


def test_admin_selected_city_module_off_returns_none_even_for_bound_manager(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    # event_city_enabled left at its default ("off") — module-off contract must still hold.
    assert asyncio.run(cities.admin_selected_city(MANAGER_ID)) is None


def test_admin_selected_city_unbound_manager_and_superadmin_unchanged(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) == "msk"  # default, no binding
    assert asyncio.run(cities.admin_selected_city(STRANGER_ID)) == "msk"


def test_set_admin_city_signature_unchanged():
    import inspect

    sig = inspect.signature(cities.set_admin_city)
    assert list(sig.parameters) == ["admin_id", "code"]


# ── Task 2: city step in «Роли и доступы» + admin_city_switch lock ─────────────────────────

def _kb_callback_data(kb: InlineKeyboardMarkup) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


def test_roles_keyboard_has_no_city_buttons_when_module_off(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    kb = asyncio.run(admin_mod.build_roles_keyboard())
    assert not any(cd.startswith("roles_city") for cd in _kb_callback_data(kb))


def test_roles_keyboard_has_city_edit_button_per_person_when_module_on(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    kb = asyncio.run(admin_mod.build_roles_keyboard())
    assert f"roles_city:{MANAGER_ID}" in _kb_callback_data(kb)


def test_roles_assign_module_off_redraws_roles_immediately(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback(f"roles_addrole:{MANAGER_ID}:reg_manager", ADMIN_ID)
    asyncio.run(admin_mod.roles_assign(cb))
    assert cb.message.edit_calls == 1
    assert "Роли и доступы" in cb.message.text
    assert asyncio.run(db.get_staff_roles(MANAGER_ID)) == ["reg_manager"]


def test_roles_assign_module_on_shows_city_step(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback(f"roles_addrole:{MANAGER_ID}:reg_manager", ADMIN_ID)
    asyncio.run(admin_mod.roles_assign(cb))
    assert asyncio.run(db.get_staff_roles(MANAGER_ID)) == ["reg_manager"]
    assert cb.message.edit_calls == 1
    cds = _kb_callback_data(cb.message.markup)
    assert f"roles_city_pick:{MANAGER_ID}:all" in cds
    assert any(cd.startswith(f"roles_city_pick:{MANAGER_ID}:") for cd in cds)


def test_roles_city_pick_all_clears_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    cb = FakeCallback(f"roles_city_pick:{MANAGER_ID}:all", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))
    assert asyncio.run(db.get_staff_city(MANAGER_ID)) is None
    assert cb.message.edit_calls == 1


def test_roles_city_pick_known_code_sets_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback(f"roles_city_pick:{MANAGER_ID}:spb", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))
    assert asyncio.run(db.get_staff_city(MANAGER_ID)) == "spb"


def test_roles_city_pick_unknown_code_alerts_and_writes_nothing(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback(f"roles_city_pick:{MANAGER_ID}:nowhere", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))
    assert asyncio.run(db.get_staff_city(MANAGER_ID)) is None
    assert cb.answered_alerts and cb.answered_alerts[-1] is True


def test_roles_city_start_gated_off_when_module_off(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    cb = FakeCallback(f"roles_city:{MANAGER_ID}", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_start(cb))
    assert cb.message.edit_calls == 0


def test_roles_city_start_shows_picker_when_module_on(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    cb = FakeCallback(f"roles_city:{MANAGER_ID}", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_start(cb))
    assert cb.message.edit_calls == 1
    cds = _kb_callback_data(cb.message.markup)
    assert f"roles_city_pick:{MANAGER_ID}:all" in cds


def test_render_roles_text_shows_city_when_module_on(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    text = asyncio.run(admin_mod.render_roles_text())
    assert "Санкт-Петербург" in text or "СПб" in text or "spb" in text.lower()


def test_admin_city_switch_bound_manager_gets_alert_not_picker(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    cb = FakeCallback("admin_city_switch", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    assert cb.message.edit_calls == 0
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "менять может суперадмин" in (cb.answered_texts[-1] or "")


def test_admin_city_switch_superadmin_opens_picker(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(ADMIN_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(ADMIN_ID, "spb"))

    cb = FakeCallback("admin_city_switch", ADMIN_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    assert cb.message.edit_calls == 1


def test_admin_city_switch_unbound_manager_opens_picker(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback("admin_city_switch", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_switch(cb))
    assert cb.message.edit_calls == 1


def test_admin_city_pick_bound_manager_other_code_rejected(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))

    cb = FakeCallback("admin_city_pick:tyumen", MANAGER_ID)
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "Неизвестный город" in (cb.answered_texts[-1] or "")
    assert asyncio.run(cities.admin_selected_city(MANAGER_ID)) == "spb"


# ── ADMIN_CAPS coverage for the two new callback prefixes ──────────────────────────────────

def test_admin_caps_covers_roles_city_callbacks():
    assert required_capability(callback_data=f"roles_city:{MANAGER_ID}") == "settings"
    assert required_capability(callback_data=f"roles_city_pick:{MANAGER_ID}:spb") == "settings"
