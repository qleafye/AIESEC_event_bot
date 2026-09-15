"""Review 260817 §B2 (п.10): per-recipient checkpointing отложенных рассылок.

До фикса: клейм `pending → sending`, цикл `_safe_send`, `mark_broadcast_sent`. Крах в середине
цикла оставлял строку в `sending` навсегда (реконсиляция на буте реармила только `pending`) —
хвост аудитории терялся, админ пересылал вручную и рисковал задвоить начало списка.

После: каждая попытка отправки пишется в `scheduled_broadcast_deliveries` (ok | failed),
повторный прогон пропускает уже записанные чаты, а застрявшие `sending` старше
`_STALE_SENDING_MINUTES` реклеймятся на буте обратно в `pending`.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД и jobstore — tmp_path
(конвенция tests/test_scheduler_reconcile_block6.py).
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db
from services import scheduler as sched


def _isolate(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "ckpt.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


class _Bot:
    """Фейковый Bot: считает отправки, по желанию падает на заданных chat_id.

    Квик 260915-twr (Task B1): send_message теперь ДОЛЖЕН отдавать объект с .message_id —
    send_scheduled_broadcast читает его в журнал (record_broadcast_delivery)."""

    def __init__(self, fail_ids=()):
        self.sent = []
        self.fail_ids = set(fail_ids)
        self._next_id = 7000

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_ids:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self.sent.append(chat_id)
        self._next_id += 1
        from types import SimpleNamespace
        return SimpleNamespace(message_id=self._next_id)


def _patch_audience(monkeypatch, ids):
    async def fake_all():
        return list(ids)
    monkeypatch.setattr(db, "get_all_users_ids", fake_all)


async def _run_send(bid, bot):
    prev = sched._bot
    sched._bot = bot
    try:
        await sched.send_scheduled_broadcast(bid)
    finally:
        sched._bot = prev


def test_resume_after_crash_skips_already_delivered(tmp_path, monkeypatch):
    """(a) Крах после 2 из 5 → повторный прогон (после реклейма в pending) шлёт только хвост."""
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2, 3, 4, 5])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1
        )
        # Крах: _safe_send глотает ошибки отправки, поэтому обрыв цикла эмулируем исключением
        # вне него — из mark_delivery после второго чекпоинта.
        bot1 = _Bot()
        calls = {"n": 0}
        real_mark = db.mark_delivery

        async def mark_then_crash(b, c, ok):
            await real_mark(b, c, ok)
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("process died")
        monkeypatch.setattr(db, "mark_delivery", mark_then_crash)
        await _run_send(bid, bot1)  # send_scheduled_broadcast глотает исключение в лог
        assert bot1.sent == [1, 2]
        assert (await db.get_scheduled_broadcast(bid))["status"] == "sending"
        assert await db.list_delivered_chat_ids(bid) == {1, 2}

        # Рестарт: строка реклеймится в pending (возраст подделываем), джоба отрабатывает снова.
        monkeypatch.setattr(db, "mark_delivery", real_mark)
        async with db._connect() as conn:
            old = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                "UPDATE scheduled_broadcasts SET sending_since = ? WHERE id = ?", (old, bid)
            )
            await conn.commit()
        assert await db.reclaim_stale_sending(sched._STALE_SENDING_MINUTES) == [bid]

        bot2 = _Bot()
        await _run_send(bid, bot2)
        assert bot2.sent == [3, 4, 5]  # 1 и 2 не получили дубль
        assert (await db.get_scheduled_broadcast(bid))["status"] == "sent"

    asyncio.run(go())


def test_failed_recipient_is_not_retried_on_resume(tmp_path, monkeypatch):
    """(b) Заблокировавший бота записан как failed и при повторе не долбится снова."""
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1
        )
        bot1 = _Bot(fail_ids={2})
        real_mark = db.mark_delivery
        calls = {"n": 0}

        async def mark_then_crash(b, c, ok):
            await real_mark(b, c, ok)
            calls["n"] += 1
            if calls["n"] == 2:  # после 1(ok) и 2(failed) — обрыв
                raise RuntimeError("process died")
        monkeypatch.setattr(db, "mark_delivery", mark_then_crash)
        await _run_send(bid, bot1)
        assert bot1.sent == [1]
        assert await db.count_deliveries(bid) == (1, 1)

        monkeypatch.setattr(db, "mark_delivery", real_mark)
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE scheduled_broadcasts SET status = 'pending' WHERE id = ?", (bid,)
            )
            await conn.commit()
        bot2 = _Bot(fail_ids={2})
        await _run_send(bid, bot2)
        assert bot2.sent == [3]  # 2 (failed) пропущен, 1 (ok) пропущен
        assert (await db.get_scheduled_broadcast(bid))["status"] == "sent"
        # После sent чекпоинты подчищены.
        assert await db.list_delivered_chat_ids(bid) == set()

    asyncio.run(go())


def test_reclaim_stale_sending_touches_only_old_rows(tmp_path, monkeypatch):
    """(c) Старые sending реармятся, свежие и legacy (sending_since IS NULL) — нет."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        await db.init_db()
        past = "2026-01-01 10:00:00"
        stale = await db.create_scheduled_broadcast("a", None, None, past, created_by=1)
        fresh = await db.create_scheduled_broadcast("b", None, None, past, created_by=1)
        legacy = await db.create_scheduled_broadcast("c", None, None, past, created_by=1)
        stale2 = await db.create_scheduled_broadcast("d", None, None, past, created_by=1)
        for b in (stale, fresh, legacy, stale2):
            assert await db.mark_broadcast_sending(b) == 1
        old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE scheduled_broadcasts SET sending_since = ? WHERE id IN (?, ?)",
                (old, stale, stale2),
            )
            await conn.execute(
                "UPDATE scheduled_broadcasts SET sending_since = NULL WHERE id = ?", (legacy,)
            )
            await conn.commit()

        # Прямой вызов: реклеймятся оба старых; legacy с sending_since=NULL не трогаем —
        # у него нет журнала доставок, повтор разослал бы всем заново.
        assert sorted(await db.reclaim_stale_sending(10)) == [stale, stale2]
        assert (await db.get_scheduled_broadcast(stale))["status"] == "pending"
        assert (await db.get_scheduled_broadcast(fresh))["status"] == "sending"
        assert (await db.get_scheduled_broadcast(legacy))["status"] == "sending"

        # Через реальный бут: вернём stale2 в sending и проверим, что реконсиляция сама
        # реклеймит и реармит её (джоба появляется); свежую и legacy — нет.
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE scheduled_broadcasts SET status = 'sending' WHERE id = ?", (stale2,)
            )
            await conn.commit()
        s = await sched.init_scheduler(bot=object())
        try:
            assert s.get_job(f"bcast_{stale}") is not None
            assert s.get_job(f"bcast_{stale2}") is not None
            assert (await db.get_scheduled_broadcast(stale2))["status"] == "pending"
            assert s.get_job(f"bcast_{fresh}") is None
            assert s.get_job(f"bcast_{legacy}") is None
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_full_pass_marks_sent_and_logs_counts(tmp_path, monkeypatch, caplog):
    """(d) Полный проход без сбоев → sent, лог с sent/skipped/failed, чекпоинты удалены."""
    import logging
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1
        )
        bot = _Bot(fail_ids={3})
        with caplog.at_level(logging.INFO, logger="services.scheduler"):
            await _run_send(bid, bot)
        assert bot.sent == [1, 2]
        assert (await db.get_scheduled_broadcast(bid))["status"] == "sent"
        assert await db.list_delivered_chat_ids(bid) == set()
        assert any(
            "sent 2, skipped 0 (already), failed 1 of 3" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    asyncio.run(go())


def test_second_fire_in_same_process_is_still_rejected(tmp_path, monkeypatch):
    """ME-02 не сломан: пока строка в sending (свежая), повторный вызов не шлёт ничего."""
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1
        )
        assert await db.mark_broadcast_sending(bid) == 1
        bot = _Bot()
        await _run_send(bid, bot)
        assert bot.sent == []

    asyncio.run(go())


