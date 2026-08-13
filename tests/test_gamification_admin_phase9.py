"""Phase 9 Plan 02 (GAME-01, D-08) — «📋 Задания» screen + `GameTaskCreate` creation wizard.

Handlers are called DIRECTLY with Fake message/callback doubles (same convention as
tests/test_city_admin_phase72.py) except for the one structural capability-map check, which
goes straight through `required_capability()` (no full dispatch needed -- CapabilityMiddleware
itself is not touched by this plan, see 09-02-PLAN.md Task 1 <behavior>).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as every other phase-9 test file.
"""
import asyncio

import aiosqlite

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from database.db import GAME_CATEGORIES, GAME_PROOF_TYPES
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.states import GameTaskCreate


ADMIN_ID = 930901


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_admin_phase9.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int = ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    """Stand-in for both `callback.message` (edit_text/answer) and a dispatched message event
    (text + answer)."""

    def __init__(self, text=None):
        self.text = text
        self.markup = None
        self.edit_calls = 0
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


async def _seed_task_at(text, category, coins, proof_type, deadline_at, created_at):
    """Insert a task with an explicit `created_at` -- `db.create_task` stamps `datetime.now()`
    internally with second resolution, so two calls within the same wall-clock second would
    tie on `ORDER BY created_at DESC` and make an ordering assertion flaky."""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, category, coins, proof_type, deadline_at, ADMIN_ID, created_at),
        )
        await conn.commit()


# ── Task 1: «📋 Задания» screen -- list + entry into the wizard ────────────────────────────

def test_admin_game_tasks_key_maps_to_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(callback_data="admin_game_tasks") == "moderate_game"


