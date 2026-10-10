"""Правило включено, а все правила выключены разом (`reject_rules_enabled` = off): карточка, экран
«Включить правило?» и окно после включения говорят, что правило пока не работает. Тому, кто управляет
рубильником, карточка даёт кнопку «✅ Включить все правила» и после неё остаётся на карточке;
менеджеру с городом — кто может включить, без кнопки."""
import json

from database import db
from domain.settings.schema import get_setting_typed
from handlers.access.admin_caps import required_capability
from handlers.applications import admin_reject_cond as arc
from handlers.applications import admin_reject_master as master
from handlers.applications import admin_reject_rules
from services.settings.audit import set_setting_by_admin
from tests.test_reject_rules_editor import (
    BOUND_MSK_ID, SUPERADMIN_ID, UNBOUND_ID, _FakeCallback, _cbs, _create_rule, _ready, _run, _setup_staff,
)


def _card(admin_id, rule_id):
    return _run(admin_reject_rules.render_rule_card(admin_id, rule_id))


def _new_rule(**kw):
    return _run(_create_rule(city="msk", reject_text="x", **kw))


def test_card_warns_and_offers_master_button_to_superadmin_and_unbound(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _new_rule(enabled=1)
    for uid in (SUPERADMIN_ID, UNBOUND_ID):
        text, kb = _card(uid, rule_id)
        assert master.OFF_LINE in text
        assert master.BOUND_LINE not in text
        assert f"arr_master_on:{rule_id}" in _cbs(kb)


def test_card_tells_city_bound_who_can_switch_on_without_button(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _new_rule(enabled=1)
    text, kb = _card(BOUND_MSK_ID, rule_id)
    assert master.OFF_LINE in text and master.BOUND_LINE in text
    assert not [cb for cb in _cbs(kb) if cb.startswith("arr_master")]


def test_card_silent_when_rules_work_or_rule_is_off(tmp_path):
    _ready(tmp_path)
    off_rule = _new_rule(enabled=0)
    text, kb = _card(SUPERADMIN_ID, off_rule)
    assert master.OFF_LINE not in text
    _run(set_setting_by_admin(SUPERADMIN_ID, "reject_rules_enabled", "on"))
    on_rule = _new_rule(enabled=1)
    text, kb = _card(SUPERADMIN_ID, on_rule)
    assert master.OFF_LINE not in text
    assert f"arr_master_on:{on_rule}" not in _cbs(kb)


def test_enable_confirmation_says_rule_does_not_work_yet(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _new_rule(enabled=0)
    gate = _FakeCallback(f"arc_gate:{rule_id}", user_id=BOUND_MSK_ID)
    _run(arc.arc_gate(gate))
    assert master.GATE_LINE in gate.message.text_edited and master.BOUND_LINE in gate.message.text_edited

    go = _FakeCallback(f"arc_dry_go:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_dry_go(go))
    alert, show_alert = go.answers[0]
    assert show_alert is True and master.OFF_LINE in alert and "теперь действует" not in alert
    assert len(alert) <= 200
    assert master.OFF_LINE in go.message.text_edited
    assert f"arr_master_on:{rule_id}" in _cbs(go.message.edit_markup)


def test_enable_confirmation_unchanged_when_rules_work(tmp_path):
    _ready(tmp_path)
    _run(set_setting_by_admin(SUPERADMIN_ID, "reject_rules_enabled", "on"))
    rule_id = _new_rule(enabled=0)
    gate = _FakeCallback(f"arc_gate:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_gate(gate))
    assert master.GATE_LINE not in gate.message.text_edited
    go = _FakeCallback(f"arc_dry_go:{rule_id}", user_id=SUPERADMIN_ID)
    _run(arc.arc_dry_go(go))
    assert go.answers[0][0] == master.ENABLED_TEXT


def test_master_button_switches_on_and_stays_on_card(tmp_path):
    _ready(tmp_path)
    rule_id = _new_rule(enabled=1)
    cb = _FakeCallback(f"arr_master_on:{rule_id}", user_id=SUPERADMIN_ID)
    _run(master.arr_master_on(cb))
    assert _run(get_setting_typed("reject_rules_enabled")) is True
    assert "Правило автоотказа" in cb.message.text_edited and master.OFF_LINE not in cb.message.text_edited
    assert _run(db.get_reject_rule(rule_id))["enabled"] == 1
    # Старая кнопка нажата повторно — правила не выключаются.
    _run(master.arr_master_on(_FakeCallback(f"arr_master_on:{rule_id}", user_id=SUPERADMIN_ID)))
    assert _run(get_setting_typed("reject_rules_enabled")) is True


def test_master_button_denied_for_city_bound(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _new_rule(enabled=1)
    cb = _FakeCallback(f"arr_master_on:{rule_id}", user_id=BOUND_MSK_ID)
    _run(master.arr_master_on(cb))
    assert cb.answers[0][1] is True and "без привязки к городу" in cb.answers[0][0]
    assert _run(get_setting_typed("reject_rules_enabled")) is False
    assert required_capability(callback_data=f"arr_master_on:{rule_id}") == "settings"


def test_master_button_keeps_rule_as_is(tmp_path):
    """Кнопка рубильника не трогает условия и включённость правила."""
    _ready(tmp_path)
    rule_id = _new_rule(enabled=1)
    before = _run(db.get_reject_rule(rule_id))
    _run(master.arr_master_on(_FakeCallback(f"arr_master_on:{rule_id}", user_id=SUPERADMIN_ID)))
    after = _run(db.get_reject_rule(rule_id))
    assert json.loads(after["conditions"]) == json.loads(before["conditions"])
    assert after["enabled"] == before["enabled"]
