"""Phase 31 Plan 04 (D-05/D-08/D-10/D-11/D-13/D-14/D-16): `services/reject_rules.py` — служебный
слой правил автоотказа. Три пласта сторожей — по одному на задачу плана:
- Задача 1: `active_rules`/`forum_date_for` — рубильник, фильтр по городу/треку, пересчёт паузы.
- Задача 2: `can_edit_city`/`save_rule`/`delete_rule`/`validate_condition` — право по городу,
  валидация условий против живых вариантов, текст обязателен для отказа.
- Задача 3: `rule_summary`/`RULE_PRESETS`/`dry_run_count` — автоописание, заготовки, честный
  read-only счётчик.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_skillup_scoring_28.py::_ready(tmp_path)`.
"""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
import services.reject_rules as rr
from tests.test_miniapp_labels_drift import _loaded_aiogram


def _ready(tmp_path, name="test_reject_rules_service.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


SUPERADMIN_ID = 900100001
BOUND_MANAGER_ID = 900100002  # привязан к msk
UNBOUND_MANAGER_ID = 900100003  # без привязки


async def _setup_staff():
    config.ADMIN_IDS = [SUPERADMIN_ID]
    await db.add_staff(BOUND_MANAGER_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MANAGER_ID, "msk")
    await db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", SUPERADMIN_ID)


async def _create_rule(**overrides):
    fields = dict(
        name=None, city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
        action="reject", reject_text="текст отказа", enabled=1, created_by=None,
    )
    fields.update(overrides)
    return await db.create_reject_rule(**fields)


async def _seed_user(tid, *, event_city=None, participant_type="full", course=None,
                      birth_date=None, resume_file_id=None):
    await db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
        "participant_type": participant_type,
        "course": course,
        "birth_date": birth_date,
        "resume_file_id": resume_file_id,
    })


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: загрузчик активных правил и дата форума
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_kill_switch_returns_empty_without_db_query(tmp_path, monkeypatch):
    """Рубильник `reject_rules_enabled` (дефолт off, D-15) выключен -> [] СРАЗУ, ни одного
    похода в базу за правилами."""
    _ready(tmp_path)
    calls = []

    async def _fake_list(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(rr, "list_reject_rules", _fake_list)
    result = _run(rr.active_rules(event_city="msk", participant_type="full"))
    assert result == []
    assert calls == [], "list_reject_rules не должен вызываться при выключенном рубильнике"


def test_reject_rules_module_does_not_load_aiogram():
    loaded = _loaded_aiogram("import services.reject_rules")
    assert loaded == [], f"services.reject_rules потянул aiogram: {loaded}"


def test_active_rules_returns_only_enabled_rules(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(reject_text="включено", enabled=1))
    _run(_create_rule(reject_text="выключено", enabled=0))

    rules = _run(rr.active_rules(event_city="msk", participant_type="full"))
    assert len(rules) == 1
    assert rules[0]["reject_text"] == "включено"


def test_active_rules_filters_by_city(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(city="spb", reject_text="spb-only"))

    assert _run(rr.active_rules(event_city="msk", participant_type="full")) == []
    spb_rules = _run(rr.active_rules(event_city="spb", participant_type="full"))
    assert len(spb_rules) == 1


def test_active_rules_all_cities_rule_matches_every_city(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(city=None, reject_text="все города"))

    assert len(_run(rr.active_rules(event_city="msk", participant_type="full"))) == 1
    assert len(_run(rr.active_rules(event_city="spb", participant_type="full"))) == 1


def test_active_rules_filters_by_track(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(tracks=json.dumps(["short"]), reject_text="short-only"))

    assert _run(rr.active_rules(event_city="msk", participant_type="full")) == []
    short_rules = _run(rr.active_rules(event_city="msk", participant_type="short"))
    assert len(short_rules) == 1


def test_active_rules_pauses_rule_on_disabled_question(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "off"))
    rule_id = _run(_create_rule(
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
    ))

    rules = _run(rr.active_rules(event_city="msk", participant_type="full"))
    assert len(rules) == 1
    assert rules[0]["paused_reason"] is not None
    assert rules[0]["paused_changed"] == "on"

    row = _run(db.get_reject_rule(rule_id))
    assert row["paused_reason"] is not None


