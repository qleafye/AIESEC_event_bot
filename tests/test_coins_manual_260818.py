"""Phase 14 Plan 04 (GAME-09) — manual coins «for humans».

Covers Task 1 (coins.source, mandatory reason on /coins, delegate notification, moderate_game
repoint), Task 2 (button wizard entry: person resolution, sign, amount) and Task 3 (reason,
confirm screen, ledger write, notification, resync) of 14-04-PLAN.md.

Handlers driven directly via Fake message/callback doubles -- same convention as
tests/test_gamification_review_phase9.py (FakeUser/FakeBot/FakeMessage/FakeCallback,
asyncio.run(), config.DB_PATH -> tmp_path). pytest-asyncio is unavailable in this env.
"""
import asyncio
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.states import CoinsManual, GameReview
from settings_schema import SETTINGS_SCHEMA


ADMIN_ID = 931401
GAME_MANAGER_ID = 931402
DELEGATE_ID = 931403


def _db_ready(tmp_path, name="test_coins_manual_260818.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeMessage:
    """Stand-in for both callback.message and a dispatched message event. `forward_origin`/
    `forward_from` default to None (plain text input) -- individual tests set them to exercise
    _resolve_staff_input's forward-message branch."""

    def __init__(self, text=None, user_id=ADMIN_ID, bot=None, forward_origin=None, forward_from=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.forward_origin = forward_origin
        self.forward_from = forward_from
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []
        self.documents = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def answer_document(self, document, caption=None):
        self.documents.append(document)

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.message = FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeForwardedSender:
    def __init__(self, uid):
        self.id = uid


class FakeForwardOrigin:
    """Minimal MessageOriginUser double -- _resolve_staff_input reads only `.sender_user`."""

    def __init__(self, uid):
        self.sender_user = FakeForwardedSender(uid)


def _coin_rows():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM coins ORDER BY id").fetchall()]
    conn.close()
    return rows


def _seed_task(coins=10, created_by=ADMIN_ID):
    return asyncio.run(db.create_task("Тестовое задание", "Light", coins, "text",
                                       "2099-01-01 00:00:00", created_by))


def _seed_submission(task_id, user_id=DELEGATE_ID, content="готово"):
    return asyncio.run(db.create_submission(task_id, user_id, "text", content, "2026-08-14 10:00:00"))


def _seed_delegate(user_id=DELEGATE_ID, username="@delegate1", full_name="Дельгат Тестов"):
    """`username` stored WITH the leading @ — get_user_by_username's own normalization only
    ADDS a missing @, it never strips one, so the seeded value must match what a real Telegram
    username row looks like (database.db.get_user_by_username, `users.username` column)."""
    asyncio.run(db.add_user({
        "telegram_id": user_id, "username": username, "full_name": full_name,
        "registration_date": "2026-08-01",
    }))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: coins.source, mandatory reason on /coins, delegate notification, moderate_game repoint
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_coins_table_has_source_column(tmp_path):
    _db_ready(tmp_path)
    conn = sqlite3.connect(config.DB_PATH)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(coins)").fetchall()]
    conn.close()
    assert "source" in cols


def test_add_coins_without_source_stays_null_regression(tmp_path):
    """Every pre-existing call site that never passes source= keeps writing NULL."""
    _db_ready(tmp_path)
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="старый вызов", changed_by=ADMIN_ID))
    rows = _coin_rows()
    assert len(rows) == 1
    assert rows[0]["source"] is None


def test_add_coins_writes_source_when_given(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="за помощь", changed_by=ADMIN_ID, source="manual"))
    rows = _coin_rows()
    assert rows[-1]["source"] == "manual"


def test_grev_approve_default_amount_writes_source_task(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(coins=30)
    sub_id = _seed_submission(task_id)
    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_mod.grev_approve(callback, state))
    rows = _coin_rows()
    assert rows[-1]["source"] == "task"


