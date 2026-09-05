"""Хотфикс 06.09 (production, ветка hotfix/finalize-draft-fields): восстановление полей,
потерянных багом `services/reg_finalize.py::finalize_data` (см. соседний коммит
"fix(registration): финал анкеты берёт город, трек, источник и реферера из черновика").

С 05.09 16:04 UTC до накатки фикса `finalize_data` строила `users` только из
`draft["answers"]`, не заглядывая в колонки черновика (`event_city`, `participant_type`) и
`draft["meta"]` (`source`/`referrer_id`). Итог у новых пользователей за это окно:
`event_city` NULL, `source` подменён дефолтом «Самостоятельно» (даже если делегат пришёл по
деп-линку с меткой кампании), `referrer_id` потерян.

Этот скрипт — ОДНОРАЗОВЫЙ ремонт уже накопленных строк, работает напрямую с sqlite-файлом
(без aiosqlite/config — тот же приём, что `_ensure_column` в `database/db.py`, только тут
даже не нужен асинхронный движок, т.к. правки идут после того, как бот уже всё записал).
Источники правды:

  1. `event_city` — из `reg_events` (события 'form_started'/'form_completed', самое свежее
     непустое значение по `ts`), а если там пусто — из `reg_started` (там же, где бот вёл
     воронку доb460826, PK по telegram_id, значение по построению одно).
  2. `source` — из журнала бота (`Saved source_tag=<tag> for user <id>`, тот же лог, который
     писала `handlers/registration.py::_start_registration_flow` ДО бага); берём последнюю по
     времени строку на пользователя, не раньше `--since`.
  3. `referrer_id` — из того же журнала (`Saved referrer_id=<id> for user <id>`).

По умолчанию — dry-run: только печатает таблицу планируемых изменений, ничего не пишет.
`--apply` применяет все правки одной транзакцией и печатает «было -> стало» на каждую
затронутую строку.

Запуск НА СЕРВЕРЕ (внутри контейнера бота):

    python tools/backfill_finalize_fields.py \\
        --db /app/data/forum.db --log /app/logs/bot.log \\
        --since "2026-09-05 16:04"              # dry-run, ничего не меняет

    python tools/backfill_finalize_fields.py \\
        --db /app/data/forum.db --log /app/logs/bot.log \\
        --since "2026-09-05 16:04" --apply       # применить
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime

SOURCE_TAG_RE = re.compile(r"Saved source_tag=([A-Za-z0-9_-]+) for user (\d+)")
REFERRER_RE = re.compile(r"Saved referrer_id=(\d+) for user (\d+)")
LOG_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")

_SINCE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

SELF_SOURCE_DEFAULT = "Самостоятельно"


def parse_since(raw: str) -> datetime:
    """Принимает `--since` в любом из трёх привычных форматов (секунды часто не помнят
    наизусть — дата+часы:минуты самый частый ввод)."""
    for fmt in _SINCE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise SystemExit(
        f"--since={raw!r} не распознан — используйте 'YYYY-MM-DD HH:MM[:SS]' или 'YYYY-MM-DD'"
    )


def parse_log(log_path: str, since_dt: datetime) -> tuple[dict[int, str], dict[int, int]]:
    """Одним проходом по логу собирает ПОСЛЕДНИЙ (по порядку строк — лог хронологичен)
    source_tag и referrer_id на пользователя, отбрасывая всё раньше `--since`. Строки без
    распознаваемой метки времени в начале (например продолжение многострочного traceback)
    просто пропускаются — regex на них всё равно не сработает."""
    source_tags: dict[int, str] = {}
    referrers: dict[int, int] = {}
    try:
        f = open(log_path, encoding="utf-8", errors="replace")
    except OSError as e:
        raise SystemExit(f"Не удалось открыть лог {log_path!r}: {e}")
    with f:
        for line in f:
            m_ts = LOG_LINE_TS_RE.match(line)
            if not m_ts:
                continue
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since_dt:
                continue
            m = SOURCE_TAG_RE.search(line)
            if m:
                tag, uid = m.group(1), int(m.group(2))
                source_tags[uid] = tag  # последняя запись в хронологическом логе побеждает
                continue
            m = REFERRER_RE.search(line)
            if m:
                ref_id, uid = int(m.group(1)), int(m.group(2))
                referrers[uid] = ref_id
    return source_tags, referrers


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _resolve_event_city(conn: sqlite3.Connection, telegram_id: int) -> str | None:
    row = conn.execute(
        "SELECT event_city FROM reg_events "
        "WHERE telegram_id = ? AND event IN ('form_started', 'form_completed') "
        "AND event_city IS NOT NULL AND event_city != '' "
        "ORDER BY ts DESC LIMIT 1",
        (telegram_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT event_city FROM reg_started WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]
    return None


def plan_changes(
    conn: sqlite3.Connection,
    since_raw: str,
    source_tags: dict[int, str],
    referrers: dict[int, int],
) -> list[dict]:
    """Возвращает список патчей `{"telegram_id": ..., <column>: (old, new), ...}` — по одной
    записи на пользователя, у которого есть хоть одно реальное изменение."""
    has_source_from_tag = _has_column(conn, "users", "source_from_tag")
    cur = conn.execute(
        "SELECT telegram_id, event_city, source, referrer_id FROM users "
        "WHERE registration_date >= ?",
        (since_raw,),
    )
    rows = cur.fetchall()

    changes: list[dict] = []
    for telegram_id, event_city, source, referrer_id in rows:
        patch: dict = {}

        if event_city is None:
            new_city = _resolve_event_city(conn, telegram_id)
            if new_city:
                patch["event_city"] = (event_city, new_city)

        if source == SELF_SOURCE_DEFAULT:
            tag = source_tags.get(telegram_id)
            if tag:
                patch["source"] = (source, tag)
                if has_source_from_tag:
                    patch["source_from_tag"] = (None, 1)

        if referrer_id is None:
            ref = referrers.get(telegram_id)
            if ref:
                patch["referrer_id"] = (referrer_id, ref)

        if patch:
            changes.append({"telegram_id": telegram_id, **patch})

    return changes


def apply_changes(conn: sqlite3.Connection, changes: list[dict]) -> None:
    """Одна транзакция на весь список — либо применяются все правки, либо (при ошибке) ни
    одной; конкретно эта функция вызывается только когда `--apply` явно передан."""
    try:
        for change in changes:
            telegram_id = change["telegram_id"]
            cols = {k: v[1] for k, v in change.items() if k != "telegram_id"}
            set_clause = ", ".join(f"{col} = ?" for col in cols)
            params = list(cols.values()) + [telegram_id]
            conn.execute(f"UPDATE users SET {set_clause} WHERE telegram_id = ?", params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _print_plan(changes: list[dict], apply: bool) -> None:
    if not changes:
        print("Изменений не найдено — база уже в порядке (или --since выбран неверно).")
        return

    verb = "ПРИМЕНЕНО" if apply else "БУДЕТ ИЗМЕНЕНО (dry-run — добавьте --apply)"
    print(f"{verb}: {len(changes)} пользователь(ей)\n")
    for change in changes:
        telegram_id = change["telegram_id"]
        parts = []
        for col, value in change.items():
            if col == "telegram_id":
                continue
            old, new = value
            parts.append(f"{col}: {old!r} -> {new!r}")
        print(f"  telegram_id={telegram_id}: " + "; ".join(parts))
    print()

    counts = {}
    for change in changes:
        for col in change:
            if col == "telegram_id":
                continue
            counts[col] = counts.get(col, 0) + 1
    summary = ", ".join(f"{col}: {n}" for col, n in counts.items())
    print(f"Итого по колонкам — {summary}")


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Восстановление event_city/source/referrer_id для заявок, потерянных багом "
            "finalize_data (05.09 16:04 UTC — накатка хотфикса)."
        )
    )
    parser.add_argument("--db", required=True, help="Путь к файлу sqlite (forum.db)")
    parser.add_argument("--log", required=True, help="Путь к логу бота (bot.log)")
    parser.add_argument(
        "--since", required=True,
        help="Начало окна бага, 'YYYY-MM-DD HH:MM[:SS]' (например '2026-09-05 16:04')",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Применить правки. Без флага — только показать план (dry-run).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    since_dt = parse_since(args.since)

    source_tags, referrers = parse_log(args.log, since_dt)

    conn = sqlite3.connect(args.db)
    try:
        changes = plan_changes(conn, args.since, source_tags, referrers)
        _print_plan(changes, args.apply)
        if args.apply and changes:
            apply_changes(conn, changes)
            print("Готово — правки закоммичены.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
