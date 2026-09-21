"""Phase 32 Plan 07 (D-14/D-25/D-27/D-35): штраф за просрочку в обеих точках одобрения сдачи,
ссылка на задание в журнале монет, итог на карточке проверки заранее и менеджерский показ
срока через общий помощник.

Конвенция с tests/test_gamification_review_phase9.py: хендлеры вызываются напрямую с Fake
message/callback-двойниками, pytest-asyncio недоступен -- всё через asyncio.run(), БД -- tmp_path
файл.
"""
import asyncio

import aiosqlite

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_gamification
from handlers.game_review_render import _render_submission_card

ADMIN_ID = 932901
DELEGATE_ID = 932902


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_late_penalty_32.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_task(text="Пост со скрином", category="Light", coins=100, proof_type="text",
                deadline_at="2026-08-25 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _seed_submission(task_id, user_id=DELEGATE_ID, content_type="text", content="вот мой пост",
                      submitted_at="2026-08-14 10:00:00"):
    return asyncio.run(db.create_submission(task_id, user_id, content_type, content, submitted_at))


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _set(key, value):
    asyncio.run(db.set_setting(key, value))


async def _coin_rows_for(user_id):
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM coins WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.text = text

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

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


# ── Task 1: штраф в обеих точках одобрения + ссылка на задание ────────────────────────────

def test_grev_approve_late_submission_penalized_30_percent(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task_id = _seed_task(coins=100, deadline_at="2026-08-10 23:59:00")
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve(callback, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 70
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 70


def test_grev_approve_amount_step_late_submission_penalized_30_percent(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task_id = _seed_task(coins=100, deadline_at="2026-08-10 23:59:00")
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    state = _new_state()
    asyncio.run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    amount_msg = FakeMessage(text="100")
    asyncio.run(admin_gamification.grev_approve_amount_step(amount_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 70
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 70


def test_grev_approve_amount_step_custom_200_late_becomes_140(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task_id = _seed_task(coins=100, deadline_at="2026-08-10 23:59:00")
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    state = _new_state()
    asyncio.run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    amount_msg = FakeMessage(text="200")
    asyncio.run(admin_gamification.grev_approve_amount_step(amount_msg, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 140
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 140


def test_grev_approve_on_time_submission_not_penalized(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task_id = _seed_task(coins=100, deadline_at="2026-08-25 23:59:00")
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve(callback, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 100
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 100


def test_grev_approve_zero_percent_matches_pre_phase_amount_and_notification(tmp_path):
    """T-32-07: процент 0 (дефолт на каждом живом событии) -- число И текст уведомления как
    раньше, ни байта разницы, даже когда сдача опоздала."""
    _db_ready(tmp_path)
    task_id = _seed_task(text="Задание Х", coins=100, deadline_at="2026-08-10 23:59:00")
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve(callback, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 100
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 100
    chat_id, text = callback.bot.sent[0]
    assert text == "✅ Задание «Задание Х» одобрено! +100🪙"


def test_grev_approve_task_without_deadline_never_penalized(tmp_path):
    _db_ready(tmp_path)
    _set("game_late_penalty_percent", "30")
    task_id = _seed_task(coins=100, deadline_at=db.NO_DEADLINE_AT)
    sub_id = _seed_submission(task_id, submitted_at="2026-08-14 10:00:00")

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve(callback, state))

    submission = asyncio.run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 100
    assert asyncio.run(db.get_balance(DELEGATE_ID)) == 100


def test_grev_approve_coins_row_has_task_id(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=30)
    sub_id = _seed_submission(task_id)

    callback = FakeCallback(f"grev_approve:{sub_id}")
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve(callback, state))

    rows = asyncio.run(_coin_rows_for(DELEGATE_ID))
    assert len(rows) == 1
    assert rows[0]["task_id"] == task_id


# ── Task 2: карточка проверки показывает итог заранее ─────────────────────────────────────

def _submission_row(task_coins=100, deadline_at="2026-08-10 23:59:00",
                     submitted_at="2026-08-14 10:00:00"):
    return {
        "task_title": None, "task_text": "Задание", "task_category": "Light",
        "task_coins": task_coins, "task_proof_type": "text", "user_full_name": "Дельгат",
        "user_username": "delegate1", "content_type": "text", "content": "пост",
        "task_archived_at": None, "submitted_at": submitted_at, "task_deadline_at": deadline_at,
    }


def test_review_card_shows_penalized_total_when_late_and_percent_set():
    row = _submission_row()
    text = _render_submission_card(row, 1, 1, penalty_percent=30)
    assert "70" in text and "100" in text
    assert "штраф" in text


def test_review_card_zero_percent_matches_prior_render_byte_for_byte():
    row = _submission_row()
    baseline = _render_submission_card(row, 1, 1)
    zero_percent = _render_submission_card(row, 1, 1, penalty_percent=0)
    assert zero_percent == baseline
    assert "штраф" not in zero_percent


def test_review_card_on_time_submission_has_no_penalty_line():
    row = _submission_row(deadline_at="2026-08-25 23:59:00")
    text = _render_submission_card(row, 1, 1, penalty_percent=30)
    assert "штраф" not in text
    assert "после дедлайна" not in text


def test_review_card_no_deadline_task_has_neither_badge_nor_penalty_line():
    # NO_DEADLINE_AT ("9999-12-31 23:59:59") строкового сравнения "после дедлайна" никогда не
    # проходит (любая настоящая сдача раньше этой метки) — бейдж и строка штрафа не появляются.
    row = _submission_row(deadline_at=db.NO_DEADLINE_AT)
    text = _render_submission_card(row, 1, 1, penalty_percent=30)
    assert "штраф" not in text
    assert "после дедлайна" not in text


def test_grev_approve_amount_step_coins_row_has_task_id(tmp_path):
    _db_ready(tmp_path)
    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_gamification.grev_approve_amount_step(amount_msg, state))

    rows = asyncio.run(_coin_rows_for(DELEGATE_ID))
    assert len(rows) == 1
    assert rows[0]["task_id"] == task_id