def test_grev_approve_custom_amount_writes_source_task(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    start_cb = FakeCallback(f"grev_approve_custom:{sub_id}")
    asyncio.run(admin_mod.grev_approve_custom_start(start_cb, state))
    assert asyncio.run(state.get_state()) == GameReview.approve_amount
    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_mod.grev_approve_amount_step(amount_msg, state))
    rows = _coin_rows()
    assert rows[-1]["source"] == "task"


def test_cmd_coins_without_reason_shows_hint_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert "причин" in msg.answers_sent[-1].lower()
    assert _coin_rows() == []


def test_cmd_coins_with_whitespace_only_reason_shows_hint_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5    ")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert "причин" in msg.answers_sent[-1].lower()
    assert _coin_rows() == []


def test_cmd_coins_with_reason_writes_manual_row(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5 за активность")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    rows = _coin_rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["reason"] == "за активность"
    assert rows[0]["changed_by"] == ADMIN_ID
    assert rows[0]["delta"] == 5


def test_cmd_coins_notifies_delegate_with_amount_reason_balance(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    msg = FakeMessage(text="/coins @delegate1 +5 за активность")
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert msg.bot.sent, "no notification sent to delegate"
    chat_id, text = msg.bot.sent[-1]
    assert chat_id == DELEGATE_ID
    assert "+5" in text
    assert "за активность" in text
    assert "5" in text  # new balance


def test_cmd_coins_blocked_bot_keeps_ledger_row_and_flags_manager(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    failing_bot = FakeBot(fail=True)
    msg = FakeMessage(text="/coins @delegate1 +5 за активность", bot=failing_bot)
    asyncio.run(admin_mod.cmd_coins(msg, msg.bot))
    assert len(_coin_rows()) == 1  # ledger write NOT rolled back by a failed notification
    assert "не получил уведомление" in msg.answers_sent[-1]


def test_notify_manual_coins_escapes_html_in_reason(tmp_path):
    _db_ready(tmp_path)
    bot = FakeBot()
    ok = asyncio.run(admin_mod._notify_manual_coins(bot, DELEGATE_ID, 5, "<b>hack</b>", 10))
    assert ok is True
    _, text = bot.sent[-1]
    assert "<b>hack</b>" not in text
    assert "&lt;b&gt;hack&lt;/b&gt;" in text


def test_coins_capability_is_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(command="coins") == "moderate_game"


def test_coins_manual_notify_text_registry_key():
    entry = SETTINGS_SCHEMA["coins_manual_notify_text"]
    assert entry["type"] == "text"
    assert entry["group"] == "game"
    for placeholder in ("{delta}", "{reason}", "{balance}"):
        assert placeholder in entry["default"]
    assert "coins_manual_notify_text" in admin_mod._GAME_FIELD_ORDER


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: button entry «🪙 Монеты вручную» — person resolution, card, sign, amount
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_menu_row_visible_for_moderate_game_hidden_for_moderate_reg(tmp_path):
    _db_ready(tmp_path)
    row = ("🪙 Монеты вручную", "admin_coins_manual")
    assert row in admin_mod._ADMIN_MENU_ROWS
    assert row in admin_mod._visible_menu_rows({"moderate_game"})
    assert row not in admin_mod._visible_menu_rows({"moderate_reg"})


def test_admin_coins_manual_sets_person_state_and_prompts(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("admin_coins_manual")
    state = _new_state()
    asyncio.run(admin_mod.admin_coins_manual(callback, state))
    assert asyncio.run(state.get_state()) == CoinsManual.person
    prompt = callback.message.answers_sent[-1]
    assert "@username" in prompt or "перешл" in prompt.lower()


def test_coinsman_person_step_forward_shows_card_with_balance_and_sign_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 7, reason="стартовый баланс", changed_by=ADMIN_ID, source="manual"))
    state = _new_state()
    asyncio.run(state.set_state(CoinsManual.person))
    msg = FakeMessage(text="перешлите это", forward_origin=FakeForwardOrigin(DELEGATE_ID))
    asyncio.run(admin_mod.coinsman_person_step(msg, state))

    card = msg.answers_sent[-1]
    assert "Дельгат Тестов" in card
    assert "7" in card  # current balance shown
    kb = msg.answer_markups[-1]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "coinsman_sign:plus" in flat
    assert "coinsman_sign:minus" in flat
    data = asyncio.run(state.get_data())
    assert data.get("cm_user_id") == DELEGATE_ID


def test_coinsman_person_step_unknown_username_stays_on_step(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(CoinsManual.person))
    msg = FakeMessage(text="@ghost_user")
    asyncio.run(admin_mod.coinsman_person_step(msg, state))
    assert "не нашёл" in msg.answers_sent[-1].lower()
    assert asyncio.run(state.get_state()) == CoinsManual.person
    data = asyncio.run(state.get_data())
    assert data.get("cm_user_id") is None


def test_coinsman_sign_step_moves_to_amount_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID))
    asyncio.run(state.set_state(CoinsManual.person))
    callback = FakeCallback("coinsman_sign:plus")
    asyncio.run(admin_mod.coinsman_sign_step(callback, state))
    assert asyncio.run(state.get_state()) == CoinsManual.amount
    data = asyncio.run(state.get_data())
    assert data.get("cm_sign") == "plus"


