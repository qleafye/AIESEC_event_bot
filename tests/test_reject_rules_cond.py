"""Phase 31 Plan 10 (D-01/D-02/D-09/D-11/D-13/D-16): конструктор условий правила автоотказа —
вопрос → оператор → значения, группы И/ИЛИ, заготовки в один тап, счётчик dry-run.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, Fake-объекты и БД —
та же форма, что `tests/test_reject_rules_editor.py`.

Три пласта сторожей:
- Задача 1: три шага сборки условия (вопрос → оператор → значения), живые данные анкеты.
- Задача 2: группы И/ИЛИ, удаление условия, заготовки в один тап.
- Задача 3: счётчик «попали бы N из M» перед включением.
"""
from __future__ import annotations

import asyncio
import json

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_reject_cond as arc
from handlers.admin_caps import required_capability
from handlers.states import RejectCond


def _run(coro):
    return asyncio.run(coro)


SUPERADMIN_ID = 900400001
BOUND_MSK_ID = 900400002   # привязан к msk
BOUND_SPB_ID = 900400003   # привязан к spb


def _ready(tmp_path, name="test_reject_rules_cond.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _setup_staff():
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _create_rule(**overrides):
    fields = dict(
        name=None, city=None, tracks=json.dumps(["full"]), conditions=json.dumps([]),
        action="reject", reject_text=None, enabled=0, created_by=None,
    )
    fields.update(overrides)
    return await db.create_reject_rule(**fields)


async def _seed_user(tid, *, event_city=None, participant_type="full", course=None):
    await db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city, "participant_type": participant_type, "course": course,
    })


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text=None, user_id=SUPERADMIN_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.text_edited = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text


class _FakeCallback:
    def __init__(self, data, user_id=SUPERADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: три шага сборки условия
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_step_screen_excludes_disabled_step(tmp_path):
    """`stack` (reg_q_stack) выключен по умолчанию (D-14 anti-pattern guard)."""
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    screen = _run(arc.render_step_screen(SUPERADMIN_ID, rule_id, -1))
    assert screen is not None
    _text, kb = screen
    cbs = _cbs(kb)
    assert not any(cb and cb.startswith(f"arc_step:{rule_id}:-1:stack") for cb in cbs)


def test_operators_for_text_category_exactly_two_no_regex(tmp_path):
    """D-01: свободный текст — только «заполнено»/«не заполнено», никаких регэкспов."""
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    text, kb = _run(arc.render_op_screen(SUPERADMIN_ID, rule_id, -1, "expectations"))
    labels = set(_texts(kb)) - {"← Отмена"}
    assert labels == {"заполнено", "не заполнено"}
    for forbidden in ("содержит", "regex", "регуляр"):
        assert forbidden not in text.lower()
        assert forbidden not in " ".join(_texts(kb)).lower()


def test_live_options_not_literal(tmp_path, monkeypatch):
    """Пикер значений показывает ОТРЕДАКТИРОВАННЫЙ менеджером список — код зовёт
    `reg_engine.options(step)` заново, не хардкодит вариант."""
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))

    async def _fake_options(step_key):
        if step_key == "course":
            return ["Отредактированный 1", "Отредактированный 2"]
        return []

    monkeypatch.setattr(arc, "options", _fake_options)
    screen = _run(arc.render_value_screen(SUPERADMIN_ID, rule_id, -1, "course", set()))
    _text, kb = screen
    labels = _texts(kb)
    assert any("Отредактированный 1" in t for t in labels)


def test_valdone_empty_values_alerts_and_does_not_save(tmp_path, monkeypatch):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    calls = []

    async def _fake_save_rule(*args, **kwargs):
        calls.append((args, kwargs))
        return rule_id, None

    monkeypatch.setattr(arc, "save_rule", _fake_save_rule)
    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="course", arc_op="in", arc_checked=[]))
    callback = _FakeCallback(f"arc_valdone:{rule_id}:-1", user_id=SUPERADMIN_ID)
    _run(arc.arc_valdone(callback, state))
    assert callback.answers and callback.answers[0][1] is True
    assert "Отметьте" in callback.answers[0][0]
    assert calls == []


