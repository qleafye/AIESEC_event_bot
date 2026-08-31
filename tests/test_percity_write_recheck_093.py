"""Review 09.3 warnings WR-01 / WR-02: write-time re-checks on the per-city write paths.

WR-01 -- `settings_edit_value` re-validates the RIGHT to write a per-city composite key at
write time (module on, code visible to the admin, header still on that city), not only when
`settings_edit_city` started the FSM. Binding/header moved in between -> nothing written,
FSM cleared, human-readable message. Unchanged -> the write lands exactly as before.

WR-02 -- `settings_regmode_reset_go` has its own explicit `cities_module_on()` guard like
every sibling reset-go handler: module OFF + forged callback -> delete never runs.

Same conventions as tests/test_regmode_header_093.py (asyncio.run, tmp_path DB).
"""
import asyncio
import inspect

from config import config
from database import db
from handlers import admin_settings
from handlers.admin_caps import role_caps_key, role_enabled_key
import cities


ADMIN_ID = 930601
MANAGER_ID = 930602


def _admin_ready(tmp_path, db_name="test_percity_write_recheck_093.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _add_bound_manager(manager_id=MANAGER_ID, city="spb"):
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;settings"))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        # aiogram отдаёт СТРОКУ («EditSetting:waiting_for_file»), а фейк хранит объект State —
        # существующие тесты сверяют именно объект. Приводим на выходе, чтобы хендлеры видели
        # ровно то, что увидят в проде.
        return getattr(self.state, "state", self.state)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_data(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data = {}
        self.state = None


class FakeMsgIn:
    def __init__(self, text, user_id=ADMIN_ID):
        self.text = text
        self.html_text = text
        self.from_user = FakeUser(user_id)
        self.answers = []

    async def answer(self, text=None, *a, **kw):
        self.answers.append(text)


# -- cities.split_per_city_key: inverse of per_city_key -------------------------------------

def test_split_per_city_key_is_inverse_of_per_city_key(tmp_path):
    _admin_ready(tmp_path)
    composed = cities.per_city_key("start_text", "spb")
    assert composed == "start_text__city__spb"
    assert cities.split_per_city_key(composed) == ("start_text", "spb")
    # not composite / unknown code / empty base -> None (never a partial tuple)
    assert cities.split_per_city_key("start_text") is None
    assert cities.split_per_city_key("start_text__city__nowhere") is None
    assert cities.split_per_city_key("__city__spb") is None
    # track suffixes are NOT per-city composites
    assert cities.split_per_city_key("approve_text__party") is None
    # _base_setting_key keeps its contract on the same inputs
    assert admin_settings._base_setting_key(composed) == "start_text"
    assert admin_settings._base_setting_key("start_text") == "start_text"


# -- WR-01: settings_edit_value re-checks the right at write time ---------------------------

def test_percity_write_refused_when_header_moved_between_fsm_entry_and_send(tmp_path):
    """Superadmin opened the СПб editor, then switched the header to Москва, then sent the
    value: nothing written under either city, FSM cleared, human message."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    state = FakeState()
    asyncio.run(admin_settings.settings_edit_city(FakeCallback("settings_edit_city:start_text"), state))
    assert state.data["setting_key"] == "start_text__city__spb"

    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))  # header moved while typing
    msg = FakeMsgIn("Питерский текст")
    asyncio.run(admin_settings.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text__city__spb")) is None
    assert asyncio.run(db.get_setting("start_text__city__msk")) is None
    assert asyncio.run(db.get_setting("start_text")) is None
    assert state.data == {} and state.state is None
    assert msg.answers == ["Город админки изменился — начните правку заново."]


def test_percity_write_refused_when_manager_rebound_to_other_city_before_send(tmp_path):
    """Bound manager (СПб) entered the FSM; a superadmin re-bound them to Москва before the
    value arrived -> the СПб write is refused (right check), FSM cleared."""
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager(MANAGER_ID, "spb")

    state = FakeState()
    asyncio.run(admin_settings.settings_edit_city(
        FakeCallback("settings_edit_city:start_text", user_id=MANAGER_ID), state))
    assert state.data["setting_key"] == "start_text__city__spb"

    asyncio.run(db.set_staff_city(MANAGER_ID, "msk"))  # binding changed while typing
    msg = FakeMsgIn("Питерский текст", user_id=MANAGER_ID)
    asyncio.run(admin_settings.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text__city__spb")) is None
    assert asyncio.run(db.get_setting("start_text__city__msk")) is None
    assert state.data == {} and state.state is None
    assert msg.answers == ["Этот город правит суперадмин — правка отменена."]


def test_percity_write_refused_when_module_switched_off_before_send(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    state = FakeState()
    asyncio.run(admin_settings.settings_edit_city(FakeCallback("settings_edit_city:start_text"), state))

    asyncio.run(db.set_setting("event_city_enabled", "off"))
    msg = FakeMsgIn("Питерский текст")
    asyncio.run(admin_settings.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text__city__spb")) is None
    assert state.data == {} and state.state is None
    assert msg.answers == ["Города выключены — правка отменена."]


def test_percity_clear_with_dash_also_recheck_refused_after_header_moved(tmp_path):
    """The "-" clear path is a write too -- an existing СПб override survives if the header
    moved before the "-" arrived."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("start_text__city__spb", "Было"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    state = FakeState()
    asyncio.run(admin_settings.settings_edit_city(FakeCallback("settings_edit_city:start_text"), state))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    msg = FakeMsgIn("-")
    asyncio.run(admin_settings.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text__city__spb")) == "Было"
    assert state.data == {} and state.state is None


def test_percity_write_lands_as_before_when_binding_and_header_unchanged(tmp_path):
    """Regression guard: nothing moved -> write and clear behave exactly as before WR-01."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    asyncio.run(db.set_setting("start_text", "Глобальный текст"))

    state = FakeState()
    asyncio.run(admin_settings.settings_edit_city(FakeCallback("settings_edit_city:start_text"), state))
    msg = FakeMsgIn("Питерский текст")
    asyncio.run(admin_settings.settings_edit_value(msg, state))

    assert asyncio.run(db.get_setting("start_text__city__spb")) == "Питерский текст"
    assert asyncio.run(db.get_setting("start_text")) == "Глобальный текст"
    assert state.data == {} and state.state is None
    assert msg.answers and msg.answers[0].startswith("🏙 ")  # back on the header editor

    # bound manager, unchanged binding -> write lands too
    _add_bound_manager(MANAGER_ID, "msk")
    state2 = FakeState()
    asyncio.run(admin_settings.settings_edit_city(
        FakeCallback("settings_edit_city:start_text", user_id=MANAGER_ID), state2))
    msg2 = FakeMsgIn("Московский текст", user_id=MANAGER_ID)
    asyncio.run(admin_settings.settings_edit_value(msg2, state2))
    assert asyncio.run(db.get_setting("start_text__city__msk")) == "Московский текст"

    # clear path unchanged
    state3 = FakeState()
    asyncio.run(admin_settings.settings_edit_city(FakeCallback("settings_edit_city:start_text"), state3))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("-"), state3))
    assert asyncio.run(db.get_setting("start_text__city__spb")) is None
    assert asyncio.run(db.get_setting("start_text")) == "Глобальный текст"


def test_non_percity_key_write_path_untouched_by_recheck(tmp_path):
    """A plain (global) key never goes through the per-city re-check -- even with the module
    on and the header on a city, and even with the module off."""
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    state = FakeState()
    asyncio.run(state.set_state(admin_settings.EditSetting.waiting_for_value))
    asyncio.run(state.update_data(setting_key="start_text"))
    msg = FakeMsgIn("Общий текст")
    asyncio.run(admin_settings.settings_edit_value(msg, state))
    assert asyncio.run(db.get_setting("start_text")) == "Общий текст"
    assert state.data == {} and state.state is None

    asyncio.run(db.set_setting("event_city_enabled", "off"))
    state2 = FakeState()
    asyncio.run(state2.update_data(setting_key="start_text"))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("Ещё текст"), state2))
    assert asyncio.run(db.get_setting("start_text")) == "Ещё текст"


# -- WR-02: settings_regmode_reset_go explicit module-off guard -----------------------------

def test_regmode_reset_go_module_off_forged_callback_refused_and_deletes_nothing(tmp_path):
    """Module OFF + forged `settings_regmode_reset_go:spb` -> the explicit guard answers
    «Города выключены», the override row is untouched, nothing is re-rendered."""
    _admin_ready(tmp_path)
    # event_city_enabled intentionally left unset (module off)
    asyncio.run(db.set_setting("registration_mode__city__spb", "full"))

    cb = FakeCallback("settings_regmode_reset_go:spb", user_id=ADMIN_ID)
    asyncio.run(admin_settings.settings_regmode_reset_go(cb))

    assert cb.answers == [("Города выключены", True)]
    assert asyncio.run(db.get_setting("registration_mode__city__spb")) == "full"
    assert cb.message.edit_calls == 0


def test_regmode_reset_go_module_off_guard_is_explicit_in_source():
    """WR-02 literally: the guard must be a local `cities_module_on()` check inside the
    handler, not an implicit consequence of admin_selected_city() returning None."""
    src = inspect.getsource(admin_settings.settings_regmode_reset_go)
    assert "if not await cities_module_on()" in src
