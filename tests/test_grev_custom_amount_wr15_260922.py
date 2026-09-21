"""Ревизия 32-FIX (фиксер 3, находка WR-15) — `handlers/admin_gamification.py::
grev_approve_custom_start`/`grev_approve_amount_step`:

штраф за просрочку применяется и к сумме, введённой менеджером вручную («своя сумма» — как раз
инструмент для ручного решения по просроченной сдаче). Подсказка об этом теперь есть ДО ввода
числа, а после одобрения менеджер видит точные числа, а не голое «Одобрено.».

Handlers called DIRECTLY with Fake message/callback doubles (pytest-asyncio недоступен) — та же
конвенция, что у tests/test_game_late_penalty_32.py.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin_gamification

ADMIN_ID = 932001
DELEGATE_ID = 932002


def _run(coro):
    return asyncio.run(coro)


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _db_ready(tmp_path, name="grev_custom_amount.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_task(coins=100, deadline_at="2026-08-10 23:59:00"):
    return _run(db.create_task("Пост со скрином", "Light", coins, "text", deadline_at, ADMIN_ID))


def _seed_submission(task_id, submitted_at):
    return _run(db.create_submission(task_id, DELEGATE_ID, "text", "вот мой пост", submitted_at))


def test_grev_approve_custom_start_hints_penalty_when_late(tmp_path):
    _db_ready(tmp_path, "grev_custom_amount_a.db")
    _run(db.set_setting("game_late_penalty_percent", "30"))
    task_id = _seed_task()
    sub_id = _seed_submission(task_id, "2026-08-14 10:00:00")  # после дедлайна
    cb = FakeCallback(f"grev_approve_custom:{sub_id}")
    _run(admin_gamification.grev_approve_custom_start(cb, _new_state()))
    prompt = cb.message.answers_sent[-1]
    assert "штраф" in prompt.lower()


def test_grev_approve_custom_start_no_hint_when_not_late(tmp_path):
    _db_ready(tmp_path, "grev_custom_amount_b.db")
    _run(db.set_setting("game_late_penalty_percent", "30"))
    task_id = _seed_task()
    sub_id = _seed_submission(task_id, "2026-08-05 10:00:00")  # до дедлайна
    cb = FakeCallback(f"grev_approve_custom:{sub_id}")
    _run(admin_gamification.grev_approve_custom_start(cb, _new_state()))
    prompt = cb.message.answers_sent[-1]
    assert "штраф" not in prompt.lower()


def test_grev_approve_amount_step_message_shows_final_numbers_when_penalized(tmp_path):
    """Сценарий WR-15: штраф 30%, менеджер решил дать 50 за опоздавшую сдачу — ответ называет
    точные числа, а не голое «Одобрено.»."""
    _db_ready(tmp_path, "grev_custom_amount_c.db")
    _run(db.set_setting("game_late_penalty_percent", "30"))
    task_id = _seed_task()
    sub_id = _seed_submission(task_id, "2026-08-14 10:00:00")
    state = _new_state()
    _run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    msg = FakeMessage(text="50")
    _run(admin_gamification.grev_approve_amount_step(msg, state))
    # answers_sent[-1] — уже следующий экран очереди («Сдач на проверке нет.»); сама
    # реплика об одобрении — предпоследняя.
    reply = msg.answers_sent[-2]
    assert "35" in reply and "50" in reply
    assert "штраф" in reply.lower()
    submission = _run(db.get_submission(sub_id))
    assert submission["coins_awarded"] == 35


def test_grev_approve_amount_step_plain_message_when_not_penalized(tmp_path):
    _db_ready(tmp_path, "grev_custom_amount_d.db")
    _run(db.set_setting("game_late_penalty_percent", "30"))
    task_id = _seed_task()
    sub_id = _seed_submission(task_id, "2026-08-05 10:00:00")  # до дедлайна — без штрафа
    state = _new_state()
    _run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))
    msg = FakeMessage(text="50")
    _run(admin_gamification.grev_approve_amount_step(msg, state))
    assert msg.answers_sent[-2] == "Одобрено."