def test_coinsman_amount_step_rejects_garbage_and_zero(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus"))
    asyncio.run(state.set_state(CoinsManual.amount))

    for bad in ("abc", "0"):
        msg = FakeMessage(text=bad)
        asyncio.run(admin_mod.coinsman_amount_step(msg, state))
        assert asyncio.run(state.get_state()) == CoinsManual.amount
        assert (asyncio.run(state.get_data())).get("cm_delta") is None


def test_coinsman_amount_step_accepts_number_and_moves_to_reason_signed(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="minus"))
    asyncio.run(state.set_state(CoinsManual.amount))
    msg = FakeMessage(text="3")
    asyncio.run(admin_mod.coinsman_amount_step(msg, state))
    assert asyncio.run(state.get_state()) == CoinsManual.reason
    data = asyncio.run(state.get_data())
    assert data.get("cm_delta") == -3  # sign applied (minus)


def test_coinsman_new_keys_resolve_to_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(callback_data="admin_coins_manual") == "moderate_game"
    assert required_capability(callback_data="coinsman_sign:plus") == "moderate_game"
    assert required_capability(callback_data="coinsman_confirm") == "moderate_game"
    assert required_capability(callback_data="coinsman_cancel") == "moderate_game"
    assert required_capability(raw_state="CoinsManual:person") == "moderate_game"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 3: reason, confirm screen, ledger write, notification, resync
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_coinsman_reason_step_empty_stays_on_step_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5))
    asyncio.run(state.set_state(CoinsManual.reason))
    msg = FakeMessage(text="   ")
    asyncio.run(admin_mod.coinsman_reason_step(msg, state))
    assert "причина обязательна" in msg.answers_sent[-1].lower()
    assert asyncio.run(state.get_state()) == CoinsManual.reason
    assert _coin_rows() == []


def test_coinsman_reason_step_shows_confirm_screen(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="minus", cm_delta=-3))
    asyncio.run(state.set_state(CoinsManual.reason))
    msg = FakeMessage(text="за нарушение")
    asyncio.run(admin_mod.coinsman_reason_step(msg, state))

    card = msg.answers_sent[-1]
    assert "Дельгат Тестов" in card
    assert "-3" in card or "−3" in card
    assert "за нарушение" in card
    kb = msg.answer_markups[-1]
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "coinsman_confirm" in flat
    assert "coinsman_cancel" in flat
    data = asyncio.run(state.get_data())
    assert data.get("cm_reason") == "за нарушение"


