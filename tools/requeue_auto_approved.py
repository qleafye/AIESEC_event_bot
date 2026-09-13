"""Разовый инструмент по инциденту прода 06.09.2026: `full_approval` случайно переключили в
`'auto'`, и 38 заявок сезона «YL 26/2» прошли автоодобрение (`status='approved'`,
`approved_at IS NULL`, без строки в `application_decisions`) — модератор их вообще не видел.

Что делает скрипт: находит ровно эти заявки (сезон + окно регистрации + признаки
автоодобрения) и возвращает им `status='pending'`, чтобы менеджер отсмотрел их штатным путём
через админку, как будто автоодобрения не было. Делегату ничего не отправляется — это чистая
операция над `users.status`, `application_decisions` не трогается вовсе.

Запуск на проде (по умолчанию — предпросмотр, ничего не пишет):

    docker exec youlead26-bot-1 python /app/tools/requeue_auto_approved.py
    docker exec youlead26-bot-1 python /app/tools/requeue_auto_approved.py --apply

ЧАСОВОЙ ПОЯС окна `--from`/`--to`: дефолты (`2026-09-06 08:00:00` — `2026-09-06 13:15:00`)
заданы в МОСКОВСКОМ времени, потому что квик 260912-mcj одноразовой миграцией сдвинул
`users.registration_date` на +3ч (UTC -> МСК). Если на целевой БД миграция ЕЩЁ НЕ прошла
(`PRAGMA user_version` = 0), `registration_date` в ней всё ещё в UTC — окно нужно передать
руками в UTC: `--from "2026-09-06 05:00:00" --to "2026-09-06 10:15:00"`.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль по умолчанию открывает stdout/stderr в cp1251 — кириллица в `--help`
# (argparse печатает докстринг модуля) и в отчёте падает `UnicodeEncodeError`.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

DEFAULT_SEASON = "YL 26/2"
DEFAULT_FROM = "2026-09-06 08:00:00"
DEFAULT_TO = "2026-09-06 13:15:00"

_MSK_MIGRATION_USER_VERSION = 1

SELECT_SQL = """
    SELECT telegram_id, username, full_name, status, registration_date, approved_at
    FROM users
    WHERE season = ?
      AND status = 'approved'
      AND approved_at IS NULL
      AND telegram_id NOT IN (SELECT telegram_id FROM application_decisions)
      AND registration_date BETWEEN ? AND ?
    ORDER BY registration_date
