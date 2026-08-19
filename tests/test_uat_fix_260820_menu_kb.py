"""UAT 19.08 (a): выходы делегата из FSM не должны оставлять его без главного меню.

Раньше «Отмена» / «✅ Готово» в сдаче задания, финал регистрации и подтверждённая отмена
регистрации слали ReplyKeyboardRemove — reply-кнопки пропадали до следующего /start.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from config import config
from database import db
from handlers import registration  # noqa: F401  -- первым: registration сам подтягивает reg_flow/reg_steps в нужном порядке
from handlers import user_actions as ua_mod
from handlers import reg_flow
from handlers.states import GameSubmit, Registration

ADMIN_ID = 960901
DELEGATE_ID = 960902


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "uat_fix_menu.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Delegate", "registration_date": "2026-08-01",
    }))


class _User:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "Delegate"


class _Message:
    def __init__(self, text=None, uid=DELEGATE_ID):
        self.text = text
        self.from_user = _User(uid)
        self.photo = self.document = self.caption = self.media_group_id = None
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        pass


class _Callback:
    def __init__(self, data, uid=DELEGATE_ID):
        self.data = data
        self.from_user = _User(uid)
        # from_user у callback.message — «бот», не делегат: хендлер обязан брать callback.from_user
        self.message = _Message(uid=1)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _state(uid=DELEGATE_ID):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _is_menu(markup):
    return isinstance(markup, ReplyKeyboardMarkup) and not isinstance(markup, ReplyKeyboardRemove) \
        and len(markup.keyboard) > 0


def test_game_submit_cancel_returns_main_menu(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", ADMIN_ID))
    state = _state()
    asyncio.run(ua_mod.mytask_submit_start(_Callback(f"mytask_submit:{task_id}"), state))
    message = _Message(text="Отмена")
    asyncio.run(ua_mod.cancel_game_submit(message, state))
    assert asyncio.run(state.get_state()) is None
    assert _is_menu(message.answers[-1][1])


def test_game_submit_done_returns_main_menu(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", ADMIN_ID))
    state = _state()
    asyncio.run(ua_mod.mytask_submit_start(_Callback(f"mytask_submit:{task_id}"), state))
    asyncio.run(ua_mod.receive_proof(_Message(text="сдаю"), _Bot(), state))
    done_cb = _Callback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(done_cb, _Bot(), state))
    assert asyncio.run(state.get_state()) is None
    assert _is_menu(done_cb.message.answers[-1][1])


def test_game_submit_done_task_vanished_returns_main_menu(tmp_path):
    _db_ready(tmp_path)
    state = _state()
    asyncio.run(state.set_state(GameSubmit.proof))
    asyncio.run(state.update_data(gs_task_id=999999, gs_parts=[{"kind": "text", "content": "x", "caption": None}]))
    done_cb = _Callback("gs_done")
    asyncio.run(ua_mod.finalize_game_submission(done_cb, _Bot(), state))
    assert asyncio.run(state.get_state()) is None
    assert "больше не доступно" in done_cb.message.answers[-1][0]
    assert _is_menu(done_cb.message.answers[-1][1])


def test_registration_cancel_confirm_returns_main_menu(tmp_path):
    _db_ready(tmp_path)
    state = _state()
    asyncio.run(state.set_state(Registration.full_name))
    cb = _Callback("reg_cancel_yes")
    asyncio.run(reg_flow.cancel_registration_confirm(cb, state))
    assert asyncio.run(state.get_state()) is None
    assert "отменена" in cb.message.answers[-1][0]
    assert _is_menu(cb.message.answers[-1][1])


def test_no_reply_keyboard_remove_left_in_delegate_exits():
    """Единственное допустимое ReplyKeyboardRemove в делегатских хендлерах — шаг «резюме»
    (середина FSM, ответ = файл/текст, следующий шаг ставит свою клавиатуру)."""
    import inspect
    assert "ReplyKeyboardRemove()" not in inspect.getsource(ua_mod)
    assert "ReplyKeyboardRemove()" not in inspect.getsource(reg_flow)
    assert inspect.getsource(registration).count("ReplyKeyboardRemove()") == 1
    from handlers import payment
    assert "ReplyKeyboardRemove()" not in inspect.getsource(payment)
