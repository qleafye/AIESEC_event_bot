"""Quick 260822: списочные настройки правятся по пунктам, а не заменой всего списка.

Прод 17.08 и 20.08: менеджер присылал ОДИН новый источник в экран правки `source_options`
и стирал весь список. Теперь экран `settings_edit:{list_key}` — это список по пунктам и три
кнопки: «➕ Добавить пункт» (одно сообщение = один пункт в конец), «🗑 Удалить пункт»
(кнопка на каждый пункт), «✏️ Заменить список целиком» (прежний путь, явный). FSM на ввод
с самого экрана больше не стартует.

Per-city: ни один списочный ключ сегодня не `per_city`, поэтому флаг подмешивается в
SETTINGS_SCHEMA на время теста — запись должна идти в `{key}__city__{code}` тем же путём,
что у `settings_edit_city` / `settings_edit_value`.

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp. Фейки
по конвенции сьюта (tests/test_admin_percity_ui.py, tests/test_settings_command_guard_260820.py).
"""
import asyncio

import pytest

import cities
from config import config
from database import db
from handlers import admin_settings, admin_settings_lists
from handlers.admin_caps import required_capability
from handlers.states import EditSetting
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

ADMIN_ID = 900822
LIST_KEY = "source_options"
SOURCE_LIST = "Соцсети Юлид\nСоцсети АЙСЕК\nДругое"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_list_items.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _FakeMessage:
    def __init__(self, uid=ADMIN_ID, text=""):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.html_text = text
        self.answers = []
        self.markups = []
        self.edited = None
        self.edited_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text
        self.edited_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, uid=ADMIN_ID):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeState:
    def __init__(self, data=None, state=None):
        self._data = dict(data or {})
        self._state = state

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def _buttons(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def _items(key=LIST_KEY):
    return asyncio.run(get_setting_typed(key))


def _open_editor(key=LIST_KEY, preset=SOURCE_LIST):
    cb = _FakeCallback(f"settings_edit:{key}")
    state = _FakeState()

    async def go():
        if preset is not None:
            await db.set_setting(key, preset)
        await admin_settings.settings_edit_start(cb, state)

    asyncio.run(go())
    return cb, state


def _add(text, key=LIST_KEY):
    """Полный путь: кнопка «➕» -> сообщение с пунктом."""
    cb = _FakeCallback(f"settings_list_add:{key}")
    state = _FakeState()
    asyncio.run(admin_settings_lists.settings_list_add_start(cb, state))
    message = _FakeMessage(text=text)
    asyncio.run(admin_settings_lists.settings_list_add_item(message, state))
    return cb, message, state


# ── права ────────────────────────────────────────────────────────────────────────────────────

def test_new_callbacks_mapped_to_settings_capability():
    for cb in (
        "settings_list_add:source_options",
        "settings_list_del:source_options",
        "settings_list_rm:source_options:0:abcd",
        "settings_list_replace:source_options",
    ):
        assert required_capability(callback_data=cb) == "settings", cb
    assert required_capability(raw_state="EditSetting:waiting_for_list_item") == "settings"


# ── экран списка ─────────────────────────────────────────────────────────────────────────────

def test_list_editor_shows_items_and_three_buttons_without_starting_fsm(tmp_path):
    _ready(tmp_path)
    cb, state = _open_editor()
    assert "• Соцсети Юлид" in cb.message.edited
    assert "(3)" in cb.message.edited
    data = [d for _t, d in _buttons(cb.message.edited_markup)]
    assert data == [
        f"settings_list_add:{LIST_KEY}",
        f"settings_list_del:{LIST_KEY}",
        f"settings_list_replace:{LIST_KEY}",
        "settings_cancel",
    ]
    assert asyncio.run(state.get_state()) is None, "случайное сообщение не должно заменить список"


def test_list_editor_with_empty_list_still_offers_add(tmp_path):
    _ready(tmp_path)
    cb, _state = _open_editor(preset=None)
    assert "(0)" in cb.message.edited
    assert f"settings_list_add:{LIST_KEY}" in [d for _t, d in _buttons(cb.message.edited_markup)]


def test_text_key_editor_unchanged(tmp_path):
    _ready(tmp_path)
    cb, state = _open_editor(key="reg_complete_text", preset="Заявка принята")
    assert [d for _t, d in _buttons(cb.message.edited_markup)] == ["settings_cancel"]
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_value


@pytest.mark.parametrize("key", [
    "source_options", "city_options", "goal_options", "formats_options",
    "study_field_options", "role_caps_reg_manager", "role_caps_game_manager",
])
def test_every_list_key_gets_item_buttons(tmp_path, key):
    assert SETTINGS_SCHEMA[key]["type"] == "list"
    _ready(tmp_path)
    cb, _state = _open_editor(key=key, preset="a\nb")
    data = [d for _t, d in _buttons(cb.message.edited_markup)]
    assert f"settings_list_add:{key}" in data
    assert f"settings_list_del:{key}" in data
    assert f"settings_list_replace:{key}" in data


# ── ➕ добавить пункт ────────────────────────────────────────────────────────────────────────

def test_add_appends_one_item_to_the_end(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    cb, message, state = _add("Узнал от друга")
    assert "Сейчас в списке: 3" in cb.message.edited
    assert _items() == ["Соцсети Юлид", "Соцсети АЙСЕК", "Другое", "Узнал от друга"]
    assert asyncio.run(state.get_state()) is None
    assert "Добавил" in message.answers[-1]
    assert "(4)" in message.answers[-1], "после добавления показываем обновлённый список"
    assert message.markups[-1] is not None


def test_add_trims_whitespace(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    _add("   Реклама   ")
    assert _items()[-1] == "Реклама"


def test_add_to_empty_list_creates_it(tmp_path):
    _ready(tmp_path)
    _add("Первый")
    assert _items() == ["Первый"]


def test_add_rejects_duplicate_with_human_message(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    _cb, message, state = _add("другое")
    assert _items() == ["Соцсети Юлид", "Соцсети АЙСЕК", "Другое"]
    assert "Такой пункт уже есть" in message.answers[-1]
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_list_item


def test_add_rejects_empty_and_command(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    for bad in ("   ", "/start"):
        _cb, message, state = _add(bad)
        assert _items() == ["Соцсети Юлид", "Соцсети АЙСЕК", "Другое"], bad
        assert asyncio.run(state.get_state()) == EditSetting.waiting_for_list_item, bad
        assert message.answers, bad


def test_add_rejects_multi_item_message_and_points_to_replace(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    _cb, message, _state = _add("Один; Два")
    assert _items() == ["Соцсети Юлид", "Соцсети АЙСЕК", "Другое"]
    assert "Заменить список целиком" in message.answers[-1]


def test_add_warns_about_reserved_word_but_saves(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, "Соцсети"))
    _cb, message, _state = _add("Пропустить")
    assert _items() == ["Соцсети", "Пропустить"]
    assert "служебными словами" in message.answers[-1]


# ── 🗑 удалить пункт ─────────────────────────────────────────────────────────────────────────

def test_delete_picker_lists_every_item_as_button(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    cb = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(cb, _FakeState()))
    buttons = _buttons(cb.message.edited_markup)
    assert [t for t, _d in buttons[:-1]] == ["✖ Соцсети Юлид", "✖ Соцсети АЙСЕК", "✖ Другое"]
    assert all(d.startswith(f"settings_list_rm:{LIST_KEY}:") for _t, d in buttons[:-1])
    assert buttons[-1][1] == f"settings_edit:{LIST_KEY}"
    assert "какой пункт убрать" in cb.message.edited.lower()


def test_delete_tap_removes_exactly_that_item_and_names_it(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    pick = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(pick, _FakeState()))
    _text, second = _buttons(pick.message.edited_markup)[1]

    cb = _FakeCallback(second)
    asyncio.run(admin_settings_lists.settings_list_rm_go(cb, _FakeState()))
    assert _items() == ["Соцсети Юлид", "Другое"]
    alert, show_alert = cb.answers[-1]
    assert "Соцсети АЙСЕК" in alert and show_alert
    # экран списка перерисован с обновлённым содержимым
    assert "(2)" in cb.message.edited
    assert "• Соцсети АЙСЕК" not in cb.message.edited


def test_delete_stale_button_does_not_remove_wrong_item(tmp_path):
    """Список поменяли после того, как клавиатура удаления уже лежала в чате."""
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    pick = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(pick, _FakeState()))
    _text, first = _buttons(pick.message.edited_markup)[0]
    asyncio.run(db.set_setting(LIST_KEY, "Новый первый\nСоцсети АЙСЕК"))

    cb = _FakeCallback(first)
    asyncio.run(admin_settings_lists.settings_list_rm_go(cb, _FakeState()))
    assert _items() == ["Новый первый", "Соцсети АЙСЕК"]
    assert "изменился" in cb.answers[-1][0]


def test_delete_last_item_clears_setting(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, "Единственный"))
    pick = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(pick, _FakeState()))
    _text, only = _buttons(pick.message.edited_markup)[0]
    asyncio.run(admin_settings_lists.settings_list_rm_go(_FakeCallback(only), _FakeState()))
    assert asyncio.run(db.get_setting(LIST_KEY)) is None


def test_delete_on_empty_list_explains(tmp_path):
    _ready(tmp_path)
    cb = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(cb, _FakeState()))
    assert cb.answers[-1] == ("Список пуст — удалять нечего", True)
    assert cb.message.edited is None


# ── ✏️ заменить целиком ──────────────────────────────────────────────────────────────────────

def test_replace_screen_warns_and_starts_the_old_fsm_path(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
    cb = _FakeCallback(f"settings_list_replace:{LIST_KEY}")
    state = _FakeState()
    asyncio.run(admin_settings_lists.settings_list_replace_start(cb, state))
    assert "ВЕСЬ список" in cb.message.edited
    assert "заменит" in cb.message.edited
    assert "• Соцсети Юлид" in cb.message.edited
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_value
    assert asyncio.run(state.get_data()) == {"setting_key": LIST_KEY}

    # сохранение идёт прежним хендлером settings_edit_value
    message = _FakeMessage(text="Только один")
    asyncio.run(admin_settings.settings_edit_value(message, state))
    assert _items() == ["Только один"]


# ── per-city ─────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def percity_list(tmp_path):
    """На время теста делаем source_options per_city и включаем города; шапка = spb."""
    _ready(tmp_path)
    SETTINGS_SCHEMA[LIST_KEY]["per_city"] = True
    try:
        asyncio.run(db.set_setting("event_city_enabled", "on"))
        assert asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
        asyncio.run(db.set_setting(LIST_KEY, SOURCE_LIST))
        yield cities.per_city_key(LIST_KEY, "spb")
    finally:
        SETTINGS_SCHEMA[LIST_KEY].pop("per_city", None)


def test_percity_editor_shows_item_buttons_in_city_branch(percity_list):
    cb, state = _open_editor(preset=None)
    data = [d for _t, d in _buttons(cb.message.edited_markup)]
    assert f"settings_list_add:{LIST_KEY}" in data
    assert "🏙" in cb.message.edited
    assert asyncio.run(state.get_state()) is None


def test_percity_add_writes_composite_key_starting_from_global_list(percity_list):
    composed = percity_list
    _cb, message, _state = _add("Питерский источник")
    assert asyncio.run(db.get_setting(composed)) == SOURCE_LIST + "\nПитерский источник"
    assert asyncio.run(db.get_setting(LIST_KEY)) == SOURCE_LIST, "общий список не тронут"
    assert "Добавил" in message.answers[-1]


def test_percity_delete_writes_composite_key(percity_list):
    composed = percity_list
    pick = _FakeCallback(f"settings_list_del:{LIST_KEY}")
    asyncio.run(admin_settings_lists.settings_list_del_pick(pick, _FakeState()))
    _text, last = _buttons(pick.message.edited_markup)[2]
    asyncio.run(admin_settings_lists.settings_list_rm_go(_FakeCallback(last), _FakeState()))
    assert asyncio.run(db.get_setting(composed)) == "Соцсети Юлид\nСоцсети АЙСЕК"
    assert asyncio.run(db.get_setting(LIST_KEY)) == SOURCE_LIST


def test_percity_replace_uses_settings_edit_value_city_path(percity_list):
    composed = percity_list
    cb = _FakeCallback(f"settings_list_replace:{LIST_KEY}")
    state = _FakeState()
    asyncio.run(admin_settings_lists.settings_list_replace_start(cb, state))
    assert asyncio.run(state.get_data()) == {"setting_key": composed, "per_city_base": LIST_KEY}
    message = _FakeMessage(text="A; B")
    asyncio.run(admin_settings.settings_edit_value(message, state))
    assert asyncio.run(db.get_setting(composed)) == "A; B"
    assert asyncio.run(db.get_setting(LIST_KEY)) == SOURCE_LIST


def test_percity_add_refuses_when_header_moved_while_typing(percity_list):
    composed = percity_list
    cb = _FakeCallback(f"settings_list_add:{LIST_KEY}")
    state = _FakeState()
    asyncio.run(admin_settings_lists.settings_list_add_start(cb, state))
    assert asyncio.run(state.get_data())["list_key"] == composed
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))

    message = _FakeMessage(text="Поздний пункт")
    asyncio.run(admin_settings_lists.settings_list_add_item(message, state))
    assert asyncio.run(db.get_setting(composed)) is None
    assert asyncio.run(db.get_setting(LIST_KEY)) == SOURCE_LIST
    assert asyncio.run(state.get_state()) is None
    assert "заново" in message.answers[-1]
