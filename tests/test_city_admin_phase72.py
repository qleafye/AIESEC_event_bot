"""Phase 07.2 Plan 02 (per-city admin panels, CITY-02) tests: the per-admin city switcher
(`admin_keyboard_for`, `admin_city_switch`, `admin_city_pick`) and both moderation queues
(«Заявки» / «Чеки») scoped by the selected city, including the safe mass-approve
confirmation text.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_admin_phase71.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_core  # Phase 13 (13-04): _admin_city_view moved here
from handlers.admin_caps import ANY_CAPABILITY, required_capability
import cities


ADMIN_ID = 920101
NON_ADMIN_ID = 920102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_admin72.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Stand-in for the aiogram Message the callback/target carries — captures both
    edit_text (callback re-render) and answer (new-message target) calls."""

    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.bot = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _seed_city(telegram_id, event_city, status="pending", payment_status=None, full_name=None):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": full_name or f"User {telegram_id}",
        "registration_date": f"2026-01-01 09:{telegram_id:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))
    if payment_status is not None:
        asyncio.run(db.update_payment_status(telegram_id, payment_status))


# ── Task 1: city switcher — admin_keyboard_for() + admin_city_switch / admin_city_pick ──

def test_admin_keyboard_for_module_off_equals_build_admin_keyboard(tmp_path):
    _admin_ready(tmp_path)
    scoped = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    # 08-05 (D-15): build_admin_keyboard is now async + capability-filtered; ADMIN_ID is a
    # bootstrap admin (ALL_CAPABILITIES) so the row set is unchanged from before 08-05.
    plain = asyncio.run(admin_mod.build_admin_keyboard(ADMIN_ID))
    assert len(scoped.inline_keyboard) == len(plain.inline_keyboard)
    for row_a, row_b in zip(scoped.inline_keyboard, plain.inline_keyboard):
        assert [b.text for b in row_a] == [b.text for b in row_b]
        assert [b.callback_data for b in row_a] == [b.callback_data for b in row_b]


