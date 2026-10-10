"""UAT 09.10 (D1): «⏭ Пропустить» прячет заявку до нового входа в «📋 Заявки» (D-07: набор
пропущенных живёт в сессии), но экран конца очереди говорил «✅ Заявок нет» — менеджер решал,
что разбирать нечего, хотя пропущенная заявка ждёт. Теперь конец очереди при пропущенных
говорит, сколько их, и даёт кнопку «показать снова» (тот же вход «📋 Заявки», сбрасывающий
набор). То же — у очереди чеков.

pytest-asyncio в окружении нет — async через `asyncio.run()`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers.applications import admin_moderation as mod
from tests._dbtpl import fast_init_db

ADMIN = 920261091


class _Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "skip_queue.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


def _seed(tid):
    asyncio.run(db.add_user({"telegram_id": tid, "full_name": f"User {tid}",
                             "registration_date": f"2026-01-01 09:{tid % 60:02d}:00"}))
    asyncio.run(db.set_user_status(tid, "pending"))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_all_pending_skipped_says_so_and_offers_to_show_again(tmp_path):
    _ready(tmp_path)
    _seed(11)
    state = _state()
    asyncio.run(state.update_data(appr_skipped=[11]))
    target = _Msg()
    asyncio.run(mod._show_current_card(target, state))
    text, kb = target.answers[-1]
    assert "Заявок нет" not in text
    assert "Пропущено: 1" in text
    first = kb.inline_keyboard[0][0]
    assert first.text == "🔁 Показать пропущенные (1)"
    assert first.callback_data == "admin_applications"  # тот же вход, что сбрасывает набор


def test_truly_empty_queue_unchanged(tmp_path):
    _ready(tmp_path)
    state = _state()
    asyncio.run(state.update_data(appr_skipped=[11]))  # пропущенная уже решена другим
    target = _Msg()
    asyncio.run(mod._show_current_card(target, state))
    text, kb = target.answers[-1]
    assert text == "✅ Заявок нет."
    assert "admin_applications" not in _cbs(kb)


def test_receipts_queue_skipped_offers_to_show_again(tmp_path):
    _ready(tmp_path)
    _seed(12)
    asyncio.run(db.update_payment_status(12, "receipt_sent"))
    state = _state()
    asyncio.run(state.update_data(rcpt_skipped=[12]))
    target = _Msg()
    asyncio.run(mod._show_current_receipt_card(target, state))
    text, kb = target.answers[-1]
    assert "Чеков на проверке нет" not in text
    assert "Пропущено: 1" in text
    assert kb.inline_keyboard[0][0].callback_data == "admin_receipts"
