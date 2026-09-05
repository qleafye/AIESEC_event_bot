"""«Тихая минута» перед рестартом бота.

FSM бота живёт в памяти: рестарт посреди анкеты сбрасывает диалог делегату (черновик в БД
уцелеет, но человек видит «не работает»). Скрипт ждёт, пока никто не правил черновик анкеты
`--quiet` секунд подряд, и только тогда выходит с кодом 0. Если тишины не дождались за
`--max-wait` секунд — код 2 (решай сам: ждать дальше или рестартовать, предупредив менеджеров).

Только stdlib, БД открывается только для чтения — можно запускать на хосте:
    python3 tools/quiet_minute.py --db data/forum.db
    python3 tools/quiet_minute.py --db data/forum.db --quiet 120 --max-wait 900
Код 0 — тихо, можно рестартовать. Код 2 — не дождались. Код 1 — БД недоступна.
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# `reg_drafts.updated_at` пишется `datetime.now()` процесса бота (UTC в контейнере), формат
# 'YYYY-MM-DD HH:MM:SS'. Сравниваем в Python, а не в SQL, чтобы не зависеть от TZ хоста.
_FMT = "%Y-%m-%d %H:%M:%S"


def _recent_activity(db_path: str, since_seconds: int) -> list[tuple]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT telegram_id, step, updated_at FROM reg_drafts ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(seconds=since_seconds)
    active = []
    for tid, step, upd in rows:
        try:
            ts = datetime.strptime((upd or "")[:19], _FMT)
        except ValueError:
            continue
        if ts >= cutoff:
            active.append((tid, step, upd))
    return active


def main() -> int:
    ap = argparse.ArgumentParser(description="Ждёт тишины в анкетах перед рестартом бота")
    ap.add_argument("--db", required=True, help="путь к forum.db (хост: data/forum.db)")
    ap.add_argument("--quiet", type=int, default=120, help="секунд тишины, по умолчанию 120")
    ap.add_argument("--max-wait", type=int, default=900, help="сколько максимум ждать, по умолчанию 900")
    ap.add_argument("--poll", type=int, default=10, help="период опроса, секунд")
    args = ap.parse_args()

    started = time.monotonic()
    while True:
        try:
            active = _recent_activity(args.db, args.quiet)
        except sqlite3.Error as e:
            print(f"БД недоступна: {e}", file=sys.stderr)
            return 1
        if not active:
            print(f"Тихо: правок черновиков не было {args.quiet} с. Можно рестартовать.")
            return 0
        waited = int(time.monotonic() - started)
        who = ", ".join(f"{tid} ({step}, {upd[11:19]} UTC)" for tid, step, upd in active[:5])
        print(f"[{waited:4d}s] в анкете сейчас {len(active)}: {who} — жду…", flush=True)
        if waited >= args.max_wait:
            print(f"Не дождались тишины за {args.max_wait} с.", file=sys.stderr)
            return 2
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