def test_admin_game_tasks_empty_state_shows_create_button(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("admin_game_tasks")
    state = _new_state()
    asyncio.run(admin_mod.show_game_tasks(callback, state))
    assert callback.message.answers_sent[-1] == "Заданий пока нет."
    assert "gtnew" in _flat_callback_data(callback.message.answer_markups[-1])


def test_admin_game_tasks_lists_existing_tasks_newest_first(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_seed_task_at("Первое задание", "Light", 15, "text", "2026-08-20 10:00:00", "2026-08-13 09:00:00"))
    asyncio.run(_seed_task_at("Второе задание", "Medium", 30, "photo", "2026-08-25 23:59:00", "2026-08-13 09:05:00"))
    callback = FakeCallback("admin_game_tasks")
    state = _new_state()
    asyncio.run(admin_mod.show_game_tasks(callback, state))
    text = callback.message.answers_sent[-1]
    first_pos = text.index("Первое задание")
    second_pos = text.index("Второе задание")
    assert second_pos < first_pos  # list_all_tasks() orders newest (created LAST) first
    assert "gtnew" in _flat_callback_data(callback.message.answer_markups[-1])


def test_gtnew_prompts_for_text_and_sets_state(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("gtnew")
    state = _new_state()
    asyncio.run(admin_mod.game_task_new(callback, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.text
    assert callback.message.answers_sent[-1] == "Введите текст задания:"


def test_menu_row_admin_game_tasks_appears_for_game_manager_only(tmp_path):
    _db_ready(tmp_path)
    rows = admin_mod._visible_menu_rows({"moderate_game"})
    assert ("📋 Задания", "admin_game_tasks") in rows
    rows_reg = admin_mod._visible_menu_rows({"moderate_reg"})
    assert ("📋 Задания", "admin_game_tasks") not in rows_reg


def test_cancel_mid_wizard_clears_state_and_shows_list(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = FakeMessage(text="Отмена")
    asyncio.run(admin_mod.cancel_game_task_create(message, state))
    assert asyncio.run(state.get_state()) is None
    assert "Заданий пока нет." in message.answers_sent[-1]


# ── Task 2: full wizard -- text -> category -> coins -> proof_type -> deadline -> confirm ──

def _drive_to_confirm(state, *, text="Пост со скрином #знакомство", category="Light",
                       coins="30", proof="photo", deadline="25.08.2026 23:59"):
    asyncio.run(admin_mod.game_task_text_step(FakeMessage(text=text), state))
    asyncio.run(admin_mod.game_task_category_step(FakeCallback(f"gtcat:{category}"), state))
    asyncio.run(admin_mod.game_task_coins_step(FakeMessage(text=coins), state))
    asyncio.run(admin_mod.game_task_proof_step(FakeCallback(f"gtproof:{proof}"), state))
    deadline_message = FakeMessage(text=deadline)
    asyncio.run(admin_mod.game_task_deadline_step(deadline_message, state))
    return deadline_message


def test_game_task_text_step_rejects_empty_and_advances_on_nonempty(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = FakeMessage(text="   ")
    asyncio.run(admin_mod.game_task_text_step(message, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.text  # stays on the same step
    assert "пуст" in message.answers_sent[-1].lower()

    message2 = FakeMessage(text="Реальный текст задания")
    asyncio.run(admin_mod.game_task_text_step(message2, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.category
    assert asyncio.run(state.get_data())["gt_text"] == "Реальный текст задания"


def test_game_task_category_buttons_cover_all_five(tmp_path):
    _db_ready(tmp_path)
    kb = admin_mod._game_task_category_kb()
    assert _flat_callback_data(kb) == [f"gtcat:{c}" for c in GAME_CATEGORIES]
    assert len(_flat_callback_data(kb)) == 5


def test_game_task_coins_step_rejects_non_positive_and_non_numeric(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.coins))
    for bad in ("0", "-5", "abc"):
        message = FakeMessage(text=bad)
        asyncio.run(admin_mod.game_task_coins_step(message, state))
        assert asyncio.run(state.get_state()) == GameTaskCreate.coins

    good = FakeMessage(text="30")
    asyncio.run(admin_mod.game_task_coins_step(good, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.proof_type
    assert asyncio.run(state.get_data())["gt_coins"] == 30


def test_game_task_proof_type_buttons_cover_all_four(tmp_path):
    _db_ready(tmp_path)
    kb = admin_mod._game_task_proof_kb()
    assert _flat_callback_data(kb) == [f"gtproof:{p}" for p in GAME_PROOF_TYPES]
    assert len(_flat_callback_data(kb)) == 4


def test_game_task_deadline_step_rejects_unparseable_and_past_dates(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.deadline))

    bad = FakeMessage(text="не дата")
    asyncio.run(admin_mod.game_task_deadline_step(bad, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert "не понял" in bad.answers_sent[-1].lower()

    past = FakeMessage(text="01.01.2020 10:00")
    asyncio.run(admin_mod.game_task_deadline_step(past, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.deadline
    assert "прошло" in past.answers_sent[-1].lower()

    future = FakeMessage(text="25.08.2099 23:59")
    asyncio.run(admin_mod.game_task_deadline_step(future, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm


def test_game_task_confirm_creates_task_via_create_task(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_to_confirm(state)
    callback = FakeCallback("gtconfirm")
    asyncio.run(admin_mod.game_task_confirm(callback, state))

    tasks = asyncio.run(db.list_all_tasks())
    assert len(tasks) == 1
    task = tasks[0]
    assert task["text"] == "Пост со скрином #знакомство"
    assert task["category"] == "Light"
    assert task["coins"] == 30
    assert task["proof_type"] == "photo"
    assert task["deadline_at"] == "2026-08-25 23:59:00"
    assert asyncio.run(state.get_state()) is None


def test_game_task_confirm_card_escapes_task_text(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    message = _drive_to_confirm(state, text="<script>alert(1)</script>")
    card_text = message.answers_sent[-1]
    assert "<script>" not in card_text
    assert "&lt;script&gt;" in card_text


def test_gtcancel_clears_state_without_creating_task(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    asyncio.run(state.set_state(GameTaskCreate.text))
    _drive_to_confirm(state)
    callback = FakeCallback("gtcancel")
    asyncio.run(admin_mod.game_task_create_cancel(callback, state))

    assert asyncio.run(db.list_all_tasks()) == []
    assert asyncio.run(state.get_state()) is None