# ── Квик 260915-twr (Task B1): запланированная рассылка попадает в журнал и отзывается ────

def test_scheduled_broadcast_appears_in_journal_with_message_ids(tmp_path, monkeypatch):
    """После отправки отложенная рассылка видна в list_recent_broadcasts() со статусом 'done',
    а list_broadcast_messages отдаёт пары (chat_id, message_id) на каждого получателя —
    «Последние рассылки» и кнопка «🗑 Удалить у получателей» больше не пропускают отложенные."""
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi всем", None, None, "2026-01-01 10:00:00", created_by=42
        )
        bot = _Bot()
        await _run_send(bid, bot)

        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 1
        log_row = rows[0]
        assert log_row["status"] == "done"
        assert log_row["delivered"] == 3
        assert log_row["blocked"] == 0

        pairs = await db.list_broadcast_messages(log_row["id"])
        assert sorted(chat_id for chat_id, _ in pairs) == [1, 2, 3]
        assert all(isinstance(message_id, int) for _, message_id in pairs)

        # Связь сохранена для resume.
        assert (await db.get_scheduled_broadcast(bid))["log_broadcast_id"] == log_row["id"]

    asyncio.run(go())


def test_scheduled_broadcast_resume_reuses_log_row(tmp_path, monkeypatch):
    """Повторный прогон той же запланированной рассылки (resume после рестарта) переиспользует
    log_broadcast_id — вторая строка в broadcasts не заводится."""
    _isolate(tmp_path, monkeypatch)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        await db.init_db()
        bid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1
        )
        bot1 = _Bot()
        real_mark = db.mark_delivery
        calls = {"n": 0}

        async def mark_then_crash(b, c, ok):
            await real_mark(b, c, ok)
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("process died")
        monkeypatch.setattr(db, "mark_delivery", mark_then_crash)
        await _run_send(bid, bot1)
        assert bot1.sent == [1, 2]
        first_log_bid = (await db.get_scheduled_broadcast(bid))["log_broadcast_id"]
        assert first_log_bid is not None
        assert len(await db.list_recent_broadcasts(10)) == 1

        # Ручной возврат в pending — эмуляция рестарта/реклейма.
        monkeypatch.setattr(db, "mark_delivery", real_mark)
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE scheduled_broadcasts SET status = 'pending' WHERE id = ?", (bid,)
            )
            await conn.commit()

        bot2 = _Bot()
        await _run_send(bid, bot2)
        assert bot2.sent == [3]  # 1 и 2 уже доставлены прошлым прогоном

        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 1  # НЕ вторая строка журнала
        assert rows[0]["id"] == first_log_bid
        assert (await db.get_scheduled_broadcast(bid))["log_broadcast_id"] == first_log_bid
        assert rows[0]["delivered"] == 3  # sent(1) + skipped(2), не меньше уже доставленного

    asyncio.run(go())
