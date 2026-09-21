"""Phase 31 Plan 11 (D-18/D-19/D-20/D-29): чат-бот журнал «🤖 Автоотказы» — экран, возврат на
модерацию с уведомлением делегата, выгрузка файлом; плюс сторож «нет кнопки применения правил
к очереди» и три orchestrator-находки волны 31-11 (человеческое имя правила, стейл-«живые»
записи, размер модулей).

pytest-asyncio недоступна — async через `asyncio.run()` (форма `tests/test_reject_rules_
editor.py`); Fake-объекты callback/message/bot — та же форма, что `tests/test_broadcast_quiet_
hours_260911.py::FakeBot`.
"""
from __future__ import annotations

import asyncio
import json

from config import config
from database import db
from handlers import admin_reject_journal as j
from handlers import admin_reject_rules
from handlers.admin_caps import required_capability
from services.timeutil import msk_now

SUPERADMIN_ID = 900400001
BOUND_MSK_ID = 900400002
BOUND_SPB_ID = 900400003


def _ready(tmp_path, name="test_reject_rules_journal_ui.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


async def _bind_staff():
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _set_field(tid, field, value):
    async with db._connect() as conn:
        await conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, tid))
        await conn.commit()


async def _get_field(tid, field):
    async with db._connect() as conn:
        async with conn.execute(f"SELECT {field} FROM users WHERE telegram_id = ?", (tid,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


def _seed_user(tid, *, event_city=None, status="rejected"):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
    }))
    _run(_set_field(tid, "status", status))


async def _create_rule(**overrides):
    fields = dict(
        name=None, city=None, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "resume", "op": "no_file", "values": []}]]),
        action="reject", reject_text="Причина отказа", enabled=1, created_by=None,
    )
    fields.update(overrides)
    return await db.create_reject_rule(**fields)


async def _trigger(tid, rule_id, *, texts=("Причина отказа.",)):
    from services.reject_journal import record_auto_reject
    return await record_auto_reject(tid, [rule_id], list(texts))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeBot:
    def __init__(self, fail_for: set | None = None):
        self.sent = []
        self._fail_for = fail_for or set()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self._fail_for:
            raise RuntimeError("бот заблокирован делегатом")
        self.sent.append((chat_id, text))
        return None


class _FakeMessage:
    def __init__(self, user_id=SUPERADMIN_ID, bot=None):
        self.from_user = _FakeUser(user_id)
        self.text_edited = None
        self.edit_markup = None
        self.documents = []
        self.bot = bot or _FakeBot()

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))


