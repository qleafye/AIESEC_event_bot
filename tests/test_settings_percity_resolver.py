"""Phase 09.2 (A) — per-city settings override: registry flag + resolver.

Назначение файла (контракт, а не просто набор проверок):

    «Реестр — единственный источник правды о том, что можно переопределить по городу.
    Модуль городов выключен -> резолвер отдаёт РОВНО то же, что get_setting/
    get_setting_typed, при любом городе и любом сохранённом переопределении.»

Секции (наполняются по задачам плана 09.2-01):
    Task 1 — состав ключей с `per_city` в SETTINGS_SCHEMA (тест-сторож, равенство множеств).
    Task 2 — `menu_*` как enum-записи реестра (group "menu"), не toggle, не в REG_DEFAULTS.
    Task 3 — резолвер `cities.get_setting_for_city`/`get_setting_typed_for_city`.

pytest-asyncio в этом окружении нет — каждый async-хелпер гоняется через asyncio.run(), а
config.DB_PATH указывает на файл в tmp_path; та же конвенция, что в
tests/test_city_offparity_phase72.py.
"""
import asyncio

from config import config
from database import db
import settings_schema as s
import cities


# ── Task 1: состав per_city-ключей реестра ──────────────────────────────────────────

# Явный список-литерал первой волны + Task 2 (menu_*) — 22 имени (12 CONTEXT B + 10
# MENU_BUTTONS). Новый per_city-ключ обязан быть осознанным изменением ЭТОГО списка, а не
# побочным эффектом правки реестра (T-092-03).
# menu_miniapp (Phase 19 D-10) — обычная enum-запись группы "menu" (settings_schema.py:1466),
# per_city у неё такой же, как у остальных menu_*; этого требуют соседние тесты
# test_menu_keys_match_menu_buttons_literal / test_menu_keys_are_enum_on_off_default_on_per_city.
EXPECTED_PER_CITY_KEYS = {
    "start_text", "start_text_registered", "reg_complete_text", "approve_text",
    "contact_person", "contact_vk", "contact_tg", "event_date", "event_time",
    "event_place_name", "event_place_address", "registration_mode",
    "menu_referral", "menu_invites", "menu_info", "menu_program", "menu_speakers",
    "menu_contacts", "menu_question", "menu_coins", "menu_game_tasks", "menu_miniapp",
}


def test_per_city_key_set_matches_first_wave_literal():
    actual = {k for k, v in s.SETTINGS_SCHEMA.items() if v.get("per_city")}
    assert actual == EXPECTED_PER_CITY_KEYS, sorted(actual)


def test_approve_text_party_has_no_per_city_flag():
    # Q2 resolution: city override composes with the base approve_text only.
    assert not s.SETTINGS_SCHEMA["approve_text__party"].get("per_city")


def test_no_per_city_key_is_photo_or_file_type():
    for key, entry in s.SETTINGS_SCHEMA.items():
        if entry.get("per_city"):
            assert entry["type"] not in ("photo", "file"), key


# ── Task 2: menu_* как enum-записи реестра (group "menu") ──────────────────────────

def test_menu_keys_match_menu_buttons_literal():
    from keyboards.builders import MENU_BUTTONS

    menu_button_keys = {k for k, _ in MENU_BUTTONS}
    registry_menu_keys = {k for k, v in s.SETTINGS_SCHEMA.items() if v["group"] == "menu"}
    assert menu_button_keys == registry_menu_keys


def test_menu_keys_are_enum_on_off_default_on_per_city():
    from keyboards.builders import MENU_BUTTONS

    for key, label in MENU_BUTTONS:
        entry = s.SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum", key
        assert entry["options"] == ["on", "off"], key
        assert entry["default"] == "on", key
        assert entry["group"] == "menu", key
        assert entry.get("per_city") is True, key
        assert entry["label"] == label, key
        assert entry["prompt"] is None, key


