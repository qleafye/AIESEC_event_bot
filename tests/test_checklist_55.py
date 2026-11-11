"""Чек-лист менеджера после одобрения. Пусто — тишина. Повтор молчит.

pytest-asyncio нет — корутины через asyncio.run.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import config
from database import db
from handlers.reg import reg_schema
from services.registration import checklist_send
from tests._dbtpl import fast_init_db

UID = 550401


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "checklist.db")
    config.ADMIN_IDS = [1]
    fast_init_db()


async def _user():
    await db.add_user({
        "telegram_id": UID,
        "full_name": "Анна",
        "registration_date": "2026-11-01 00:00:00",
    })
    await db.set_user_status(UID, "approved")


class _Bot:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("нет сети")
        self.sent.append(("text", chat_id, text))

    async def send_document(self, chat_id, document, **kwargs):
        if self.fail:
            raise RuntimeError("нет сети")
        self.sent.append(("doc", chat_id, document))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append(("photo", chat_id, photo))


def test_checklist_empty_sends_nothing(tmp_path):
    _ready(tmp_path)
    _run(_user())
    bot = _Bot()
    _run(checklist_send.send_manager_checklist(bot, UID))
    assert bot.sent == []
    assert _run(db.checklist_was_sent(UID)) is False


def test_checklist_text_and_file_go_once(tmp_path):
    _ready(tmp_path)
    _run(_user())
    _run(db.set_setting("checklist_text", "Возьми то, что написал менеджер"))
    _run(db.set_setting("checklist_doc_file_id", "DOC-1"))
    bot = _Bot()
    _run(checklist_send.send_manager_checklist(bot, UID))
    assert ("doc", UID, "DOC-1") in bot.sent
    assert any(kind == "text" and "менеджер" in text for kind, _cid, text in bot.sent)
    assert _run(db.checklist_was_sent(UID)) is True
    bot.sent.clear()
    _run(checklist_send.send_manager_checklist(bot, UID))
    assert bot.sent == []


def test_checklist_failed_send_does_not_stick(tmp_path):
    _ready(tmp_path)
    _run(_user())
    _run(db.set_setting("checklist_text", "Текст менеджера"))
    _run(checklist_send.send_manager_checklist(_Bot(fail=True), UID))
    assert _run(db.checklist_was_sent(UID)) is False
    bot = _Bot()
    _run(checklist_send.send_manager_checklist(bot, UID))
    assert bot.sent
    assert _run(db.checklist_was_sent(UID)) is True


def test_checklist_resend_skips_and_approval_still_sends_letter(tmp_path):
    _ready(tmp_path)
    _run(_user())
    _run(db.set_setting("checklist_text", "Только после решения"))
    bot = _Bot()
    err = _run(reg_schema.send_completion_and_bonus(bot, UID, with_menu=False))
    assert err is None
    assert any("Только после решения" in item[2] for item in bot.sent if item[0] == "text")
    assert _run(db.checklist_was_sent(UID)) is True
    # Переотправка решения чек-лист не повторяет, даже если флаг снять.
    async def _clear():
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET checklist_sent = 0 WHERE telegram_id = ?", (UID,))
            await conn.commit()
    _run(_clear())
    bot.sent.clear()
    _run(reg_schema.resend_approve_text(bot, UID))
    assert not any("Только после решения" in item[2] for item in bot.sent if item[0] == "text")
    src = Path("services/registration/checklist_send.py").read_text(encoding="utf-8")
    assert "носк" not in src and "чемодан" not in src
    assert "notice_blocked_ids" not in src


def test_checklist_photo_when_no_document(tmp_path):
    _ready(tmp_path)
    _run(_user())
    _run(db.set_setting("checklist_photo_file_id", "PH-1"))
    bot = _Bot()
    _run(checklist_send.send_manager_checklist(bot, UID))
    assert ("photo", UID, "PH-1") in bot.sent
