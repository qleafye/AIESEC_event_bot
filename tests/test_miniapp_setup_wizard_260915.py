"""Квик 260915-4mu: мастер первой настройки события в Mini App.

Часть 1 (Task 1) — реестр шагов (`miniapp/setup_wizard.py`) и `GET /app/api/admin/setup`.
Харнесс — `tests/test_miniapp_routes.py` (тот же приём, что `test_miniapp_settings_registry.py`).
Часть 2 (Task 2) — статические сторожа фронтового модуля `screens/setup.js` (без DOM, тот же
приём, что `tests/test_miniapp_nav_icons_260912.py`).
"""
from __future__ import annotations

import asyncio
import re

import settings_ops
from settings_schema import SETTINGS_SCHEMA

from miniapp.setup_wizard import STEPS, step_done, visible_steps

from tests.test_miniapp_routes import (
    ADMIN_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, name="miniapp_setup_wizard.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def _setup_resp(client, user=ADMIN_ID):
    return client.get("/app/api/admin/setup", headers=_hdr(user))


def _all_resp(client, user=ADMIN_ID):
    return client.get("/app/api/admin/settings/all", headers=_hdr(user))


def _all_items(body) -> list[dict]:
    out = []
    for section in body["sections"]:
        out.extend(section["toggles"])
        for group in section["groups"]:
            out.extend(group["items"])
    return out


def _step(body, key) -> dict:
    return next(s for s in body["steps"] if s["key"] == key)


# ══ 1: сторож реестра (без БД) — каждый ключ шага в SETTINGS_SCHEMA и в editable_keys() ═══

def test_every_step_field_is_a_known_editable_key():
    editable = set(settings_ops.editable_keys())
    for step in STEPS:
        for key in step.fields:
            assert key in SETTINGS_SCHEMA, f"{step.key}: {key} отсутствует в SETTINGS_SCHEMA"
            assert key in editable, f"{step.key}: {key} не входит в editable_keys()"


# ══ 2: сторож данных — kind/fields/link/уникальность/первый шаг ══════════════════════════

def test_step_shapes_are_internally_consistent():
    seen_keys = set()
    for step in STEPS:
        assert step.kind in ("fields", "note", "link")
        if step.kind == "fields":
            assert step.fields, f"{step.key}: kind=fields без полей"
        if step.kind == "link":
            assert step.link, f"{step.key}: kind=link без link"
        assert step.key not in seen_keys, f"{step.key}: дублирующийся ключ шага"
        seen_keys.add(step.key)
    assert STEPS[0].key == "event_type"


# ══ 3/4/5: visible_steps — фильтр по event_type и по флагам модулей ══════════════════════

def test_visible_steps_without_event_type_excludes_conditional_steps():
    steps = visible_steps(None, {})
    keys = {s.key for s in steps}
    for step in STEPS:
        if step.event_types is None and not step.requires:
            assert step.key in keys, step.key
    assert "su_options" not in keys
    assert "su_score" not in keys
    assert "su_resume" not in keys
    assert "su_rebuild" not in keys
    assert "cities" not in keys
    assert "consent" not in keys
    assert "payment" not in keys


def test_visible_steps_skillup_includes_su_steps_in_declared_order():
    steps = visible_steps("skillup", {})
    keys = [s.key for s in steps]
    su_keys = [k for k in keys if k.startswith("su_")]
    assert su_keys == ["su_options", "su_score", "su_resume", "su_rebuild"]

    forum_steps = visible_steps("forum", {})
    forum_keys = {s.key for s in forum_steps}
    assert not any(k.startswith("su_") for k in forum_keys)


def test_visible_steps_respects_module_flags():
    without_payment = {s.key for s in visible_steps("forum", {"payment_enabled": False})}
    with_payment = {s.key for s in visible_steps("forum", {"payment_enabled": True})}
    assert "payment" not in without_payment
    assert "payment" in with_payment


# ══ 6: step_done — all/any/note/link ══════════════════════════════════════════════════════

def test_step_done_all_rule_requires_every_field():
    step = next(s for s in STEPS if s.key == "event_info")
    assert step.done_rule == "all"
    all_filled = {k: True for k in step.fields}
    assert step_done(step, all_filled) is True
    one_missing = dict(all_filled)
    one_missing[step.fields[0]] = False
    assert step_done(step, one_missing) is False


def test_step_done_any_rule_requires_one_field():
    step = next(s for s in STEPS if s.key == "contacts")
    assert step.done_rule == "any"
    assert step_done(step, {k: False for k in step.fields}) is False
    one_set = {k: False for k in step.fields}
    one_set[step.fields[0]] = True
    assert step_done(step, one_set) is True


def test_step_done_note_and_link_always_false():
    note_step = next(s for s in STEPS if s.kind == "note")
    link_step = next(s for s in STEPS if s.kind == "link")
    assert step_done(note_step, {}) is False
    assert step_done(link_step, {}) is False


# ══ 7: HTTP — 403 без права settings / при выключенном разделе ═══════════════════════════

def test_setup_without_settings_cap_403(tmp_path):
    client = _setup(tmp_path)
    resp = _setup_resp(client, GAME_MANAGER_ID)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_setup_section_off_403(tmp_path):
    client = _setup(tmp_path)
    _set("miniapp_section_settings", "off")
    resp = _setup_resp(client)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ══ 8: HTTP — done становится true/false, done_count растёт ровно на один ════════════════

def test_event_info_step_done_toggles_with_all_five_fields(tmp_path):
    client = _setup(tmp_path)
    body = _setup_resp(client).json()
    step = _step(body, "event_info")
    assert step["done"] is False
    before_done_count = body["done_count"]

    _set("event_date", "12 сентября")
    _set("event_time", "10:00")
    _set("event_place_name", "Экспоцентр")
    _set("event_place_address", "Москва, наб. 14")
    _set("event_name", "YouLead")

    body2 = _setup_resp(client).json()
    step2 = _step(body2, "event_info")
    assert step2["done"] is True
    assert body2["done_count"] == before_done_count + 1

    _set("event_name", "")
    body3 = _setup_resp(client).json()
    step3 = _step(body3, "event_info")
    assert step3["done"] is False
    assert body3["done_count"] == before_done_count


# ══ 9: HTTP — event_type поле совпадает с settings/all (option_labels, confirm_text) ══════

def test_event_type_field_matches_settings_all(tmp_path):
    client = _setup(tmp_path)
    setup_body = _setup_resp(client).json()
    all_body = _all_resp(client).json()

    setup_field = _step(setup_body, "event_type")["fields"][0]
    all_field = next(i for i in _all_items(all_body) if i["key"] == "event_type")

    assert setup_field["option_labels"] == all_field["option_labels"]
    assert set(setup_field["option_labels"].values()) == {"Форум", "Конференция", "Вручную", "Форум СкиллАп"}
    assert setup_field["confirm_text"] == all_field["confirm_text"]
    assert setup_field["confirm_text"]


# ══ 10: HTTP — show_tile ═══════════════════════════════════════════════════════════════

def test_show_tile_false_when_dismissed(tmp_path):
    client = _setup(tmp_path)
    _set("setup_wizard_dismissed", "on")
    body = _setup_resp(client).json()
    assert body["show_tile"] is False


def test_show_tile_false_when_all_steps_done(tmp_path):
    client = _setup(tmp_path)
    body = _setup_resp(client).json()
    for step in body["steps"]:
        if not step["counts"]:
            continue
        for item in step["fields"]:
            if item.get("options") == ["on", "off"]:
                # «Задано» для тумблера значит «не дефолт» — если дефолт уже "on",
                # писать нужно "off", а не одно и то же значение всегда.
                value = "off" if item.get("default") == "on" else "on"
            else:
                value = "some value"
            _set(item["base_key"], value)
    body2 = _setup_resp(client).json()
    assert body2["done_count"] == body2["total"]
    assert body2["show_tile"] is False


# ══ 11: HTTP — POST /app/api/admin/setup -> 405 (нет своего пути записи) ═════════════════

def test_setup_has_no_post_endpoint(tmp_path):
    client = _setup(tmp_path)
    resp = client.post("/app/api/admin/setup", headers=_hdr(ADMIN_ID), json={})
    assert resp.status_code == 405


# ══ Task 2 (2.4): статические сторожа исходника screens/setup.js / hub.js ════════════════
# Без DOM — тот же приём, что tests/test_miniapp_nav_icons_260912.py: читаем файл, ищем
# подстроки/литералы регэкспом. Хелперы — из read-only tests/test_miniapp_frontend.py.

from tests.test_miniapp_frontend import (  # noqa: E402
    SCREENS_DIR,
    _HEX_OR_RGB_COLOR,
    _STRING_LITERAL,
    _js_without_comments,
)

SETUP_JS = SCREENS_DIR / "setup.js"
HUB_JS = SCREENS_DIR / "hub.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_setup_js_reuses_form_controls_no_own_widgets():
    text = _js_without_comments(SETUP_JS)
    assert 'from "../form.js"' in text
    for name in ("field", "settingSpec", "confirmBox"):
        assert name in text, name


def test_setup_js_writes_only_through_settings_batch():
    text = _js_without_comments(SETUP_JS)
    assert '"/admin/settings/batch"' in text
    # "/admin/setup" встречается только в GET-вызовах — рядом с ним не должно быть POST.
    for m in re.finditer(r'"/admin/setup"', text):
        window = text[max(0, m.start() - 200): m.start() + 200]
        assert 'method: "POST"' not in window


def test_setup_js_has_no_cyrillic_string_literal():
    text = _js_without_comments(SETUP_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)


def test_setup_js_has_no_hardcoded_colors():
    text = _js_without_comments(SETUP_JS)
    assert not _HEX_OR_RGB_COLOR.search(text)


def test_hub_js_wires_setup_tile():
    text = _js_without_comments(HUB_JS)
    assert "#/setup" in text
    assert "show_tile" in text


def test_visible_steps_conference_includes_lc_step_only_for_conference():
    conf_keys = [s.key for s in visible_steps("conference", {})]
    assert "conf_lc" in conf_keys
    assert conf_keys.index("conf_lc") < conf_keys.index("countdown")
    for other in ("forum", "skillup", "custom", None):
        assert "conf_lc" not in {s.key for s in visible_steps(other, {})}, other
