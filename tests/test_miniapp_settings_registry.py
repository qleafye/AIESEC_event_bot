"""Phase 22 Plan 04 Task 1 (WEB-SET-01/03/04, D-01..D-04, D-11): `GET /app/api/admin/settings/all`
— весь правимый реестр одним запросом (разделы → группы → строки, шапка города, тексты экрана)
и `POST /app/api/admin/settings/city` — переключение шапки той же функцией, что у бота.

Харнесс — `tests/test_miniapp_routes.py`; образец стиля — `tests/test_miniapp_settings_api.py`.
"""
from __future__ import annotations

import asyncio

import pytest

import settings_ops
from cities import ALL_CITIES, PER_CITY_SEP, city_codes
from database import db as bot_db
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from settings_synonyms import SETTINGS_SYNONYMS

from tests.test_miniapp_routes import (
    ADMIN_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

# Менеджер с правом `settings`, привязанный к городу spb (reg_manager, права роли расширены
# через реестр `role_caps_reg_manager` — так же, как это сделал бы владелец в боте).
SETTINGS_MANAGER_SPB = 900610

ITEM_FIELDS = {
    "key", "base_key", "label", "help", "type", "options", "value", "raw", "display",
    "is_default", "default", "per_city", "is_city_override", "city_override_count",
    "city_override_labels", "dangerous", "confirm_text", "html", "max_len", "search_terms",
    "editable",
    # Quick 260904-8o3 Task 3 (E5/E6): различает ключи оформления внутри группы "miniapp"
    # (вперемешку с обычными текстами) — без второго хардкод-списка ключей во фронте.
    "theme_key",
}


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, name="miniapp_settings_registry.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _seed(staff=[(SETTINGS_MANAGER_SPB, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    return _client(_cfg(db_path))


def _all(client, user=ADMIN_ID):
    return client.get("/app/api/admin/settings/all", headers=_hdr(user))


def _items(body) -> list[dict]:
    out = []
    for section in body["sections"]:
        out.extend(section["toggles"])
        for group in section["groups"]:
            out.extend(group["items"])
    return out


def _item(body, key) -> dict:
    return next(i for i in _items(body) if i["key"] == key)


# ── весь реестр, по разделам, без ролей ──────────────────────────────────────────────────

def test_all_returns_every_editable_key_exactly_once(tmp_path):
    client = _setup(tmp_path)
    resp = _all(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    all_items = _items(body)
    keys = [i["key"] for i in all_items]
    assert len(all_items) == len(settings_ops.editable_keys())
    assert len(set(keys)) == len(keys)
    assert set(keys) == set(settings_ops.editable_keys())
    assert body["total"] == len(all_items)


def test_sections_follow_section_groups_order_and_hide_empty(tmp_path):
    client = _setup(tmp_path)
    body = _all(client).json()
    tokens = [s["token"] for s in body["sections"]]
    expected_order = [s for s, _l, _g in settings_ops.SECTION_GROUPS]
    assert tokens == [t for t in expected_order if t in tokens]
    labels = dict((s, l) for s, l, _g in settings_ops.SECTION_GROUPS)
    for section in body["sections"]:
        assert section["label"] == labels[section["token"]]
        assert section["groups"] or section["toggles"], section["token"]
        for group in section["groups"]:
            assert group["items"], (section["token"], group["token"])
            assert group["label"] == settings_ops.GROUP_LABELS[group["token"]]
            for item in group["items"]:
                assert SETTINGS_SCHEMA[item["base_key"]]["group"] == group["token"]
        for item in section["toggles"]:
            assert settings_ops.TOGGLE_SECTION[item["key"]] == section["token"]


def test_sections_carry_tier_from_settings_main_sections(tmp_path):
    """Phase 22 Plan 07 (D-16): стартовый экран настроек группирует плитки разделов в два
    ряда по `sections[].tier` — веб-слой читает `settings_ops.SETTINGS_MAIN_SECTIONS`, а не
    решает сам, какой раздел «нужен часто»."""
    client = _setup(tmp_path)
    body = _all(client).json()
    for section in body["sections"]:
        expected = "main" if section["token"] in settings_ops.SETTINGS_MAIN_SECTIONS else "rare"
        assert section["tier"] == expected, section["token"]
    tiers = {s["tier"] for s in body["sections"]}
    assert tiers <= {"main", "rare"}


def test_no_roles_keys_in_response(tmp_path):
    client = _setup(tmp_path)
    keys = {i["key"] for i in _items(_all(client).json())}
    assert not any(k.startswith("role_caps_") for k in keys)
    assert not any(SETTINGS_SCHEMA[k].get("group") == "roles" for k in keys)


def test_item_shape_and_registry_sourced_fields(tmp_path):
    client = _setup(tmp_path)
    body = _all(client).json()
    for item in _items(body):
        assert set(item.keys()) == ITEM_FIELDS, item["key"]
        meta = SETTINGS_SCHEMA[item["base_key"]]
        assert item["label"] == meta["label"]
        assert item["help"] == meta.get("prompt")
        assert item["type"] == meta["type"]
        assert item["options"] == meta.get("options")
        assert item["per_city"] == bool(meta.get("per_city"))
        assert item["html"] == (item["base_key"] in settings_ops.HTML_SETTINGS)
        assert item["dangerous"] == (item["base_key"] in settings_ops.DANGEROUS_KEYS)
        assert item["search_terms"] == SETTINGS_SYNONYMS.get(item["base_key"], [])
        assert isinstance(item["display"], str)
        assert item["editable"] is True  # без модуля городов правится всё


def test_is_default_and_raw_reflect_db(tmp_path):
    client = _setup(tmp_path)
    _set("event_name", "форума YouLead")  # явно задано, отличается от дефолта
    body = _all(client).json()
    assert _item(body, "event_name")["is_default"] is False
    assert _item(body, "event_name")["raw"] == "форума YouLead"
    assert _item(body, "event_name")["value"] == "форума YouLead"
    untouched = _item(body, "payment_deadline")
    assert untouched["raw"] is None and untouched["is_default"] is True


def test_confirm_text_from_registry_only_for_dangerous_direction(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_staff_only", "off")
    body = _all(client).json()
    staff_only = _item(body, "miniapp_staff_only")
    assert staff_only["confirm_text"] == _run(get_setting_typed("miniapp_confirm_staff_only_text"))
    assert "<" not in staff_only["confirm_text"]
    reg_mode = _item(body, "registration_mode")
    assert reg_mode["confirm_text"] == _run(get_setting_typed("miniapp_settings_confirm_reg_mode_text"))
    assert _item(body, "main_sheet_tab")["confirm_text"] is None  # считается при записи (строки вкладки)
    assert _item(body, "event_name")["confirm_text"] is None

    _set("miniapp_staff_only", "on")
    body = _all(client).json()
    assert _item(body, "miniapp_staff_only")["confirm_text"] is None  # обратное направление безопасно


def test_texts_carry_every_miniapp_settings_key(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_settings_saved_toast_text", "Готово!")
    texts = _all(client).json()["texts"]
    expected = {k for k in settings_ops.editable_keys() if k.startswith("miniapp_settings_")}
    assert set(texts) == expected
    assert len(texts) >= 41
    assert texts["miniapp_settings_saved_toast_text"] == "Готово!"
    assert all(isinstance(v, str) and v for v in texts.values())


# ── права ────────────────────────────────────────────────────────────────────────────────

def test_all_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _all(client, GAME_MANAGER_ID)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_all_section_off_403(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_section_settings", "off")
    resp = _all(client)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ── шапка города (D-04) ──────────────────────────────────────────────────────────────────

def test_city_header_absent_when_cities_module_off(tmp_path):
    client = _setup(tmp_path)
    body = _all(client).json()
    assert body["city_header"] is None
    assert not any(PER_CITY_SEP in i["key"] for i in _items(body))


def test_city_header_unbound_admin_sees_every_city(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    header = _all(client).json()["city_header"]
    assert header["selected"] == "msk"  # город по умолчанию, пока выбора нет
    assert [c["code"] for c in header["cities"]] == city_codes()
    assert all(c["label"] for c in header["cities"])
    assert header["can_select_all"] is True
    assert header["all_cities"] == ALL_CITIES


def test_city_header_bound_manager_sees_only_own_city(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    header = _all(client, SETTINGS_MANAGER_SPB).json()["city_header"]
    assert header["selected"] == "spb"
    assert [c["code"] for c in header["cities"]] == ["spb"]
    assert header["can_select_all"] is False


def test_per_city_key_is_composite_when_city_selected(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set("start_text", "Общий привет")
    body = _all(client).json()
    item = _item(body, f"start_text{PER_CITY_SEP}msk")
    assert item["base_key"] == "start_text"
    assert item["is_city_override"] is False
    assert item["value"] == "Общий привет"  # фолбэк на общее значение
    assert item["editable"] is True
    assert not any(i["key"] == "start_text" for i in _items(body))

    _set(f"start_text{PER_CITY_SEP}msk", "Привет, Москва")
    body = _all(client).json()
    item = _item(body, f"start_text{PER_CITY_SEP}msk")
    assert item["is_city_override"] is True
    assert item["raw"] == "Привет, Москва"
    assert item["value"] == "Привет, Москва"


def test_per_city_key_is_global_with_override_count_for_all_cities(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set(f"admin_city__{ADMIN_ID}", ALL_CITIES)
    _set(f"start_text{PER_CITY_SEP}spb", "Привет, Питер")
    body = _all(client).json()
    assert body["city_header"]["selected"] == ALL_CITIES
    item = _item(body, "start_text")
    assert item["city_override_count"] == 1
    assert item["city_override_labels"] and "spb" not in item["city_override_labels"][0].lower()
    assert item["editable"] is True
    assert not any(PER_CITY_SEP in i["key"] for i in _items(body))


def test_non_per_city_keys_stay_global_under_city_header(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    body = _all(client).json()
    assert _item(body, "payment_deadline")["key"] == "payment_deadline"
    assert _item(body, "payment_deadline")["city_override_count"] == 0


def test_post_city_switches_header_with_bot_function(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    resp = client.post("/app/api/admin/settings/city", json={"code": "spb"}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["selected"] == "spb"
    assert _run(bot_db.get_setting(f"admin_city__{ADMIN_ID}")) == "spb"  # та же запись, что у бота
    assert _all(client).json()["city_header"]["selected"] == "spb"

    resp = client.post("/app/api/admin/settings/city", json={"code": ALL_CITIES}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200
    assert resp.json()["selected"] == ALL_CITIES


def test_post_city_rejects_foreign_city_and_unknown_code(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    for code in ("msk", ALL_CITIES):
        resp = client.post("/app/api/admin/settings/city", json={"code": code}, headers=_hdr(SETTINGS_MANAGER_SPB))
        assert resp.status_code == 400, code
        assert resp.json()["reason"] == "bad_city"
    resp = client.post("/app/api/admin/settings/city", json={"code": "mars"}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 400
    assert _run(bot_db.get_setting(f"admin_city__{ADMIN_ID}")) is None


def test_post_city_with_module_off_400(tmp_path):
    client = _setup(tmp_path)
    resp = client.post("/app/api/admin/settings/city", json={"code": "spb"}, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 400
    assert resp.json()["reason"] == "cities_off"


# ── старые ручки не тронуты ──────────────────────────────────────────────────────────────

def test_legacy_settings_list_still_serves_whitelist(tmp_path):
    from miniapp.routers.settings import EDITABLE_KEYS
    client = _setup(tmp_path)
    resp = client.get("/app/api/admin/settings", headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200
    assert {i["key"] for i in resp.json()} == set(EDITABLE_KEYS)


# ── подсказка про незаданную дату отсчёта (quick 260903, BACKLOG-0309-COUNTDOWN) ─────────

def _hints(client, user=ADMIN_ID):
    return client.get("/app/api/admin/settings/hints", headers=_hdr(user))


def test_hints_countdown_null_when_date_set_and_module_off(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_hub_countdown_date", "31.10.2026")
    resp = _hints(client)
    assert resp.status_code == 200, resp.text
    # Quick 260904-dq1: quiet_queue — тот же контракт, что countdown (None при пустой очереди).
    assert resp.json() == {"countdown": None, "quiet_queue": None}


def test_hints_countdown_present_when_date_empty_and_module_off(tmp_path):
    client = _setup(tmp_path)
    resp = _hints(client)
    assert resp.status_code == 200, resp.text
    countdown = resp.json()["countdown"]
    assert countdown is not None
    assert "Дата отсчёта до форума" in countdown["text"]
    assert "делегаты не видят" in countdown["text"]
    assert countdown["hash"] == "#/settings"


def test_hints_countdown_selected_city_checks_only_that_city(tmp_path):
    """Шапка на конкретном городе (менеджер, привязанный к spb) — проверяется только spb,
    даже если у msk (город админа по умолчанию) своя дата есть."""
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set(f"miniapp_hub_countdown_date{PER_CITY_SEP}msk", "31.10.2026")

    resp = _hints(client)  # ADMIN_ID не привязан -> видит msk по умолчанию
    assert resp.json()["countdown"] is None

    resp = _hints(client, SETTINGS_MANAGER_SPB)  # привязан к spb, у spb даты нет
    countdown = resp.json()["countdown"]
    assert countdown is not None
    assert "Города без даты" not in countdown["text"]  # не «все города» — просто текст


def test_hints_countdown_all_cities_lists_missing_city_labels(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set(f"admin_city__{ADMIN_ID}", ALL_CITIES)
    _set(f"miniapp_hub_countdown_date{PER_CITY_SEP}msk", "31.10.2026")

    resp = _hints(client)
    countdown = resp.json()["countdown"]
    assert countdown is not None
    assert "Города без даты:" in countdown["text"]

    async def _labels():
        import cities as cities_mod
        return {code: await cities_mod.city_label(code) for code in city_codes()}

    labels = asyncio.run(_labels())
    assert labels["msk"] not in countdown["text"]
    for code in ("spb", "tyumen"):
        assert labels[code] in countdown["text"]


def test_hints_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _hints(client, GAME_MANAGER_ID)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


# ── живое превью оформления (quick 260904-8o3 Task 3, E5/E6) ────────────────────────────────

def _group_for_key(body, key):
    for section in body["sections"]:
        for group in section["groups"]:
            if any(i["key"] == key for i in group["items"]):
                return group
    return None


def test_theme_keys_group_carries_theme_preview_flag(tmp_path):
    """Решение «где рисовать превью» — сервер (row["theme_preview"], тот же приём, что
    row["matrix"] у reg_questions): группа, несущая ключи web_theme.THEME_KEYS, помечена."""
    import web_theme

    client = _setup(tmp_path)
    body = _all(client).json()
    group = _group_for_key(body, "miniapp_theme_preset")
    assert group is not None and group.get("theme_preview") is True
    # Группа реально пересекается с THEME_KEYS (не пустой признак).
    keys = {i["key"] for i in group["items"]}
    assert keys & set(web_theme.THEME_KEYS.values())
    # Раздел без ключей оформления флага не несёт.
    other = _group_for_key(body, "event_name")
    assert other is not None and not other.get("theme_preview")


def test_theme_key_flag_distinguishes_theme_items_within_shared_group(tmp_path):
    """Группа "miniapp" несёт ключи оформления вперемешку с обычными текстами (D-контекст
    плана) — `item.theme_key` не даёт фронту случайно отправить неродственную правку в
    theme/preview (и получить 403, роняющий весь экран, см. api.js::authErrorHandler)."""
    import web_theme

    client = _setup(tmp_path)
    body = _all(client).json()
    group = _group_for_key(body, "miniapp_theme_preset")
    theme_keys = set(web_theme.THEME_KEYS.values())
    for item in group["items"]:
        assert item["theme_key"] == (item["base_key"] in theme_keys), item["key"]
    assert any(i["theme_key"] for i in group["items"])
    assert any(not i["theme_key"] for i in group["items"])  # группа реально смешанная


def _preview(client, changes, user=ADMIN_ID):
    return client.post(
        "/app/api/admin/theme/preview", json={"changes": changes}, headers=_hdr(user),
    )


def test_theme_preview_returns_vars_for_changed_preset_without_saving(tmp_path):
    client = _setup(tmp_path)
    resp = _preview(client, [{"key": "miniapp_theme_preset", "value": "realtalk"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "realtalk" in body["vars"]["light"]["--plate-pattern"]
    # Ничего не сохранилось — активный пресет в БД не тронут.
    assert _run(get_setting_typed("miniapp_theme_preset")) != "realtalk"


def test_theme_preview_different_presets_give_different_plate_pattern(tmp_path):
    client = _setup(tmp_path)
    realtalk = _preview(client, [{"key": "miniapp_theme_preset", "value": "realtalk"}]).json()
    bluebook = _preview(client, [{"key": "miniapp_theme_preset", "value": "bluebook"}]).json()
    assert realtalk["vars"]["light"]["--plate-pattern"] != bluebook["vars"]["light"]["--plate-pattern"]


def test_theme_preview_foreign_key_is_403_not_editable(tmp_path):
    client = _setup(tmp_path)
    before = _run(bot_db.get_setting("event_name"))
    resp = _preview(client, [{"key": "event_name", "value": "Взлом"}])
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_editable"
    assert _run(bot_db.get_setting("event_name")) == before  # ручка ничего не пишет в БД


def test_theme_preview_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _preview(client, [{"key": "miniapp_theme_preset", "value": "realtalk"}], user=GAME_MANAGER_ID)
    assert resp.status_code == 403


def test_theme_preview_file_id_pattern_goes_through_file_proxy(tmp_path):
    client = _setup(tmp_path)
    file_id = "AgACAgIAAxkBAAI" + "c" * 15
    # Quick 260904-kk6 (E5): чистый стенд стартует от пресета bluebook (pattern_enabled=off) —
    # гейт в theme_css_vars погасил бы --plate-pattern независимо от plate_pattern. Тест про
    # резолюцию file_id через файловый прокси, не про гейт — включаем паттерн явно.
    resp = _preview(client, [
        {"key": "miniapp_theme_pattern_enabled", "value": "on"},
        {"key": "miniapp_theme_pattern", "value": file_id},
    ])
    assert resp.status_code == 200, resp.text
    assert f"/app/api/file/{file_id}" in resp.json()["vars"]["light"]["--plate-pattern"]


# ── quick 260904-de4 Task 5: счётчики склоняются — дефолты несут группу из трёх форм ────────

@pytest.mark.parametrize("key", ["miniapp_settings_batch_bar_text", "miniapp_settings_tile_count_text"])
def test_counter_defaults_carry_three_plural_forms(key):
    default = SETTINGS_SCHEMA[key]["default"]
    assert default.count("|") == 2, default
    assert "{" in default and "}" in default
