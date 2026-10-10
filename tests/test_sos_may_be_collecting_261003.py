"""`services.sos.may_be_collecting` — признак «делегат, возможно, дописывает SOS» для рассылок
из планировщика: им нельзя присылать главное меню, оно заменит клавиатуру «Готово»."""
import asyncio
from datetime import timedelta

from config import config
from database import db
from services import sos as sos_service
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

DELEGATE_ID = 904010


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "sos_collecting.db")
    fast_init_db()


async def _age_report(rid: int, minutes: int) -> None:
    stamp = (msk_now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with db._connect() as conn:
        await conn.execute("UPDATE sos_reports SET created_at = ? WHERE id = ?", (stamp, rid))
        await conn.commit()


def test_fresh_open_report_counts_as_collecting(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.create_sos_report(DELEGATE_ID, None)
        return await sos_service.may_be_collecting(DELEGATE_ID)

    assert asyncio.run(go()) is True


def test_no_report_or_resolved_or_stale_is_not_collecting(tmp_path):
    _ready(tmp_path)

    async def go():
        nothing = await sos_service.may_be_collecting(DELEGATE_ID)
        rid = await db.create_sos_report(DELEGATE_ID, None)
        await _age_report(rid, sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES + 1)
        stale = await sos_service.may_be_collecting(DELEGATE_ID)
        rid2 = await db.create_sos_report(DELEGATE_ID, None)
        await db.resolve_sos_report(rid2, 1, "Орг")
        await db.resolve_sos_report(rid, 1, "Орг")
        resolved = await sos_service.may_be_collecting(DELEGATE_ID)
        return nothing, stale, resolved

    assert asyncio.run(go()) == (False, False, False)


def test_reopened_old_report_counts_from_reentry(tmp_path):
    """Старая открытая заявка, в дозапись которой делегат вернулся повторным «🆘 SOS»: таймер
    дозаписи перезапущен, значит и утренний QR не должен заменить ему клавиатуру «Готово»."""
    _ready(tmp_path)

    async def go():
        rid = await db.create_sos_report(DELEGATE_ID, None)
        await _age_report(rid, sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES + 30)
        before = await sos_service.may_be_collecting(DELEGATE_ID)
        await db.mark_sos_collecting_started(rid)
        after = await sos_service.may_be_collecting(DELEGATE_ID)
        return before, after

    assert asyncio.run(go()) == (False, True)


def test_enter_collecting_records_start_in_db(tmp_path):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers.forum import sos as sos_handlers

    _ready(tmp_path)

    async def go():
        rid = await db.create_sos_report(DELEGATE_ID, None)
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=DELEGATE_ID, user_id=DELEGATE_ID))
        await sos_handlers._enter_collecting(state, rid, None)
        return (await db.get_sos_report(rid))["collecting_started_at"]

    assert asyncio.run(go())
