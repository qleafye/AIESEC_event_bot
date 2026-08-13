"""Phase 9 Plan 04 (GAME-02/03, D-01, A-04/A-05) — «🎮 Проверка заданий» moderation queue.

Handlers are called DIRECTLY with Fake message/callback doubles (same convention as
tests/test_gamification_admin_phase9.py / tests/test_gamification_delegate_phase9.py). The
approve/reject paths need a `.bot` double on both the message and the callback (delegate
notification, A-02) and file-resend calls (`answer_photo`/`answer_document`), which the wave-2
doubles didn't need -- extended here, not reused verbatim.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run() and
config.DB_PATH points at a tmp_path file, same convention as every other phase-9 test file.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability
from handlers.states import GameReview


ADMIN_ID = 930901
MANAGER2_ID = 930904
DELEGATE_ID = 930902


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_review_phase9.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_task(text="Пост со скрином #знакомство", category="Light", coins=30,
               proof_type="text", deadline_at="2026-08-25 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _seed_submission(task_id, user_id=DELEGATE_ID, content_type="text", content="вот мой пост",
                      submitted_at="2026-08-14 10:00:00"):
    return asyncio.run(db.create_submission(task_id, user_id, content_type, content, submitted_at))


def _new_state(uid=ADMIN_ID) -> FSMContext:
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
    """Stand-in for both `callback.message` (delete/answer/answer_photo/answer_document) and a
    dispatched message event (text + answer + a `.bot` double for delegate notification)."""

    def __init__(self, text=None, user_id=ADMIN_ID, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []
        self.answer_parse_modes = []
        self.photos_sent = []
        self.documents_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_parse_modes.append(parse_mode)
        self.text = text

    async def answer_photo(self, photo):
        self.photos_sent.append(photo)

    async def answer_document(self, document):
        self.documents_sent.append(document)

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


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


# ── Task 1: queue — card + pagination + skip ────────────────────────────────────────────────

def test_admin_game_review_key_maps_to_moderate_game(tmp_path):
    _db_ready(tmp_path)
    assert required_capability(callback_data="admin_game_review") == "moderate_game"


def test_admin_game_review_empty_queue_shows_no_pending_text(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))
    assert callback.message.answers_sent[-1] == "Сдач на проверке нет."


def test_show_current_submission_card_renders_task_and_submitter_and_content_type(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(text="Знакомство <b>тест</b>", category="Light", coins=30, proof_type="link")
    asyncio.run(db.add_user({"telegram_id": DELEGATE_ID, "full_name": "Дельгат Тестов",
                              "username": "delegate1", "registration_date": "2026-08-01"}))
    _seed_submission(task_id, content_type="link", content="https://example.com/post")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))

    text = callback.message.answers_sent[-1]
    assert "&lt;b&gt;тест&lt;/b&gt;" in text  # task text escaped
    assert "Light" in text
    assert "30🪙" in text
    assert "Дельгат Тестов" in text and "delegate1" in text
    assert "https://example.com/post" in text  # link content visible in the card
    kb = callback.message.answer_markups[-1]
    data = _flat_callback_data(kb)
    assert any(d.startswith("grev_approve:") for d in data)
    assert any(d.startswith("grev_approve_custom:") for d in data)
    assert any(d.startswith("grev_reject:") for d in data)
    assert any(d.startswith("grev_skip:") for d in data)
    assert not any(d == "grev_all" for d in data)  # no "approve all" (each needs real review)
    assert callback.message.photos_sent == []
    assert callback.message.documents_sent == []


def test_show_current_submission_card_photo_resends_file_separately(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(proof_type="photo")
    _seed_submission(task_id, content_type="photo", content="file_abc123")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))

    text = callback.message.answers_sent[-1]
    assert "file_abc123" not in text
    assert "см. файл ниже" in text
    assert callback.message.photos_sent == ["file_abc123"]


def test_show_current_submission_card_pdf_resends_document_separately(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(proof_type="pdf")
    _seed_submission(task_id, content_type="pdf", content="doc_xyz")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))

    assert callback.message.documents_sent == ["doc_xyz"]


def test_late_submission_card_shows_deadline_warning(tmp_path):
    _db_ready(tmp_path)
    late_task = _seed_task(text="Просрочено", deadline_at="2026-08-10 23:59:00")
    _seed_submission(late_task, content_type="text", content="late", submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))
    text = callback.message.answers_sent[-1]
    assert "после дедлайна" in text
    assert "2026-08-10 23:59:00" in text


def test_on_time_submission_card_has_no_deadline_warning(tmp_path):
    _db_ready(tmp_path)
    on_time_task = _seed_task(text="В срок", deadline_at="2026-08-25 23:59:00")
    _seed_submission(on_time_task, content_type="text", content="ok", submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))
    text = callback.message.answers_sent[-1]
    assert "после дедлайна" not in text


def test_grev_skip_advances_to_next_and_is_session_only(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task()
    sub1 = _seed_submission(task_id, user_id=DELEGATE_ID, content="первая", submitted_at="2026-08-14 09:00:00")
    sub2 = _seed_submission(task_id, user_id=DELEGATE_ID + 1, content="вторая", submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))
    assert "первая" in callback.message.answers_sent[-1]

    skip_cb = FakeCallback(f"grev_skip:{sub1}")
    asyncio.run(admin_mod.grev_skip(skip_cb, state))
    assert skip_cb.message.deleted
    assert "вторая" in skip_cb.message.answers_sent[-1]
    assert asyncio.run(state.get_data())["grev_skipped"] == [sub1]

    # Reopening the screen resets the skip set (same as appr_skipped / D-07).
    reopen_cb = FakeCallback("admin_game_review")
    asyncio.run(admin_mod.show_game_review(reopen_cb, state))
    assert "первая" in reopen_cb.message.answers_sent[-1]


def test_pagination_uses_get_pending_submissions_limit_offset(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task()
    for i in range(3):
        _seed_submission(task_id, user_id=DELEGATE_ID + i, content=f"сдача {i}",
                          submitted_at=f"2026-08-14 0{i}:00:00")

    callback = FakeCallback("admin_game_review")
    state = _new_state()
    asyncio.run(admin_mod.show_game_review(callback, state))
    assert "1/3" in callback.message.answers_sent[-1]


# ── Task 2: approve (default/custom amount) / reject — atomic claim + credit + notify ──────

def test_grev_approve_default_amount_credits_task_coins(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=30)
    sub_id = _seed_submission(task_id)

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_mod.grev_approve(callback, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["status"] == "approved"
    assert submission["coins_awarded"] == 30
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 30
    assert callback.answers[0] == ("Одобрено", False)


def test_grev_approve_race_second_tap_does_not_double_credit(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=20)
    sub_id = _seed_submission(task_id)
    state = _new_state()

    asyncio.run(admin_mod.grev_approve(FakeCallback(f"grev_approve:{sub_id}"), state))
    second = FakeCallback(f"grev_approve:{sub_id}", user_id=MANAGER2_ID)
    asyncio.run(admin_mod.grev_approve(second, state))

    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 20  # credited exactly once
    assert second.answers[0] == ("Уже обработано", False)


def test_grev_approve_custom_amount_prompts_and_credits_entered_value(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()

    start_cb = FakeCallback(f"grev_approve_custom:{sub_id}")
    asyncio.run(admin_mod.grev_approve_custom_start(start_cb, state))
    assert asyncio.run(state.get_state()) == GameReview.approve_amount
    assert "5" in start_cb.message.answers_sent[-1]

    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_mod.grev_approve_amount_step(amount_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 45
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 45  # not the task's default (5)
    assert asyncio.run(state.get_state()) is None


def test_grev_approve_custom_amount_rejects_non_positive(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_mod.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))

    for bad in ("0", "-5", "abc"):
        msg = FakeMessage(text=bad)
        asyncio.run(admin_mod.grev_approve_amount_step(msg, state))
        assert asyncio.run(state.get_state()) == GameReview.approve_amount

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["status"] == "pending"  # claim_submission never called on bad input


def test_grev_approve_notifies_delegate_with_amount(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(text="Задание Х", coins=30)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_mod.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_mod.grev_approve_amount_step(amount_msg, state))

    assert len(amount_msg.bot.sent) == 1
    chat_id, text = amount_msg.bot.sent[0]
    assert chat_id == DELEGATE_ID
    assert "+45🪙" in text
    assert "Задание Х" in text


def test_grev_reject_prompts_reason_and_notifies_with_reason_escaped(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(text="Задание <i>Y</i>")
    sub_id = _seed_submission(task_id)
    state = _new_state()

    start_cb = FakeCallback(f"grev_reject:{sub_id}")
    asyncio.run(admin_mod.grev_reject_start(start_cb, state))
    assert asyncio.run(state.get_state()) == GameReview.reject_reason

    reason_msg = FakeMessage(text="плохо <b>видно</b> хэштег")
    asyncio.run(admin_mod.grev_reject_reason(reason_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["status"] == "rejected"
    assert submission["reject_reason"] == "плохо <b>видно</b> хэштег"
    assert len(reason_msg.bot.sent) == 1
    _chat_id, text = reason_msg.bot.sent[0]
    assert "&lt;i&gt;Y&lt;/i&gt;" in text
    assert "&lt;b&gt;видно&lt;/b&gt;" in text
    assert asyncio.run(state.get_state()) is None


def test_grev_reject_without_reason_omits_reason_line(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task()
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_mod.grev_reject_start(FakeCallback(f"grev_reject:{sub_id}"), state))

    reason_msg = FakeMessage(text="-")
    asyncio.run(admin_mod.grev_reject_reason(reason_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["reject_reason"] is None
    _chat_id, text = reason_msg.bot.sent[0]
    assert "Причина" not in text


def test_grev_reject_cancel_intercepted_before_catchall(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task()
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_mod.grev_reject_start(FakeCallback(f"grev_reject:{sub_id}"), state))

    cancel_msg = FakeMessage(text="/broadcast")
    asyncio.run(admin_mod.grev_step_cancel(cancel_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["status"] == "pending"  # claim_submission never called
    assert asyncio.run(state.get_state()) is None


def test_grev_approve_amount_cancel_intercepted_before_catchall(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_mod.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))

    cancel_msg = FakeMessage(text="Отмена")
    asyncio.run(admin_mod.grev_step_cancel(cancel_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["status"] == "pending"
    assert asyncio.run(state.get_state()) is None


def test_grev_review_out_of_queue_id_shows_already_processed(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=10)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    # First approve wins, flips status to 'approved' (a stale/re-rendered card, not a race).
    asyncio.run(admin_mod.grev_approve(FakeCallback(f"grev_approve:{sub_id}"), state))

    stale_cb = FakeCallback(f"grev_approve:{sub_id}")
    asyncio.run(admin_mod.grev_approve(stale_cb, state))
    assert stale_cb.answers[0] == ("Уже обработано", False)
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 10  # no second credit


def test_grev_approve_unknown_id_alerts_not_found(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    callback = FakeCallback("grev_approve:999999")
    asyncio.run(admin_mod.grev_approve(callback, state))
    assert callback.answers[0] == ("Не найдено", True)
