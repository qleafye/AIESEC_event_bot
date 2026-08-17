"""Quick 260817-bvb — closes 09.1-REVIEW.md CR-03 / WR-02 / WR-06.

Same class across all three: the QUEUE/LIST is scoped by city, but the per-item ACTION
(callback_data carries a bare id, cards/buttons never expire) was not — a bound manager or
delegate could act outside their city scope via a stale button from chat history. This file
mirrors the harness/seeding conventions of test_gamification_review_phase9.py (grev_* FakeBot/
FakeMessage/FakeCallback), test_manager_city_091.py (roles_* FakeCallback with
answered_texts/answered_alerts), and test_game_city_tasks_091.py (_seed_delegate/city seeding).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as every other phase-9/09.1 file.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
import cities
from handlers import admin as admin_mod
from handlers import user_actions as ua_mod


ADMIN_ID = 931001       # superadmin, config.ADMIN_IDS
MANAGER_SPB_ID = 931002  # bound to spb, NOT in config.ADMIN_IDS
DELEGATE_SPB_ID = 931003
DELEGATE_MSK_ID = 931004
STRANGER_ID = 931005     # holds no role, not a superadmin


def _db_ready(tmp_path, dbname="test_game_city_auth_260817.db"):
    config.DB_PATH = str(tmp_path / dbname)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMessage:
    """Stand-in for both callback.message (delete/answer/answer_photo/answer_document/
    edit_text) and a dispatched message event -- superset of the fields used across
    test_gamification_review_phase9.py and test_manager_city_091.py's doubles."""

    def __init__(self, text=None, user_id=ADMIN_ID, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []
        self.edit_calls = 0
        self.markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer_photo(self, photo):
        pass

    async def answer_document(self, document):
        pass

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.message = FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []
        self.answered_texts = []
        self.answered_alerts = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        self.answered_texts.append(text)
        self.answered_alerts.append(show_alert)


def _kb_callback_data(kb: InlineKeyboardMarkup) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


def _seed_task(text="Пост со скрином", category="Light", coins=30, proof_type="text",
               deadline_at="2099-01-01 00:00:00", created_by=None, event_city=None):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at,
                                       created_by, event_city=event_city))


def _seed_submission(task_id, user_id, user_city, content="готово",
                      submitted_at="2026-08-14 10:00:00"):
    asyncio.run(db.add_user({
        "telegram_id": user_id,
        "full_name": f"Delegate {user_id}",
        "registration_date": "2026-08-01",
        "event_city": user_city,
    }))
    return asyncio.run(db.create_submission(task_id, user_id, "text", content, submitted_at))


