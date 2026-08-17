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
import settings_schema as s


# ── Task 1: состав per_city-ключей реестра ──────────────────────────────────────────

# Явный список-литерал первой волны + Task 2 (menu_*) — 21 имя (12 CONTEXT B + 9
# MENU_BUTTONS). Новый per_city-ключ обязан быть осознанным изменением ЭТОГО списка, а не
# побочным эффектом правки реестра (T-092-03).
EXPECTED_PER_CITY_KEYS = {
    "start_text", "start_text_registered", "reg_complete_text", "approve_text",
    "contact_person", "contact_vk", "contact_tg", "event_date", "event_time",
    "event_place_name", "event_place_address", "registration_mode",
    "menu_referral", "menu_invites", "menu_info", "menu_program", "menu_speakers",
    "menu_contacts", "menu_question", "menu_coins", "menu_game_tasks",
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
    from handlers.registration import REG_DEFAULTS
    from keyboards.builders import MENU_BUTTONS

    assert not (set(REG_DEFAULTS) & {k for k, _ in MENU_BUTTONS})
