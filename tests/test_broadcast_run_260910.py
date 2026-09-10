"""Quick 260910-okb (BC-01..06): журнал рассылок в БД + фоновый прогон со стопом/отзывом.

09.09 менеджер отправил «Привет» на 951 человека и не смог остановить рассылку — цикл шёл
внутри хендлера, никого не слушал, ни лога, ни message_id не оставалось. Эти тесты покрывают
`services/broadcast_run.py`: журнал заполняется, стоп адресен по broadcast_id и прерывает
цикл на ближайшей итерации, отзыв удаляет сохранённые message_id.

Стиль tests/test_broadcast_checkpointing_260819.py: pytest-asyncio в проекте нет, async
гоняется через asyncio.run(); БД — tmp_path. Пауза 0.05 с внутри прогона монкипатчится, чтобы
не растягивать тесты.
"""
import asyncio

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import config
from database import db
from services import broadcast_run as br


def _isolate(tmp_path):
    config.DB_PATH = str(tmp_path / "br.db")


def _fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


def test_full_run_writes_broadcast_row_and_deliveries(tmp_path, monkeypatch):
    """5 получателей: одна строка в broadcasts (done, total=5, delivered/blocked посчитаны),
    по строке в broadcast_deliveries на каждое доставленное сообщение."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "привет", 5)

        async def send_one(chat_id):
            return [10_000 + chat_id]

        finishes = []

        async def on_finish(status, delivered, blocked):
            finishes.append((status, delivered, blocked))

        await br.run_broadcast(bid, [1, 2, 3, 4, 5], send_one, on_finish=on_finish)

        row = await db.get_broadcast(bid)
        assert row["status"] == "done"
        assert row["total"] == 5
        assert row["delivered"] == 5
        assert row["blocked"] == 0
        assert row["finished_at"]
        pairs = await db.list_broadcast_messages(bid)
        assert len(pairs) == 5
        assert sorted(pairs) == [(1, 10001), (2, 10002), (3, 10003), (4, 10004), (5, 10005)]
        assert finishes == [("done", 5, 0)]

    asyncio.run(go())


def test_album_send_one_returns_several_ids_one_row_each(tmp_path, monkeypatch):
    """Альбом: send_one вернул три message_id на один chat_id — три строки в deliveries."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "[альбом x 3]", 1)

        async def send_one(chat_id):
            return [501, 502, 503]

        await br.run_broadcast(bid, [77], send_one)

        pairs = await db.list_broadcast_messages(bid)
        assert sorted(pairs) == [(77, 501), (77, 502), (77, 503)]
        row = await db.get_broadcast(bid)
        assert row["delivered"] == 1
        assert row["status"] == "done"

    asyncio.run(go())


def test_stop_before_third_iteration_sends_exactly_two(tmp_path, monkeypatch):
    """request_stop(bid) перед третьей итерацией: отправок ровно две, status='stopped',
    finish-колбэк получил ("stopped", 2, 0)."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "hi", 5)
        calls = []

        async def send_one(chat_id):
            calls.append(chat_id)
            if len(calls) == 2:
                br.request_stop(bid)
            return [chat_id]

        finishes = []

        async def on_finish(status, delivered, blocked):
            finishes.append((status, delivered, blocked))

        await br.run_broadcast(bid, [1, 2, 3, 4, 5], send_one, on_finish=on_finish)

        assert calls == [1, 2]
        assert finishes == [("stopped", 2, 0)]
        row = await db.get_broadcast(bid)
        assert row["status"] == "stopped"
        assert row["delivered"] == 2
        # Флаг снят после завершения — следующий прогон с тем же id не остановится сразу.
        assert br.is_stopped(bid) is False

    asyncio.run(go())


def test_stop_is_addressed_by_broadcast_id(tmp_path, monkeypatch):
    """Стоп адресный: request_stop(1) не останавливает прогон с bid=2."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid1 = await db.create_broadcast(1, "a", 3)
        bid2 = await db.create_broadcast(1, "b", 3)
        br.request_stop(bid1)

        calls = []

        async def send_one(chat_id):
            calls.append(chat_id)
            return [chat_id]

        await br.run_broadcast(bid2, [10, 20, 30], send_one)

        assert calls == [10, 20, 30]
        row2 = await db.get_broadcast(bid2)
        assert row2["status"] == "done"
        br.clear_stop(bid1)

    asyncio.run(go())


def test_forbidden_recipient_counts_blocked_and_does_not_crash(tmp_path, monkeypatch):
    """Недоступный получатель (TelegramForbiddenError) увеличивает blocked, цикл не падает."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "hi", 3)

        async def send_one(chat_id):
            if chat_id == 2:
                raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
            return [chat_id]

        await br.run_broadcast(bid, [1, 2, 3], send_one)

        row = await db.get_broadcast(bid)
        assert row["delivered"] == 2
        assert row["blocked"] == 1
        assert row["status"] == "done"

    asyncio.run(go())


def test_retry_after_success_counts_delivered_not_blocked(tmp_path, monkeypatch):
    """429: первая попытка TelegramRetryAfter, ретрай успешен -> delivered, не blocked."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "hi", 1)
        attempts = {"n": 0}

        async def send_one(chat_id):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TelegramRetryAfter(method=None, message="retry", retry_after=0)
            return [chat_id]

        await br.run_broadcast(bid, [42], send_one)

        row = await db.get_broadcast(bid)
        assert row["delivered"] == 1
        assert row["blocked"] == 0
        pairs = await db.list_broadcast_messages(bid)
        assert pairs == [(42, 42)]

    asyncio.run(go())


class _RevokeBot:
    """Фейковый Bot: журнал удалений, по желанию падает на заданных (chat_id, message_id)."""

    def __init__(self, fail_pairs=()):
        self.deleted = []
        self.fail_pairs = set(fail_pairs)

    async def delete_message(self, chat_id, message_id):
        if (chat_id, message_id) in self.fail_pairs:
            raise Exception("message to delete not found")
        self.deleted.append((chat_id, message_id))


def test_run_revoke_deletes_every_saved_pair_and_marks_revoked(tmp_path, monkeypatch):
    """run_revoke: bot.delete_message вызван по каждой сохранённой паре, ошибка удаления
    считается «не удалось» и не роняет цикл, статус становится 'revoked'."""
    _isolate(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "hi", 3)

        async def send_one(chat_id):
            return [chat_id * 100]

        await br.run_broadcast(bid, [1, 2, 3], send_one)

        bot = _RevokeBot(fail_pairs={(2, 200)})
        finishes = []

        async def on_finish(deleted, failed):
            finishes.append((deleted, failed))

        await br.run_revoke(bot, bid, on_finish=on_finish)

        assert sorted(bot.deleted) == [(1, 100), (3, 300)]
        assert finishes == [(2, 1)]
        row = await db.get_broadcast(bid)
        assert row["status"] == "revoked"

    asyncio.run(go())


def test_can_revoke_window():
    """can_revoke(started_at) — True для «час назад», False для «49 часов назад»."""
    from datetime import datetime, timedelta

    hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    long_ago = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
    assert br.can_revoke(hour_ago) is True
    assert br.can_revoke(long_ago) is False
    assert br.can_revoke(None) is False
    assert br.can_revoke("") is False
    assert br.can_revoke("не дата") is False
