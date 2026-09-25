"""Нагрузочный прогон 25.09: рассылка на 2000 человек шла ~160 мс на получателя, из них
~70 мс — сам бот: на КАЖДОГО получателя язык и тумблер читались из БД, а строка журнала
доставки открывала своё соединение и коммитила (закрытие последнего соединения с WAL-базой —
чекпоинт и fsync). Теперь язык всех читается один раз на рассылку (`RecipientLangs`), журнал
пишется пачкой, чекпоинт отложенной рассылки — на каждого, но через одно соединение на цикл.

Эти тесты держат две вещи: поведение то же (язык/кнопка «🔕» совпадают с прежним путём,
журнал полон, в т.ч. после стопа), и число открытий соединения не растёт обратно с числом
получателей. Время не меряем — это делает tools/bench_broadcast.py.
"""
import asyncio
from types import SimpleNamespace

import pytest

from config import config
from database import db
from services import broadcast_run as br
from services import i18n as i18n_service
from services import scheduler as sched
from tests._dbtpl import fast_init_db


def _isolate(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "bcperf.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)

    async def _noop(_seconds):
        return None
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


def _count_connects(monkeypatch):
    counter = {"n": 0}
    real = db._connect

    def counting():
        counter["n"] += 1
        return real()
    monkeypatch.setattr(db, "_connect", counting)
    return counter


async def _seed_langs():
    """Все ветки лестницы языка: users.lang, откат на reg_started.lang, пустые значения."""
    async with db._connect() as conn:
        await conn.executemany(
            "INSERT INTO users (telegram_id, full_name, lang) VALUES (?, ?, ?)",
            [(1, "en", "en"), (2, "ru", "ru"), (3, "fallback en", None),
             (4, "empty ru", ""), (6, "none", None)],
        )
        await conn.executemany(
            "INSERT INTO reg_started (telegram_id, started_at, lang) VALUES (?, ?, ?)",
            [(3, "2026-09-25 10:00:00", "en"), (4, "2026-09-25 10:00:00", "ru"),
             (5, "2026-09-25 10:00:00", "en"), (1, "2026-09-25 10:00:00", "ru")],
        )
        await conn.execute(
            "INSERT INTO translations (lang, src_hash, src_text, text) VALUES (?, ?, ?, ?)",
            ("en", i18n_service.src_hash("Не присылать сегодня"), "Не присылать сегодня",
             "Mute for today"),
        )
        await conn.commit()


@pytest.mark.parametrize("module_on", ["on", "off"])
def test_recipient_langs_match_per_recipient_context(tmp_path, monkeypatch, module_on):
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        await _seed_langs()
        await db.set_setting("delegate_lang_enabled", module_on)
        langs = await sched.load_recipient_langs()
        for chat_id in (1, 2, 3, 4, 5, 6, 7):
            assert langs.context(chat_id) == await i18n_service.context(chat_id), chat_id
            assert langs.mute_button(chat_id) == await sched.mute_button(chat_id), chat_id
            assert (
                await sched.recipient_markup(chat_id, False, langs=langs)
                == await sched.recipient_markup(chat_id, False)
            )
        if module_on == "on":
            assert langs.context(1)[0] == "en" and langs.context(3)[0] == "en"
            assert langs.context(4)[0] == "ru" and langs.context(7)[0] == "ask"

    asyncio.run(go())


def test_recipient_langs_fail_soft_to_russian(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        await db.set_setting("delegate_lang_enabled", "on")

        async def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(db, "list_stored_langs", boom)
        langs = await sched.load_recipient_langs()
        assert langs.context(1) == ("ru", {})

    asyncio.run(go())


def test_instant_run_journal_complete_and_connections_flat(tmp_path, monkeypatch):
    """60 получателей (больше пачки журнала), каждый третий блокирует бота: журнал полон,
    соединений — единицы на весь прогон, не по одному-два на получателя."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        bid = await db.create_broadcast(1, "привет", 60)
        langs = await sched.load_recipient_langs()

        async def send_one(chat_id):
            if chat_id % 3 == 0:
                raise RuntimeError("blocked")
            await sched.recipient_markup(chat_id, False, None, langs)
            return [10_000 + chat_id]

        counter = _count_connects(monkeypatch)
        await br.run_broadcast(bid, list(range(1, 61)), send_one)
        assert counter["n"] <= 4
        row = await db.get_broadcast(bid)
        assert (row["status"], row["delivered"], row["blocked"]) == ("done", 40, 20)
        pairs = await db.list_broadcast_messages(bid)
        assert sorted(pairs) == [(c, 10_000 + c) for c in range(1, 61) if c % 3]

    asyncio.run(go())


def test_instant_run_stop_flushes_journal(tmp_path, monkeypatch):
    """Стоп посреди пачки: всё, что успело уйти, в журнале — иначе отзыв не удалит хвост."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        bid = await db.create_broadcast(1, "привет", 10)

        async def send_one(chat_id):
            if chat_id == 7:
                br.request_stop(bid)
            return [500 + chat_id]

        await br.run_broadcast(bid, list(range(1, 11)), send_one)
        assert (await db.get_broadcast(bid))["status"] == "stopped"
        assert sorted(await db.list_broadcast_messages(bid)) == [(c, 500 + c) for c in range(1, 8)]

    asyncio.run(go())


def test_instant_run_journal_write_failure_does_not_stop_broadcast(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(br, "_JOURNAL_FLUSH_EVERY_N", 2)

    async def go():
        fast_init_db()
        bid = await db.create_broadcast(1, "привет", 6)
        real = br.record_broadcast_deliveries
        calls = {"n": 0}

        async def flaky(rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            await real(rows)
        monkeypatch.setattr(br, "record_broadcast_deliveries", flaky)

        async def send_one(chat_id):
            return [chat_id]

        await br.run_broadcast(bid, [1, 2, 3, 4, 5, 6], send_one)
        assert (await db.get_broadcast(bid))["delivered"] == 6
        # Строки из упавшей пачки дописаны следующей.
        assert sorted(await db.list_broadcast_messages(bid)) == [(c, c) for c in range(1, 7)]

    asyncio.run(go())


def test_scheduled_run_checkpoint_per_recipient_on_one_connection(tmp_path, monkeypatch):
    """Отложенная: чекпоинт на каждого (после краха нет дублей — см. test_broadcast_
    checkpointing_260819), журнал полон, соединение на цикл одно."""
    _isolate(tmp_path, monkeypatch)
    ids = list(range(100, 140))

    async def fake_all():
        return list(ids)
    monkeypatch.setattr(db, "get_all_users_ids", fake_all)

    class _Bot:
        def __init__(self):
            self.markups = []

        async def send_message(self, chat_id, text, reply_markup=None):
            if chat_id == 105:
                from aiogram.exceptions import TelegramForbiddenError
                raise TelegramForbiddenError(method=None, message="blocked")
            self.markups.append(reply_markup)
            return SimpleNamespace(message_id=chat_id * 10)

    async def go():
        fast_init_db()
        sid = await db.create_scheduled_broadcast(
            "hi", None, None, "2026-01-01 10:00:00", created_by=1,
        )
        bot = _Bot()
        prev = sched._bot
        sched._bot = bot
        counter = _count_connects(monkeypatch)
        try:
            await sched.send_scheduled_broadcast(sid)
        finally:
            sched._bot = prev
        assert counter["n"] <= 16  # было ~4 на получателя (160+)
        assert all(m.inline_keyboard[-1][0].callback_data == sched.MUTE_TODAY_CALLBACK
                   for m in bot.markups)
        row = await db.get_scheduled_broadcast(sid)
        assert row["status"] == "sent"
        pairs = await db.list_broadcast_messages(row["log_broadcast_id"])
        assert sorted(pairs) == [(c, c * 10) for c in ids if c != 105]
        log = await db.get_broadcast(row["log_broadcast_id"])
        assert (log["delivered"], log["blocked"]) == (39, 1)

    asyncio.run(go())


def test_delivery_writer_rolls_back_failed_write(tmp_path, monkeypatch):
    """Упавшая запись на общем соединении не оставляет открытую транзакцию — следующая
    запись и параллельный писатель проходят."""
    _isolate(tmp_path, monkeypatch)

    async def go():
        fast_init_db()
        bid = await db.create_broadcast(1, "x", 2)
        async with db.delivery_writer():
            await db.record_broadcast_deliveries([(bid, 1, 11, "2026-09-25 10:00:00")])
            # Первая строка пачки уже вставлена в неявной транзакции, вторая падает на NOT NULL.
            with pytest.raises(Exception):
                await db.record_broadcast_deliveries([
                    (bid, 2, 22, "2026-09-25 10:00:00"), (bid, 2, None, "2026-09-25 10:00:00"),
                ])
            # Другой писатель не упирается в блокировку (busy_timeout 5 с не нужен).
            await db.set_setting("bench_probe", "1")
            await db.record_broadcast_deliveries([(bid, 3, 33, "2026-09-25 10:00:00")])
        assert sorted(await db.list_broadcast_messages(bid)) == [(1, 11), (3, 33)]

    asyncio.run(go())