"""


async def read_user_version() -> int:
    """PRAGMA user_version текущей БД (`config.DB_PATH`) — используется только для того,
    чтобы предупредить оператора о часовом поясе окна, миграцию НЕ запускает и не трогает."""
    import aiosqlite

    from config import config

    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def select_rows(season: str, date_from: str, date_to: str) -> list[dict]:
    """Выборка автоодобренных заявок (см. докстринг модуля). Параметризованный SQL —
    никакой конкатенации значений `season`/`date_from`/`date_to` в текст запроса."""
    import aiosqlite

    from config import config

    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(SELECT_SQL, (season, date_from, date_to)) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


def backup_db(backups_dir: Path) -> Path:
    """Файловая копия БД ДО любой записи. `sqlite3.Connection.backup()`, а не `shutil.copy`
    файла `.db` напрямую — БД работает в режиме WAL, простое копирование файла потеряло бы
    ещё не слитые в него изменения из `-wal`. Возвращает путь копии; любое исключение здесь
    означает отказ от записи (вызывающий код не перехватывает — пусть уронит `main`)."""
    from services.timeutil import msk_now
    from config import config

    backups_dir.mkdir(parents=True, exist_ok=True)
    dst = backups_dir / f"forum-{msk_now():%Y%m%d-%H%M%S}-requeue.db"

    src_conn = sqlite3.connect(config.DB_PATH)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


async def fetch_by_ids(ids: list[int]) -> list[dict]:
    """Читает текущее состояние ровно этих `telegram_id` — используется для отчёта «статус
    ПОСЛЕ» так, чтобы он показывал реально записанное значение, а не предположение."""
    import aiosqlite

    from config import config

    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, username, full_name, status, registration_date, approved_at "
            f"FROM users WHERE telegram_id IN ({placeholders}) ORDER BY registration_date",
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def requeue(ids: list[int]) -> int:
    """`UPDATE users SET status='pending' WHERE telegram_id IN (...)` по явному списку id
    (плейсхолдеры по числу id — никакой конкатенации). Возвращает `rowcount` — число реально
    изменённых строк."""
    import aiosqlite

    from config import config

    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE users SET status = 'pending' WHERE telegram_id IN ({placeholders})",
            ids,
        )
        await db.commit()
        return cursor.rowcount


def _print_table(rows: list[dict], status_col: str = "status") -> None:
    if not rows:
        print("  (пусто)")
        return
    for r in rows:
        print(
            f"  {r['telegram_id']} / @{r.get('username') or '-'} / "
            f"{r.get('registration_date') or '-'} / {r.get(status_col) or '-'}"
        )


async def main(
    season: str,
    date_from: str,
    date_to: str,
    apply: bool,
    expect: int | None,
) -> int:
    version = await read_user_version()
    print(f"PRAGMA user_version = {version}; окно выборки: {date_from} .. {date_to}")
    if version < _MSK_MIGRATION_USER_VERSION:
        print(
            "ВНИМАНИЕ: БД не прошла миграцию МСК (квик 260912-mcj) — дефолтное окно задано "
            "в московском времени, а registration_date в этой БД, вероятно, ещё в UTC. "
            "Похоже, нужно передать --from/--to в UTC.",
            file=sys.stderr,
        )

    rows = await select_rows(season, date_from, date_to)
    print(f"\nНайдено заявок: {len(rows)}")
    _print_table(rows)

    if expect is not None and len(rows) != expect:
        print(
            f"\nВНИМАНИЕ: ожидалось {expect}, найдено {len(rows)} — проверьте окно/сезон.",
            file=sys.stderr,
        )

    if not rows:
        print("\nВыборка пуста — писать нечего.")
        return 0

    if not apply:
        print("\nЭто предпросмотр. Чтобы записать, добавь --apply")
        return 0

    from config import config

    backups_dir = Path(config.DB_PATH).resolve().parent / "backups"
    try:
        backup_path = backup_db(backups_dir)
    except Exception as e:
        print(f"\nОшибка бэкапа: {e} — запись НЕ выполнена.", file=sys.stderr)
        return 1
    size = backup_path.stat().st_size
    print(f"\nБэкап БД: {backup_path} ({size} байт)")

    ids = [r["telegram_id"] for r in rows]
    changed = await requeue(ids)
    print(f"Изменено строк в БД: {changed}")

    try:
        from services.sheets import bulk_update_status_in_sheet

        result = await bulk_update_status_in_sheet({str(i): "Новая" for i in ids})
        if result == -1:
            print(
                "ВНИМАНИЕ: bulk_update_status_in_sheet вернул ошибку (-1) — строки в БД уже "
                "возвращены в очередь, подписи в таблице обновите вручную.",
                file=sys.stderr,
            )
        else:
            print(f"Обновлено ячеек в таблице: {result}")
    except Exception as e:
        print(
            f"ВНИМАНИЕ: обновление Google Sheets упало ({e}) — строки в БД уже возвращены "
            "в очередь, подписи в таблице обновите вручную.",
            file=sys.stderr,
        )

    print("\nСтатус ПОСЛЕ:")
    _print_table(await fetch_by_ids(ids))
    print(f"\n{changed} возвращено в очередь.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--season", default=DEFAULT_SEASON, help=f"сезон (по умолчанию «{DEFAULT_SEASON}»)")
    parser.add_argument(
        "--from", dest="date_from", default=DEFAULT_FROM,
        help=(
            f"начало окна регистрации, по умолчанию «{DEFAULT_FROM}» — МОСКОВСКОЕ время "
            "(квик 260912-mcj сдвинул registration_date на +3ч UTC->МСК); если PRAGMA "
            "user_version БД = 0 (миграция ещё не прошла), передайте окно в UTC"
        ),
    )
    parser.add_argument(
        "--to", dest="date_to", default=DEFAULT_TO,
        help=(
            f"конец окна регистрации, по умолчанию «{DEFAULT_TO}» — МОСКОВСКОЕ время, "
            "см. пояснение к --from про PRAGMA user_version/UTC"
        ),
    )
    parser.add_argument("--apply", action="store_true", help="писать в базу и таблицу (иначе только отчёт)")
    parser.add_argument("--expect", type=int, default=None, help="ожидаемое число строк — предупредить при несовпадении")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.season, args.date_from, args.date_to, args.apply, args.expect))
    )
