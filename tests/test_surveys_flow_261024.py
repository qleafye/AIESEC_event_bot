"""Анкета из нескольких вопросов, сторона делегата: рассылка приглашения, закрытие, прохождение
всех типов вопросов кнопками, повторы и закрытая анкета.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from config import config
from database import db, survey_db
from services.comms import polls as polls_svc
from tests._dbtpl import fast_init_db

ADMIN_ID = 920001
U1 = 920011
U2 = 920012


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "flow.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _add_user(tid, status="approved"):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, participant_type) "
            "VALUES (?, ?, ?, ?, 'full')", (tid, f"U{tid}", f"@u{tid}", status),
        )
        await conn.commit()


async def _survey(questions=(("single", {}),), intro="Привет, пройди опрос", outro=None, status="scheduled"):
    pid = await survey_db.create_survey_draft("Анкета", ADMIN_ID)
    await survey_db.update_survey_texts(pid, intro=intro, outro=outro)
    qids = []
    for qtype, kw in questions:
        kw = dict(kw)
        if qtype in ("single", "multi") and "options" not in kw:
            kw["options"] = [{"t": "Да", "more": None}, {"t": "Нет", "more": None}]
        if qtype == "table" and "rows" not in kw:
            kw["rows"] = ["Еда", "Зал"]
        qids.append(await survey_db.add_survey_question(pid, qtype, f"Вопрос {qtype}", **kw))
    await db.set_poll_status(pid, status)
    return pid, qids


class FakeBot:
    def __init__(self):
        self.messages = []
        self.polls = []
        self.stopped = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=500 + len(self.messages))

    async def send_poll(self, *a, **kw):
        self.polls.append((a, kw))

    async def stop_poll(self, chat_id, message_id):
        self.stopped.append((chat_id, message_id))


# ── рассылка и закрытие ──────────────────────────────────────────────────────────────────────

def test_deliver_survey_sends_invites_only(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        await _add_user(U2)
        pid, _ = await _survey()
        bot = FakeBot()
        stats = await polls_svc.deliver_poll(bot, pid)
        return pid, bot, stats

    pid, bot, stats = _run(go())
    assert stats["sent"] == 2
    assert bot.polls == []
    assert len(bot.messages) == 2
    for _, text, kb in bot.messages:
        assert "Привет" in text
        assert kb.inline_keyboard[0][0].callback_data == f"sv:s:{pid}"
    assert _run(db.get_poll(pid))["status"] == "open"
    assert len(_run(db.list_poll_messages(pid))) == 2


def test_redeliver_does_not_duplicate(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        await polls_svc.deliver_poll(bot, pid)
        await db.set_poll_status(pid, "scheduled")
        stats = await polls_svc.deliver_poll(bot, pid)
        return bot, stats

    bot, stats = _run(go())
    assert len(bot.messages) == 1 and stats["skipped"] == 1


def test_deliver_survey_quiet_hours_queues_but_checkpoints(tmp_path, monkeypatch):
    _ready(tmp_path)
    from services.comms import quiet_hours

    async def fake_due(now, user_id):
        return datetime(2030, 1, 1, 9, 0)

    monkeypatch.setattr(quiet_hours, "defer_until", fake_due)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        stats = await polls_svc.deliver_poll(bot, pid)
        return pid, bot, stats

    pid, bot, stats = _run(go())
    assert bot.messages == [] and stats["queued"] == 1 and stats["sent"] == 0
    assert _run(survey_db.survey_recipient(pid, U1)) is True


def test_close_survey_does_not_stop_poll(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(U1)
        pid, _ = await _survey()
        bot = FakeBot()
        await polls_svc.deliver_poll(bot, pid)
        res = await polls_svc.close_poll(bot, pid)
        return pid, bot, res

    pid, bot, res = _run(go())
    assert res == (0, 0) and bot.stopped == []
    assert _run(db.get_poll(pid))["status"] == "closed"
