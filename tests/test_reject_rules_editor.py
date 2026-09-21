"""Phase 31 Plan 08 (D-09/D-10/D-12/D-15/D-16/D-21): «🚫 Правила автоотказа» в чат-боте — экран
списка, карточка правила, общий рубильник, копирование в другой город, удаление с подтверждением.

pytest-asyncio недоступна в этом окружении — async через `asyncio.run()` (форма
`tests/test_faq_260906.py`); БД — `tmp_path`, Fake-объекты callback/message — та же форма, что
`tests/test_faq_260906.py::_FakeCallback/_FakeMessage`.

Три пласта сторожей — по задачам плана:
- Задача 1 (этот срез): экран списка, capability-гейт, общий рубильник, стейл-гард чужого
  правила, заготовки. Задачи 2/3 дописывают карточку/копирование/удаление отдельными коммитами.
"""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
from handlers import admin_reject_rules
from handlers.admin_caps import required_capability
from settings_schema import get_setting_typed


def _run(coro):
    return asyncio.run(coro)


SUPERADMIN_ID = 900300001
BOUND_MSK_ID = 900300002   # привязан к msk
BOUND_SPB_ID = 900300003   # привязан к spb
UNBOUND_ID = 900300004     # без привязки


def _ready(tmp_path, name="test_reject_rules_editor.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _setup_staff():
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")
    await db.add_staff(UNBOUND_ID, "reg_manager", SUPERADMIN_ID)


async def _create_rule(**overrides):
    fields = dict(
        name=None, city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
        action="reject", reject_text="Текст отказа", enabled=1, created_by=None,
    )
    fields.update(overrides)
    return await db.create_reject_rule(**fields)


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text=None, user_id=SUPERADMIN_ID):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.answer_markups = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=SUPERADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Задача 1: экран списка, capability, общий рубильник ─────────────────────────────────────

def test_render_rules_screen_empty_invites_to_use_preset(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_reject_rules.render_rules_screen(SUPERADMIN_ID))
    assert "Правил пока нет" in text
    assert "arr_new" in _cbs(kb)


def test_render_rules_screen_shows_autodescription(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_create_rule(city="msk", reject_text="Причина"))
    text, kb = _run(admin_reject_rules.render_rules_screen(SUPERADMIN_ID))
    assert "→ отказ" in text
    assert any(cb and cb.startswith("arr_v:") for cb in _cbs(kb))


def test_render_rules_screen_marks_paused_rule_with_explanation(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    rule_id = _run(_create_rule(reject_text="Причина"))
    _run(db.update_reject_rule(rule_id, paused_reason="📖 Курс"))
    text, _kb = _run(admin_reject_rules.render_rules_screen(SUPERADMIN_ID))
    assert "⚠️" in text
    assert "Курс" in text
    assert "выключен" in text


def test_render_rules_screen_hides_other_citys_rule_from_bound_manager(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    _run(db.set_setting("reject_rules_enabled", "on"))
    msk_id = _run(_create_rule(city="msk", reject_text="msk"))
    spb_id = _run(_create_rule(city="spb", reject_text="spb"))
    text, kb = _run(admin_reject_rules.render_rules_screen(BOUND_MSK_ID))
    cbs = _cbs(kb)
    assert f"arr_v:{msk_id}" in cbs
    assert f"arr_v:{spb_id}" not in cbs


def test_arr_toggle_enabled_on_foreign_city_rule_alerts_and_does_not_change_db(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    spb_id = _run(_create_rule(city="spb", reject_text="spb", enabled=1))
    callback = _FakeCallback(f"arr_t:{spb_id}", user_id=BOUND_MSK_ID)
    _run(admin_reject_rules.arr_toggle_enabled(callback))
    assert callback.answers and callback.answers[0][1] is True
    row = _run(db.get_reject_rule(spb_id))
    assert row["enabled"] == 1


def test_arr_toggle_enabled_success_flips_state(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(reject_text="x", enabled=1))
    callback = _FakeCallback(f"arr_t:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_toggle_enabled(callback))
    row = _run(db.get_reject_rule(rule_id))
    assert row["enabled"] == 0
    assert callback.answers and callback.answers[0][1] is True


def test_arr_master_toggles_only_global_setting_not_rule_enabled(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(reject_text="x", enabled=1))
    # Дефолт `reject_rules_enabled` — "off" (D-15). Первый тап включает, второй — выключает.
    callback = _FakeCallback("arr_master", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_master_toggle(callback))
    assert _run(get_setting_typed("reject_rules_enabled")) is True
    row = _run(db.get_reject_rule(rule_id))
    assert row["enabled"] == 1  # состояние правила не тронуто (D-15)

    callback2 = _FakeCallback("arr_master", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_master_toggle(callback2))
    assert _run(get_setting_typed("reject_rules_enabled")) is False
    row2 = _run(db.get_reject_rule(rule_id))
    assert row2["enabled"] == 1


def test_required_capability_covers_every_new_callback_prefix():
    samples = [
        "admin_reject_rules", "arr_p:0", "arr_v:1", "arr_t:1", "arr_master", "arr_act:1",
        "arr_city:1", "arr_citypick:1:msk", "arr_track:1:full", "arr_name:1", "arr_text:1",
        "arr_copy:1", "arr_copygo:1:msk", "arr_new", "arr_preset:0", "arr_d:1", "arr_dgo:1",
        "arr_noop",
    ]
    for cb in samples:
        assert required_capability(callback_data=cb) == "settings", cb
    assert required_capability(raw_state="RejectRuleEdit:name") == "settings"
    assert required_capability(raw_state="RejectRuleEdit:text") == "settings"


def test_admin_reject_rules_wired_into_apps_section():
    from handlers import admin_sections as sec
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    assert any(row[0] == "screen" and row[1] == "admin_reject_rules" for row in apps_rows)


def test_arr_new_screen_lists_presets_and_own_rule_option(tmp_path):
    _ready(tmp_path)
    text, kb = _run(admin_reject_rules.render_new_rule_screen(SUPERADMIN_ID))
    assert "Новое правило" in text
    cbs = _cbs(kb)
    assert "arr_preset:0" in cbs
    assert any(cb and cb.startswith("arr_preset:") for cb in cbs)


def test_arr_preset_creates_disabled_rule_from_preset(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("arr_preset:0", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_preset_pick(callback))
    rules = _run(db.list_reject_rules())
    assert len(rules) == 1
    assert rules[0]["enabled"] == 0


def test_arr_preset_custom_creates_blank_rule(tmp_path):
    _ready(tmp_path)
    presets = _run(admin_reject_rules.RULE_PRESETS())
    callback = _FakeCallback(f"arr_preset:{len(presets)}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_preset_pick(callback))
    rules = _run(db.list_reject_rules())
    assert len(rules) == 1
    assert rules[0]["name"] is None
    assert json.loads(rules[0]["conditions"]) == []
