"""Phase 9 Plan 03 (GAME-01/02, D-01/D-02/D-05) — delegate-facing task list + submission wizard.

Handlers are called DIRECTLY with Fake message/callback doubles (same convention as
tests/test_gamification_admin_phase9.py / tests/test_roles_phase8.py) -- user_actions.router
carries no CapabilityMiddleware (that only sits on admin.router), so no capability-map
structural test is needed here, only behavioral ones.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as every other phase-9 test file.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import user_actions as ua_mod
from handlers.states import GameSubmit
from keyboards.builders import get_main_menu_kb


ADMIN_ID = 930901
DELEGATE_ID = 930902
STRANGER_ID = 930903


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_delegate_phase9.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID, status="approved"):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
    }))
    if status != "approved":
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, uid))
        conn.commit()
        conn.close()


def _seed_task(text="Пост со скрином #знакомство", category="Light", coins=30,
               proof_type="photo", deadline_at="2026-08-25 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _new_state(uid=DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid, full_name=None):
        self.id = uid
        self.full_name = full_name


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID, photo=None, document=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.photo = photo
        self.document = document
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeDocument:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, full_name=None):
        self.data = data
        self.from_user = FakeUser(user_id, full_name=full_name)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _flat_kb_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _flat_kb_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Task 1: «🎯 Задания» menu button + delegate task list ──────────────────────────────────

def test_menu_game_tasks_button_shown_by_default(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(get_main_menu_kb())
    texts = [btn.text for row in kb.keyboard for btn in row]
    assert "🎯 Задания" in texts


def test_menu_game_tasks_button_hidden_when_setting_off(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("menu_game_tasks", "off"))
    kb = asyncio.run(get_main_menu_kb())
    texts = [btn.text for row in kb.keyboard for btn in row]
    assert "🎯 Задания" not in texts


def test_show_game_tasks_requires_registration(tmp_path):
    _db_ready(tmp_path)
    _seed_task()
    message = FakeMessage(user_id=STRANGER_ID)  # never registered
    asyncio.run(ua_mod.show_game_tasks(message))
    assert len(message.answers) == 1
    text, _pm, _rm = message.answers[0]
    assert "зарегистрир" in text.lower()


def test_show_game_tasks_empty_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    message = FakeMessage()
    asyncio.run(ua_mod.show_game_tasks(message))
    text, _pm, _rm = message.answers[-1]
    assert text == "Активных заданий сейчас нет. Загляни попозже!"


def test_show_game_tasks_marks_active_submission_status(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    open_task_id = _seed_task(text="Открытое задание", deadline_at="2026-08-20 23:59:00")
    pending_task_id = _seed_task(text="Задание на проверке", deadline_at="2026-08-21 23:59:00")
    approved_task_id = _seed_task(text="Одобренное задание", coins=30, deadline_at="2026-08-22 23:59:00")

    asyncio.run(db.create_submission(pending_task_id, DELEGATE_ID, "photo", "file123",
                                      "2026-08-14 10:00:00"))
    approved_sub_id = asyncio.run(db.create_submission(approved_task_id, DELEGATE_ID, "photo",
                                                         "file456", "2026-08-14 10:00:00"))
    asyncio.run(db.claim_submission(approved_sub_id, ADMIN_ID, "approved", coins_awarded=25))

    message = FakeMessage()
    asyncio.run(ua_mod.show_game_tasks(message))
    text, parse_mode, markup = message.answers[-1]

    assert parse_mode == "HTML"
    assert "Открытое задание" in text
    assert "Задание на проверке" in text and "⏳ на проверке" in text
    assert "Одобренное задание" in text and "✅" in text and "+25🪙" in text

    button_data = _flat_kb_data(markup)
    assert f"mytask_submit:{open_task_id}" in button_data
    assert f"mytask_submit:{pending_task_id}" not in button_data
    assert f"mytask_submit:{approved_task_id}" not in button_data


def test_show_game_tasks_excludes_past_deadline(tmp_path):
    """list_active_tasks() itself never filters by deadline (A-05 override, 09-01) -- this
    only proves the delegate render doesn't add a second, divergent filter on top."""
    _db_ready(tmp_path)
    _seed_delegate()
    _seed_task(text="Просроченное задание", deadline_at="2020-01-01 10:00:00")
    message = FakeMessage()
    asyncio.run(ua_mod.show_game_tasks(message))
    text, _pm, _rm = message.answers[-1]
    assert "Просроченное задание" in text


# ── Task 2: submission wizard + moderate_game notification ─────────────────────────────────

def _start_submission(task_id, uid=DELEGATE_ID):
    """Drives mytask_submit_start and returns (state, callback) so callers can inspect the
    prompt / continue into GameSubmit.proof with a fresh FakeMessage."""
    state = _new_state(uid)
    callback = FakeCallback(f"mytask_submit:{task_id}", user_id=uid)
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    return state, callback