class _FakeCallback:
    def __init__(self, data, user_id=SUPERADMIN_ID, bot=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.bot = bot or _FakeBot()
        self.message = _FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: экран журнала
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_render_journal_screen_shows_entries_and_matching_counter(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3001, event_city="msk")
    _run(_trigger(3001, rule_id))
    text, kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Всего: 1" in text
    assert "Delegate 3001" in text
    assert any(cb and cb.startswith("arj_back:") for cb in _cbs(kb))


def test_render_journal_screen_empty_explains_when_module_off(tmp_path):
    _ready(tmp_path)
    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Правила пока никого не отклонили" in text
    assert "выключен" in text


def test_render_journal_screen_empty_explains_when_module_on(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Правила пока никого не отклонили" in text
    assert "выключен" not in text


def test_render_journal_screen_pagination_at_twelve_records(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    for i in range(12):
        tid = 3100 + i
        _seed_user(tid, event_city="msk")
        _run(_trigger(tid, rule_id))
    text, kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Всего: 12" in text
    assert any(cb == "arj_p:0:10" for cb in _cbs(kb))

    text2, kb2 = _run(j.render_journal_screen(SUPERADMIN_ID, offset=10))
    assert "Всего: 12" in text2
    assert any(cb == "arj_p:0:0" for cb in _cbs(kb2))


def test_render_journal_screen_include_returned_toggles_count(tmp_path):
    _ready(tmp_path)
    from services.reject_journal import return_to_moderation
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3200, event_city="msk")
    entry_id = _run(_trigger(3200, rule_id))
    _run(return_to_moderation(SUPERADMIN_ID, entry_id))

    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Всего: 0" in text
    text_all, _kb2 = _run(j.render_journal_screen(SUPERADMIN_ID, include_returned=True))
    assert "Всего: 1" in text_all
    assert "возвращена на модерацию" in text_all


def test_render_journal_screen_city_manager_sees_only_own_city(tmp_path):
    _ready(tmp_path)
    _run(_bind_staff())
    rule_msk = _run(_create_rule(city="msk"))
    rule_spb = _run(_create_rule(city="spb"))
    _seed_user(3300, event_city="msk")
    _seed_user(3301, event_city="spb")
    _run(_trigger(3300, rule_msk))
    _run(_trigger(3301, rule_spb))

    text, _kb = _run(j.render_journal_screen(BOUND_MSK_ID))
    assert "Delegate 3300" in text
    assert "Delegate 3301" not in text


def test_required_capability_covers_every_arj_callback():
    samples = [
        "admin_reject_journal", "arj_p:0", "arj_p:1:20", "arj_all:1",
        "arj_back:1", "arj_backgo:1", "arj_csv", "appr_flag:1",
    ]
    for cb in samples:
        assert required_capability(callback_data=cb) == "moderate_reg", cb


def test_no_apply_to_queue_wording_on_journal_or_rules_screens(tmp_path):
    """D-19 (инцидент 06.09): ни экран журнала, ни экран правил не могут предложить применить
    правило/возврат КО ВСЕЙ очереди."""
    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3400, event_city="msk")
    _run(_trigger(3400, rule_id))

    forbidden = ("к очереди", "применить ко всем", "пересчитать заявки", "вернуть всех")
    _text_j, kb_j = _run(j.render_journal_screen(SUPERADMIN_ID))
    _text_r, kb_r = _run(admin_reject_rules.render_rules_screen(SUPERADMIN_ID))
    for texts in (_texts(kb_j), _texts(kb_r)):
        for btn_text in texts:
            lowered = (btn_text or "").lower()
            for phrase in forbidden:
                assert phrase not in lowered, (btn_text, phrase)


def test_rules_screen_has_entry_button_into_journal(tmp_path):
    _ready(tmp_path)
    _text, kb = _run(admin_reject_rules.render_rules_screen(SUPERADMIN_ID))
    assert "admin_reject_journal" in _cbs(kb)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: возврат на модерацию + выгрузка CSV
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_arj_back_confirm_names_rule_and_attempt_count(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", name="Младше 18"))
    _seed_user(3500, event_city="msk")
    entry_id = _run(_trigger(3500, rule_id))
    _run(_trigger(3500, rule_id))  # вторая попытка -> attempt_count == 2

    callback = _FakeCallback(f"arj_back:{entry_id}", user_id=SUPERADMIN_ID)
    _run(j.arj_back_confirm(callback))
    assert callback.message.text_edited is not None
    assert "Delegate 3500" in callback.message.text_edited
    assert "Младше 18" in callback.message.text_edited
    assert "попыток автоотказа: 2" in callback.message.text_edited
    assert f"arj_backgo:{entry_id}" in _cbs(callback.message.edit_markup)


def test_arj_backgo_success_sets_pending_and_sends_one_message(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3501, event_city="msk")
    entry_id = _run(_trigger(3501, rule_id))

    callback = _FakeCallback(f"arj_backgo:{entry_id}", user_id=SUPERADMIN_ID)
    _run(j.arj_back_go(callback))

    assert _run(_get_field(3501, "status")) == "pending"
    assert len(callback.bot.sent) == 1
    assert callback.bot.sent[0][0] == 3501
    assert callback.answers and callback.answers[-1][1] is False  # обычный toast, не alert


def test_arj_backgo_send_failure_does_not_roll_back_status(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3502, event_city="msk")
    entry_id = _run(_trigger(3502, rule_id))

    bot = _FakeBot(fail_for={3502})
    callback = _FakeCallback(f"arj_backgo:{entry_id}", user_id=SUPERADMIN_ID, bot=bot)
    _run(j.arj_back_go(callback))

    assert _run(_get_field(3502, "status")) == "pending"
    assert bot.sent == []


def test_arj_backgo_second_tap_shows_reason_and_sends_no_second_message(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3503, event_city="msk")
    entry_id = _run(_trigger(3503, rule_id))

    bot = _FakeBot()
    first = _FakeCallback(f"arj_backgo:{entry_id}", user_id=SUPERADMIN_ID, bot=bot)
    _run(j.arj_back_go(first))
    assert len(bot.sent) == 1

    second = _FakeCallback(f"arj_backgo:{entry_id}", user_id=SUPERADMIN_ID, bot=bot)
    _run(j.arj_back_go(second))
    assert len(bot.sent) == 1  # второго сообщения не было
    assert second.answers and second.answers[0][1] is True
    assert "уже" in (second.answers[0][0] or "").lower()


def test_arj_backgo_foreign_city_manager_rejected(tmp_path):
    _ready(tmp_path)
    _run(_bind_staff())
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3504, event_city="msk")
    entry_id = _run(_trigger(3504, rule_id))

    callback = _FakeCallback(f"arj_backgo:{entry_id}", user_id=BOUND_SPB_ID)
    _run(j.arj_back_go(callback))
    assert _run(_get_field(3504, "status")) == "rejected"
    assert callback.answers and callback.answers[0][1] is True


def test_arj_csv_empty_journal_alerts_without_document(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback("arj_csv", user_id=SUPERADMIN_ID)
    _run(j.arj_csv_export(callback))
    assert callback.message.documents == []
    assert callback.answers and callback.answers[0][1] is True
    assert "нечего" in (callback.answers[0][0] or "").lower()


def test_arj_csv_with_entries_sends_document_with_content(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3505, event_city="msk")
    _run(_trigger(3505, rule_id))

    callback = _FakeCallback("arj_csv", user_id=SUPERADMIN_ID)
    _run(j.arj_csv_export(callback))
    assert len(callback.message.documents) == 1
    document, _caption = callback.message.documents[0]
    assert len(document.data) > 0


def test_no_update_user_answers_or_set_user_status_calls_in_journal_handler():
    """T-31-11-03: единственная дверь мутации — `services.reject_journal.return_to_moderation`,
    не прямой UPDATE из хендлера."""
    import inspect
    source = inspect.getsource(j)
    assert "update_user_answers" not in source
    assert "set_user_status" not in source


# ══════════════════════════════════════════════════════════════════════════════════════════
# Orchestrator finding 1: человеческое имя правила
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_journal_line_uses_live_rule_name_when_rule_still_exists(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", name="Младше 18 на дату форума"))
    _seed_user(3600, event_city="msk")
    _run(_trigger(3600, rule_id))
    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Младше 18 на дату форума" in text


def test_journal_line_uses_autodescription_when_rule_has_no_own_name(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk", name=None))
    _seed_user(3601, event_city="msk")
    _run(_trigger(3601, rule_id))
    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "→ отказ" in text  # rule_summary всегда заканчивается стрелкой к действию


def test_journal_line_falls_back_to_reject_text_snapshot_when_rule_deleted(tmp_path):
    _ready(tmp_path)
    from services.reject_rules import delete_rule
    rule_id = _run(_create_rule(city="msk", name="Временное правило", reject_text="Слишком юн."))
    _seed_user(3602, event_city="msk")
    _run(_trigger(3602, rule_id, texts=("Слишком юн.",)))
    _run(delete_rule(SUPERADMIN_ID, rule_id))
    text, _kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert "Временное правило" not in text
    assert "Слишком юн" in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Orchestrator finding 2: стейл-«живые» записи (делегат сам поправил анкету / решил человек)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_stale_live_entry_has_no_return_button_and_explains_itself(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3700, event_city="msk")
    entry_id = _run(_trigger(3700, rule_id))
    _run(_set_field(3700, "status", "pending"))  # делегат сам поправил анкету (план 31-06)

    text, kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert f"arj_back:{entry_id}" not in _cbs(kb)
    assert "поправил анкету" in text


def test_stale_live_entry_human_decided_explains_itself(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3701, event_city="msk")
    entry_id = _run(_trigger(3701, rule_id))
    _run(_set_field(3701, "status", "approved"))  # решение принял человек

    text, kb = _run(j.render_journal_screen(SUPERADMIN_ID))
    assert f"arj_back:{entry_id}" not in _cbs(kb)
    assert "человек" in text


def test_arj_back_confirm_on_stale_entry_gives_friendly_alert_not_crash(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3702, event_city="msk")
    entry_id = _run(_trigger(3702, rule_id))
    _run(_set_field(3702, "status", "pending"))

    callback = _FakeCallback(f"arj_back:{entry_id}", user_id=SUPERADMIN_ID)
    _run(j.arj_back_confirm(callback))
    assert callback.answers and callback.answers[0][1] is True
    assert "уже" in (callback.answers[0][0] or "").lower()


def test_arj_backgo_on_stale_entry_gives_friendly_alert_and_no_send(tmp_path):
    _ready(tmp_path)
    rule_id = _run(_create_rule(city="msk"))
    _seed_user(3703, event_city="msk")
    entry_id = _run(_trigger(3703, rule_id))
    _run(_set_field(3703, "status", "pending"))

    callback = _FakeCallback(f"arj_backgo:{entry_id}", user_id=SUPERADMIN_ID)
    _run(j.arj_back_go(callback))
    assert callback.bot.sent == []
    assert callback.answers and callback.answers[0][1] is True
    assert _run(_get_field(3703, "status")) == "pending"  # не тронуто повторно


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: чип «только помеченные правилами» в очереди заявок бота
# ══════════════════════════════════════════════════════════════════════════════════════════

async def _seed_pending(uid, *, flagged_rule_id=None, city=None):
    row = {
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "username": "d" + str(uid),
        "registration_date": "2026-09-01 10:00:00",
        "event_city": city,
        "participant_type": "full",
        "course": "1",
    }
    await db.add_user(row)
    await db.set_user_status(uid, "pending")
    if flagged_rule_id is not None:
        await db.update_user_answers(
            uid, {"flagged_rule_ids": json.dumps([flagged_rule_id])},
            allowed_columns=["flagged_rule_ids"],
        )


def test_flag_chip_hidden_when_module_off(tmp_path):
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeMessage, _new_state

    _ready(tmp_path)
    rule_id = _run(_create_rule(city=None))
    _run(_seed_pending(3800, flagged_rule_id=rule_id))
    state = _new_state(3800)
    target = FakeMessage()
    _run(admin_moderation._show_current_card(target, state))
    cbs = [b.callback_data for row in target.markup.inline_keyboard for b in row]
    assert not any(cb and cb.startswith("appr_flag:") for cb in cbs)


def test_flag_chip_shown_when_module_on(tmp_path):
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeMessage, _new_state

    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    rule_id = _run(_create_rule(city=None))
    _run(_seed_pending(3801, flagged_rule_id=rule_id))
    state = _new_state(3801)
    target = FakeMessage()
    _run(admin_moderation._show_current_card(target, state))
    cbs = [b.callback_data for row in target.markup.inline_keyboard for b in row]
    assert any(cb and cb.startswith("appr_flag:") for cb in cbs)


def test_appr_flag_toggle_filters_queue_to_flagged_only(tmp_path):
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeCallback, _new_state

    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    rule_id = _run(_create_rule(city=None))
    _run(_seed_pending(3802, flagged_rule_id=rule_id))  # помечен
    _run(_seed_pending(3803))  # обычный

    state = _new_state(3802)
    callback = FakeCallback("appr_flag:1", user_id=3802)
    _run(admin_moderation.appr_flag_toggle(callback, state))

    stored = _run(state.get_data())
    assert stored.get("appr_flagged_only") is True
    assert "Delegate 3802" in callback.message.text
    assert "Delegate 3803" not in callback.message.text


def test_empty_flagged_queue_explains_filter_with_the_word(tmp_path):
    from handlers import admin_moderation
    from tests.test_city_admin_phase72 import FakeMessage, _new_state

    _ready(tmp_path)
    _run(db.set_setting("reject_rules_enabled", "on"))
    _run(_seed_pending(3804))  # ни одного помеченного

    state = _new_state(3804)
    _run(state.update_data(appr_flagged_only=True))
    target = FakeMessage()
    _run(admin_moderation._show_current_card(target, state))
    assert "фильтр" in target.text.lower()


def test_required_capability_appr_flag():
    assert required_capability(callback_data="appr_flag:1") == "moderate_reg"