def test_active_rules_unpauses_when_question_reenabled(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(db.set_setting("reg_q_course", "off"))
    _run(_create_rule(
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
    ))
    _run(rr.active_rules(event_city="msk", participant_type="full"))  # первая загрузка ставит паузу

    _run(db.set_setting("reg_q_course", "on"))
    rules = _run(rr.active_rules(event_city="msk", participant_type="full"))
    assert rules[0]["paused_reason"] is None
    assert rules[0]["paused_changed"] == "off"


def test_active_rules_survives_broken_conditions_json(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(conditions="не json { { {"))

    rules = _run(rr.active_rules(event_city="msk", participant_type="full"))
    assert rules == []


def test_forum_date_for_empty_db_returns_none_without_raising(tmp_path):
    _ready(tmp_path)
    assert _run(rr.forum_date_for(None)) is None


def test_forum_date_for_formats_saved_date(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "31.10.2026"))
    assert _run(rr.forum_date_for("msk")) == "31.10.2026"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: CRUD, права по городу, валидация условий
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_can_edit_city_bound_manager_only_own_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    assert _run(rr.can_edit_city(BOUND_MANAGER_ID, "msk")) is True
    assert _run(rr.can_edit_city(BOUND_MANAGER_ID, "spb")) is False
    assert _run(rr.can_edit_city(BOUND_MANAGER_ID, None)) is False


def test_can_edit_city_unbound_manager_sees_all_cities(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    assert _run(rr.can_edit_city(UNBOUND_MANAGER_ID, "msk")) is True
    assert _run(rr.can_edit_city(UNBOUND_MANAGER_ID, "spb")) is True
    assert _run(rr.can_edit_city(UNBOUND_MANAGER_ID, None)) is True


def test_can_edit_city_superadmin_sees_everything(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    assert _run(rr.can_edit_city(SUPERADMIN_ID, "spb")) is True
    assert _run(rr.can_edit_city(SUPERADMIN_ID, None)) is True


def _reject_rule_fields(**overrides):
    fields = dict(
        city=None, tracks=["full"],
        conditions=[[{"step": "resume", "op": "no_file", "values": []}]],
        action="reject", reject_text="текст отказа", enabled=1,
    )
    fields.update(overrides)
    return fields


def test_save_rule_city_rights_bound_manager_cannot_save_other_city(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(BOUND_MANAGER_ID, None, **_reject_rule_fields(city="spb")))
    assert rule_id is None
    assert error


def test_save_rule_city_rights_bound_manager_cannot_create_all_cities_rule(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(BOUND_MANAGER_ID, None, **_reject_rule_fields(city=None)))
    assert rule_id is None
    assert error


def test_save_rule_all_cities_unbound_manager_creates_it(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(UNBOUND_MANAGER_ID, None, **_reject_rule_fields(city=None)))
    assert error is None
    assert rule_id is not None


def test_save_rule_steal_foreign_rule_into_own_city_rejected(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    foreign_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(city="spb")))
    assert error is None

    result_id, steal_error = _run(rr.save_rule(
        BOUND_MANAGER_ID, foreign_id, **_reject_rule_fields(city="msk"),
    ))
    assert result_id is None
    assert steal_error


def test_save_rule_unknown_operator_rejected(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(
        conditions=[[{"step": "course", "op": "contains", "values": ["1"]}]],
    )))
    assert rule_id is None
    assert error
    assert "step" not in error and "op" not in error


def test_save_rule_vanished_option_value_rejected_without_step_key(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(
        conditions=[[{"step": "course", "op": "in", "values": ["999-не-существует"]}]],
    )))
    assert rule_id is None
    assert error
    assert "step" not in error and "op" not in error


def test_save_rule_between_with_one_value_rejected(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(
        conditions=[[{"step": "age", "op": "between", "values": [18]}]],
    )))
    assert rule_id is None
    assert error


def test_save_rule_reject_action_without_text_not_enabled(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(reject_text="")))
    assert rule_id is None
    assert error


def test_save_rule_success_round_trip(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields()))
    assert error is None
    saved = _run(db.get_reject_rule(rule_id))
    assert saved["action"] == "reject"
    assert json.loads(saved["conditions"]) == [[{"step": "resume", "op": "no_file", "values": []}]]


def test_delete_rule_respects_city_right(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    foreign_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_reject_rule_fields(city="spb")))
    assert error is None

    ok, delete_error = _run(rr.delete_rule(BOUND_MANAGER_ID, foreign_id))
    assert ok is False
    assert delete_error
    assert _run(db.get_reject_rule(foreign_id)) is not None

    ok2, delete_error2 = _run(rr.delete_rule(SUPERADMIN_ID, foreign_id))
    assert ok2 is True
    assert delete_error2 is None
    assert _run(db.get_reject_rule(foreign_id)) is None