def test_coinsman_confirm_writes_exactly_one_row_with_source_manual(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5, cm_reason="за помощь"))
    asyncio.run(state.set_state(CoinsManual.reason))
    callback = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(callback, state))

    rows = _coin_rows()
    assert len(rows) == 1
    assert rows[0]["delta"] == 5
    assert rows[0]["reason"] == "за помощь"
    assert rows[0]["changed_by"] == ADMIN_ID
    assert rows[0]["source"] == "manual"
    assert asyncio.run(state.get_state()) is None


def test_coinsman_confirm_repeated_tap_does_not_double_write(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5, cm_reason="за помощь"))
    asyncio.run(state.set_state(CoinsManual.reason))
    first = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(first, state))

    second = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(second, state))

    assert len(_coin_rows()) == 1  # exactly one row -- state was already cleared
    assert second.answers == [("Операция уже завершена или отменена", True)]


def test_coinsman_confirm_notifies_delegate_after_ledger_write(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5, cm_reason="за помощь"))
    asyncio.run(state.set_state(CoinsManual.reason))
    callback = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(callback, state))

    assert len(_coin_rows()) == 1  # ledger write happened
    assert callback.bot.sent, "delegate was not notified"
    chat_id, text = callback.bot.sent[-1]
    assert chat_id == DELEGATE_ID
    assert "+5" in text
    assert "за помощь" in text


def test_coinsman_confirm_blocked_bot_keeps_row_and_flags_manager(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5, cm_reason="за помощь"))
    asyncio.run(state.set_state(CoinsManual.reason))
    failing_bot = FakeBot(fail=True)
    callback = FakeCallback("coinsman_confirm", user_id=ADMIN_ID, bot=failing_bot)
    asyncio.run(admin_mod.coinsman_confirm(callback, state))

    assert len(_coin_rows()) == 1  # ledger write NOT rolled back
    assert "не получил уведомление" in callback.message.answers_sent[-1]


def test_coinsman_confirm_triggers_game_resync(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed_delegate()
    resync_calls = []
    monkeypatch.setattr(admin_mod, "_request_game_resync", lambda *a, **k: resync_calls.append(1))
    state = _new_state()
    asyncio.run(state.update_data(cm_user_id=DELEGATE_ID, cm_sign="plus", cm_delta=5, cm_reason="за помощь"))
    asyncio.run(state.set_state(CoinsManual.reason))
    callback = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(callback, state))
    assert resync_calls == [1]


def test_coinsman_confirm_with_no_state_data_alerts_and_writes_nothing(tmp_path):
    """A stale/expired confirm screen (state already cleared) must not record anything."""
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    callback = FakeCallback("coinsman_confirm", user_id=ADMIN_ID)
    asyncio.run(admin_mod.coinsman_confirm(callback, state))
    assert _coin_rows() == []
    assert callback.answers == [("Операция уже завершена или отменена", True)]


