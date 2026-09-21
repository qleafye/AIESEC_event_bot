"""Phase 31 Plan 08 (D-09/D-10/D-12/D-15/D-16/D-21): «🚫 Правила автоотказа» в чат-боте — экран
списка, карточка правила, общий рубильник, копирование в другой город, удаление с подтверждением.

pytest-asyncio недоступна в этом окружении — async через `asyncio.run()` (форма
`tests/test_faq_260906.py`); БД — `tmp_path`, Fake-объекты callback/message — та же форма, что
`tests/test_faq_260906.py::_FakeCallback/_FakeMessage`.

Три пласта сторожей — по задачам плана:
- Задача 1: экран списка, capability-гейт, общий рубильник, стейл-гард чужого правила, заготовки.
- Задача 2: карточка правила по макету D-09, единственная дверь записи, право по городу, треки,
  FSM имени/текста отказа.
- Задача 3 (этот срез добавляет): копирование правила в другой город (выключенным), удаление с
  подтверждением, которое называет последствия и не трогает журнал автоотказов.
"""
from __future__ import annotations

import asyncio
import inspect
import json

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_reject_rules
from handlers.admin_caps import required_capability
from handlers.states import RejectRuleEdit
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


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


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


# ── Задача 2: карточка правила по макету D-09, единственная дверь записи, право по городу ───

def test_render_rule_card_matches_layout_and_has_no_raw_codes(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk",
        conditions=json.dumps([
            [
                {"step": "course", "op": "in", "values": ["1", "2"]},
                {"step": "birth_date", "op": "age_on_forum_lt", "values": [21]},
            ],
            [{"step": "resume", "op": "no_file", "values": []}],
        ]),
        reject_text="Причина отказа",
    ))
    text, _kb = _run(admin_reject_rules.render_rule_card(SUPERADMIN_ID, rule_id))
    assert "Группа 1 (все условия сразу):" in text
    assert "— ИЛИ —" in text
    assert "Группа 2:" in text
    for forbidden in ("course", "birth_date", "msk", "full", "party_overnight"):
        assert forbidden not in text


def test_admin_reject_rules_module_never_calls_update_reject_rule_directly():
    """T-31-08 (acceptance): единственная дверь записи — save_rule/delete_rule; прямого
    точечного UPDATE строки базы в этом файле быть не должно."""
    source = inspect.getsource(admin_reject_rules)
    assert "update_reject_rule" not in source


def test_admin_reject_rules_module_calls_can_edit_city_in_every_mutating_handler():
    """Сторож T-31-08-01: право по городу перепроверяется в каждом мутирующем хендлере —
    нижняя граница числа вызовов `can_edit_city` внутри модуля."""
    source = inspect.getsource(admin_reject_rules)
    assert source.count("can_edit_city(") >= 10


def test_reject_rule_edit_states_exist():
    assert hasattr(RejectRuleEdit, "name")
    assert hasattr(RejectRuleEdit, "text")