def _mark_approved(uid):
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status = 'approved' WHERE telegram_id = ?", (uid,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════════════════
# CR-03: _submission_out_of_scope + gates on grev_approve / grev_approve_custom_start /
# grev_reject_start
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_submission_out_of_scope_none_submission_is_false(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(admin_mod._submission_out_of_scope(ADMIN_ID, None)) is False


def test_submission_out_of_scope_module_off_is_always_false(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task()
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")
    submission = asyncio.run(db.get_submission(sid))
    # module off entirely -- no event_city_enabled setting written
    assert asyncio.run(admin_mod._submission_out_of_scope(ADMIN_ID, submission)) is False


def test_submission_out_of_scope_manager_bound_other_city_is_true(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task()
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")
    submission = asyncio.run(db.get_submission(sid))
    assert asyncio.run(admin_mod._submission_out_of_scope(MANAGER_SPB_ID, submission)) is True


def test_submission_out_of_scope_manager_bound_same_city_is_false(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task()
    sid = _seed_submission(task_id, DELEGATE_SPB_ID, "spb")
    submission = asyncio.run(db.get_submission(sid))
    assert asyncio.run(admin_mod._submission_out_of_scope(MANAGER_SPB_ID, submission)) is False


def test_grev_approve_out_of_scope_alerts_no_coins_status_stays_pending(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task(coins=30)
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")

    state = _new_state(MANAGER_SPB_ID)
    cb = FakeCallback(f"grev_approve:{sid}", MANAGER_SPB_ID)
    asyncio.run(admin_mod.grev_approve(cb, state))

    assert cb.answers[-1] == ("Эта сдача из другого города — переключите город.", True)
    assert asyncio.run(db.get_balance(DELEGATE_MSK_ID)) == 0
    submission = asyncio.run(db.get_submission(sid))
    assert submission["status"] == "pending"
    assert cb.message.deleted is False
    assert cb.bot.sent == []


def test_grev_approve_in_scope_still_credits_coins_and_notifies(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task(coins=30)
    sid = _seed_submission(task_id, DELEGATE_SPB_ID, "spb")

    state = _new_state(MANAGER_SPB_ID)
    cb = FakeCallback(f"grev_approve:{sid}", MANAGER_SPB_ID)
    asyncio.run(admin_mod.grev_approve(cb, state))

    assert asyncio.run(db.get_balance(DELEGATE_SPB_ID)) == 30
    submission = asyncio.run(db.get_submission(sid))
    assert submission["status"] == "approved"
    assert cb.bot.sent  # delegate notified


def test_grev_approve_custom_start_out_of_scope_alerts_and_sets_no_fsm_state(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task()
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")

    state = _new_state(MANAGER_SPB_ID)
    cb = FakeCallback(f"grev_approve_custom:{sid}", MANAGER_SPB_ID)
    asyncio.run(admin_mod.grev_approve_custom_start(cb, state))

    assert cb.answers[-1] == ("Эта сдача из другого города — переключите город.", True)
    assert asyncio.run(state.get_state()) is None
    assert cb.message.answers_sent == []


def test_grev_reject_start_out_of_scope_alerts_and_sets_no_fsm_state(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_SPB_ID, "spb"))
    task_id = _seed_task()
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")

    state = _new_state(MANAGER_SPB_ID)
    cb = FakeCallback(f"grev_reject:{sid}", MANAGER_SPB_ID)
    asyncio.run(admin_mod.grev_reject_start(cb, state))

    assert cb.answers[-1] == ("Эта сдача из другого города — переключите город.", True)
    assert asyncio.run(state.get_state()) is None
    assert cb.message.answers_sent == []


def test_grev_approve_superadmin_scoped_via_header_choice_rejects_other_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    task_id = _seed_task(coins=30)
    sid = _seed_submission(task_id, DELEGATE_MSK_ID, "msk")

    state = _new_state(ADMIN_ID)
    cb = FakeCallback(f"grev_approve:{sid}", ADMIN_ID)
    asyncio.run(admin_mod.grev_approve(cb, state))

    assert cb.answers[-1] == ("Эта сдача из другого города — переключите город.", True)
    assert asyncio.run(db.get_balance(DELEGATE_MSK_ID)) == 0


# ═══════════════════════════════════════════════════════════════════════════════════════════
# WR-02: only a superadmin (config.ADMIN_IDS) may rebind a manager's city; honest failure on
# set_staff_city returning False; build_roles_keyboard(viewer_id) hides the button
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_roles_city_pick_non_superadmin_alerts_and_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback(f"roles_city_pick:{MANAGER_SPB_ID}:spb", STRANGER_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))

    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "суперадмин" in (cb.answered_texts[-1] or "")
    assert asyncio.run(db.get_staff_city(MANAGER_SPB_ID)) is None
    assert cb.message.edit_calls == 0


def test_roles_city_start_non_superadmin_alerts_no_picker(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback(f"roles_city:{MANAGER_SPB_ID}", STRANGER_ID)
    asyncio.run(admin_mod.roles_city_start(cb))

    assert cb.message.edit_calls == 0
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "суперадмин" in (cb.answered_texts[-1] or "")


def test_roles_city_pick_superadmin_still_works_as_before(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    cb = FakeCallback(f"roles_city_pick:{MANAGER_SPB_ID}:spb", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))

    assert asyncio.run(db.get_staff_city(MANAGER_SPB_ID)) == "spb"
    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "Санкт-Петербург" in (cb.answered_texts[-1] or "") or "спб" in (cb.answered_texts[-1] or "").lower()
    assert cb.message.edit_calls == 1


def test_roles_city_pick_set_staff_city_false_shows_honest_alert_no_lie(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    # MANAGER_SPB_ID is NOT in staff -- set_staff_city must return False.
    assert asyncio.run(db.get_staff_city(MANAGER_SPB_ID)) is None

    cb = FakeCallback(f"roles_city_pick:{MANAGER_SPB_ID}:spb", ADMIN_ID)
    asyncio.run(admin_mod.roles_city_pick(cb))

    assert cb.answered_alerts and cb.answered_alerts[-1] is True
    assert "уже нет в списке" in (cb.answered_texts[-1] or "")
    assert cb.message.edit_calls == 0


def test_build_roles_keyboard_hides_city_button_for_non_superadmin_viewer(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    kb = asyncio.run(admin_mod.build_roles_keyboard(viewer_id=STRANGER_ID))
    assert not any(cd.startswith("roles_city:") for cd in _kb_callback_data(kb))


def test_build_roles_keyboard_shows_city_button_for_superadmin_viewer(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    kb = asyncio.run(admin_mod.build_roles_keyboard(viewer_id=ADMIN_ID))
    assert f"roles_city:{MANAGER_SPB_ID}" in _kb_callback_data(kb)


def test_build_roles_keyboard_default_viewer_id_none_still_shows_button(tmp_path):
    # Backward compatibility: existing call sites / tests call the function with no args.
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_SPB_ID, "reg_manager", ADMIN_ID))

    kb = asyncio.run(admin_mod.build_roles_keyboard())
    assert f"roles_city:{MANAGER_SPB_ID}" in _kb_callback_data(kb)


def test_roles_assign_city_step_shown_only_to_superadmin(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    cb = FakeCallback(f"roles_addrole:{MANAGER_SPB_ID}:reg_manager", STRANGER_ID)
    asyncio.run(admin_mod.roles_assign(cb))

    assert cb.message.edit_calls == 1
    cds = _kb_callback_data(cb.message.markup)
    assert not any(cd.startswith("roles_city_pick:") for cd in cds)


def test_roles_assign_city_step_shown_to_superadmin(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))

    cb = FakeCallback(f"roles_addrole:{MANAGER_SPB_ID}:reg_manager", ADMIN_ID)
    asyncio.run(admin_mod.roles_assign(cb))

    assert cb.message.edit_calls == 1
    cds = _kb_callback_data(cb.message.markup)
    assert f"roles_city_pick:{MANAGER_SPB_ID}:all" in cds


def test_gate_no_legacy_admin_check_remains():
    """Structural guard (mirrors tests/test_roles_phase8.py): the new gates must be
    positive-form (`x in config.ADMIN_IDS`), never `not in`."""
    with open("handlers/admin.py", encoding="utf-8") as f:
        lines = f.readlines()
    code_lines = [ln for ln in lines if not ln.strip().startswith("#")]
    joined = "".join(code_lines)
    assert "not in config.ADMIN_IDS" not in joined


# ═══════════════════════════════════════════════════════════════════════════════════════════
# WR-06: mytask_submit_start rejects a delegate submitting to another city's task
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_mytask_submit_start_other_city_task_alerts_no_state(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ua_mod._gs_pending_albums.clear()
    task_id = _seed_task(event_city="msk")
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": "spb",
    }))
    _mark_approved(DELEGATE_SPB_ID)

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert cb.answers[-1] == ("Это задание для другого города", True)
    assert asyncio.run(state.get_state()) is None
    assert cb.message.answers_sent == []


def test_mytask_submit_start_same_city_task_allows(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ua_mod._gs_pending_albums.clear()
    task_id = _seed_task(event_city="spb")
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": "spb",
    }))
    _mark_approved(DELEGATE_SPB_ID)

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert asyncio.run(state.get_state()) == "GameSubmit:proof"
    assert cb.message.answers_sent  # prompt sent


def test_mytask_submit_start_null_city_task_allows_any_delegate(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ua_mod._gs_pending_albums.clear()
    task_id = _seed_task(event_city=None)
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": "spb",
    }))
    _mark_approved(DELEGATE_SPB_ID)

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert asyncio.run(state.get_state()) == "GameSubmit:proof"


def test_mytask_submit_start_module_off_allows_cross_city(tmp_path):
    _db_ready(tmp_path)
    # module off entirely -- no event_city_enabled setting written
    ua_mod._gs_pending_albums.clear()
    task_id = _seed_task(event_city="msk")
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": "spb",
    }))
    _mark_approved(DELEGATE_SPB_ID)

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert asyncio.run(state.get_state()) == "GameSubmit:proof"


def test_mytask_submit_start_delegate_without_city_matches_default_city_task(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ua_mod._gs_pending_albums.clear()
    default_code = cities.default_city_code()
    task_id = _seed_task(event_city=default_code)
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": None,
    }))
    _mark_approved(DELEGATE_SPB_ID)

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert asyncio.run(state.get_state()) == "GameSubmit:proof"


def test_mytask_submit_start_duplicate_submission_still_blocked(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    ua_mod._gs_pending_albums.clear()
    task_id = _seed_task(event_city="spb")
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_SPB_ID, "full_name": "D", "registration_date": "2026-08-01",
        "event_city": "spb",
    }))
    _mark_approved(DELEGATE_SPB_ID)
    asyncio.run(db.create_submission(task_id, DELEGATE_SPB_ID, "text", "готово",
                                      "2026-08-14 10:00:00"))

    state = _new_state(DELEGATE_SPB_ID)
    cb = FakeCallback(f"mytask_submit:{task_id}", DELEGATE_SPB_ID)
    asyncio.run(ua_mod.mytask_submit_start(cb, state))

    assert cb.answers[-1] == ("Уже отправлено, ожидай проверки", True)