def test_coinsman_end_to_end_forward_minus_amount_reason_confirm(tmp_path):
    """Сквозной сценарий: пересланное сообщение -> ➖ Списать -> 3 -> «за нарушение» ->
    подтверждение -> баланс уменьшился на 3, уведомление ушло."""
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 10, reason="стартовый баланс", changed_by=ADMIN_ID, source="manual"))
    state = _new_state()

    # 1) admin_coins_manual
    open_cb = FakeCallback("admin_coins_manual")
    asyncio.run(admin_mod.admin_coins_manual(open_cb, state))
    assert asyncio.run(state.get_state()) == CoinsManual.person

    # 2) forward message resolves the delegate
    person_msg = FakeMessage(forward_origin=FakeForwardOrigin(DELEGATE_ID))
    asyncio.run(admin_mod.coinsman_person_step(person_msg, state))

    # 3) ➖ Списать
    sign_cb = FakeCallback("coinsman_sign:minus")
    asyncio.run(admin_mod.coinsman_sign_step(sign_cb, state))
    assert asyncio.run(state.get_state()) == CoinsManual.amount

    # 4) сумма 3
    amount_msg = FakeMessage(text="3")
    asyncio.run(admin_mod.coinsman_amount_step(amount_msg, state))
    assert asyncio.run(state.get_state()) == CoinsManual.reason

    # 5) причина
    reason_msg = FakeMessage(text="за нарушение")
    asyncio.run(admin_mod.coinsman_reason_step(reason_msg, state))

    # 6) подтверждение
    confirm_bot = FakeBot()
    confirm_cb = FakeCallback("coinsman_confirm", bot=confirm_bot)
    asyncio.run(admin_mod.coinsman_confirm(confirm_cb, state))

    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 7  # 10 - 3
    assert confirm_bot.sent, "delegate was not notified"
    chat_id, text = confirm_bot.sent[-1]
    assert chat_id == DELEGATE_ID
    assert "-3" in text or "−3" in text
    assert "за нарушение" in text


# ═══════════════════════════════════════════════════════════════════════════════════════════
# 14-05 Task 1: list_manual_coin_entries / count_manual_coin_entries / export_coins_journal_csv
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_list_manual_coin_entries_returns_freshest_manual_rows_with_recipient_fields(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 1, reason="r1", changed_by=ADMIN_ID, source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 2, reason="r2", changed_by=ADMIN_ID, source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 3, reason="r3", changed_by=ADMIN_ID, source="manual"))
    rows = asyncio.run(db.list_manual_coin_entries(limit=2, offset=0))
    assert len(rows) == 2
    assert rows[0]["reason"] == "r3"  # id DESC -- newest first
    assert rows[1]["reason"] == "r2"
    assert rows[0]["user_full_name"] == "Дельгат Тестов"
    assert rows[0]["user_username"] == "@delegate1"


def test_list_and_count_manual_coin_entries_exclude_task_and_legacy_rows(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="ручное", changed_by=ADMIN_ID, source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 30, reason="за задание", changed_by=ADMIN_ID, source="task"))
    asyncio.run(db.add_coins(DELEGATE_ID, 10, reason="легаси", changed_by=ADMIN_ID))  # source=None
    rows = asyncio.run(db.list_manual_coin_entries(limit=10, offset=0))
    assert len(rows) == 1
    assert rows[0]["reason"] == "ручное"
    assert asyncio.run(db.count_manual_coin_entries()) == 1


def test_list_manual_coin_entries_offset_paginates(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for i in range(3):
        asyncio.run(db.add_coins(DELEGATE_ID, 1, reason=f"m{i}", changed_by=ADMIN_ID, source="manual"))
    page1 = asyncio.run(db.list_manual_coin_entries(limit=2, offset=0))
    page2 = asyncio.run(db.list_manual_coin_entries(limit=2, offset=2))
    assert [r["reason"] for r in page1] == ["m2", "m1"]
    assert [r["reason"] for r in page2] == ["m0"]


def test_export_coins_journal_csv_includes_manual_task_and_legacy_rows(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="ручное", changed_by=ADMIN_ID, source="manual"))
    asyncio.run(db.add_coins(DELEGATE_ID, 30, reason="за задание", changed_by=ADMIN_ID, source="task"))
    asyncio.run(db.add_coins(DELEGATE_ID, 10, reason="легаси", changed_by=ADMIN_ID))
    headers, rows = asyncio.run(db.export_coins_journal_csv())
    assert headers == [
        "ID", "Когда", "Кому (ID)", "Кому (ФИО)", "Юзернейм", "Город", "Сколько", "Тип",
        "Причина", "Кто изменил (ID)",
    ]
    assert len(rows) == 3
    type_col = headers.index("Тип")
    reason_col = headers.index("Причина")
    types_by_reason = {r[reason_col]: r[type_col] for r in rows}
    assert types_by_reason["ручное"] == "Вручную"
    assert types_by_reason["за задание"] == "За задание"
    assert types_by_reason["легаси"] == "До обновления"


