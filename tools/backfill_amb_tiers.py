"""Разовый пересчёт ступеней амбассадоров (1–5, квота на любой ступени) для уже одобренных приглашённых.

Живые пути одобрения проверяют ступени сами, но только с момента включения программы —
приглашённые, одобренные раньше, ступень сами не дадут. Этот скрипт находит таких
амбассадоров и выдаёт ступени через ту же точку, что и живые одобрения
(`services.amb_tiers.check_tiers`: `INSERT OR IGNORE` по PRIMARY KEY, квота ступеней в
одной транзакции) — повторный запуск ничего не добавляет. Дедлайн подсчёта соблюдается.

По умолчанию — только предпросмотр «кому какая ступень», в базу ничего не пишется.
`--apply` записывает ступени ТИХО (амбассадоры сообщений не получают); `--notify` вместе с
`--apply` ставит каждому сообщение о старшей новой ступени (и отдельно — о каждой новой
ступени с квотой: место или лист ожидания) в очередь бота.
`--apply` работает и при выключенной программе — так и задумано: сначала пересчёт, потом
включение, иначе живые одобрения в промежутке обгоняют давно дошедших в очереди за местами
квот. `--notify` требует включённой программы.

Запуск на сервере:

    docker exec <контейнер-бота> python /app/tools/backfill_amb_tiers.py
    docker exec <контейнер-бота> python /app/tools/backfill_amb_tiers.py --apply
    docker exec <контейнер-бота> python /app/tools/backfill_amb_tiers.py --apply --notify
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль открывает stdout в cp1251 — кириллица падала бы UnicodeEncodeError (тот же
# приём, что tools/backfill_referral_credits.py).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROGRAM_OFF_NOTIFY_MESSAGE = (
    "Программа ступеней выключена, а --notify рассылает сообщения о ней. Запустите без "
    "--notify (ступени запишутся тихо) или сначала включите программу в админке "
    "(«🎮 Геймификация → 🎓 Ступени амбассадоров»)."
)
_O2O = {"granted": "квота: место выдано", "waitlist": "квота: лист ожидания"}


def _format_entry(entry: dict) -> str:
    who = f"@{entry['username']}" if entry["username"] else "без username"
    parts = []
    for tier in entry["tiers"]:
        label = f"{tier['tier']}"
        if tier["o2o_status"] in _O2O:
            label += f" ({_O2O[tier['o2o_status']]})"
        if tier["exists"]:
            label += " — уже есть"
        parts.append(label)
    return f"  {who} ({entry['telegram_id']}): прошли отбор {entry['qualified']} → ступени {', '.join(parts)}"


async def _run(apply: bool, notify: bool) -> int:
    """`database.db.init_db()` не зовём: таблицы создал бот при обычном старте, скрипт
    открывает существующую базу как есть."""
    from services import amb_tiers

    program = await amb_tiers.program_on()
    if apply and notify and not program:
        print(PROGRAM_OFF_NOTIFY_MESSAGE)
        return 2
    if await amb_tiers.deadline_passed():
        print("Дедлайн подсчёта ступеней уже прошёл — новые ступени не выдаются.")
        return 0

    preview = await amb_tiers.preview_backfill()
    season = await amb_tiers.current_season()
    print(f"Сезон: {season or 'не задан'}")
    if not program:
        print("Программа ступеней сейчас выключена." + ("" if apply else " Это предпросмотр."))
    if not preview:
        print("Новых ступеней к выдаче нет.")
        return 0

    new_count = granted = waitlist = 0
    print("\nКому какая ступень (в порядке очереди на места квот):")
    for entry in preview:
        print(_format_entry(entry))
        for tier in entry["tiers"]:
            if tier["exists"]:
                continue
            new_count += 1
            granted += tier["o2o_status"] == "granted"
            waitlist += tier["o2o_status"] == "waitlist"
    print(f"\nСтупеней к выдаче: {new_count}, места квот выданы/лист ожидания: {granted}/{waitlist}")

    if not apply:
        print(
            "\nЭто предпросмотр, ничего не записано. Запустите с --apply, чтобы записать; "
            "добавьте --notify, чтобы амбассадоры получили сообщения."
        )
        return 0

    written = 0
    for entry in preview:
        rows = await amb_tiers.check_tiers([entry["telegram_id"]], notify=notify, force=True)
        written += len(rows)
    print(f"\nЗаписано ступеней: {written}.")
    print("Сообщения амбассадорам поставлены в очередь бота." if notify
          else "Сообщений амбассадорам не отправляли (без --notify).")
    if not program:
        print("Теперь включите программу: «🎮 Геймификация → 🎓 Ступени амбассадоров».")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="записать ступени (без флага — только предпросмотр)")
    parser.add_argument("--notify", action="store_true", help="вместе с --apply: отправить амбассадорам сообщения о ступенях")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.notify and not args.apply:
        parser.error("--notify работает только вместе с --apply: без записи ступеней сообщать не о чем")
    return asyncio.run(_run(args.apply, args.notify))


if __name__ == "__main__":
    raise SystemExit(main())
