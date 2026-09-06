"""Квик 260906-7zv (HELP-01/02/03) — редактор подсказок формата под вопросом анкеты
(«💡 Подсказка», экран «✏️ Тексты вопросов»).

Ключ `reg_help_<step>` ГЛОБАЛЬНЫЙ — без осей города и трека (D-1): валидатор ответа,
`reg_engine._validate_answer_core`, один на все города и оба трека (аргумент `participant_type`
в его теле не используется ни разу, `city_code` в него вообще не передаётся) — подсказка,
отличающаяся по городу/треку, могла бы разойтись с ним только во вранье делегату.

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path (та же конвенция, что у
tests/test_admin_percity_prompts_25.py, чей идиом FakeCallback/FakeMessage/FakeState/
FakeMsgIn и хелперы _kb_texts/_kb_callbacks здесь переиспользуются дословно).

Sections:
    A — has_help/help_default: базовые случаи + перекрёстная сходимость по всем шагам.
    B — help_text: регрессия (override/пустая строка/дефолт байт-в-байт).
    C — клавиатура: кнопка 💡 рядом с кнопкой вопроса, маркер «своя»/«стандартная».
    D — reg_help_edit: FSM несёт ГЛОБАЛЬНЫЙ ключ на обоих треках, экран правки.
    E — шаг без подсказки: отказ алертом, ничего не пишется.
    F — право (D-3): привязанный к городу менеджер отказывается, суперадмин — нет.
    G — сброс (reg_help_rst / reg_help_rst_go): подтверждение, идемпотентность, перерисовка.
    H — ADMIN_CAPS: все три префикса (+ :party) резолвятся в "settings".
"""
import asyncio

from config import config
from database import db
from handlers import admin_reg_percity
from handlers import admin_settings
from handlers.admin_caps import required_capability, role_caps_key, role_enabled_key
import cities
import reg_engine


ADMIN_ID = 920906
MANAGER_ID = 920907


def _admin_ready(tmp_path, db_name="test_reg_help_editor_260906.db"):
    config.DB_PATH = str(tmp_path / db_name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _add_bound_manager(manager_id=MANAGER_ID, city="spb"):
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))
    asyncio.run(db.set_setting(role_enabled_key("reg_manager"), "on"))
    asyncio.run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg;moderate_receipts;settings"))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.reply_markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.reply_markup = reply_markup
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

    async def answer(self, *a, **kw):
        pass


def _kb_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _kb_row_with(kb, callback_data):
    for row in kb.inline_keyboard:
        if any(b.callback_data == callback_data for b in row):
            return row
    return None


# ══════════════════════════════════════════════════════════════════════════════════════════
# A: has_help/help_default — базовые случаи + перекрёстная сходимость
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_has_help_true_for_phone_and_date_step():
    assert reg_engine.has_help("phone") is True
    assert reg_engine.has_help("arrival_date") is True  # тип date


def test_has_help_false_for_goal_and_unknown_step():
    assert reg_engine.has_help("goal") is False
    assert reg_engine.has_help("нет-такого-шага") is False


def test_has_help_matches_help_default_for_every_prompt_step(tmp_path):
    _admin_ready(tmp_path)

    async def scenario():
        for step_key, _label in admin_reg_percity._prompt_steps():
            default = await reg_engine.help_default(step_key, None)
            assert reg_engine.has_help(step_key) == (default is not None), step_key

    asyncio.run(scenario())


def test_help_default_resume_global_and_text_only_city(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("reg_resume_mode__city__spb", "text_only"))

    assert asyncio.run(reg_engine.help_default("resume", None)) == reg_engine.STEP_HELP["resume"]
    assert asyncio.run(reg_engine.help_default("resume", "spb")) == "Коротко, текстом в чате."


def test_help_default_date_step_returns_shared_date_help(tmp_path):
    _admin_ready(tmp_path)
    assert asyncio.run(reg_engine.help_default("birth_date", None)) == reg_engine._DATE_HELP


