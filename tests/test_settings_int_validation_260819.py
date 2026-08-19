"""Quick 260819: валидация значения по типу ключа в settings_edit_value (до записи).

- int-ключ: «abc» / «-5» -> отказ с подсказкой, FSM НЕ сброшен, в БД ничего не записано;
  «120» -> запись; « 120 » -> запись «120» (нормализовано); «0» -> запись; «-» -> сброс как
  раньше.
- enum-ключ: значение вне options -> отказ; значение в другом регистре -> каноническое.
- text-ключ: без изменений (любой текст сохраняется).
- Чистый валидатор handlers/settings_validation.py: согласован с _parse_setting (что
  прошло валидацию — читается как число, а не как дефолт).

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp. Фейки
скопированы по конвенции сьюта (tests/test_audit_criticals_260816.py).
"""
import asyncio

import pytest

from config import config
from database import db
from handlers import admin_settings
from handlers.settings_validation import validate_setting_value
from settings_schema import SETTINGS_SCHEMA, _parse_setting

ADMIN_ID = 900801


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_validation.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = None


class _FakeSettingsMessage:
    def __init__(self, uid=ADMIN_ID, text=""):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.html_text = text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


class _FakeFSMState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = "EditSetting:waiting_for_value"

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


def _edit(tmp_path, key, text, preset=None):
    """Прогнать settings_edit_value для key/text; вернуть (message, state, saved_raw)."""
    _ready(tmp_path)
    message = _FakeSettingsMessage(text=text)
    state = _FakeFSMState({"setting_key": key})

    async def go():
        if preset is not None:
            await db.set_setting(key, preset)
        await admin_settings.settings_edit_value(message, state)
        return await db.get_setting(key)

    saved = asyncio.run(go())
    return message, state, saved


INT_KEY = "nudge_after_minutes"  # type int, default 120


def test_schema_has_no_min_max_fields():
    """Фиксируем предпосылку валидатора: схема не задаёт min/max — правило «целое >= 0»."""
    assert SETTINGS_SCHEMA[INT_KEY]["type"] == "int"
    for entry in SETTINGS_SCHEMA.values():
        assert "min" not in entry and "max" not in entry


# ── int: отказ ────────────────────────────────────────────────────────────────────────────────

def test_int_key_rejects_non_number_keeps_state_and_writes_nothing(tmp_path):
    message, state, saved = _edit(tmp_path, INT_KEY, "abc")
    assert saved is None, "непарсящееся значение записано в int-ключ"
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"
    assert message.answers, "менеджер должен получить подсказку"
    hint = message.answers[-1]
    assert "целое число" in hint.lower()
    assert "120" in hint  # пример из дефолта схемы
    assert "«-»" in hint  # как сбросить


def test_int_key_rejects_negative(tmp_path):
    message, state, saved = _edit(tmp_path, INT_KEY, "-5")
    assert saved is None
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"
    assert "отрицательн" in message.answers[-1].lower()


def test_int_key_rejects_float(tmp_path):
    _message, state, saved = _edit(tmp_path, INT_KEY, "12.5")
    assert saved is None
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"


def test_int_rejection_does_not_overwrite_existing_value(tmp_path):
    _message, _state, saved = _edit(tmp_path, INT_KEY, "abc", preset="60")
    assert saved == "60"


# ── int: запись ──────────────────────────────────────────────────────────────────────────────

def test_int_key_saves_plain_number(tmp_path):
    _message, state, saved = _edit(tmp_path, INT_KEY, "120")
    assert saved == "120"
    assert asyncio.run(state.get_state()) is None  # FSM очищен после успешной записи


def test_int_key_strips_whitespace(tmp_path):
    _message, state, saved = _edit(tmp_path, INT_KEY, "  120  ")
    assert saved == "120"
    assert asyncio.run(state.get_state()) is None


def test_int_key_accepts_zero(tmp_path):
    """0 осмыслен для части ключей (game_resubmit_limit: «0 = без лимита»)."""
    _message, _state, saved = _edit(tmp_path, "game_resubmit_limit", "0")
    assert saved == "0"


def test_int_key_dash_resets_as_before(tmp_path):
    _message, state, saved = _edit(tmp_path, INT_KEY, "-", preset="60")
    assert saved is None
    assert asyncio.run(state.get_state()) is None


# ── enum ─────────────────────────────────────────────────────────────────────────────────────

def test_enum_key_rejects_unknown_option(tmp_path):
    message, state, saved = _edit(tmp_path, "event_type", "party")
    assert saved is None
    assert asyncio.run(state.get_state()) == "EditSetting:waiting_for_value"
    hint = message.answers[-1]
    assert "forum" in hint and "conference" in hint and "custom" in hint


def test_enum_key_normalizes_case(tmp_path):
    _message, state, saved = _edit(tmp_path, "event_type", " Conference ")
    assert saved == "conference"
    assert asyncio.run(state.get_state()) is None


# ── text: без изменений ──────────────────────────────────────────────────────────────────────

def test_text_key_unchanged(tmp_path):
    _message, state, saved = _edit(tmp_path, "event_place_name", "abc 12.5 -5")
    assert saved == "abc 12.5 -5"
    assert asyncio.run(state.get_state()) is None


# ── чистый валидатор ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["120", " 120 ", "0", "7"])
def test_validator_int_ok_is_readable_by_parse_setting(raw):
    value, error = validate_setting_value(INT_KEY, raw)
    assert error is None
    assert value == str(int(raw))
    parsed = _parse_setting(INT_KEY, value)
    # что прошло валидацию — читается как число (0 -> дефолт по контракту _parse_setting)
    assert parsed == (int(raw) if int(raw) > 0 else SETTINGS_SCHEMA[INT_KEY]["default"])


@pytest.mark.parametrize("raw", ["abc", "-1", "1e3", "12,5", ""])
def test_validator_int_rejects(raw):
    value, error = validate_setting_value(INT_KEY, raw)
    assert value is None and error


def test_validator_per_city_composite_uses_base_type():
    value, error = validate_setting_value("nudge_after_minutes__city__msk", "abc")
    assert value is None and error


def test_validator_unknown_key_and_other_types_pass_through():
    assert validate_setting_value("no_such_key_xyz", "whatever") == ("whatever", None)
    assert validate_setting_value("event_place_name", "Зал 3") == ("Зал 3", None)