def test_valdone_saves_labels_not_indices(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="course", arc_op="in", arc_checked=[0, 1]))
    callback = _FakeCallback(f"arc_valdone:{rule_id}:-1", user_id=SUPERADMIN_ID)
    _run(arc.arc_valdone(callback, state))
    row = _run(db.get_reject_rule(rule_id))
    conditions = json.loads(row["conditions"])
    assert conditions[0][0]["values"] == ["1", "2"]


def test_between_input_produces_two_numbers(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="age", arc_op="between"))
    _run(state.set_state(RejectCond.num))
    message = _FakeMessage(text="18;21", user_id=SUPERADMIN_ID)
    _run(arc.arc_num_step(message, state))
    row = _run(db.get_reject_rule(rule_id))
    conditions = json.loads(row["conditions"])
    assert conditions[0][0]["values"] == [18, 21]


def test_arc_step_foreign_city_denied(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _run(_create_rule(city="spb"))
    state = _new_state(BOUND_MSK_ID)
    callback = _FakeCallback(f"arc_step:{rule_id}:-1:course", user_id=BOUND_MSK_ID)
    _run(arc.arc_step(callback, state))
    assert callback.answers and callback.answers[0][1] is True
    data = _run(state.get_data())
    assert data.get("arc_step") is None


def test_required_capability_covers_every_arc_prefix():
    samples = [
        "arc_add:1:0", "arc_steppage:1:0:0", "arc_step:1:0:course", "arc_op:1:0:in",
        "arc_val:1:0:2", "arc_valpage:1:0:0", "arc_valdone:1:0", "arc_num:1:0",
        "arc_del:1:0:0", "arc_dellist:1", "arc_cancel:1", "arc_presetlist:1",
        "arc_preset:new:0", "arc_dry:1", "arc_gate:1", "arc_dry_go:1",
    ]
    for cb in samples:
        assert required_capability(callback_data=cb) == "settings", cb
    assert required_capability(raw_state="RejectCond:num") == "settings"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: группы И/ИЛИ, удаление, заготовки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_group_structure_after_three_adds(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))

    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="course", arc_op="in", arc_checked=[0]))
    cb1 = _FakeCallback(f"arc_valdone:{rule_id}:-1", user_id=SUPERADMIN_ID)
    _run(arc.arc_valdone(cb1, state))

    _run(state.update_data(arc_rule=rule_id, arc_group=0, arc_step="resume", arc_op="no_file"))
    cb2 = _FakeCallback(f"arc_op:{rule_id}:0:no_file", user_id=SUPERADMIN_ID)
    _run(arc._finish_condition(cb2, state, rule_id, 0, "resume", "no_file", []))

    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="expectations", arc_op="filled"))
    cb3 = _FakeCallback(f"arc_op:{rule_id}:-1:filled", user_id=SUPERADMIN_ID)
    _run(arc._finish_condition(cb3, state, rule_id, -1, "expectations", "filled", []))

    row = _run(db.get_reject_rule(rule_id))
    conditions = json.loads(row["conditions"])
    assert len(conditions) == 2
    assert len(conditions[0]) == 2
    assert len(conditions[1]) == 1


def test_delete_last_condition_of_group_removes_group(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk", enabled=0,
        conditions=json.dumps([
            [{"step": "resume", "op": "no_file", "values": []}],
            [{"step": "expectations", "op": "filled", "values": []}],
        ]),
    ))
    callback = _FakeCallback(f"arc_del:{rule_id}:0:0", user_id=SUPERADMIN_ID)
    _run(arc.arc_del(callback))
    row = _run(db.get_reject_rule(rule_id))
    conditions = json.loads(row["conditions"])
    assert len(conditions) == 1
    assert conditions[0][0]["step"] == "expectations"


