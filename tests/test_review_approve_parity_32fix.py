"""Фикс фазы 32 (CR-01/CR-02): паритет одобрения просроченной сдачи между ботом
(`handlers/admin_gamification.py::grev_approve`) и Mini App (`miniapp/routers/review.py::
review_approve`) — тот же `task_id` в строке `coins` (иначе баллы не попадают в рейтинг
волны, `database.db.sum_task_coins_for_wave`), тот же штраф за просрочку и одно и то же
место в рейтинге волны (`services.ambassador_waves.wave_rating`).

Харнесс: `config.DB_PATH` — общий для обоих путей в одном тестовом процессе (тот же приём,
что у `tests/test_game_late_penalty_32.py`), поверх него — HTTP-клиент Mini App
(`tests/test_miniapp_routes.py`)."""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db as bot_db
from handlers import admin_gamification
from services.ambassador_waves import wave_rating

from tests.test_miniapp_routes import GAME_MANAGER_ID, _cfg, _client, _hdr, _seed, _use_tmp_db

ADMIN_ID = 932970
DELEGATE_BOT = 932971
DELEGATE_WEB = 932972


def _run(coro):
    return asyncio.run(coro)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, user_id=ADMIN_ID, bot=None):
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False

    async def answer(self, text, parse_mode=None, reply_markup=None):
        pass

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


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _setup(tmp_path):
    db_path = _use_tmp_db(tmp_path, "review_approve_parity.db")
    config.ADMIN_IDS = [ADMIN_ID]
    _seed(
        staff=[(GAME_MANAGER_ID, "game_manager", None)],
        users=[(DELEGATE_BOT, "approved"), (DELEGATE_WEB, "approved")],
        settings={"game_late_penalty_percent": "30", "miniapp_enabled": "on"},
    )
    wave_id = _run(bot_db.create_wave("2026-08-01 00:00:00", "2026-08-31 23:59:59"))
    assert _run(bot_db.set_wave_state(wave_id, "active", expected_state="draft"))
    for tid in (DELEGATE_BOT, DELEGATE_WEB):
        _run(bot_db.set_ambassador_flag(tid, active=True, at="2026-07-01 00:00:00"))
    task_bot = _run(bot_db.create_task(
        "Пост со скрином", "Light", 100, "text", "2026-08-10 23:59:00", ADMIN_ID, wave_id=wave_id,
    ))
    task_web = _run(bot_db.create_task(
        "Пост со скрином", "Light", 100, "text", "2026-08-10 23:59:00", ADMIN_ID, wave_id=wave_id,
    ))
    sub_bot = _run(bot_db.create_submission(
        task_bot, DELEGATE_BOT, "text", "готово", submitted_at="2026-08-14 10:00:00",
    ))
    sub_web = _run(bot_db.create_submission(
        task_web, DELEGATE_WEB, "text", "готово", submitted_at="2026-08-14 10:00:00",
    ))
    return db_path, wave_id, sub_bot, sub_web


def test_bot_and_miniapp_approve_same_late_submission_the_same_way(tmp_path):
    db_path, wave_id, sub_bot, sub_web = _setup(tmp_path)

    # Бот-путь.
    callback = FakeCallback(f"grev_approve:{sub_bot}")
    _run(admin_gamification.grev_approve(callback, _new_state()))
    bot_submission = _run(bot_db.get_submission(sub_bot))

    # Mini App-путь — та же просрочка, тот же процент.
    client = _client(_cfg(db_path))
    resp = client.post(
        f"/app/api/review/{sub_web}/approve", json=None, headers=_hdr(GAME_MANAGER_ID),
    )
    assert resp.status_code == 200, resp.text
    web_body = resp.json()
    web_submission = _run(bot_db.get_submission(sub_web))

    # Оба одобрения дают ровно 70 из 100 (штраф 30%), не полную сумму.
    assert bot_submission["coins_awarded"] == 70
    assert web_body["coins"] == 70 and web_submission["coins_awarded"] == 70

    async def _ledger_row(user_id):
        async with bot_db._connect() as conn:
            import aiosqlite
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM coins WHERE user_id = ?", (user_id,),
            ) as cur:
                return dict(await cur.fetchone())

    bot_row = _run(_ledger_row(DELEGATE_BOT))
    web_row = _run(_ledger_row(DELEGATE_WEB))
    # Та же строка леджера: тот же delta, тот же source, оба несут task_id (иначе выпали бы
    # из рейтинга волны — CR-01).
    assert bot_row["delta"] == web_row["delta"] == 70
    assert bot_row["source"] == web_row["source"] == "task"
    assert bot_row["task_id"] is not None and web_row["task_id"] is not None

    # Одно и то же место в рейтинге волны — оба получили одинаковые баллы за одинаковую
    # просрочку, поэтому делят место.
    rating = {row["user_id"]: row for row in _run(wave_rating(wave_id))}
    assert rating[DELEGATE_BOT]["points"] == rating[DELEGATE_WEB]["points"] == 70
    assert rating[DELEGATE_BOT]["place"] == rating[DELEGATE_WEB]["place"]
