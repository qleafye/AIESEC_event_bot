"""Автоочистка служебных уведомлений под нагрузкой: массовое вступление по ссылке.

Ссылку на чат рассылают в письме об одобрении — вступления приходят сотнями в ту же минуту,
что и сама рассылка. Удаление не должно превращаться в сотни одновременных вызовов API:
на чат — не больше одного вызова за паузу, накопленное уходит пачкой; 429 — ждём и
повторяем. Отложенное удаление — очередь в БД и одна джоба, а не джоба на сообщение; перед
удалением очередь перепроверяет галочку типа и привязку чата.
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from config import config
from database import db
from services import chat_cleanup
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db

CHAT = -1009280101
OTHER_CHAT = -1009280102


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, monkeypatch, *, types_="join\nleave", pause=0.05):
    config.DB_PATH = str(tmp_path / "ratelimit.db")
    config.ADMIN_IDS = [1]
    fast_init_db()
    chat_cleanup._warned.clear()
    chat_cleanup._lanes.clear()
    monkeypatch.setattr(chat_cleanup, "DELETE_PAUSE_SECONDS", pause)
    _run(db.set_setting("delegate_chat_id", str(CHAT)))
    _run(db.set_setting("delegate_chat_title", "Делегаты"))
    if types_ is not None:
        _run(db.set_setting(chat_cleanup.TYPES_KEY, types_))


class _BatchBot:
    """Фейк с deleteMessages: пишет каждый вызов API и время вызова."""

    def __init__(self, fail_first: Exception | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.times: list[float] = []
        self.fail_first = fail_first

    def _record(self, name, ids):
        self.times.append(time.monotonic())
        if self.fail_first is not None:
            error, self.fail_first = self.fail_first, None
            raise error
        self.calls.append((name, tuple(ids)))

    async def delete_message(self, chat_id, message_id):
        self._record("one", [message_id])
        return True

    async def delete_messages(self, chat_id, message_ids):
        self._record("many", message_ids)
        return True

    def deleted(self):
        return sorted(mid for _name, ids in self.calls for mid in ids)


class _RetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__(f"Telegram server says - Too Many Requests: retry after {seconds}")
        self.retry_after = seconds


def test_burst_of_joins_is_batched_and_paced(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    bot = _BatchBot()

    async def burst():
        await asyncio.gather(*(
            chat_cleanup.handle_service_message(bot, CHAT, mid, "join") for mid in range(1, 41)
        ))

    _run(burst())
    assert bot.deleted() == list(range(1, 41))
    assert len(bot.calls) < 10  # не 40 одновременных вызовов
    assert any(name == "many" and len(ids) > 1 for name, ids in bot.calls)
    gaps = [b - a for a, b in zip(bot.times, bot.times[1:])]
    assert all(gap >= chat_cleanup.DELETE_PAUSE_SECONDS * 0.8 for gap in gaps), gaps


def test_retry_after_waits_and_retries(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    bot = _BatchBot(fail_first=_RetryAfter(0))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 7, "join"))
    assert bot.deleted() == [7]
    assert len(bot.times) == 2  # первая попытка — 429, вторая прошла


def test_retry_after_recognised_by_text(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    bot = _BatchBot(fail_first=Exception("Too Many Requests: retry after 0"))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 8, "join"))
    assert bot.deleted() == [8]


async def _queue_rows():
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT chat_id, message_id FROM chat_cleanup_queue ORDER BY message_id"
        ) as cur:
            return await cur.fetchall()


def _delay(seconds=30):
    _run(db.set_setting(chat_cleanup.DELAY_KEY, str(seconds)))


def test_delayed_notices_drain_in_one_batch_after_due(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    _delay(30)
    bot = _BatchBot()
    for mid in (21, 22, 23):
        _run(chat_cleanup.handle_service_message(bot, CHAT, mid, "join"))
    assert bot.calls == [] and len(_run(_queue_rows())) == 3

    assert _run(chat_cleanup.drain_queue(bot, now=msk_now())) == 0  # срок не наступил
    assert bot.calls == []

    later = msk_now() + timedelta(seconds=31)
    assert _run(chat_cleanup.drain_queue(bot, now=later)) == 3
    assert bot.calls == [("many", (21, 22, 23))]
    assert _run(_queue_rows()) == []


def test_drain_rechecks_ticked_type(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    _delay(30)
    bot = _BatchBot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 31, "join"))
    _run(chat_cleanup.handle_service_message(bot, CHAT, 32, "leave"))
    _run(db.set_setting(chat_cleanup.TYPES_KEY, "leave"))  # «вступил(а)» сняли за задержку
    _run(chat_cleanup.drain_queue(bot, now=msk_now() + timedelta(seconds=31)))
    assert bot.deleted() == [32]
    assert _run(_queue_rows()) == []  # снятое тоже ушло из очереди


def test_drain_rechecks_chat_binding(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    _delay(30)
    bot = _BatchBot()
    _run(chat_cleanup.handle_service_message(bot, CHAT, 41, "join"))
    _run(db.delete_setting("delegate_chat_id"))  # чат отвязали за задержку
    _run(chat_cleanup.drain_queue(bot, now=msk_now() + timedelta(seconds=31)))
    assert bot.calls == []
    assert _run(_queue_rows()) == []


def test_drain_skips_notices_older_than_telegram_allows(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    old = (msk_now() - timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S")

    async def _old_row():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO chat_cleanup_queue (chat_id, message_id, code, due_at, created_at) "
                "VALUES (?, ?, 'join', ?, ?)", (CHAT, 51, old, old),
            )
            await conn.commit()

    _run(_old_row())
    bot = _BatchBot()
    _run(chat_cleanup.drain_queue(bot))
    assert bot.calls == []
    assert _run(_queue_rows()) == []


def test_drain_job_is_one_interval_job(tmp_path, monkeypatch):
    import services.scheduler as sched

    config.DB_PATH = str(tmp_path / "drain_sched.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)

    async def go():
        fast_init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            job = s.get_job("chat_cleanup_drain")
            assert job is not None
            assert job.func is sched.chat_cleanup_drain_job
            assert job.trigger.interval.total_seconds() <= 60
        finally:
            s.shutdown(wait=False)

    _run(go())