def test_delete_last_condition_of_rule_disables_it(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk", enabled=1, reject_text="x",
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
    ))
    callback = _FakeCallback(f"arc_del:{rule_id}:0:0", user_id=SUPERADMIN_ID)
    _run(arc.arc_del(callback))
    row = _run(db.get_reject_rule(rule_id))
    assert json.loads(row["conditions"]) == []
    assert row["enabled"] == 0
    assert callback.answers and "выключено" in callback.answers[0][0]


def test_card_shows_group_layout_after_building_via_handlers(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", reject_text="Причина"))
    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(arc_rule=rule_id, arc_group=-1, arc_step="course", arc_op="in", arc_checked=[0]))
    cb1 = _FakeCallback(f"arc_valdone:{rule_id}:-1", user_id=SUPERADMIN_ID)
    _run(arc.arc_valdone(cb1, state))
    _run(state.update_data(arc_rule=rule_id, arc_group=-1))
    cb2 = _FakeCallback(f"arc_op:{rule_id}:-1:no_file", user_id=SUPERADMIN_ID)
    _run(arc._finish_condition(cb2, state, rule_id, -1, "resume", "no_file", []))
    from handlers.admin_reject_rules import render_rule_card
    text, _kb = _run(render_rule_card(SUPERADMIN_ID, rule_id))
    assert "Группа 1 (все условия сразу):" in text
    assert "— ИЛИ —" in text
    assert "Группа 2:" in text


def test_preset_new_creates_disabled_rule_with_condition_and_text(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("arc_preset:new:0", user_id=SUPERADMIN_ID)
    _run(arc.arc_preset_pick(callback))
    rules = _run(db.list_reject_rules())
    assert len(rules) == 1
    assert rules[0]["enabled"] == 0
    conditions = json.loads(rules[0]["conditions"])
    assert len(conditions) == 1
    assert rules[0]["reject_text"]


def test_card_buttons_no_longer_stub_lead_to_arc_add(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    from handlers.admin_reject_rules import render_rule_card
    _text, kb = _run(render_rule_card(SUPERADMIN_ID, rule_id))
    cbs = _cbs(kb)
    assert not any(cb == "arr_noop" for cb in cbs)
    assert any(cb and cb.startswith("arc_add:") for cb in cbs)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: счётчик «попали бы N из M»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_arc_dry_shows_matched_and_total_and_explains_no_side_effect(tmp_path):
    _ready(tmp_path)
    _run(_seed_user(1, event_city="msk", course="1"))
    _run(_seed_user(2, event_city="msk", course="2"))
    _run(_seed_user(3, event_city="msk", course="3"))
    rule_id = _run(_create_rule(
        city="msk", enabled=0,
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
    ))
    statuses_before = {tid: _run(db.get_user(tid))["status"] for tid in (1, 2, 3)}
    callback = _FakeCallback(f"arc_dry:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_dry(callback))
    assert callback.answers
    text, show_alert = callback.answers[0]
    assert show_alert is True
    assert "2 из 3" in text
    assert "уже подан" in text
    for tid in (1, 2, 3):
        assert _run(db.get_user(tid))["status"] == statuses_before[tid]
    assert _run(db.count_auto_reject_log()) == 0


def test_arc_gate_first_tap_shows_counter_does_not_enable(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk", enabled=0, reject_text="x",
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
    ))
    callback = _FakeCallback(f"arc_gate:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_gate(callback))
    row = _run(db.get_reject_rule(rule_id))
    assert row["enabled"] == 0
    assert callback.message.text_edited and "Включить правило?" in callback.message.text_edited


def test_arc_dry_go_enables_after_gate_confirm(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk", enabled=0, reject_text="x",
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
    ))
    callback = _FakeCallback(f"arc_dry_go:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_dry_go(callback))
    row = _run(db.get_reject_rule(rule_id))
    assert row["enabled"] == 1
