"""Phase 22 Plan 04 Task 2/3 (WEB-SET-01/03/04, D-06..D-11): `POST /app/api/admin/settings/batch`
— атомарное сохранение пакета правок с подтверждениями (опасные ключи, вкладки Sheets) и
защитой от параллельной правки в боте (`stale`); `GET .../settings/preview`; staff-путь
загрузок и чтения файлов для менеджера с правом `settings`.

Харнесс — `tests/test_miniapp_routes.py`; мок `tab_row_count` — по образцу
`tests/test_sheet_tabs_settings_260815.py` (monkeypatch имени в модуле-потребителе).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import settings_ops
from cities import ALL_CITIES, PER_CITY_SEP
from database import db as bot_db
from settings_schema import get_setting_typed

from miniapp.routers import settings as settings_router

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

SETTINGS_MANAGER_SPB = 900610  # reg_manager + право settings через реестр, привязан к spb
SEEDED_EVENT_NAME = "форума YouLead"  # _standard_seed уже пишет event_name


def _run(coro):
    return asyncio.run(coro)


def _raw(key):
    return _run(bot_db.get_setting(key))


def _setup(tmp_path, name="miniapp_settings_batch.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _seed(staff=[(SETTINGS_MANAGER_SPB, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    return _client(_cfg(db_path))


def _batch(client, changes, *, base=None, confirm=None, user=ADMIN_ID):
    body = {"changes": [{"key": k, "value": v} for k, v in changes], "base": base or {}, "confirm": confirm or []}
    return client.post("/app/api/admin/settings/batch", json=body, headers=_hdr(user))


@pytest.fixture
def no_tab(monkeypatch):
    """Sheets: вкладки нет — гейт не срабатывает."""
    async def probe(title):
        return (False, 0)
    monkeypatch.setattr(settings_router, "tab_row_count", probe)


# ── атомарность (T-22-02) ────────────────────────────────────────────────────────────────

def test_three_valid_changes_are_all_saved(tmp_path, no_tab):
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "форума RusCo"), ("reg_resume_ttl_hours", " 48 "), ("nudge_after_minutes", "15")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved"] == ["event_name", "reg_resume_ttl_hours", "nudge_after_minutes"]
    assert body["errors"] == {} and body["needs_confirm"] == [] and body["stale"] == []
    assert _raw("event_name") == "форума RusCo"
    assert _raw("reg_resume_ttl_hours") == "48"  # нормализовано тем же валидатором, что у бота
    assert _run(get_setting_typed("nudge_after_minutes")) == 15
    fresh = {i["key"]: i for i in body["items"]}
    assert set(fresh) == set(body["saved"])
    assert fresh["event_name"]["raw"] == "форума RusCo" and fresh["event_name"]["is_default"] is False


def test_one_bad_key_of_three_writes_nothing(tmp_path, no_tab):
    """Атомарность: один невалидный ключ — ни одной записи, остальные значения прежние."""
    client = _setup(tmp_path)
    _set("event_name", "старое")
    resp = _batch(client, [("event_name", "новое"), ("reg_resume_ttl_hours", "сорок восемь"), ("nudge_after_minutes", "15")])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved"] == []
    assert list(body["errors"]) == ["reg_resume_ttl_hours"]
    text = body["errors"]["reg_resume_ttl_hours"]
    assert "целое число" in text and "<" not in text  # человеческий plain-текст, без HTML бота
    assert _raw("event_name") == "старое"
    assert _raw("reg_resume_ttl_hours") is None
    assert _raw("nudge_after_minutes") is None


def test_command_like_value_rejected_like_in_bot(tmp_path, no_tab):
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "/start"), ("nudge_after_minutes", "15")])
    body = resp.json()
    assert "event_name" in body["errors"] and "команда" in body["errors"]["event_name"]
    assert _raw("event_name") == SEEDED_EVENT_NAME and _raw("nudge_after_minutes") is None


def test_empty_value_rejected(tmp_path, no_tab):
    client = _setup(tmp_path)
    body = _batch(client, [("event_name", "   ")]).json()
    assert "event_name" in body["errors"]
    assert _raw("event_name") == SEEDED_EVENT_NAME


def test_not_editable_key_is_403_and_nothing_written(tmp_path, no_tab):
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "новое"), ("role_caps_reg_manager", "settings")])
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_editable"
    assert _raw("event_name") == SEEDED_EVENT_NAME
    resp = _batch(client, [("bot_token", "x")])
    assert resp.status_code == 403


def test_duplicate_key_in_batch_is_400(tmp_path, no_tab):
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "a"), ("event_name", "b")])
    assert resp.status_code == 400
    assert resp.json()["reason"] == "duplicate_key"
    assert _raw("event_name") == SEEDED_EVENT_NAME


def test_batch_without_settings_cap_403(tmp_path, no_tab):
    client = _setup(tmp_path)
    resp = _batch(client, [("event_name", "x")], user=GAME_MANAGER_ID)
    assert resp.status_code == 403 and resp.json()["reason"] == "no_cap"


# ── вкладки Sheets: confirm по числу строк, предупреждение при недоступных Sheets ────────

def test_existing_sheet_tab_needs_confirm_then_confirm_writes(tmp_path, monkeypatch):
    client = _setup(tmp_path)
    calls = []

    async def probe(title):
        calls.append(title)
        return (True, 30)
    monkeypatch.setattr(settings_router, "tab_row_count", probe)

    resp = _batch(client, [("game_matrix_tab", "GAMIFICATION бот"), ("event_name", "форума")])
    body = resp.json()
    assert body["saved"] == []
    assert [c["key"] for c in body["needs_confirm"]] == ["game_matrix_tab"]
    text = body["needs_confirm"][0]["text"]
    assert "GAMIFICATION бот" in text and "30" in text and "<" not in text
    assert _raw("game_matrix_tab") is None and _raw("event_name") == SEEDED_EVENT_NAME  # ничего не записано
    assert calls == ["GAMIFICATION бот"]

    resp = _batch(client, [("game_matrix_tab", "GAMIFICATION бот"), ("event_name", "форума")], confirm=["game_matrix_tab"])
    body = resp.json()
    assert body["saved"] == ["game_matrix_tab", "event_name"]
    assert _raw("game_matrix_tab") == "GAMIFICATION бот" and _raw("event_name") == "форума"
    assert calls == ["GAMIFICATION бот"]  # подтверждённый ключ повторно не проверяется


def test_sheets_unreachable_saves_with_warning(tmp_path, monkeypatch):
    client = _setup(tmp_path)

    async def probe(title):
        return None
    monkeypatch.setattr(settings_router, "tab_row_count", probe)

    body = _batch(client, [("incomplete_sheet_tab", "Какая-то вкладка")]).json()
    assert body["saved"] == ["incomplete_sheet_tab"]
    assert "проверить вкладку" in body["warnings"]["incomplete_sheet_tab"].lower()
    assert _raw("incomplete_sheet_tab") == "Какая-то вкладка"


def test_tab_key_save_resets_sheet_cache(tmp_path, no_tab, monkeypatch):
    client = _setup(tmp_path)
    resets = []
    monkeypatch.setattr(settings_ops, "_reset_sheet_cache", lambda: resets.append(1))
    body = _batch(client, [("main_sheet_tab", "Реги бот")]).json()
    assert body["saved"] == ["main_sheet_tab"]
    assert resets == [1]


def test_reset_of_tab_key_skips_probe(tmp_path, monkeypatch):
    client = _setup(tmp_path)
    _set("main_sheet_tab", "Старая")

    async def probe(title):
        raise AssertionError("сброс не должен проверять вкладку")
    monkeypatch.setattr(settings_router, "tab_row_count", probe)

    body = _batch(client, [("main_sheet_tab", None)]).json()
    assert body["saved"] == ["main_sheet_tab"]
    assert _raw("main_sheet_tab") is None


# ── stale: параллельная правка в боте (D-09, T-22-04) ────────────────────────────────────

def test_stale_key_is_not_overwritten_until_confirmed(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_name", "изменено в боте")
    resp = _batch(client, [("event_name", "из приложения"), ("nudge_after_minutes", "15")], base={"event_name": None})
    body = resp.json()
    assert body["saved"] == []
    assert body["stale"] == [{"key": "event_name", "raw": "изменено в боте", "value": "изменено в боте"}]
    assert _raw("event_name") == "изменено в боте" and _raw("nudge_after_minutes") is None

    resp = _batch(client, [("event_name", "из приложения"), ("nudge_after_minutes", "15")],
                  base={"event_name": None}, confirm=["event_name"])
    body = resp.json()
    assert body["saved"] == ["event_name", "nudge_after_minutes"] and body["stale"] == []
    assert _raw("event_name") == "из приложения"


def test_matching_base_is_not_stale(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_name", "то же")
    body = _batch(client, [("event_name", "новое")], base={"event_name": "то же"}).json()
    assert body["stale"] == [] and body["saved"] == ["event_name"]


# ── сброс (D-10) и побочные шаги записи ──────────────────────────────────────────────────

def test_null_value_deletes_setting(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_name", "своё")
    body = _batch(client, [("event_name", None)]).json()
    assert body["saved"] == ["event_name"]
    assert _raw("event_name") is None
    assert body["items"][0]["is_default"] is True


def test_null_on_per_city_key_removes_only_city_override(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set("start_text", "общий")
    _set(f"start_text{PER_CITY_SEP}msk", "московский")
    body = _batch(client, [(f"start_text{PER_CITY_SEP}msk", None)]).json()
    assert body["saved"] == [f"start_text{PER_CITY_SEP}msk"]
    assert _raw(f"start_text{PER_CITY_SEP}msk") is None
    assert _raw("start_text") == "общий"


def test_event_type_applies_module_preset_after_confirm(tmp_path, no_tab):
    client = _setup(tmp_path)
    body = _batch(client, [("event_type", "conference")]).json()
    assert body["saved"] == []
    assert [c["key"] for c in body["needs_confirm"]] == ["event_type"]
    assert body["needs_confirm"][0]["text"] == _run(get_setting_typed("miniapp_settings_confirm_event_type_text"))
    assert _raw("event_type") is None

    body = _batch(client, [("event_type", "conference")], confirm=["event_type"]).json()
    assert body["saved"] == ["event_type"]
    assert _raw("payment_enabled") == "on" and _raw("consent_enabled") == "on"


def test_options_reserved_words_warn_but_save(tmp_path, no_tab):
    client = _setup(tmp_path)
    body = _batch(client, [("source_options", "ВКонтакте\nОтмена\nДруг")]).json()
    assert body["saved"] == ["source_options"]
    assert "Отмена" in body["warnings"]["source_options"]
    assert _raw("source_options") == "ВКонтакте\nОтмена\nДруг"


def test_audit_line_names_who_edits(tmp_path, no_tab, caplog):
    client = _setup(tmp_path)
    with caplog.at_level(logging.INFO, logger="miniapp.routers.settings"):
        _batch(client, [("event_name", "форума")])
    assert any(f"admin {ADMIN_ID} правит настройку event_name" in r.getMessage() for r in caplog.records)


# ── опасные тумблеры: судья — сервер (T-22-13) ───────────────────────────────────────────

def test_dangerous_toggle_needs_confirm_only_in_dangerous_direction(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("miniapp_staff_only", "off")
    body = _batch(client, [("miniapp_staff_only", "on")]).json()
    assert body["saved"] == [] and [c["key"] for c in body["needs_confirm"]] == ["miniapp_staff_only"]
    assert body["needs_confirm"][0]["text"] == _run(get_setting_typed("miniapp_confirm_staff_only_text"))
    assert _raw("miniapp_staff_only") == "off"

    body = _batch(client, [("miniapp_staff_only", "on")], confirm=["miniapp_staff_only"]).json()
    assert body["saved"] == ["miniapp_staff_only"] and _raw("miniapp_staff_only") == "on"

    body = _batch(client, [("miniapp_staff_only", "off")]).json()  # обратно — безопасно, без confirm
    assert body["saved"] == ["miniapp_staff_only"] and _raw("miniapp_staff_only") == "off"


def test_ordinary_toggle_saves_without_confirm(tmp_path, no_tab):
    client = _setup(tmp_path)
    body = _batch(client, [("miniapp_section_stats", "off")]).json()
    assert body["saved"] == ["miniapp_section_stats"] and body["needs_confirm"] == []
    assert _run(get_setting_typed("miniapp_section_stats")) == "off"


# ── per-city: три отказа, ни одной записи (D-11, T-22-01) ────────────────────────────────

def test_per_city_key_with_cities_module_off_refused(tmp_path, no_tab):
    client = _setup(tmp_path)
    body = _batch(client, [(f"start_text{PER_CITY_SEP}msk", "привет"), ("event_name", "x")]).json()
    assert "Города выключены" in body["errors"][f"start_text{PER_CITY_SEP}msk"]
    assert body["saved"] == [] and _raw("event_name") == SEEDED_EVENT_NAME


def test_per_city_key_of_foreign_city_refused_for_bound_manager(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    body = _batch(client, [(f"start_text{PER_CITY_SEP}msk", "привет")], user=SETTINGS_MANAGER_SPB).json()
    assert "суперадмин" in body["errors"][f"start_text{PER_CITY_SEP}msk"]
    assert _raw(f"start_text{PER_CITY_SEP}msk") is None

    body = _batch(client, [(f"start_text{PER_CITY_SEP}spb", "привет, Питер")], user=SETTINGS_MANAGER_SPB).json()
    assert body["saved"] == [f"start_text{PER_CITY_SEP}spb"]
    assert _raw(f"start_text{PER_CITY_SEP}spb") == "привет, Питер"


def test_per_city_key_not_matching_header_refused(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set(f"admin_city__{ADMIN_ID}", "spb")
    body = _batch(client, [(f"start_text{PER_CITY_SEP}msk", "привет")]).json()
    assert "Город админки изменился" in body["errors"][f"start_text{PER_CITY_SEP}msk"]
    assert _raw(f"start_text{PER_CITY_SEP}msk") is None


def test_per_city_key_of_non_per_city_base_is_not_editable(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    resp = _batch(client, [(f"event_name{PER_CITY_SEP}msk", "x")])
    assert resp.status_code == 403 and resp.json()["reason"] == "not_editable"


def test_global_per_city_key_under_all_cities_header_saves_global(tmp_path, no_tab):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    _set(f"admin_city__{ADMIN_ID}", ALL_CITIES)
    body = _batch(client, [("start_text", "общий привет")]).json()
    assert body["saved"] == ["start_text"] and _raw("start_text") == "общий привет"


# ── регресс Phase 25 (CITYQ-01, 65255e9): items ответа на композит трек×город ────────────
#
# `settings_ops.reg_setting_city_track_base` перехватывал ЛЮБОЙ per-city композит (не только
# трек×город), из-за чего `_editable_target` возвращал в `targets[key]` сам композит, а не
# базу реестра — `_item_for(targets[key], ctx)` в ответе `settings/batch` падал `KeyError` на
# `SETTINGS_SCHEMA[композит]`. Ниже — оба случая: обычный per-city ключ БЕЗ трека (не только
# `reg_q_*`) и композит трек×город над вопросом анкеты.

def test_non_reg_per_city_key_batch_item_resolves_to_schema_base(tmp_path, no_tab):
    """`start_text` — не `reg_q_*`, обычный per-city ключ. Раньше падал `KeyError` в `_item_for`
    на построении `items` ответа; сейчас ключ композита резолвится к схемной базе `start_text`."""
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    key = f"start_text{PER_CITY_SEP}msk"
    body = _batch(client, [(key, "привет, Москва")]).json()
    assert body["errors"] == {}, body["errors"]
    assert body["saved"] == [key]
    items = {i["key"]: i for i in body["items"]}
    assert key in items
    assert items[key]["base_key"] == "start_text"
    assert items[key]["is_city_override"] is True
    assert items[key]["raw"] == "привет, Москва"


def test_reg_question_track_city_composite_batch_saves_without_crash(tmp_path, no_tab):
    """`reg_q_age__party__city__msk` — композит трек×город (Phase 25 CITYQ-01): сохраняется,
    не роняет ответ `KeyError`-ом и не попадает в `items` (нет item_spec-обёртки — свою ячейку
    красит матрица, см. комментарий у `_editable_target`/сборки `items` в
    `miniapp/routers/settings.py`)."""
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    key = f"reg_q_age__party{PER_CITY_SEP}msk"
    body = _batch(client, [(key, "off")]).json()
    assert body["errors"] == {}, body["errors"]
    assert body["saved"] == [key]
    assert _raw(key) == "off"
    assert key not in {i["key"] for i in body["items"]}


# ── ядро вне веба ────────────────────────────────────────────────────────────────────────

def test_validate_batch_item_mirrors_bot_check_order(tmp_path):
    _use_tmp_db(tmp_path, "batch_core.db")
    kw = dict(visible_codes=["msk", "spb"], selected_city=None, cities_on=False)

    empty = _run(settings_ops.validate_batch_item("event_name", "  ", **kw))
    assert empty.error and empty.value is None
    cmd = _run(settings_ops.validate_batch_item("event_name", "/start@YouLead_bot", **kw))
    assert cmd.error and "команда" in cmd.error
    bad = _run(settings_ops.validate_batch_item("reg_resume_ttl_hours", "-5", **kw))
    assert bad.error and "отрицательным" in bad.error and "<" not in bad.error
    ok = _run(settings_ops.validate_batch_item("registration_mode", "FULL", **kw, confirmed=True))
    assert ok.value == "full" and ok.error is None and ok.needs_confirm is None
    reset = _run(settings_ops.validate_batch_item("event_name", None, **kw))
    assert reset.value is None and reset.error is None
    gate = _run(settings_ops.validate_batch_item("main_sheet_tab", "Реги", **kw, tab_probe=(True, 12)))
    assert gate.needs_confirm and "12" in gate.needs_confirm and gate.error is None
    unknown = _run(settings_ops.validate_batch_item("main_sheet_tab", "Реги", **kw, tab_probe=None))
    assert unknown.warning and unknown.needs_confirm is None and unknown.value == "Реги"


# ══ Task 3: preview — текст глазами делегата (D-07) ═══════════════════════════════════════

import re

from settings_schema import SETTINGS_SCHEMA

from miniapp.deps import Principal
from miniapp.routers.files import can_read_file

from tests.test_miniapp_routes import _probe_client

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _preview(client, key, value, user=ADMIN_ID):
    return client.get("/app/api/admin/settings/preview", params={"key": key, "value": value}, headers=_hdr(user))


def test_preview_substitutes_placeholders_like_consumers_do(tmp_path):
    client = _setup(tmp_path)
    _set("payment_deadline", "15.10.2026 23:59")
    resp = _preview(client, "payment_details_template_text", "Тариф {option}: {amount} ₽ до {deadline}. {unknown}")
    assert resp.status_code == 200, resp.text
    text = resp.json()["text"]
    assert text == "Тариф Полная форма: 3500 ₽ до 15.10.2026. {unknown}"  # чужие {} не трогаются, как в .replace бота
    # тот же образец, что видит делегат у консьюмера (handlers/registration.py: deadline.split()[0])
    samples = _run(settings_ops.preview_samples())
    assert samples["deadline"] == "15.10.2026"


def test_preview_html_key_arrives_as_plain_text(tmp_path):
    client = _setup(tmp_path)
    assert "payment_details_template_text" in settings_ops.HTML_SETTINGS
    text = _preview(client, "payment_details_template_text", "<b>Реквизиты:</b> {requisites} &amp; всё").json()["text"]
    assert "<" not in text and text.startswith("Реквизиты: ") and "& всё" in text


def test_preview_foreign_key_403_and_no_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _preview(client, "role_caps_reg_manager", "x")
    assert resp.status_code == 403 and resp.json()["reason"] == "not_editable"
    resp = _preview(client, "event_name", "x", user=GAME_MANAGER_ID)
    assert resp.status_code == 403 and resp.json()["reason"] == "no_cap"


def test_preview_uses_replace_chain_not_format():
    # Текст менеджера с посторонними фигурными скобками не роняет превью (у .format упало бы).
    text = settings_ops.preview_text("event_name", "{season} {не плейсхолдер} {", samples={"season": "YL26"})
    assert text == "YL26 {не плейсхолдер} {"


def test_every_registry_placeholder_is_covered_by_preview_samples_or_addressee():
    """Сторож: каждый плейсхолдер в дефолте текстового ключа реестра либо подставляется
    превью (PREVIEW_SAMPLES), либо явно объявлен как «подставляет адресат, не превью»."""
    covered = set(settings_ops.PREVIEW_SAMPLES) | settings_ops.PREVIEW_ADDRESSEE_PLACEHOLDERS
    missing = {}
    for key in settings_ops.editable_keys():
        meta = SETTINGS_SCHEMA[key]
        if meta.get("type") != "text" or not isinstance(meta.get("default"), str):
            continue
        for name in _PLACEHOLDER_RE.findall(meta["default"]):
            if name not in covered:
                missing.setdefault(name, []).append(key)
    assert missing == {}, missing
    assert not (set(settings_ops.PREVIEW_SAMPLES) & settings_ops.PREVIEW_ADDRESSEE_PLACEHOLDERS)


# ══ Task 3: uploads и чтение файлов через существующий staff-путь (D-05, T-22-05/T-22-12) ═

SETTINGS_FILE_ID = "AgACAgIAAxkBAAIsettingsPh01"
OTHER_FILE_ID = "AgACAgIAAxkBAAIsomeoneElse1"


def test_settings_manager_gets_staff_upload_branch(tmp_path):
    db_path = _use_tmp_db(tmp_path, "settings_uploads.db")
    _standard_seed()
    _seed(staff=[(SETTINGS_MANAGER_SPB, "reg_manager", "spb")])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    probe = _probe_client(_cfg(db_path))
    resp = probe.post("/app/api/_probe/upload", headers=_hdr(SETTINGS_MANAGER_SPB))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"telegram_id": SETTINGS_MANAGER_SPB, "is_staff_upload": True}
    # без права settings/moderate_game незарегистрированный staff по-прежнему не грузит
    _set("role_caps_reg_manager", "moderate_reg")
    resp = probe.post("/app/api/_probe/upload", headers=_hdr(SETTINGS_MANAGER_SPB))
    assert resp.status_code == 403


def test_can_read_file_for_settings_cap_only_current_setting_values(tmp_path):
    _use_tmp_db(tmp_path, "settings_files.db")
    _standard_seed()
    _set("program", SETTINGS_FILE_ID)
    with_settings = Principal(telegram_id=SETTINGS_MANAGER_SPB, via="initdata", caps=frozenset({"settings"}), city="spb")
    without = Principal(telegram_id=SETTINGS_MANAGER_SPB, via="initdata", caps=frozenset({"moderate_reg"}), city="spb")
    assert _run(can_read_file(with_settings, SETTINGS_FILE_ID)) is True
    assert _run(can_read_file(with_settings, OTHER_FILE_ID)) is False  # произвольный file_id — нет
    assert _run(can_read_file(without, SETTINGS_FILE_ID)) is False
    _set("program", "")  # значение снято — доступ пропал вместе с ним
    assert _run(can_read_file(with_settings, SETTINGS_FILE_ID)) is False


def test_file_setting_keys_are_exactly_photo_and_file_types():
    keys = settings_ops.file_setting_keys()
    assert keys and all(SETTINGS_SCHEMA[k]["type"] in ("photo", "file") for k in keys)
    assert "program" in keys and "miniapp_logo" in keys and "event_name" not in keys
