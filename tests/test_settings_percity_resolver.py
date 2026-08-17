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

# Явный список-литерал — 12 имён первой волны (CONTEXT B). Новый per_city-ключ обязан
# быть осознанным изменением ЭТОГО списка, а не побочным эффектом правки реестра.
EXPECTED_PER_CITY_KEYS = {
    "start_text", "start_text_registered", "reg_complete_text", "approve_text",
    "contact_person", "contact_vk", "contact_tg", "event_date", "event_time",
    "event_place_name", "event_place_address", "registration_mode",
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