def test_mytask_submit_rejects_task_not_found(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    callback = FakeCallback("mytask_submit:99999")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    assert callback.answers == [("Задание не найдено", True)]
    assert asyncio.run(state.get_state()) is None


def test_mytask_submit_rejects_malformed_task_id(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    state = _new_state()
    callback = FakeCallback("mytask_submit:abc")
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    assert callback.answers == [("Некорректное задание", True)]


def test_mytask_submit_rejects_when_active_submission_exists(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task()
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "photo", "file1", "2026-08-14 10:00:00"))

    state, callback = _start_submission(task_id)
    assert callback.answers == [("Уже отправлено, ожидай проверки", True)]
    assert asyncio.run(state.get_state()) is None

    # second create_submission must never have been attempted -- exactly one row exists
    submissions = asyncio.run(db.list_all_submissions())
    assert len(submissions) == 1


def test_mytask_submit_past_deadline_still_opens_with_warning(tmp_path):
    """A-05 (созвон 13.08, rewrite noted in the executor brief): the deadline is SOFT. A direct
    callback on an expired task_id does NOT get refused -- the wizard opens with a first-line
    warning that the deadline passed and the manager decides on coins. (The plan's own
    <behavior> bullet for this test predates the 13.08 rewrite and still describes the OLD
    hard-deadline refusal; the <action> block and 09-CONTEXT.md A-05 are authoritative and are
    what this test asserts -- see plan-authoring inconsistency note in the SUMMARY.)"""
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(deadline_at="2020-01-01 10:00:00")

    state, callback = _start_submission(task_id)
    assert callback.answers == [(None, False)]  # plain callback.answer(), not an alert
    assert asyncio.run(state.get_state()) == GameSubmit.proof
    prompt, _pm, _rm = callback.message.answers[-1]
    assert prompt.startswith("⏰ Срок сдачи вышел.")
    assert "менеджер" in prompt.lower()


def test_mytask_submit_prompts_matching_proof_type(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    expectations = {
        "photo": "Пришли скриншот/фото:",
        "pdf": "Пришли файл (PDF):",
        "text": "Напиши текстом:",
        "link": "Пришли ссылку:",
    }
    for proof_type, expected_tail in expectations.items():
        task_id = _seed_task(proof_type=proof_type, deadline_at="2026-12-31 23:59:00")
        _state, callback = _start_submission(task_id)
        prompt, _pm, _rm = callback.message.answers[-1]
        assert prompt == expected_tail


def test_submit_photo_extracts_file_id_and_creates_submission(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(proof_type="photo", deadline_at="2026-12-31 23:59:00")
    state, _callback = _start_submission(task_id)

    message = FakeMessage(photo=[FakePhotoSize("small_id"), FakePhotoSize("big_id")])
    asyncio.run(ua_mod.receive_proof(message, FakeBot(), state))

    assert asyncio.run(state.get_state()) is None
    submissions = asyncio.run(db.list_all_submissions())
    assert len(submissions) == 1
    assert submissions[0]["content"] == "big_id"
    assert submissions[0]["content_type"] == "photo"
    assert "Принято!" in message.answers[-1][0]


def test_submit_wrong_content_type_is_rereprompted(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(proof_type="photo", deadline_at="2026-12-31 23:59:00")
    state, _callback = _start_submission(task_id)

    message = FakeMessage(text="вот мой скрин, честно")
    asyncio.run(ua_mod.receive_proof(message, FakeBot(), state))

    assert asyncio.run(state.get_state()) == GameSubmit.proof  # stays put
    assert "скриншот/фото" in message.answers[-1][0]
    assert asyncio.run(db.list_all_submissions()) == []


def test_submit_success_notifies_moderate_game_via_capability_fanout(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(text="Знакомство", proof_type="text", deadline_at="2026-12-31 23:59:00")
    state, _callback = _start_submission(task_id)

    bot = FakeBot()
    message = FakeMessage(text="Вот мой пост #знакомство")
    asyncio.run(ua_mod.receive_proof(message, bot, state))

    assert len(bot.sent) == 1  # ADMIN_ID is the sole moderate_game fallback recipient
    chat_id, text = bot.sent[0]
    assert chat_id == ADMIN_ID
    assert "Знакомство" in text
    assert "Новая сдача" in text


def test_submit_duplicate_race_returns_none_shows_already_submitted(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(proof_type="text", deadline_at="2026-12-31 23:59:00")
    state, _callback = _start_submission(task_id)

    # Simulate a second device winning the race: a non-rejected submission lands for this
    # pair BETWEEN mytask_submit_start's own check and receive_proof actually inserting.
    asyncio.run(db.create_submission(task_id, DELEGATE_ID, "text", "raced in first", "2026-08-14 10:00:00"))

    bot = FakeBot()
    message = FakeMessage(text="моя попытка")
    asyncio.run(ua_mod.receive_proof(message, bot, state))

    assert asyncio.run(state.get_state()) is None
    assert "Уже отправлено" in message.answers[-1][0]
    assert bot.sent == []  # no manager notification on a lost race


def test_cancel_game_submit_clears_state(tmp_path):
    _db_ready(tmp_path)
    _seed_delegate()
    task_id = _seed_task(deadline_at="2026-12-31 23:59:00")
    state, _callback = _start_submission(task_id)

    message = FakeMessage(text="Отмена")
    asyncio.run(ua_mod.cancel_game_submit(message, state))

    assert asyncio.run(state.get_state()) is None
    assert "отменено" in message.answers[-1][0].lower()