def test_admin_keyboard_for_module_on_has_city_header_row(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    kb = asyncio.run(admin_mod.admin_keyboard_for(ADMIN_ID))
    plain = asyncio.run(admin_mod.build_admin_keyboard(ADMIN_ID))
    assert len(kb.inline_keyboard) == len(plain.inline_keyboard) + 1
    header_row = kb.inline_keyboard[0]
    assert len(header_row) == 1
    assert header_row[0].callback_data == "admin_city_switch"
    assert "🏙 Город:" in header_row[0].text
    # remaining rows unchanged
    assert [ [b.callback_data for b in row] for row in kb.inline_keyboard[1:] ] == \
           [ [b.callback_data for b in row] for row in plain.inline_keyboard ]


def test_admin_city_switch_screen_lists_all_cities_and_is_capability_guarded(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    # Phase 8 / D-01: the old per-handler `config.ADMIN_IDS` check (and the direct-call
    # rejection this test used to exercise on a non-admin FakeCallback) is gone (08-04,
    # one-shot migration, D-03) -- CapabilityMiddleware is now the ONLY enforcement point,
    # and it only wraps events dispatched through the real router, not direct handler calls.
    # The structural guarantee survives with a new carrier: the handler stays registered,
    # and its callback_data resolves to a real capability.
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "admin_city_switch" in names
    # Phase 09.3 (CITY-08, Pitfall 2): widened moderate_reg -> ANY_CAPABILITY -- the header is
    # now the sole context for settings/menu editing too, not just moderation queues.
    assert required_capability(callback_data="admin_city_switch") == ANY_CAPABILITY

    cb2 = FakeCallback("admin_city_switch")
    asyncio.run(admin_mod.admin_city_switch(cb2))
    assert cb2.message.edit_calls == 1
    flat = _flat_callback_data(cb2.message.markup)
    for code in cities.city_codes():
        assert f"admin_city_pick:{code}" in flat
    assert "admin_menu" in flat


def test_admin_city_pick_valid_code_sets_and_rerenders(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_pick:spb")
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}")) == "spb"
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) == "spb"
    assert cb.message.edit_calls == 1
    assert "Панель администратора" in cb.message.text
    header_row = cb.message.markup.inline_keyboard[0]
    assert header_row[0].callback_data == "admin_city_switch"


def test_admin_city_pick_unknown_code_rejected_no_write(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    cb = FakeCallback("admin_city_pick:__evil__")
    asyncio.run(admin_mod.admin_city_pick(cb))
    assert cb.answers[-1] == ("Неизвестный город", True)
    assert cb.message.edit_calls == 0
    assert asyncio.run(db.get_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}")) is None


def test_admin_city_pick_is_capability_guarded(tmp_path):
    # Phase 8 / D-01: see test_admin_city_switch_screen_lists_all_cities_and_is_capability_
    # guarded above.
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    names = {h.callback.__name__ for h in admin_mod.router.callback_query.handlers}
    assert "admin_city_pick" in names
    # Phase 09.3 (CITY-08, Pitfall 2): widened moderate_reg -> ANY_CAPABILITY (see the sibling
    # test above for admin_city_switch).
    assert required_capability(callback_data="admin_city_pick:spb") == ANY_CAPABILITY


# ── Task 2: applications queue city-scoped + safe mass-approve ──────────────────────────

def _seed_pending_three_cities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None)
    _seed_city(2, "msk")
    _seed_city(3, "spb")
    _seed_city(4, "spb")
    _seed_city(5, "tyumen")


def test_show_current_card_city_scoped_spb(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "1/2" in target.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.text


def test_show_current_card_city_scoped_msk_includes_null(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "1/2" in target.text  # NULL + msk


def test_render_application_card_header_byte_identical_without_city_label():
    user = {"telegram_id": 1, "full_name": "X"}
    text = admin_mod._render_application_card(user, 1, 1)
    assert text.startswith("📋 <b>Заявка 1/1</b>")
    assert "🏙" not in text.split("\n")[0]


def test_render_application_card_header_has_city_when_label_given():
    user = {"telegram_id": 1, "full_name": "X"}
    text = admin_mod._render_application_card(user, 1, 1, city_label_text="СПб")
    first_line = text.split("\n")[0]
    assert first_line == "📋 <b>Заявка 1/1</b> · 🏙 СПб"


def test_show_current_card_module_off_header_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert target.text.startswith("📋 <b>Заявка 1/1</b>\n")
    assert "🏙" not in target.text.split("\n")[0]


def test_show_current_card_empty_queue_names_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, "msk", status="approved")  # no pending rows at all
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert "Заявок нет" in target.answers_sent[-1]
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.answers_sent[-1]


def test_show_current_card_empty_queue_module_off_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    assert target.answers_sent[-1] == "✅ Заявок нет."


def test_show_current_card_empty_queue_escapes_city_label(tmp_path):
    """CR-01: метка города редактируется из админки, а сообщение уходит с parse_mode=HTML по
    умолчанию — без экранирования экран пустой очереди перестал бы открываться вообще."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__spb", "<script>alert(1)</script>"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_card(target, state))
    sent = target.answers_sent[-1]
    assert "<script>alert(1)</script>" not in sent
    assert "&lt;script&gt;" in sent


def test_show_current_receipt_card_empty_queue_escapes_city_label(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__spb", "<script>alert(1)</script>"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    sent = target.answers_sent[-1]
    assert "<script>alert(1)</script>" not in sent
    assert "&lt;script&gt;" in sent


def test_appr_all_confirm_escapes_city_label(tmp_path):
    """CR-01 на самом опасном экране: подтверждение необратимого массового одобрения."""
    _seed_pending_three_cities(tmp_path)
    asyncio.run(db.set_setting("city_label__spb", "Питер <осн>"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "Питер <осн>" not in cb.message.text
    assert "Питер &lt;осн&gt;" in cb.message.text


def test_appr_all_confirm_names_city_and_count(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "2" in cb.message.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in cb.message.text


def test_appr_all_confirm_module_off_equals_todays_literal(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert cb.message.text == "Одобрить все 1 заявок?"


def test_appr_all_confirm_binds_the_city_into_the_callback_data(tmp_path):
    """CR-02: кнопка «Да» обязана нести код города, который назван в тексте диалога."""
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "appr_all_yes:spb" in _flat_callback_data(cb.message.markup)


def test_appr_all_confirm_module_off_binds_empty_city(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    assert "appr_all_yes:" in _flat_callback_data(cb.message.markup)


def test_appr_all_yes_scoped_does_not_touch_other_cities(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all_yes:spb")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))

    async def check():
        for tid in (1, 2, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending"
        for tid in (3, 4):
            user = await db.get_user(tid)
            assert user["status"] == "approved"

    asyncio.run(check())


def test_appr_all_yes_refuses_when_admin_city_changed_after_the_dialog(tmp_path):
    """CR-02, главный сценарий: диалог показан по Питеру, админ переключился на Москву в
    другом сообщении и вернулся к старой кнопке. Одобрять НИЧЕГО нельзя."""
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_confirm(cb, state))
    # админ переключил город уже после показа диалога
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    stale = FakeCallback("appr_all_yes:spb")
    asyncio.run(admin_mod.appr_all_yes(stale, state))
    assert stale.answers[0][1] is True
    assert "изменил" in stale.answers[0][0]

    async def check():
        for tid in (1, 2, 3, 4, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


def test_appr_all_yes_bare_callback_refuses_when_module_on(tmp_path):
    """CR-02: подтверждение старого формата (без кода города) при включённом модуле не
    имеет права падать обратно на «текущий выбранный город» — оно отвергается."""
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all_yes")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))
    assert cb.answers[0][1] is True

    async def check():
        for tid in (1, 2, 3, 4, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


def test_appr_all_yes_refuses_forged_city_code(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_all_yes:__evil__")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_all_yes(cb, state))
    assert cb.answers[0][1] is True

    async def check():
        for tid in (1, 2, 3, 4, 5):
            user = await db.get_user(tid)
            assert user["status"] == "pending", tid

    asyncio.run(check())


# ── WR-03: действия на КАРТОЧКЕ тоже обязаны перепроверять город ────────────────────────
#
# Очередь сузили, но `appr_approve:{tid}` / `appr_reject:{tid}` / `rcpt_confirm:{uid}` /
# `rcpt_reject:{uid}` адресуют строку по telegram_id из callback-data. Карточки остаются в
# истории чата, кнопки не истекают: карточка, отрисованная для города A и нажатая после
# переключения на город B, выполняла действие ВНЕ текущего скоупа. Действие точечное, поэтому
# достаточно проверки — поток карточек не переделываем.

def test_card_out_of_scope_detects_a_foreign_city(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 2)) is True   # msk
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 3)) is False  # spb


def test_card_out_of_scope_puts_null_city_rows_in_the_default_city(tmp_path):
    """Заявка без города читается как город по умолчанию — тот же резолвер, что и в SQL
    очереди, иначе строка оказалась бы «нигде» и стала неодобряемой."""
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.default_city_code()))
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 1)) is False
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 1)) is True


def test_card_out_of_scope_is_false_with_the_module_off(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "msk"))
    assert asyncio.run(admin_mod._card_out_of_scope(ADMIN_ID, 1)) is False


def test_appr_approve_refuses_a_card_from_another_city(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_approve:2")  # москвич, карточка из старого экрана
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_approve(cb, state))
    assert cb.answers[-1][1] is True
    assert "другого города" in cb.answers[-1][0]
    assert asyncio.run(db.get_user(2))["status"] == "pending"


def test_appr_approve_still_works_inside_the_scope(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_approve:3")
    cb.bot = None
    state = _new_state(ADMIN_ID)
    try:
        asyncio.run(admin_mod.appr_approve(cb, state))
    except Exception:
        pass  # приветствие уходит через bot=None — статус к этому моменту уже переключён
    assert asyncio.run(db.get_user(3))["status"] == "approved"


def test_appr_reject_start_refuses_a_card_from_another_city(tmp_path):
    _seed_pending_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("appr_reject:2")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.appr_reject_start(cb, state))
    assert cb.answers[-1][1] is True
    assert cb.message.answers_sent == []  # причину даже не спрашиваем
    assert asyncio.run(state.get_data()).get("appr_reject_id") is None


def test_rcpt_confirm_refuses_a_card_from_another_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(2, "msk", status="approved", payment_status="receipt_sent")
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("rcpt_confirm:2")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.rcpt_confirm(cb, state))
    assert cb.answers[-1][1] is True
    assert asyncio.run(db.get_user(2))["payment_status"] == "receipt_sent"


def test_rcpt_reject_start_refuses_a_card_from_another_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(2, "msk", status="approved", payment_status="receipt_sent")
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("rcpt_reject:2")
    state = _new_state(ADMIN_ID)
    asyncio.run(admin_mod.rcpt_reject_start(cb, state))
    assert cb.answers[-1][1] is True
    assert cb.message.answers_sent == []
    assert asyncio.run(state.get_data()).get("rcpt_reject_uid") is None


# ── Task 3: receipts queue city-scoped, same resolver as applications ───────────────────

def _seed_receipts_three_cities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None, status="approved", payment_status="receipt_sent")
    _seed_city(2, "msk", status="approved", payment_status="receipt_sent")
    _seed_city(3, "spb", status="approved", payment_status="receipt_sent")


def test_show_current_receipt_card_city_scoped_spb(tmp_path):
    _seed_receipts_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert "1/1" in target.text
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.text


def test_show_current_receipt_card_city_scoped_msk_includes_null(tmp_path):
    _seed_receipts_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert "1/2" in target.text


def test_show_current_receipt_card_module_off_header_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb", status="approved", payment_status="receipt_sent")
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert target.text.startswith("🧾 <b>Чек 1/1</b>\n")
    assert "🏙" not in target.text.split("\n")[0]


def test_show_current_receipt_card_empty_queue_names_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, "msk", status="approved")  # no receipt-pending rows
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert "Чеков на проверке нет" in target.answers_sent[-1]
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in target.answers_sent[-1]


def test_show_current_receipt_card_empty_queue_module_off_byte_identical(tmp_path):
    _admin_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    target = FakeMessage()
    asyncio.run(admin_mod._show_current_receipt_card(target, state))
    assert target.answers_sent[-1] == "✅ Чеков на проверке нет."


def test_both_moderation_queues_use_the_same_city_resolver():
    src_apps = inspect.getsource(admin_mod._show_current_card)
    src_receipts = inspect.getsource(admin_mod._show_current_receipt_card)
    assert "_admin_city_view" in src_apps
    assert "_admin_city_view" in src_receipts


# ── WR-05: город резолвится РОВНО ОДИН РАЗ на отрисовку ─────────────────────────────────
#
# `_admin_city_scope` и `_admin_city_label` — две независимые корутины, каждая заново читает
# `admin_city__{id}` (cities_module_on() + get_setting = ещё два коннекта к SQLite). aiogram
# обрабатывает апдейты конкурентно, поэтому между двумя await настройка может смениться: тогда
# выборка идёт по одному городу, а заголовок карточки/имя CSV называет другой — ровно та
# ошибка, против которой заголовок и вводился.

def _count_city_reads(monkeypatch):
    # Phase 13 (13-04): _admin_city_view (the sole caller) moved to handlers/admin_core.py --
    # its own `admin_selected_city` call resolves via admin_core's module globals, not
    # handlers.admin's, so the patch target follows the function to its real home.
    calls = []
    original = admin_core.admin_selected_city

    async def counting(admin_id):
        calls.append(admin_id)
        return await original(admin_id)

    monkeypatch.setattr(admin_core, "admin_selected_city", counting)
    return calls


def test_admin_city_view_returns_scope_and_label_from_one_read(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    calls = _count_city_reads(monkeypatch)
    scope, label = asyncio.run(admin_mod._admin_city_view(ADMIN_ID))
    assert scope == cities.city_scope("spb")
    assert label == asyncio.run(cities.city_label("spb"))
    assert len(calls) == 1


def test_admin_city_view_module_off_is_no_scope(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", "spb"))
    assert asyncio.run(admin_mod._admin_city_view(ADMIN_ID)) == (None, None)


def test_applications_queue_resolves_the_city_once_per_render(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    _seed_city(1, "spb")
    calls = _count_city_reads(monkeypatch)
    asyncio.run(admin_mod._show_current_card(FakeMessage(), _new_state(ADMIN_ID)))
    assert len(calls) == 1


def test_receipts_queue_resolves_the_city_once_per_render(tmp_path, monkeypatch):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    _seed_city(1, "spb", status="approved", payment_status="receipt_sent")
    calls = _count_city_reads(monkeypatch)
    asyncio.run(admin_mod._show_current_receipt_card(FakeMessage(), _new_state(ADMIN_ID)))
    assert len(calls) == 1


def test_csv_export_resolves_the_city_once_per_render(tmp_path, monkeypatch):
    """Имя файла (`users_{scope[0]}.csv`) и подпись строятся из ОДНОГО чтения — иначе выгрузка
    может называться одним городом, а содержать другой."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    _seed_city(1, "spb")
    calls = _count_city_reads(monkeypatch)
    cb = FakeCallback("admin_export_csv")
    cb.message.documents = []

    async def answer_document(document, caption=None):
        cb.message.documents.append((document.filename, caption))

    cb.message.answer_document = answer_document
    asyncio.run(admin_mod.show_admin_export(cb))
    assert len(calls) == 1
    filename, caption = cb.message.documents[-1]
    assert filename == "users_spb.csv"
    assert asyncio.run(cities.city_label("spb")) in caption
