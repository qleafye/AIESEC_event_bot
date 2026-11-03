"""Слой редактора правил автоотказа: слепок, гейт включения, каталог, подтверждения.

pytest-asyncio нет — async через asyncio.run. Временная база — fast_init_db.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from config import config
from database import db
from domain.cities import city_codes
from domain.regform.engine import label_for, options
import services.applications.reject_rules as rr
import services.applications.reject_rules_editor as editor
from tests._dbtpl import fast_init_db
from tests.test_miniapp_labels_drift import _loaded_aiogram

SUPERADMIN_ID = 370100001
BOUND_MANAGER_ID = 370100002
UNBOUND_MANAGER_ID = 370100003


def _ready(tmp_path, name="test_reject_rules_editor.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


async def _setup_staff():
    await db.add_staff(BOUND_MANAGER_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MANAGER_ID, "msk")
    await db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", SUPERADMIN_ID)


async def _course_values():
    live = await options("course")
    return live[:2]


def _draft(**overrides):
    fields = dict(
        name="Школьники", city=None, tracks=["full"],
        conditions=[[{"step": "resume", "op": "no_file", "values": []}]],
        action="reject", reject_text="текст отказа", enabled=1,
    )
    fields.update(overrides)
    return fields


async def _course_draft(**overrides):
    values = await _course_values()
    fields = _draft(
        conditions=[[{"step": "course", "op": "in", "values": values}]],
    )
    fields.update(overrides)
    return fields


def _chat_fp(conditions):
    raw = json.dumps(conditions or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


async def _snap():
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT id, name, enabled, conditions, updated_at FROM reject_rules ORDER BY id"
        ) as cursor:
            rules = await cursor.fetchall()
        async with conn.execute(
            "SELECT telegram_id, full_name, course, birth_date FROM users ORDER BY telegram_id"
        ) as cursor:
            users = await cursor.fetchall()
    return rules, users


# ══════════════════════════════════════════════════════════════════════════════════════════
# Примитивы
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_editor_module_does_not_load_aiogram():
    loaded = _loaded_aiogram("import services.applications.reject_rules_editor")
    assert loaded == [], f"редактор потянул aiogram: {loaded}"


def test_conditions_fp_matches_chat_formula():
    conditions = [[{"step": "course", "op": "in", "values": ["1", "2"]}]]
    assert editor.conditions_fp(conditions) == _chat_fp(conditions)
    assert len(editor.conditions_fp(conditions)) == 8
    assert editor.conditions_fp([]) == editor.conditions_fp(None)
    assert editor.conditions_fp([]) == _chat_fp(None)


def test_rule_rev_ignores_updated_at_and_pause(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(name="До паузы", enabled=1)
        rule_id, error = await rr.save_rule(SUPERADMIN_ID, None, **draft)
        assert error is None
        await db.set_setting("reg_q_course", "off")
        await db.set_setting("reject_rules_enabled", "on")
        before = await editor.load_rule(rule_id)
        rev_before = editor.rule_rev(before)
        updated_before = before["updated_at"]
        await rr.active_rules(event_city="msk", participant_type="full")
        after = await editor.load_rule(rule_id)
        return rev_before, editor.rule_rev(after), updated_before, after

    rev_before, rev_after, _updated_before, after = _run(_scenario())
    assert after["paused_reason"]
    assert rev_before == rev_after


def test_rule_rev_changes_on_content():
    base = {
        "name": "А", "city": None, "tracks": ["full"],
        "conditions": [[{"step": "resume", "op": "no_file", "values": []}]],
        "action": "reject", "reject_text": "текст", "enabled": 1,
        "updated_at": "2026-01-01", "paused_reason": None,
    }
    original = editor.rule_rev(base)
    assert editor.rule_rev({**base, "name": "Б"}) != original
    assert editor.rule_rev({**base, "conditions": []}) != original
    assert editor.rule_rev({**base, "enabled": 0}) != original
    assert editor.rule_rev({**base, "updated_at": "2026-11-03", "paused_reason": "Курс"}) == original


def test_catalog_only_enabled_steps(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        await db.set_setting("reg_q_course", "off")
        cat = await editor.catalog(SUPERADMIN_ID, "msk", ["full"])
        study = next(step for step in cat["steps"] if step["step"] == "study_field")
        live = await options("study_field")
        return cat, study, live

    cat, study, live = _run(_scenario())
    assert "course" not in {step["step"] for step in cat["steps"]}
    assert study["category"] in ("select", "multi")
    assert study["options"] == live
    assert study["options"]
    for op in study["operators"]:
        assert op["label"] == rr.OPERATOR_LABELS[op["op"]]
        assert op["label"] != op["op"]
    for city in cat["cities"]:
        assert city["label"] != city["code"]
        assert city["label"] not in city_codes()
    assert any(city["label"] == "Все города" and city["code"] is None for city in cat["cities"])
    assert cat["tracks"][0] == {"code": "full", "label": "Полная"}


def test_enable_guard_no_conditions(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id, error = _run(rr.save_rule(
        SUPERADMIN_ID, None, **_draft(conditions=[], enabled=1),
    ))
    assert rule_id is None
    assert error == "В правиле нет ни одного условия — включить нечего. Добавьте условие."


def test_enable_guard_disabled_step(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        await db.set_setting("reg_q_course", "off")
        draft = await _course_draft(city="msk", enabled=1)
        return await rr.save_rule(SUPERADMIN_ID, None, **draft)

    rule_id, error = _run(_scenario())
    assert rule_id is None
    assert label_for("course") in error
    assert "Включите вопрос в «⚙️ Настройки» или уберите это условие" in error
    assert "course" not in error
    assert "msk" not in error


def test_enable_guard_rename_paused_ok(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(name="Старое", city="msk", enabled=1)
        rule_id, error = await rr.save_rule(SUPERADMIN_ID, None, **draft)
        assert error is None
        await db.set_setting("reg_q_course", "off")
        await db.set_setting("reject_rules_enabled", "on")
        await rr.active_rules(event_city="msk", participant_type="full")
        loaded = await editor.load_rule(rule_id)
        assert loaded["paused_reason"]
        renamed, rename_error = await rr.save_rule(
            SUPERADMIN_ID, rule_id,
            name="Новое", city=loaded["city"], tracks=loaded["tracks"],
            conditions=loaded["conditions"], action=loaded["action"],
            reject_text=loaded["reject_text"], enabled=1,
        )
        saved = await db.get_reject_rule(rule_id)
        return renamed, rename_error, saved["name"]

    renamed, rename_error, name = _run(_scenario())
    assert rename_error is None
    assert renamed is not None
    assert name == "Новое"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Операции
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_save_draft_stale(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        created = await editor.create_rule(SUPERADMIN_ID, _draft(name="Первое", enabled=1))
        assert created.error is None
        base = created.view["rev"]
        rule_id = created.view["id"]
        first = await editor.save_draft(
            SUPERADMIN_ID, rule_id, _draft(name="Второе", enabled=1), base,
        )
        second = await editor.save_draft(
            SUPERADMIN_ID, rule_id, _draft(name="Третье", enabled=1), base,
        )
        row = await db.get_reject_rule(rule_id)
        return first, second, row["name"]

    first, second, name = _run(_scenario())
    assert first.stale is False and first.view["name"] == "Второе"
    assert second.stale is True
    assert second.fresh["name"] == "Второе"
    assert name == "Второе"


def test_save_draft_pause_recompute_not_stale(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(name="Живое", enabled=1)
        rule_id, error = await rr.save_rule(SUPERADMIN_ID, None, **draft)
        assert error is None
        loaded = await editor.load_rule(rule_id)
        base = editor.rule_rev(loaded)
        await db.set_setting("reg_q_course", "off")
        await db.set_setting("reject_rules_enabled", "on")
        await rr.active_rules(event_city="msk", participant_type="full")
        result = await editor.save_draft(
            SUPERADMIN_ID, rule_id,
            {**draft, "name": "После паузы", "tracks": loaded["tracks"], "conditions": loaded["conditions"]},
            base,
        )
        return result

    result = _run(_scenario())
    assert result.stale is False
    assert result.error is None
    assert result.view["name"] == "После паузы"


def test_save_draft_forces_create_disabled(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    created = _run(editor.create_rule(SUPERADMIN_ID, _draft(enabled=1)))
    assert created.error is None
    assert created.view["enabled"] == 0
    row = _run(db.get_reject_rule(created.view["id"]))
    assert row["enabled"] == 0


def test_create_from_preset_disabled(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        blocked = await editor.create_from_preset(SUPERADMIN_ID, 0, "msk")
        made = await editor.create_from_preset(SUPERADMIN_ID, 1, "msk")
        row = await db.get_reject_rule(made.view["id"]) if made.view else None
        return blocked, made, row

    blocked, made, row = _run(_scenario())
    assert blocked.error
    assert "Нужен вопрос" in blocked.error and "выключен" in blocked.error
    assert made.error is None
    assert made.view["enabled"] == 0
    assert row["enabled"] == 0
    assert json.loads(row["conditions"])


def test_copy_disabled_and_scoped(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        created = await editor.create_rule(
            SUPERADMIN_ID, _draft(name="Школьники", city="msk", enabled=0),
        )
        rule_id = created.view["id"]
        foreign = await editor.copy_to_city(BOUND_MANAGER_ID, rule_id, "spb")
        own = await editor.copy_to_city(UNBOUND_MANAGER_ID, rule_id, "spb")
        row = await db.get_reject_rule(own.view["id"]) if own.view else None
        return foreign, own, row

    foreign, own, row = _run(_scenario())
    assert foreign.error
    assert foreign.view is None
    assert own.error is None
    assert own.view["enabled"] == 0
    assert own.view["name"] == "Школьники (копия)"
    assert row["enabled"] == 0
    assert row["city"] == "spb"


def test_set_enabled_needs_confirm(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(name="Ждёт", enabled=0)
        created = await editor.create_rule(SUPERADMIN_ID, draft)
        values = draft["conditions"][0][0]["values"]
        await db.add_user({
            "telegram_id": 370210001,
            "full_name": "Попадает",
            "registration_date": "2026-01-01 00:00:00",
            "participant_type": "full",
            "course": values[0],
        })
        rule_id = created.view["id"]
        base = created.view["rev"]
        asked = await editor.set_enabled(
            SUPERADMIN_ID, rule_id, True, confirm=False, base_rev=base,
        )
        still = await db.get_reject_rule(rule_id)
        turned = await editor.set_enabled(
            SUPERADMIN_ID, rule_id, True, confirm=True, base_rev=base,
        )
        await editor.save_draft(
            SUPERADMIN_ID, rule_id, {**draft, "name": "Уже другое"}, turned.view["rev"],
        )
        stale = await editor.set_enabled(
            SUPERADMIN_ID, rule_id, False, confirm=True, base_rev=base,
        )
        return asked, still["enabled"], turned, stale

    asked, enabled, turned, stale = _run(_scenario())
    assert asked.needs_confirm
    assert "Уже поданные заявки не изменятся" in asked.needs_confirm["text"]
    assert enabled == 0
    assert turned.error is None
    assert turned.view["enabled"] == 1
    assert stale.stale is True


def test_enable_guard_via_editor(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        created = await editor.create_rule(
            SUPERADMIN_ID, _draft(conditions=[], reject_text="текст", enabled=1),
        )
        return await editor.set_enabled(
            SUPERADMIN_ID, created.view["id"], True, confirm=True, base_rev=created.view["rev"],
        )

    result = _run(_scenario())
    assert result.error == "В правиле нет ни одного условия — включить нечего. Добавьте условие."
    assert result.view is None


def test_preview_readonly_and_no_forum_date(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        before = await _snap()
        result = await editor.preview(SUPERADMIN_ID, _draft(
            city="msk",
            conditions=[[{"step": "birth_date", "op": "age_on_forum_lt", "values": [18]}]],
        ))
        after = await _snap()
        return result, before, after

    result, before, after = _run(_scenario())
    assert before == after
    assert result["forum_date_missing"] is True
    assert result["text"].startswith("Не посчитать: у ")
    assert "не указана дата форума" in result["text"]
    assert "0 из" not in result["text"]
    assert "Попали бы" not in result["text"]


def test_preview_too_wide(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        for tid in (370200001, 370200002):
            await db.add_user({
                "telegram_id": tid,
                "full_name": f"D{tid}",
                "registration_date": "2026-01-01 00:00:00",
                "participant_type": "full",
            })
        return await editor.preview(SUPERADMIN_ID, _draft())

    result = _run(_scenario())
    assert result["too_wide"] is True
    assert result["matched"] == 2
    assert result["total"] == 2


def test_delete_needs_confirm(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(name="Удаляемое", city=None, enabled=1)
        rule_id, error = await rr.save_rule(SUPERADMIN_ID, None, **draft)
        assert error is None
        asked = await editor.delete(SUPERADMIN_ID, rule_id, confirm=False)
        still = await db.get_reject_rule(rule_id)
        bound = await editor.delete(BOUND_MANAGER_ID, rule_id, confirm=True)
        removed = await editor.delete(SUPERADMIN_ID, rule_id, confirm=True)
        gone = await db.get_reject_rule(rule_id)
        return asked, still, bound, removed, gone

    asked, still, bound, removed, gone = _run(_scenario())
    assert still is not None
    assert "пропадёт совсем, вернуть его будет нельзя" in asked.needs_confirm["text"]
    assert "Сейчас правило включено" in asked.needs_confirm["text"]
    assert asked.needs_confirm["can_disable_instead"] is True
    assert bound.error
    assert bound.forbidden is True
    assert removed.error is None
    assert gone is None


def test_master_state_bound_vs_unbound(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        draft = await _course_draft(enabled=1)
        _rule_id, error = await rr.save_rule(SUPERADMIN_ID, None, **draft)
        assert error is None
        bound = await editor.master_state(BOUND_MANAGER_ID)
        unbound = await editor.master_state(UNBOUND_MANAGER_ID)
        return bound, unbound

    bound, unbound = _run(_scenario())
    assert bound["can_toggle"] is False
    assert "руководитель без привязки" in bound["status_text"]
    assert unbound["can_toggle"] is True
    assert unbound["enabled_count"] >= 1
    phrase = (
        f"Перестанут работать {unbound['enabled_count']} включённых правил во всех городах"
    )
    assert phrase in unbound["confirm_text"]


def test_preview_1500_rows_timing(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())

    async def _scenario():
        rows = [
            (370300000 + i, f"D{i}", "2026-01-01 00:00:00", "full", "msk")
            for i in range(1500)
        ]
        async with db._connect() as conn:
            await conn.executemany(
                "INSERT INTO users (telegram_id, full_name, registration_date, "
                "participant_type, event_city) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await conn.commit()
        started = time.perf_counter()
        result = await editor.preview(SUPERADMIN_ID, _draft(city="msk"))
        spent = time.perf_counter() - started
        print(f"preview 1500 rows: {spent:.3f}s matched={result['matched']} total={result['total']}")
        return result, spent

    result, spent = _run(_scenario())
    assert result["total"] == 1500
    assert spent < 2
