"""Приёмка 09.10 (D3): отклонённый в ТЕКУЩЕМ сезоне на /start получал приветствие
возвращенца «Ты уже был(а) с нами на Yl-test-2» — с текущим сезоном, да ещё кодом.

Теперь баннер возвращенца — только для заявки из ПРОШЛОГО сезона; отклонённому в этом сезоне —
свой экран «заявка отклонена, можно подать заново» с той же кнопкой «🚀 Обновить анкету»
(если повторная подача разрешена) или текст закрытия (если запрещена). Сезон в этом экране не
называется вовсе.
"""
import asyncio

from aiogram.types import InlineKeyboardMarkup

from database import db
from handlers import registration as reg
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db
from tests.test_returning_delegate_073 import (
    UID, FakeCommand, _KBCapturingMessage, _callback_datas, _new_state, _register, _texts,
    _use_tmp_db,
)


def _rereg_buttons(msg):
    return [
        rm for (_, rm, _) in msg.sent
        if isinstance(rm, InlineKeyboardMarkup) and "rereg_start" in _callback_datas(rm)
    ]


async def _start(uid=UID):
    msg = _KBCapturingMessage(uid, "delegate")
    await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand(None))
    return msg


def test_rejected_current_season_gets_rejected_screen_not_returning(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_season", "Yl-test-2")
        await _register(UID, "delegate", status="rejected", season="Yl-test-2")

        msg = await _start()
        texts = [t or "" for t in _texts(msg)]
        joined = "\n".join(texts)
        assert "Yl-test-2" not in joined, joined
        assert "был(а) с нами" not in joined, joined
        assert SETTINGS_SCHEMA["start_text_rejected"]["default"] in texts, joined
        assert len(_rereg_buttons(msg)) == 1

    asyncio.run(go())


def test_rejected_without_season_module_gets_rejected_screen(tmp_path):
    """Сезон не настроен — отклонённый всё равно «этого» сезона, не возвращенец."""
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await _register(UID, "delegate", status="rejected", season=None)

        msg = await _start()
        joined = "\n".join(t or "" for t in _texts(msg))
        assert "прошлом событии" not in joined, joined
        assert "был(а) с нами" not in joined, joined
        assert SETTINGS_SCHEMA["start_text_rejected"]["default"] in joined
        assert len(_rereg_buttons(msg)) == 1

    asyncio.run(go())


def test_rejected_current_season_custom_text(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_season", "YL'26")
        await db.set_setting("start_text_rejected", "Отказ, но <b>можно</b> снова")
        await _register(UID, "delegate", status="rejected", season="YL'26")

        msg = await _start()
        assert "Отказ, но <b>можно</b> снова" in _texts(msg)
        assert len(_rereg_buttons(msg)) == 1

    asyncio.run(go())


def test_rejected_current_season_resubmit_denied_gets_closed_text(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_season", "YL'26")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await _register(UID, "delegate", status="rejected", season="YL'26")

        msg = await _start()
        joined = "\n".join(t or "" for t in _texts(msg))
        assert SETTINGS_SCHEMA["reg_resubmit_closed_text"]["default"] in joined
        assert SETTINGS_SCHEMA["start_text_rejected"]["default"] not in joined
        assert "был(а) с нами" not in joined
        assert _rereg_buttons(msg) == []

    asyncio.run(go())


def test_rejected_past_season_still_gets_returning_banner(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="rejected", season="YL'25")

        msg = await _start()
        joined = "\n".join(t or "" for t in _texts(msg))
        assert "был(а) с нами на YL'25" in joined, joined
        assert SETTINGS_SCHEMA["start_text_rejected"]["default"] not in joined
        assert len(_rereg_buttons(msg)) == 1

    asyncio.run(go())


def test_approved_past_season_still_gets_returning_banner(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        fast_init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'25")

        msg = await _start()
        joined = "\n".join(t or "" for t in _texts(msg))
        assert "был(а) с нами на YL'25" in joined, joined
        assert len(_rereg_buttons(msg)) == 1

    asyncio.run(go())
