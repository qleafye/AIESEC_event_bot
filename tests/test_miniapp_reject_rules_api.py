"""HTTP редактора правил автоотказа: список, каталог, запись, рубильник.

Харнесс — tests/test_miniapp_routes.py. Города подменяются на время теста и
возвращаются: reload_cities мутирует общий кэш.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from database import db as bot_db
from miniapp.routers.reject_rules import COMMON_GREY_TEXT
from services.applications.reject_rules import active_rules
from services.applications.reject_rules_editor import load_rule, save_patch
from tests.test_miniapp_routes import (
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

BOUND_KZN = 900620
UNBOUND = 900621
_LIST = "/app/api/admin/reject-rules"


def _run(coro):
    return asyncio.run(coro)


def _course_draft(**overrides) -> dict:
    draft = {
        "name": "Курс",
        "city": None,
        "tracks": ["full"],
        "conditions": [[{"step": "course", "op": "in", "values": ["1"]}]],
        "action": "reject",
        "reject_text": "На этот курс набор закрыт.",
    }
    draft.update(overrides)
    return draft


async def _create_rule(**overrides) -> int:
    fields = dict(
        name="Правило",
        city=None,
        tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1"]}]]),
        action="reject",
        reject_text="Текст отказа",
        enabled=0,
        created_by=UNBOUND,
    )
    fields.update(overrides)
    if not isinstance(fields["tracks"], str):
        fields["tracks"] = json.dumps(fields["tracks"])
    if not isinstance(fields["conditions"], str):
        fields["conditions"] = json.dumps(fields["conditions"])
    return await bot_db.create_reject_rule(**fields)


def _seed_cities():
    _run(bot_db.insert_city("msk", "Москва", "", 0))
    _run(bot_db.insert_city("kzn", "Казань", "", 1))
    import domain.cities as cities
    _run(cities.reload_cities())


@pytest.fixture(autouse=True)
def _restore_cities_cache():
    import domain.cities as cities
    snapshot = list(cities.CITIES)
    yield
    cities.CITIES.clear()
    cities.CITIES.extend(snapshot)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "reject_rules_api.db")
    _standard_seed()
    _seed(staff=[
        (BOUND_KZN, "reg_manager", "kzn"),
        (UNBOUND, "reg_manager", None),
    ])
    _set("role_caps_reg_manager", "moderate_reg\nsettings")
    _seed_cities()
    return _client(_cfg(db_path))


def _body(**overrides) -> dict:
    return _course_draft(**overrides)


# ── доступ ───────────────────────────────────────────────────────────────────────────────


def test_no_auth_401(client):
    resp = client.get(_LIST)
    assert resp.status_code == 401
    assert resp.json()["reason"] == "no_auth"


def test_no_cap_403(client):
    resp = client.get(_LIST, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_section_off_403(client):
    _set("miniapp_section_settings", "off")
    resp = client.get(_LIST, headers=_hdr(UNBOUND))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "settings"}


# ── список, каталог, чтение ──────────────────────────────────────────────────────────────


def test_list_unbound_sees_all_cities_and_master(client):
    _run(_create_rule(name="Москва", city="msk"))
    _run(_create_rule(name="Казань", city="kzn"))
    _run(_create_rule(name="Общее", city=None))
    resp = client.get(_LIST, headers=_hdr(UNBOUND))
    assert resp.status_code == 200
    body = resp.json()
    assert {item["city"] for item in body["items"]} == {"msk", "kzn", None}
    assert body["can_toggle_master"] is True
    assert body["master"]["can_toggle"] is True
    labels = {city["label"] for city in body["cities"]}
    assert "Москва" in labels
    assert "Казань" in labels
    assert body["cities"]


def test_list_bound_sees_own_and_common_grey(client):
    _run(_create_rule(name="Москва", city="msk"))
    _run(_create_rule(name="Казань", city="kzn"))
    _run(_create_rule(name="Общее", city=None))
    resp = client.get(_LIST, headers=_hdr(BOUND_KZN))
    assert resp.status_code == 200
    body = resp.json()
    by_city = {item["city"]: item for item in body["items"]}
    assert "msk" not in by_city
    assert by_city["kzn"]["can_edit"] is True
    assert by_city[None]["can_edit"] is False
    assert by_city[None]["locked_text"] == COMMON_GREY_TEXT
    assert body["can_toggle_master"] is False
    assert body["master"]["status_text"]


def test_list_paused_status_text(client):
    _run(_create_rule(
        name="Без резюме",
        enabled=1,
        conditions=[[{"step": "resume", "op": "no_file", "values": []}]],
    ))
    resp = client.get(_LIST, headers=_hdr(UNBOUND))
    item = resp.json()["items"][0]
    assert item["status"] == "paused"
    assert item["paused_text"]
    assert "resume" not in item["paused_text"]
    assert "Резюме" in item["paused_text"]
    assert resp.json()["paused_count"] == 1


def test_list_fields_and_hint(client):
    rule_id = _run(_create_rule(name="Московское", city="msk"))
    _run(bot_db.add_user({
        "telegram_id": 370400001,
        "full_name": "Делегат",
        "registration_date": "2026-01-01 00:00:00",
        "participant_type": "full",
        "event_city": "msk",
    }))
    _run(bot_db.upsert_auto_reject_log(
        370400001, json.dumps([rule_id]), json.dumps(["Текст отказа"]), "2026-11-03 12:00:00",
    ))
    resp = client.get(_LIST, headers=_hdr(UNBOUND))
    item = next(row for row in resp.json()["items"] if row["id"] == rule_id)
    assert item["rejected_count"] == 1
    assert item["summary"]
    assert item["city_label"] == "Москва"
    assert "msk" not in item["city_label"]
    assert "🤖 Автоотказы" in resp.json()["hint"]


def test_catalog_only_enabled_steps_no_codes(client):
    resp = client.get(f"{_LIST}/catalog", headers=_hdr(UNBOUND))
    assert resp.status_code == 200
    steps = resp.json()["steps"]
    codes = {step["step"] for step in steps}
    assert "birth_date" not in codes
    assert "resume" not in codes
    assert "course" in codes
    for step in steps:
        assert step["label"] != step["step"]
        assert "reg_q_" not in step["label"]


def test_get_rule_out_of_scope(client):
    rule_id = _run(_create_rule(name="Чужое", city="msk"))
    denied = client.get(f"{_LIST}/{rule_id}", headers=_hdr(BOUND_KZN))
    assert denied.status_code == 403
    assert denied.json()["reason"] == "out_of_scope"
    missing = client.get(f"{_LIST}/999999", headers=_hdr(BOUND_KZN))
    assert missing.status_code == 404
    assert missing.json()["reason"] == "not_found"


# ── запись ───────────────────────────────────────────────────────────────────────────────


def test_create_always_disabled(client):
    _set("reg_q_birth_date", "on")
    draft = _body(name="С лишним флагом")
    draft["enabled"] = 1
    created = client.post(_LIST, json={"draft": draft, "city": "kzn"}, headers=_hdr(UNBOUND))
    assert created.status_code == 200
    assert created.json()["rule"]["enabled"] == 0
    preset = client.post(_LIST, json={"preset": 0, "city": "kzn"}, headers=_hdr(UNBOUND))
    assert preset.status_code == 200
    rule = preset.json()["rule"]
    assert rule["enabled"] == 0
    steps = [cond["step"] for group in rule["conditions"] for cond in group]
    assert "birth_date" in steps


def test_save_validation(client):
    rule_id = _run(_create_rule(name="Черновик", city="kzn"))
    rev = client.get(f"{_LIST}/{rule_id}", headers=_hdr(UNBOUND)).json()["rule"]["rev"]
    empty = client.put(
        f"{_LIST}/{rule_id}",
        json=_body(tracks=[], base_rev=rev),
        headers=_hdr(UNBOUND),
    )
    assert empty.status_code == 400
    assert empty.json()["reason"] == "invalid"
    assert "тип заявки" in empty.json()["text"]
    unknown = client.put(
        f"{_LIST}/{rule_id}",
        json=_body(action="ban", base_rev=rev),
        headers=_hdr(UNBOUND),
    )
    assert unknown.status_code == 400
    assert "действие" in unknown.json()["text"]
    missing_option = client.put(
        f"{_LIST}/{rule_id}",
        json=_body(
            conditions=[[{"step": "course", "op": "in", "values": ["нет такого"]}]],
            base_rev=rev,
        ),
        headers=_hdr(UNBOUND),
    )
    assert missing_option.status_code == 400
    assert missing_option.json()["reason"] == "invalid"
    assert missing_option.json()["text"]
    assert "course" not in missing_option.json()["text"]


def test_stale_409(client):
    rule_id = _run(_create_rule(name="Слепок", city="kzn"))
    rev = client.get(f"{_LIST}/{rule_id}", headers=_hdr(UNBOUND)).json()["rule"]["rev"]
    first = client.put(
        f"{_LIST}/{rule_id}", json=_body(name="Первое", base_rev=rev), headers=_hdr(UNBOUND),
    )
    assert first.status_code == 200
    second = client.put(
        f"{_LIST}/{rule_id}", json=_body(name="Второе", base_rev=rev), headers=_hdr(UNBOUND),
    )
    assert second.status_code == 409
    assert second.json()["reason"] == "stale"
    fresh = second.json()["rule"]["rev"]
    assert fresh and fresh != rev
    third = client.put(
        f"{_LIST}/{rule_id}", json=_body(name="Третье", base_rev=fresh), headers=_hdr(UNBOUND),
    )
    assert third.status_code == 200
    assert third.json()["rule"]["name"] == "Третье"


def test_chat_edit_between_get_and_put_409(client):
    rule_id = _run(_create_rule(name="До чата", city="kzn"))
    rev = client.get(f"{_LIST}/{rule_id}", headers=_hdr(UNBOUND)).json()["rule"]["rev"]
    rule = _run(load_rule(rule_id))
    _run(save_patch(UNBOUND, rule, name="Из чата"))
    resp = client.put(
        f"{_LIST}/{rule_id}", json=_body(name="Поверх", base_rev=rev), headers=_hdr(UNBOUND),
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale"


def test_pause_recompute_not_stale(client):
    _set("reject_rules_enabled", "on")
    rule_id = _run(_create_rule(
        name="До паузы",
        enabled=1,
        conditions=[[{"step": "resume", "op": "no_file", "values": []}]],
        reject_text="Пришлите резюме.",
    ))
    current = client.get(f"{_LIST}/{rule_id}", headers=_hdr(UNBOUND)).json()["rule"]
    _run(active_rules())
    resp = client.put(
        f"{_LIST}/{rule_id}",
        json={
            "name": "После паузы",
            "city": current["city"],
            "tracks": current["tracks"],
            "conditions": current["conditions"],
            "action": current["action"],
            "reject_text": current["reject_text"],
            "base_rev": current["rev"],
        },
        headers=_hdr(UNBOUND),
    )
    assert resp.status_code == 200
    assert resp.json()["rule"]["name"] == "После паузы"


async def _table_snap():
    async with bot_db._connect() as conn:
        rules = await (await conn.execute(
            "SELECT id, name, enabled, conditions FROM reject_rules ORDER BY id"
        )).fetchall()
        users = await (await conn.execute(
            "SELECT telegram_id, course, full_name FROM users ORDER BY telegram_id"
        )).fetchall()
        return [tuple(row) for row in rules], [tuple(row) for row in users]


def test_preview_readonly(client):
    before = _run(_table_snap())
    resp = client.post(
        f"{_LIST}/preview",
        json=_body(
            city=None,
            conditions=[[{"step": "birth_date", "op": "age_on_forum_lt", "values": ["18"]}]],
        ),
        headers=_hdr(UNBOUND),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["forum_date_missing"] is True
    assert "0 из" not in body["text"]
    assert _run(_table_snap()) == before


def test_enable_needs_confirm(client):
    rule_id = _run(_create_rule(name="Ждёт", enabled=0, city=None))
    _run(bot_db.add_user({
        "telegram_id": 370400002,
        "full_name": "Попадает",
        "registration_date": "2026-01-01 00:00:00",
        "participant_type": "full",
        "course": "1",
    }))
    rev = client.get(f"{_LIST}/{rule_id}", headers=_hdr(UNBOUND)).json()["rule"]["rev"]
    asked = client.post(
        f"{_LIST}/{rule_id}/enable",
        json={"confirm": False, "base_rev": rev},
        headers=_hdr(UNBOUND),
    )
    assert asked.status_code == 200
    confirm = asked.json()["needs_confirm"]
    assert confirm["matched"]
    assert confirm["total"]
    assert "Уже поданные заявки не изменятся" in confirm["text"]
    assert _run(bot_db.get_reject_rule(rule_id))["enabled"] == 0
    turned = client.post(
        f"{_LIST}/{rule_id}/enable",
        json={"confirm": True, "base_rev": rev},
        headers=_hdr(UNBOUND),
    )
    assert turned.status_code == 200
    assert turned.json()["rule"]["enabled"] == 1


def test_enable_guard_400(client):
    rule_id = _run(_create_rule(
        name="Пустое", enabled=0, conditions=[], reject_text="Текст есть.",
    ))
    resp = client.post(
        f"{_LIST}/{rule_id}/enable",
        json={"confirm": True},
        headers=_hdr(UNBOUND),
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid"
    assert "нет ни одного условия" in resp.json()["text"]
    assert _run(bot_db.get_reject_rule(rule_id))["enabled"] == 0


def test_copy_disabled(client):
    source = _run(_create_rule(name="Школьники", city="msk"))
    copied = client.post(
        f"{_LIST}/{source}/copy", json={"city": "kzn"}, headers=_hdr(UNBOUND),
    )
    assert copied.status_code == 200
    rule = copied.json()["rule"]
    assert rule["enabled"] == 0
    assert rule["name"].endswith(" (копия)")
    assert rule["city"] == "kzn"
    own = _run(_create_rule(name="Своё", city="kzn"))
    denied = client.post(
        f"{_LIST}/{own}/copy", json={"city": "msk"}, headers=_hdr(BOUND_KZN),
    )
    assert denied.status_code == 403
    assert denied.json()["reason"] == "out_of_scope"


def test_delete_confirm(client):
    rule_id = _run(_create_rule(name="Удалить", enabled=1, city="kzn"))
    _run(bot_db.add_user({
        "telegram_id": 370400003,
        "full_name": "Журнал",
        "registration_date": "2026-01-01 00:00:00",
    }))
    _run(bot_db.upsert_auto_reject_log(
        370400003, json.dumps([rule_id]), json.dumps(["отказ"]), "2026-11-03 12:00:00",
    ))
    asked = client.delete(f"{_LIST}/{rule_id}?confirm=0", headers=_hdr(UNBOUND))
    assert asked.status_code == 200
    confirm = asked.json()["needs_confirm"]
    assert confirm["rejected_so_far"] == 1
    assert confirm["can_disable_instead"] is True
    assert _run(bot_db.get_reject_rule(rule_id)) is not None
    removed = client.delete(f"{_LIST}/{rule_id}?confirm=1", headers=_hdr(UNBOUND))
    assert removed.status_code == 200
    assert removed.json()["ok"] is True
    assert _run(bot_db.get_reject_rule(rule_id)) is None


def test_master_bound_403_and_confirm(client):
    _run(_create_rule(name="Включено", enabled=1, city="kzn"))
    denied = client.post(_LIST + "/master", json={"enabled": False}, headers=_hdr(BOUND_KZN))
    assert denied.status_code == 403
    assert denied.json()["reason"] == "out_of_scope"
    assert denied.json()["text"]
    turned_on = client.post(_LIST + "/master", json={"enabled": True}, headers=_hdr(UNBOUND))
    assert turned_on.status_code == 200
    assert turned_on.json()["enabled"] is True
    assert _run(bot_db.get_setting("reject_rules_enabled")) == "on"
    asked = client.post(_LIST + "/master", json={"enabled": False}, headers=_hdr(UNBOUND))
    assert asked.status_code == 200
    confirm = asked.json()["needs_confirm"]
    assert confirm["enabled_count"] == 1
    assert "Перестанут работать" in confirm["text"]
    assert _run(bot_db.get_setting("reject_rules_enabled")) == "on"
    turned_off = client.post(
        _LIST + "/master", json={"enabled": False, "confirm": True}, headers=_hdr(UNBOUND),
    )
    assert turned_off.status_code == 200
    assert turned_off.json()["enabled"] is False
    assert _run(bot_db.get_setting("reject_rules_enabled")) == "off"


@pytest.mark.parametrize("kind", ["put", "enable", "disable", "copy", "delete", "post"])
@pytest.mark.parametrize("target", ["msk", "all"])
def test_scope_every_mutation(client, kind, target):
    city = "msk" if target == "msk" else None
    rule_id = _run(_create_rule(name="Чужое", city=city))
    hdr = _hdr(BOUND_KZN)
    if kind == "put":
        resp = client.put(f"{_LIST}/{rule_id}", json=_body(city=city), headers=hdr)
    elif kind == "enable":
        resp = client.post(f"{_LIST}/{rule_id}/enable", json={"confirm": True}, headers=hdr)
    elif kind == "disable":
        resp = client.post(f"{_LIST}/{rule_id}/disable", json={}, headers=hdr)
    elif kind == "copy":
        resp = client.post(f"{_LIST}/{rule_id}/copy", json={"city": "kzn"}, headers=hdr)
    elif kind == "delete":
        resp = client.delete(f"{_LIST}/{rule_id}?confirm=1", headers=hdr)
    else:
        resp = client.post(_LIST, json={"draft": _body(), "city": city}, headers=hdr)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "out_of_scope"
