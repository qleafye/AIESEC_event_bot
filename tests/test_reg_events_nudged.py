"""Событие `nudged` в `reg_events`: след напоминалки о брошенной анкете, который переживает подачу.

`reg_started.nudged_at` удаляется вместе со строкой при подаче (`clear_reg_started`), поэтому
«скольких брошенных догнала напоминалка» по нему не посчитать. Append-only строка
`reg_events.event = 'nudged'` пишется ТОЛЬКО после реально доставленного напоминания; воронка
(start → form_started → form_completed) новое событие не видит.

pytest-asyncio недоступен — async-хелперы через asyncio.run(), БД на tmp_path (конвенция
tests/test_reg_events_log.py).
"""
import asyncio

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from config import config
from database import db
from dashboard import db as dash_db
from dashboard.queries import Scope, funnel, kpi_row
from tests._dbtpl import fast_init_db


DELEGATE = 950101
DELEGATE_BLOCKED = 950102
DELEGATE_BROKEN = 950103


def _ready(tmp_path, name):
    path = str(tmp_path / name)
    config.DB_PATH = path
    fast_init_db()
    return path


async def _seed_candidate(telegram_id, city="spb"):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO reg_started (telegram_id, username, started_at, event_city) "
            "VALUES (?, ?, ?, ?)",
            (telegram_id, "@u", "2020-01-01 10:00:00", city),
        )
        await conn.commit()


async def _nudged_rows():
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT telegram_id, event, event_city, season, ts FROM reg_events "
            "WHERE event = 'nudged' ORDER BY id"
        ) as cur:
            return await cur.fetchall()


class _Bot:
    def __init__(self, fail_for=None):
        self.sent = []
        self.fail_for = fail_for or {}

    async def get_me(self):
        class _Me:
            username = "test_bot"
        return _Me()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        exc = self.fail_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text))


def _run_job(bot):
    from services import scheduler as sched
    sched._bot = bot
    asyncio.run(sched.nudge_incomplete_registrations())


def _setup(tmp_path, name, *ids):
    _ready(tmp_path, name)
    asyncio.run(db.set_setting("nudge_enabled", "on"))
    asyncio.run(db.set_setting("event_season", "YL 26/2"))
    for tid in ids:
        asyncio.run(_seed_candidate(tid))


def test_delivered_nudge_writes_one_nudged_event(tmp_path):
    _setup(tmp_path, "nudged_ok.db", DELEGATE)
    bot = _Bot()
    _run_job(bot)

    assert [c for c, _ in bot.sent] == [DELEGATE]
    rows = asyncio.run(_nudged_rows())
    assert len(rows) == 1
    tid, event, city, season, ts = rows[0]
    assert (tid, event, city, season) == (DELEGATE, "nudged", "spb", "YL 26/2")
    assert ts

    # one-shot: повторный прогон не шлёт и не пишет второе событие
    _run_job(bot)
    assert len(asyncio.run(_nudged_rows())) == 1


def test_nudged_event_survives_submit(tmp_path):
    _setup(tmp_path, "nudged_survives.db", DELEGATE)
    _run_job(_Bot())
    asyncio.run(db.clear_reg_started(DELEGATE))
    assert len(asyncio.run(_nudged_rows())) == 1


def test_failed_send_writes_no_nudged_event(tmp_path):
    _setup(tmp_path, "nudged_fail.db", DELEGATE_BLOCKED, DELEGATE_BROKEN)
    forbidden = TelegramForbiddenError(
        method=SendMessage(chat_id=DELEGATE_BLOCKED, text="x"), message="bot was blocked by the user"
    )
    bot = _Bot(fail_for={DELEGATE_BLOCKED: forbidden, DELEGATE_BROKEN: RuntimeError("network")})
    _run_job(bot)

    assert bot.sent == []
    assert asyncio.run(_nudged_rows()) == []


def test_nudged_event_write_failure_does_not_break_job(tmp_path, monkeypatch):
    _setup(tmp_path, "nudged_writefail.db", DELEGATE)

    async def _boom(*_a, **_k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "record_reg_event", _boom)
    bot = _Bot()
    _run_job(bot)

    assert [c for c, _ in bot.sent] == [DELEGATE]
    async def _nudged_at():
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT nudged_at FROM reg_started WHERE telegram_id = ?", (DELEGATE,)
            ) as cur:
                return (await cur.fetchone())[0]
    assert asyncio.run(_nudged_at()) is not None  # one-shot пометка не сорвана записью события


def test_record_reg_event_does_not_warn_on_nudged(tmp_path, caplog):
    _ready(tmp_path, "nudged_warn.db")
    assert "nudged" not in db.REG_EVENT_KINDS  # воронка — только три ступени
    with caplog.at_level("WARNING"):
        asyncio.run(db.record_reg_event(1, "nudged"))
    assert "unexpected event kind" not in caplog.text


def test_funnel_and_kpi_ignore_nudged_events(tmp_path):
    path = _ready(tmp_path, "nudged_funnel.db")

    async def _seed():
        async with db._connect() as conn:
            rows = [
                (1, "start", "2026-09-01 10:00:00"),
                (1, "form_started", "2026-09-01 10:01:00"),
                (2, "start", "2026-09-01 10:02:00"),
                (2, "form_started", "2026-09-01 10:03:00"),
                (2, "form_completed", "2026-09-01 10:04:00"),
            ]
            for tid, ev, ts in rows:
                await conn.execute(
                    "INSERT INTO reg_events (telegram_id, event, ts) VALUES (?, ?, ?)", (tid, ev, ts)
                )
            await conn.commit()

    async def _add_nudges():
        async with db._connect() as conn:
            for tid in (1, 3, 4):
                await conn.execute(
                    "INSERT INTO reg_events (telegram_id, event, ts) VALUES (?, 'nudged', ?)",
                    (tid, "2026-09-02 10:00:00"),
                )
            await conn.commit()

    asyncio.run(_seed())
    with dash_db.read_conn(path) as conn:
        before_funnel = funnel(conn, Scope())
        before_kpi = kpi_row(conn, Scope())
    asyncio.run(_add_nudges())
    with dash_db.read_conn(path) as conn:
        after_funnel = funnel(conn, Scope())
        after_kpi = kpi_row(conn, Scope())

    assert after_funnel == before_funnel
    stages = dict(after_funnel)
    assert (stages["Зашли"], stages["Начали анкету"], stages["Дошли до конца"]) == (2, 2, 1)
    assert after_kpi == before_kpi
