"""Quick 260924-4qf: `event_name` разведён на именительный (`event_name`) и родительный
(`event_name_genitive`) падеж — прод SkillUp вписал в `event_name` родительный падеж по
подсказке, и шапка дашборда/Mini App показала «форума SkillUp 5». Решение владельца:
`event_name` — именительный (шапки, не трогаем потребителей), новый `event_name_genitive` —
родительный, подставляется в дефолт вопроса анкеты `expectations` («Что ты ожидаешь от …?»).

БД поднимается по образцу `tests/test_reg_engine_parity.py::_ready` (pytest-asyncio
недоступен — async идёт через `asyncio.run()`, правило проекта).
"""
import asyncio

from config import config
from database.db import set_setting
from handlers import admin_settings
from reg_engine import _default_prompt_text
from domain.settings.schema import SETTINGS_SCHEMA
from domain.settings.synonyms import SETTINGS_SYNONYMS
from tests._dbtpl import fast_init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_event_name_genitive_260924.db")
    fast_init_db()


def test_expectations_prefers_genitive_over_name(tmp_path):
    _ready(tmp_path)

    async def _run():
        await set_setting("event_name", "СкиллАп 5")
        await set_setting("event_name_genitive", "форума СкиллАп")
        return await _default_prompt_text("expectations", None)

    text = asyncio.run(_run())
    assert text == "Что ты ожидаешь от форума СкиллАп? Что хотел(а) бы узнать или получить?"


def test_expectations_falls_back_to_name_without_genitive(tmp_path):
    _ready(tmp_path)

    async def _run():
        await set_setting("event_name", "форума Юлид")
        return await _default_prompt_text("expectations", None)

    text = asyncio.run(_run())
    assert text == "Что ты ожидаешь от форума Юлид? Что хотел(а) бы узнать или получить?"


def test_expectations_falls_back_to_literal_without_either(tmp_path):
    _ready(tmp_path)

    async def _run():
        return await _default_prompt_text("expectations", None)

    text = asyncio.run(_run())
    assert text == "Что ты ожидаешь от мероприятия? Что хотел(а) бы узнать или получить?"


def test_event_name_genitive_registered_right_after_event_name():
    assert "event_name_genitive" in SETTINGS_SCHEMA
    entry = SETTINGS_SCHEMA["event_name_genitive"]
    assert entry["type"] == "text"
    assert entry["group"] == "event"
    assert entry["default"] is None
    assert "per_city" not in entry

    keys = list(SETTINGS_SCHEMA)
    assert keys.index("event_name_genitive") == keys.index("event_name") + 1


def test_event_name_genitive_in_admin_event_field_order_right_after_event_name():
    order = admin_settings._EVENT_FIELD_ORDER
    assert order.index("event_name_genitive") == order.index("event_name") + 1


def test_event_name_genitive_has_synonyms():
    assert "event_name_genitive" in SETTINGS_SYNONYMS
    assert len(SETTINGS_SYNONYMS["event_name_genitive"]) >= 2
