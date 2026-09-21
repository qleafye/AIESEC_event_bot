"""Phase 31 (31-03, D-15/D-18/D-21/D-30): реестровая часть под правила автоотказа.

- Тип `date_only` (T-31-03-01): дата БЕЗ времени, отдельная от `date` (`payment_deadline`
  сохраняет время суток — по нему планировщик ставит напоминания T-3/T-1). Круговой тест
  «записали 31.10.2026 через set_setting, прочитали get_setting_typed("forum_date")» ловит
  именно найденный исследованием баг: переиспользование `date` как есть молча сбрасывало бы
  «дд.мм.гггг» в default, потому что формат `date` требует ещё и «ЧЧ:ММ».
- Три ключа реестра: `forum_date` (per_city, event), `reject_rules_enabled` (toggle, apps,
  default off — D-15), `reject_rules_return_text` (text, reg — D-18, попадает в делегатский
  корпус перевода автоматически).

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp_path. Конвенция
сьюта (см. tests/test_settings_int_validation_260819.py).
"""
import asyncio
from datetime import datetime

from config import config
from database import db
from services.i18n_sources import delegate_registry_keys
from settings_schema import SETTINGS_SCHEMA, _parse_setting, get_setting_typed
from settings_validation import validate_setting_value


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reject_rules_settings.db")
    asyncio.run(db.init_db())


# ── Задача 1: тип date_only ─────────────────────────────────────────────────────────────

def test_forum_date_round_trip_survives_save_and_read(tmp_path):
    """Круговой тест, который ловит T-31-03-01: менеджер вводит РОВНО то, что просит
    подсказка («дд.мм.гггг»), значение сохраняется и читается как datetime, а не молча
    становится None."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("forum_date", "31.10.2026")
        return await get_setting_typed("forum_date")

    value = asyncio.run(go())
    assert value == datetime(2026, 10, 31)
    assert value is not None


def test_forum_date_garbage_input_falls_back_to_default(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("forum_date", "не дата")
        return await get_setting_typed("forum_date")

    assert asyncio.run(go()) is None


def test_date_only_parse_direct():
    assert _parse_setting("forum_date", "31.10.2026") == datetime(2026, 10, 31)
    assert _parse_setting("forum_date", "мусор") is None
    assert _parse_setting("forum_date", None) is None


def test_date_only_validation_rejects_nonexistent_date():
    value, error = validate_setting_value("forum_date", "31.02.2026")
    assert value is None
    assert error  # непустой текст ошибки (CLAUDE.md: объясняет, что прислать)
    assert "ДД.ММ.ГГГГ" in error or "дд.мм.гггг" in error.lower()


def test_date_only_validation_normalizes_missing_leading_zeros():
    value, error = validate_setting_value("forum_date", "1.9.2026")
    assert error is None
    assert value == "01.09.2026"
    # Нормализованное значение переживает следующий круг «сохранили — прочитали».
    assert _parse_setting("forum_date", value) == datetime(2026, 9, 1)


def test_date_only_validation_rejects_garbage_with_example():
    value, error = validate_setting_value("forum_date", "abc")
    assert value is None
    assert "15.10.2026" in error


def test_payment_deadline_still_requires_and_accepts_time():
    """Старый тип `date` не тронут — payment_deadline по-прежнему требует ЧЧ:ММ."""
    assert SETTINGS_SCHEMA["payment_deadline"]["type"] == "date"
    parsed = _parse_setting("payment_deadline", "15.08.2026 23:59")
    assert parsed == datetime(2026, 8, 15, 23, 59)
    # «дд.мм.гггг» без времени, который forum_date ПРИНИМАЕТ, для payment_deadline — брак.
    assert _parse_setting("payment_deadline", "15.08.2026") is None


# ── Задача 2: три ключа реестра ─────────────────────────────────────────────────────────

def test_three_keys_have_expected_shape():
    forum_date = SETTINGS_SCHEMA["forum_date"]
    assert forum_date["type"] == "date_only"
    assert forum_date["group"] == "event"
    assert forum_date["per_city"] is True

    reject_rules_enabled = SETTINGS_SCHEMA["reject_rules_enabled"]
    assert reject_rules_enabled["type"] == "toggle"
    assert reject_rules_enabled["group"] == "apps"
    assert reject_rules_enabled["default"] == "off"

    reject_rules_return_text = SETTINGS_SCHEMA["reject_rules_return_text"]
    assert reject_rules_return_text["type"] == "text"
    assert reject_rules_return_text["group"] == "reg"


def test_reject_rules_enabled_reads_as_false_on_empty_db(tmp_path):
    """D-15: общий рубильник по умолчанию выключен — пустая база, менеджер ничего не трогал."""
    _ready(tmp_path)
    value = asyncio.run(get_setting_typed("reject_rules_enabled"))
    assert value is False


def test_reject_rules_return_text_is_a_delegate_text():
    """D-18: делегатский текст возврата попадает в корпус машинного перевода автоматически
    (group "reg" + type "text" -> services.i18n_sources.delegate_registry_keys), без второго
    ключа реестра и без ручной врезки в очередь перевода."""
    assert "reject_rules_return_text" in delegate_registry_keys()


def test_forum_date_resolves_per_city_with_city_override_winning(tmp_path):
    """forum_date помечен per_city и резолвится через cities.get_setting_typed_for_city —
    городской override побеждает глобальное значение (тот же приём, что
    test_typed_resolver_registration_mode в tests/test_settings_percity_resolver.py)."""
    _ready(tmp_path)
    import cities

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        code = cities.city_codes()[0]
        other_code = cities.city_codes()[-1]

        await db.set_setting("forum_date", "31.10.2026")
        resolved_other = await cities.get_setting_typed_for_city("forum_date", other_code)
        assert resolved_other == datetime(2026, 10, 31)

        await db.set_setting(cities.per_city_key("forum_date", code), "01.11.2026")
        resolved = await cities.get_setting_typed_for_city("forum_date", code)
        assert resolved == datetime(2026, 11, 1)

    asyncio.run(scenario())


def test_forum_date_in_event_field_order():
    from handlers.admin_settings import _EVENT_FIELD_ORDER, _REG_FIELD_ORDER
    assert "forum_date" in _EVENT_FIELD_ORDER
    assert "reject_rules_return_text" in _REG_FIELD_ORDER