def test_menu_parse_equivalence_with_live_idiom():
    """`_parse_setting("menu_info", raw)` for the "should the button show" decision must
    agree with today's live idiom `val is None or val == "on"`, for every raw value the
    toggle handler actually ever writes (None/"on"/"off") plus an unrecognized value
    ("junk" — never written, but must still hide the button like today)."""
    # Literal assertions from CONTEXT/plan behavior: None and "" (both falsy) resolve to
    # the registry default "on" via the enum branch's `raw if raw else default` (D-15,
    # the SAME "falsy -> default" contract every other enum key already uses). "" is not a
    # raw value the toggle handler ever writes (it only ever writes "on"/"off"/leaves the
    # row absent), so this is a documented, unreachable-in-practice widening of the old
    # idiom, not a live behavior change.
    assert s._parse_setting("menu_info", None) == "on"
    assert s._parse_setting("menu_info", "") == "on"
    assert s._parse_setting("menu_info", "off") == "off"
    assert s._parse_setting("menu_info", "junk") == "junk"

    def live_idiom_shows_button(val):
        return val is None or val == "on"

    # Equivalence table over every REACHABLE raw value (None/"on"/"off") plus "junk" (an
    # unrecognized value, which must still resolve to "hidden" under both the old raw
    # idiom and the registry).
    for raw in (None, "on", "off", "junk"):
        resolved_shows_button = s._parse_setting("menu_info", raw) == "on"
        assert resolved_shows_button == live_idiom_shows_button(raw), raw


def test_menu_keys_not_in_reg_defaults():
    from handlers.reg_schema import REG_DEFAULTS
    from keyboards.builders import MENU_BUTTONS

    assert not (set(REG_DEFAULTS) & {k for k, _ in MENU_BUTTONS})


# ── Task 3: резолвер cities.get_setting_for_city / get_setting_typed_for_city ──────

def _db_ready(tmp_path, name):
    """Свежая база. `event_city_enabled` НЕ выставляется, если явно не сказано иначе —
    так тест ловит и случайное изменение дефолта тумблера."""
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_per_city_key_builder_closed_set():
    code = cities.city_codes()[0]
    assert cities.per_city_key("start_text", code) == f"start_text__city__{code}"
    assert cities.per_city_key("start_text", "zzz-not-a-real-city") is None


def test_is_per_city():
    assert cities.is_per_city("start_text") is True
    assert cities.is_per_city("event_name") is False
    assert cities.is_per_city("нетТакого") is False


def test_get_setting_for_city_calls_cities_module_on_and_normalize_city():
    import inspect

    src = inspect.getsource(cities.get_setting_for_city)
    assert "cities_module_on" in src
    assert "normalize_city" in src


def test_resolver_module_off_is_byte_identical_to_get_setting(tmp_path):
    """Свежая база, event_city_enabled НЕ выставляется вовсе — переопределения для ВСЕХ
    городов записаны, но резолвер обязан вернуть только глобальное значение (T-092-02)."""
    _db_ready(tmp_path, "test_percity_offparity.db")

    async def scenario():
        await db.set_setting("start_text", "Глобальный текст")
        for code in cities.city_codes():
            await db.set_setting(cities.per_city_key("start_text", code), f"Текст для {code}")
        for code in list(cities.city_codes()) + [None, "unknown-code"]:
            resolved = await cities.get_setting_for_city("start_text", code)
            direct = await db.get_setting("start_text")
            assert resolved == direct == "Глобальный текст"

    asyncio.run(scenario())


