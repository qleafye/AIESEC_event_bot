"""Приёмка 09.10: делегат отправил анкету в чате, потом пишет боту «когда ответ» — тишина.

`reg_already_submitted_text` отвечал только из `RegHandoffGuard` (состояние `Registration:*`),
а после подачи из чата FSM очищен — текст не ловил ни один хендлер. Последняя точка
текстового пайплайна (`user_actions.reg_handoff_idle_fallback`) теперь отвечает делегату с
поданной и ещё не рассмотренной заявкой.

Фейки — из tests/test_reg_handoff_260904.py."""
import asyncio

from config import config
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
from tests.test_reg_handoff_260904 import USER_ID, _FakeMessage2, _ready, _texts2

SUBMITTED_TEXT = SETTINGS_SCHEMA["reg_already_submitted_text"]["default"]


def _seed_user(status: str, season: str = "YL 26/2", registration_date: str = "2026-10-09 12:00:00"):
    async def go():
        await db.set_setting("event_season", "YL 26/2")
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, registration_date, season, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (USER_ID, "Иванов Иван", registration_date, season, status),
            )
            await conn.commit()
    asyncio.run(go())


def _send(text="когда ответ"):
    from handlers.user_actions import reg_handoff_idle_fallback
    msg = _FakeMessage2(USER_ID, text=text)
    asyncio.run(reg_handoff_idle_fallback(msg))
    return _texts2(msg)


def test_pending_delegate_free_text_gets_submitted_reply(tmp_path):
    _ready(tmp_path)
    _seed_user("pending")
    assert _send() == [SUBMITTED_TEXT]


def test_no_row_still_silent(tmp_path):
    _ready(tmp_path)
    assert _send() == []


def test_approved_delegate_not_told_application_is_pending(tmp_path):
    _ready(tmp_path)
    _seed_user("approved")
    assert SUBMITTED_TEXT not in _send()


def test_previous_season_row_not_treated_as_submitted(tmp_path):
    _ready(tmp_path)
    _seed_user("pending", season="YL 26/1")
    assert _send() == []


def test_staff_gets_no_delegate_reply(tmp_path):
    _ready(tmp_path)
    _seed_user("pending")
    saved = list(config.ADMIN_IDS)
    config.ADMIN_IDS = [USER_ID]
    try:
        assert _send() == []
    finally:
        config.ADMIN_IDS = saved