def test_export_coins_journal_csv_neutralizes_formula_injection_in_reason(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for bad_reason in ("=cmd()", "+1", "-1", "@SUM(1)"):
        asyncio.run(db.add_coins(DELEGATE_ID, 1, reason=bad_reason, changed_by=ADMIN_ID, source="manual"))
    headers, rows = asyncio.run(db.export_coins_journal_csv())
    reason_col = headers.index("Причина")
    reasons = [r[reason_col] for r in rows]
    for bad_reason in ("=cmd()", "+1", "-1", "@SUM(1)"):
        assert ("'" + bad_reason) in reasons


# ═══════════════════════════════════════════════════════════════════════════════════════════
# 14-05 Task 2: «📜 Журнал монет» screen — pagination + CSV export button
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_coins_journal_menu_row_visible_for_moderate_game_hidden_for_moderate_reg(tmp_path):
    _db_ready(tmp_path)
    row = ("📜 Журнал монет", "admin_coins_journal")
    assert row in admin_mod._ADMIN_MENU_ROWS
    assert row in admin_mod._visible_menu_rows({"moderate_game"})
    assert row not in admin_mod._visible_menu_rows({"moderate_reg"})


def test_coins_journal_screen_paginates_ten_per_page_with_nav_buttons(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    for i in range(15):
        asyncio.run(db.add_coins(DELEGATE_ID, 1, reason=f"m{i}", changed_by=ADMIN_ID, source="manual"))

    text, kb = asyncio.run(admin_mod._coins_journal_screen(offset=0))
    assert "Страница 1 из 2" in text
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any(cd.startswith("coinsjrn_page:") for cd in flat)  # "Позже →" present
    assert not any(cd == "coinsjrn_page:-10" for cd in flat)  # no "← Раньше" on page 1

    text2, kb2 = asyncio.run(admin_mod._coins_journal_screen(offset=10))
    assert "Страница 2 из 2" in text2
    flat2 = [btn.callback_data for row in kb2.inline_keyboard for btn in row]
    assert "coinsjrn_page:0" in flat2  # "← Раньше" back to page 1
    assert not any(cd.startswith("coinsjrn_page:20") for cd in flat2)  # no "Позже →" on last page


def test_coins_journal_screen_row_shows_recipient_amount_reason_changer_no_raw_source(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="за помощь", changed_by=ADMIN_ID, source="manual"))
    text, _ = asyncio.run(admin_mod._coins_journal_screen(offset=0))
    assert "Дельгат Тестов" in text
    assert "+5" in text
    assert "за помощь" in text
    assert str(ADMIN_ID) in text
    assert "manual" not in text
    assert "source" not in text


def test_coins_journal_screen_empty_shows_placeholder_and_csv_button(tmp_path):
    _db_ready(tmp_path)
    text, kb = asyncio.run(admin_mod._coins_journal_screen(offset=0))
    assert "Ручных операций пока не было." in text
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "coinsjrn_csv" in flat


def test_coinsjrn_csv_sends_document_with_content(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    asyncio.run(db.add_coins(DELEGATE_ID, 5, reason="за помощь", changed_by=ADMIN_ID, source="manual"))
    callback = FakeCallback("coinsjrn_csv")
    asyncio.run(admin_mod.coinsjrn_csv(callback))
    assert callback.message.documents, "no document sent"
    document = callback.message.documents[-1]
    assert document.data  # BufferedInputFile has non-empty bytes


def test_coinsjrn_new_keys_resolve_to_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(callback_data="admin_coins_journal") == "moderate_game"
    assert required_capability(callback_data="coinsjrn_page:10") == "moderate_game"
    assert required_capability(callback_data="coinsjrn_csv") == "moderate_game"
