"""Бенчмарк рассылки: сколько стоит ОДИН получатель на стороне бота, без сети.

Гоняет настоящий код обоих путей рассылки на временной БД с синтетическими получателями:
  * мгновенная — `handlers/comms/admin_broadcasts.py::bc_go` (фейковые callback/FSM, фоновый прогон
    `services/broadcast_run.run_broadcast` дожидается здесь же);
  * отложенная — `services/scheduler.py::send_scheduled_broadcast`.

Bot API — заглушка без сна (отдаёт message_id сразу). Пауза антифлуда (`asyncio.sleep(0.05)`
между отправками) в коде остаётся, здесь она подменена на счётчик — отчёт показывает, сколько
пауз было бы, но в миллисекунды на получателя они не входят. Итог — мс на получателя по каждому
пути, счётчик открытий соединения с БД и топ функций по cProfile (--profile).

Запуск (из корня репозитория; в обычный прогон pytest не входит — это не тест):
    BOT_TOKEN=123:test ADMIN_IDS="[1]" python tools/bench_broadcast.py --n 500 [--en 0.3] [--profile]

Ничего не трогает, кроме временного каталога: БД создаётся с нуля через init_db().
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import os
import pstats
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ADMIN_IDS", "[1]")

ADMIN_ID = 1
BASE_ID = 7_000_000_000


class FakeBot:
    """Bot API без сети и без сна: ответ с message_id мгновенно."""

    def __init__(self):
        self.calls = 0

    def _ok(self, chat_id):
        self.calls += 1
        return SimpleNamespace(message_id=self.calls, chat=SimpleNamespace(id=chat_id))

    async def send_message(self, chat_id, text=None, **kwargs):
        return self._ok(chat_id)

    async def copy_message(self, chat_id, from_chat_id=None, message_id=None, **kwargs):
        return self._ok(chat_id)

    async def send_photo(self, chat_id, photo=None, **kwargs):
        return self._ok(chat_id)


class _ProgressMsg:
    async def edit_text(self, *a, **k):
        return None

    async def delete(self):
        return None

    chat = SimpleNamespace(id=ADMIN_ID)


class _State:
    def __init__(self, data):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


async def _seed(ids, en_share):
    from database import db

    n_en = int(len(ids) * en_share)
    async with db._connect() as conn:
        await conn.executemany(
            "INSERT INTO users (telegram_id, full_name, lang) VALUES (?, ?, ?)",
            [(tid, f"BENCH {tid}", "en" if i < n_en else None) for i, tid in enumerate(ids)],
        )
        # Немного переводов — чтобы у английской ветки была непустая карта, как в проде.
        await conn.executemany(
            "INSERT OR IGNORE INTO translations (lang, src_hash, src_text, text) VALUES (?, ?, ?, ?)",
            [("en", f"{i:032x}", f"src {i}", f"text {i}") for i in range(300)],
        )
        # Сырой INSERT, не set_setting: тот ставит фоновый перевод, и воркер переводов
        # крутился бы в том же цикле, съедая время замера.
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on" if en_share > 0 else "off"),
        )
        await conn.commit()


class _Counters:
    """Считает открытия соединения (`db._connect`) и паузы антифлуда вместо сна."""

    def __init__(self):
        self.connects = 0
        self.pauses = 0

    def install(self):
        from database import db
        import services.comms.broadcast_run as br
        import services.scheduler as sched

        real_connect = db._connect

        def counting_connect(*a, **k):
            self.connects += 1
            return real_connect(*a, **k)

        db._connect = counting_connect
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds, *a, **k):
            if seconds == 0.05:
                self.pauses += 1
                return None
            return await real_sleep(0)

        br.asyncio = SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep})
        sched.asyncio = SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep})
        import handlers.comms.admin_broadcasts as ab
        ab.asyncio = SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep})

    def reset(self):
        self.connects = 0
        self.pauses = 0


async def run_instant(ids, bot, *, important=False, kind="text"):
    import handlers.comms.admin_broadcasts as ab

    spawned = []
    ab._spawn = lambda coro: spawned.append(coro)
    state = _State({
        "bc_users": list(ids),
        "bc_preview": "BENCH рассылка",
        "bc_kind": kind,
        "bc_content_html": "BENCH рассылка <b>текст</b>",
        "bc_chat_id": ADMIN_ID,
        "bc_message_id": 1,
        "bc_important": important,
    })
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN_ID), message=_ProgressMsg(),
        answer=_async_none,
    )
    await ab.bc_go(callback, state, bot)
    for coro in spawned:
        await coro


async def run_scheduled(bot, *, important=False):
    from database import db
    import services.scheduler as sched

    bid = await db.create_scheduled_broadcast(
        "BENCH отложенная", None, None, "2026-01-01 10:00:00", created_by=ADMIN_ID,
        important=important,
    )
    sched._bot = bot
    await sched.send_scheduled_broadcast(bid)
    row = await db.get_scheduled_broadcast(bid)
    assert row["status"] == "sent", row


async def _async_none(*a, **k):
    return None


async def main_async(args):
    from config import config
    tmp = tempfile.mkdtemp(prefix="bench_broadcast_")
    config.DB_PATH = os.path.join(tmp, "bench.db")
    from database import db
    await db.init_db()
    ids = [BASE_ID + i for i in range(args.n)]
    await _seed(ids, args.en)

    counters = _Counters()
    counters.install()
    bot = FakeBot()
    results = {}
    scenarios = [
        ("instant_text", lambda: run_instant(ids, bot, kind="text")),
        ("instant_copy", lambda: run_instant(ids, bot, kind="media")),
        ("instant_important", lambda: run_instant(ids, bot, kind="text", important=True)),
        ("scheduled", lambda: run_scheduled(bot)),
    ]
    profiler = cProfile.Profile() if args.profile else None
    for name, make in scenarios:
        if args.only and name not in args.only:
            continue
        counters.reset()
        calls_before = bot.calls
        if profiler:
            profiler.enable()
        t0 = time.perf_counter()
        await make()
        dt = time.perf_counter() - t0
        if profiler:
            profiler.disable()
        sent = bot.calls - calls_before
        results[name] = {
            "sent": sent,
            "ms_per_recipient": round(dt * 1000 / max(1, len(ids)), 2),
            "total_s": round(dt, 2),
            "db_connects_per_recipient": round(counters.connects / max(1, len(ids)), 2),
            "antiflood_pauses": counters.pauses,
        }
    for name, r in results.items():
        print(f"{name:18} {r['ms_per_recipient']:7.2f} мс/получатель  "
              f"(всего {r['total_s']} с, отправок {r['sent']}, "
              f"соединений БД/получатель {r['db_connects_per_recipient']}, "
              f"пауз антифлуда {r['antiflood_pauses']})")
    if profiler:
        buf = io.StringIO()
        st = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
        st.print_stats(args.top)
        print(buf.getvalue())
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--n", type=int, default=500, help="получателей (по умолчанию 500)")
    p.add_argument("--en", type=float, default=0.3,
                   help="доля получателей с английским языком (0 — модуль языка выключен)")
    p.add_argument("--profile", action="store_true", help="напечатать топ cProfile")
    p.add_argument("--top", type=int, default=35)
    p.add_argument("--only", nargs="*", help="только эти сценарии")
    args = p.parse_args(argv)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