def test_resolver_non_per_city_key_ignores_override_even_when_module_on(tmp_path):
    _db_ready(tmp_path, "test_percity_nonflagged.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("event_name", "Глобальное название")
        code = cities.city_codes()[0]
        await db.set_setting(f"event_name__city__{code}", "Городское название")
        resolved = await cities.get_setting_for_city("event_name", code)
        assert resolved == "Глобальное название"

    asyncio.run(scenario())


def test_resolver_city_code_none_returns_global(tmp_path):
    _db_ready(tmp_path, "test_percity_none_city.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("start_text", "Глобальный текст")
        resolved = await cities.get_setting_for_city("start_text", None)
        assert resolved == "Глобальный текст"

    asyncio.run(scenario())


def test_resolver_unknown_city_code_normalizes_to_default(tmp_path):
    _db_ready(tmp_path, "test_percity_unknown_city.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        default_code = cities.default_city_code()
        await db.set_setting("start_text", "Глобальный текст")
        await db.set_setting(
            cities.per_city_key("start_text", default_code), "Текст города по умолчанию",
        )
        resolved = await cities.get_setting_for_city("start_text", "totally-unknown-code")
        assert resolved == "Текст города по умолчанию"

    asyncio.run(scenario())


def test_resolver_override_present_returns_override(tmp_path):
    _db_ready(tmp_path, "test_percity_override_present.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        code = cities.city_codes()[0]
        await db.set_setting("start_text", "Глобальный текст")
        await db.set_setting(cities.per_city_key("start_text", code), "Городской текст")
        resolved = await cities.get_setting_for_city("start_text", code)
        assert resolved == "Городской текст"

    asyncio.run(scenario())


def test_resolver_empty_or_deleted_override_falls_back_to_global(tmp_path):
    _db_ready(tmp_path, "test_percity_empty_override.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        code = cities.city_codes()[0]
        await db.set_setting("start_text", "Глобальный текст")
        key = cities.per_city_key("start_text", code)

        await db.set_setting(key, "")
        resolved = await cities.get_setting_for_city("start_text", code)
        assert resolved == "Глобальный текст"

        await db.set_setting(key, "Городской текст")
        resolved = await cities.get_setting_for_city("start_text", code)
        assert resolved == "Городской текст"

        await db.delete_setting(key)
        resolved = await cities.get_setting_for_city("start_text", code)
        assert resolved == "Глобальный текст"

    asyncio.run(scenario())


def test_typed_resolver_registration_mode(tmp_path):
    _db_ready(tmp_path, "test_percity_typed_regmode.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        code = cities.city_codes()[0]
        await db.set_setting(cities.per_city_key("registration_mode", code), "full")
        resolved = await cities.get_setting_typed_for_city("registration_mode", code)
        assert resolved == "full"

        other_code = cities.city_codes()[-1]
        resolved_other = await cities.get_setting_typed_for_city("registration_mode", other_code)
        expected_global = await s.get_setting_typed("registration_mode")
        assert resolved_other == expected_global

    asyncio.run(scenario())


def test_typed_resolver_menu_toggle(tmp_path):
    _db_ready(tmp_path, "test_percity_typed_menu.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        code = cities.city_codes()[0]
        await db.set_setting(cities.per_city_key("menu_info", code), "off")
        resolved = await cities.get_setting_typed_for_city("menu_info", code)
        assert resolved == "off"

        other_code = cities.city_codes()[-1]
        resolved_other = await cities.get_setting_typed_for_city("menu_info", other_code)
        assert resolved_other == "on"

    asyncio.run(scenario())


def test_city_override_codes(tmp_path):
    _db_ready(tmp_path, "test_percity_override_codes.db")

    async def scenario():
        # Module off -> empty list even with overrides already written.
        first = cities.city_codes()[0]
        await db.set_setting(f"start_text__city__{first}", "Городской текст")
        assert await cities.city_override_codes("start_text") == []

        await db.set_setting("event_city_enabled", "on")
        assert await cities.city_override_codes("start_text") == [first]

        last = cities.city_codes()[-1]
        await db.set_setting(f"start_text__city__{last}", "Другой городской текст")
        result = await cities.city_override_codes("start_text")
        # Order follows CITIES, only non-empty overrides included.
        expected_order = [c for c in cities.city_codes() if c in (first, last)]
        assert result == expected_order

        # Non-per_city key -> always empty.
        assert await cities.city_override_codes("event_name") == []

    asyncio.run(scenario())