def test_arr_toggle_enabled_now_reopens_card(tmp_path):
    """Задача 2: как только карточка появилась, arr_t редрейит её (не список)."""
    _ready(tmp_path)
    rule_id = _run(_create_rule(reject_text="x", enabled=1))
    callback = _FakeCallback(f"arr_t:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_toggle_enabled(callback))
    assert "Правило автоотказа" in callback.message.text_edited


def test_arr_act_toggle_changes_only_action(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(action="reject", reject_text="x", city="msk"))
    callback = _FakeCallback(f"arr_act:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_act_toggle(callback))
    row = _run(db.get_reject_rule(rule_id))
    assert row["action"] == "flag"
    assert row["city"] == "msk"
    assert row["reject_text"] == "x"


def test_city_assign_screen_does_not_offer_all_cities_to_bound_manager(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _run(_create_rule(city="msk", reject_text="x"))
    screen = _run(admin_reject_rules.render_city_assign_screen(BOUND_MSK_ID, rule_id))
    assert screen is not None
    _text, kb = screen
    cbs = _cbs(kb)
    assert f"arr_citypick:{rule_id}:*" not in cbs


def test_city_assign_screen_offers_all_cities_to_superadmin(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", reject_text="x"))
    _text, kb = _run(admin_reject_rules.render_city_assign_screen(SUPERADMIN_ID, rule_id))
    cbs = _cbs(kb)
    assert f"arr_citypick:{rule_id}:*" in cbs


def test_arr_track_toggle_rejects_unchecking_last_track(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(tracks=json.dumps(["full"]), reject_text="x"))
    callback = _FakeCallback(f"arr_track:{rule_id}:full", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_track_toggle(callback))
    assert callback.answers and callback.answers[0][1] is True
    row = _run(db.get_reject_rule(rule_id))
    assert json.loads(row["tracks"]) == ["full"]


def test_arr_track_toggle_adds_and_removes_party_pair_together(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(tracks=json.dumps(["full"]), reject_text="x"))
    callback = _FakeCallback(f"arr_track:{rule_id}:party", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_track_toggle(callback))
    row = _run(db.get_reject_rule(rule_id))
    tracks = set(json.loads(row["tracks"]))
    assert {"party_overnight", "party_noovernight"} <= tracks


def test_arr_text_step_saves_via_save_rule_not_update_reject_rule_directly(tmp_path, monkeypatch):
    _ready(tmp_path)
    rule_id = _run(_create_rule(reject_text="старый", city="msk"))
    calls = []

    async def _fake_save_rule(admin_id, rid, **fields):
        calls.append((admin_id, rid, fields))
        return rid, None

    monkeypatch.setattr(admin_reject_rules, "save_rule", _fake_save_rule)

    def _boom(*args, **kwargs):
        raise AssertionError("update_reject_rule должен вызываться только внутри save_rule")

    monkeypatch.setattr(db, "update_reject_rule", _boom)

    state = _new_state(SUPERADMIN_ID)
    _run(state.update_data(rre_rule_id=rule_id))
    _run(state.set_state(RejectRuleEdit.text))
    message = _FakeMessage(text="Строка один;Строка два", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_text_step(message, state))

    assert len(calls) == 1
    assert calls[0][1] == rule_id
    assert calls[0][2]["reject_text"] == "Строка один\nСтрока два"


# ── Задача 3: копирование в другой город, удаление с подтверждением ─────────────────────────

def test_arr_copy_go_creates_disabled_copy_with_new_city_original_unchanged(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(
        city="msk", action="reject", reject_text="Причина",
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
        enabled=1,
    ))
    callback = _FakeCallback(f"arr_copygo:{rule_id}:spb", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_copy_go(callback))

    original = _run(db.get_reject_rule(rule_id))
    assert original["city"] == "msk"
    assert original["enabled"] == 1

    all_rules = _run(db.list_reject_rules())
    copy_row = next(r for r in all_rules if r["id"] != rule_id)
    assert copy_row["city"] == "spb"
    assert copy_row["enabled"] == 0
    assert copy_row["action"] == original["action"]
    assert copy_row["conditions"] == original["conditions"]


def test_copy_screen_excludes_city_without_right(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _run(_create_rule(city="msk", reject_text="x"))
    screen = _run(admin_reject_rules.render_copy_screen(BOUND_MSK_ID, rule_id))
    _text, kb = screen
    cbs = _cbs(kb)
    assert not any(cb and cb.startswith("arr_copygo:") and ":spb" in cb for cb in cbs)


def test_copy_go_rejects_city_without_right(tmp_path):
    _ready(tmp_path)
    _run(_setup_staff())
    rule_id = _run(_create_rule(city="msk", reject_text="x"))
    callback = _FakeCallback(f"arr_copygo:{rule_id}:spb", user_id=BOUND_MSK_ID)
    _run(admin_reject_rules.arr_copy_go(callback))
    assert callback.answers and callback.answers[0][1] is True
    all_rules = _run(db.list_reject_rules())
    assert len(all_rules) == 1


def test_rule_reject_count_no_longer_scans_journal_in_python():
    """WR-01: счётчик обязан считать в SQL, а не вычитывать журнал целиком (был `limit=100000`)
    в Python-цикле — сторож ловит регрессию к старой O(journal size) реализации."""
    source = inspect.getsource(admin_reject_rules._rule_reject_count)
    assert "100000" not in source
    assert "count_auto_reject_log_for_rule" in source


def test_delete_confirm_shows_autodescription_and_disable_alternative(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", reject_text="Причина", enabled=1))
    callback = _FakeCallback(f"arr_d:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_delete_confirm(callback))
    text = callback.message.text_edited
    assert "выключить" in text
    assert "→ отказ" in text


def test_delete_go_does_not_touch_journal_or_delegate_status(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", reject_text="Причина"))
    telegram_id = 900300099
    _run(db.add_user({
        "telegram_id": telegram_id, "full_name": "Delegate", "registration_date": "2026-01-01 00:00:00",
        "event_city": "msk",
    }))
    _run(db.set_user_status(telegram_id, "rejected"))
    _run(db.upsert_auto_reject_log(
        telegram_id, json.dumps([rule_id]), json.dumps(["Причина"]), "2026-01-01 00:00:00",
    ))

    callback = _FakeCallback(f"arr_d:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_delete_confirm(callback))
    assert "заявок: 1" in callback.message.text_edited

    go_callback = _FakeCallback(f"arr_dgo:{rule_id}", user_id=SUPERADMIN_ID)
    _run(admin_reject_rules.arr_delete_go(go_callback))

    assert _run(db.get_reject_rule(rule_id)) is None
    journal_rows = _run(db.list_auto_reject_log(city_scope=None))
    assert len(journal_rows) == 1
    user = _run(db.get_user(telegram_id))
    assert user["status"] == "rejected"