# ══════════════════════════════════════════════════════════════════════════════════════════
# B: help_text — регрессия (семантика `or default` байт-в-байт)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_help_text_regression_override_and_empty_reset(tmp_path):
    _admin_ready(tmp_path)

    assert asyncio.run(reg_engine.help_text("vk")) == reg_engine.STEP_HELP["vk"]

    asyncio.run(db.set_setting("reg_help_vk", "Кастом"))
    assert asyncio.run(reg_engine.help_text("vk")) == "Кастом"

    asyncio.run(db.set_setting("reg_help_vk", ""))
    assert asyncio.run(reg_engine.help_text("vk")) == reg_engine.STEP_HELP["vk"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# C: клавиатура — кнопка 💡 рядом с кнопкой вопроса, маркер «своя»/«стандартная»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_keyboard_help_button_shares_row_with_prompt_button(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_reg_percity.build_prompts_keyboard("full"))
    row = _kb_row_with(kb, "reg_prompt_edit:phone")
    assert row is not None
    assert "reg_help_edit:phone" in [b.callback_data for b in row]


def test_keyboard_no_help_button_for_step_without_help(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_reg_percity.build_prompts_keyboard("full"))
    row = _kb_row_with(kb, "reg_prompt_edit:goal")
    assert row is not None
    assert not any(cd and cd.startswith("reg_help_edit:") for cd in [b.callback_data for b in row])


def test_keyboard_party_track_help_callback_carries_party_suffix(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_reg_percity.build_prompts_keyboard("party"))
    flat = _kb_callbacks(kb)
    assert "reg_help_edit:phone:party" in flat


def test_keyboard_help_marker_default_then_custom(tmp_path):
    _admin_ready(tmp_path)
    kb = asyncio.run(admin_reg_percity.build_prompts_keyboard("full"))
    row = _kb_row_with(kb, "reg_prompt_edit:phone")
    texts = [b.text for b in row]
    assert any("стандартная" in t for t in texts)

    asyncio.run(db.set_setting("reg_help_phone", "Свой формат"))
    kb2 = asyncio.run(admin_reg_percity.build_prompts_keyboard("full"))
    row2 = _kb_row_with(kb2, "reg_prompt_edit:phone")
    texts2 = [b.text for b in row2]
    assert any("своя" in t for t in texts2)


# ══════════════════════════════════════════════════════════════════════════════════════════
# D: reg_help_edit — FSM несёт ГЛОБАЛЬНЫЙ ключ на обоих треках, экран правки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_help_edit_fsm_carries_global_key_full_track(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:phone")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    assert state.data == {"setting_key": "reg_help_phone"}
    assert state.state is not None


def test_reg_help_edit_fsm_carries_global_key_party_track_too(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:phone:party")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    # D-1: трек НЕ часть ключа — trackовый reg_help_phone__party в базе не появляется.
    assert state.data == {"setting_key": "reg_help_phone"}


def test_reg_help_edit_screen_shows_default_as_example_and_global_note(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:phone")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    text = cb.message.text
    assert reg_engine.STEP_HELP["phone"] in text
    assert "город" in text.lower()
    assert "«-»" in text


def test_reg_help_edit_no_reset_button_until_custom_set(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:phone")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))
    assert not any(cd and cd.startswith("reg_help_rst:") for cd in _kb_callbacks(cb.message.markup))

    asyncio.run(db.set_setting("reg_help_phone", "Свой формат"))
    cb2 = FakeCallback("reg_help_edit:phone")
    state2 = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb2, state2))
    assert any(cd == "reg_help_rst:phone" for cd in _kb_callbacks(cb2.message.markup))


def test_settings_edit_value_round_trip_writes_and_clears_global_key(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:phone")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    asyncio.run(admin_settings.settings_edit_value(
        FakeMsgIn("Только цифры, например 79161234567"), state,
    ))
    assert asyncio.run(reg_engine.help_text("phone")) == "Только цифры, например 79161234567"

    cb2 = FakeCallback("reg_help_edit:phone")
    state2 = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb2, state2))
    asyncio.run(admin_settings.settings_edit_value(FakeMsgIn("-"), state2))
    assert asyncio.run(reg_engine.help_text("phone")) == reg_engine.STEP_HELP["phone"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# E: шаг без подсказки — отказ алертом, ничего не пишется
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_help_edit_step_without_help_refuses_and_writes_nothing(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("reg_help_edit:goal")
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    assert cb.answers and cb.answers[0][1] is True  # show_alert
    assert state.data == {}
    assert asyncio.run(db.get_setting("reg_help_goal")) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# F: право (D-3) — привязанный к городу менеджер отказывается, суперадмин — нет
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_bound_manager_refused_on_edit_and_reset_go(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    _add_bound_manager()
    assert len(cities.city_codes()) >= 2  # msk/spb/tyumen из config.EVENT_CITIES по умолчанию

    cb = FakeCallback("reg_help_edit:phone", user_id=MANAGER_ID)
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))
    assert cb.answers and cb.answers[0][1] is True
    assert state.data == {}

    # Крафченый reg_help_rst_go тоже отказывает, значение не меняется.
    cb2 = FakeCallback("reg_help_rst_go:phone", user_id=MANAGER_ID)
    asyncio.run(admin_reg_percity.reg_help_rst_go(cb2))
    assert cb2.answers and cb2.answers[0][1] is True
    assert asyncio.run(db.get_setting("reg_help_phone")) is None


def test_superadmin_with_city_header_edits_successfully(tmp_path):
    _admin_ready(tmp_path)
    _enable_cities()
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))

    cb = FakeCallback("reg_help_edit:phone", user_id=ADMIN_ID)
    state = FakeState()
    asyncio.run(admin_reg_percity.reg_help_edit(cb, state))

    assert state.data == {"setting_key": "reg_help_phone"}
    assert not (cb.answers and cb.answers[0][1] is True and cb.answers[0][0])


# ══════════════════════════════════════════════════════════════════════════════════════════
# G: сброс — reg_help_rst называет вопрос, reg_help_rst_go идемпотентен и перерисовывает
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_reg_help_rst_names_question_and_shows_default(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("reg_help_phone", "Свой формат"))

    cb = FakeCallback("reg_help_rst:phone")
    asyncio.run(admin_reg_percity.reg_help_rst(cb))

    label = dict(admin_reg_percity._prompt_steps())["phone"]
    assert label in cb.message.text
    assert reg_engine.STEP_HELP["phone"] in cb.message.text


def test_reg_help_rst_go_deletes_and_is_idempotent_and_redraws(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("reg_help_phone", "Свой формат"))

    cb = FakeCallback("reg_help_rst_go:phone")
    asyncio.run(admin_reg_percity.reg_help_rst_go(cb))
    assert asyncio.run(db.get_setting("reg_help_phone")) is None
    assert cb.message.edit_calls == 1

    cb2 = FakeCallback("reg_help_rst_go:phone")
    asyncio.run(admin_reg_percity.reg_help_rst_go(cb2))  # idempotent
    assert asyncio.run(db.get_setting("reg_help_phone")) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# H: ADMIN_CAPS — все три префикса (+ :party) резолвятся в "settings"
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_required_capability_settings_for_all_three_prefixes_and_party():
    for cd in (
        "reg_help_edit:phone", "reg_help_edit:phone:party",
        "reg_help_rst:phone", "reg_help_rst:phone:party",
        "reg_help_rst_go:phone", "reg_help_rst_go:phone:party",
    ):
        assert required_capability(callback_data=cd) == "settings", cd
