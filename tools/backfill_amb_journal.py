"""Бэкафилл журнала зачётов приглашённых (referral_credits) для действующих событий.

Находит одобренных приглашённых текущего сезона, у которых в журнале ещё нет строки, и
дописывает строки с баллами 0 и source 'backfill'. Никому ничего НЕ начисляет, ступени не
выдаёт, сообщений не пишет; существующие строки и начисления не меняет. INSERT OR IGNORE по
PRIMARY KEY — повторный запуск ничего не добавляет.

По умолчанию — предпросмотр (ничего не пишет). Перед --apply на проде сделать бэкап forum.db.

Запуск:

    docker exec <контейнер бота> python /app/tools/backfill_amb_journal.py
    docker exec <контейнер бота> python /app/tools/backfill_amb_journal.py --apply
    docker exec <контейнер бота> python /app/tools/backfill_amb_journal.py --season "YL 26/2"

Схема журнала создаётся при старте бота на новой версии; скрипт init_db не зовёт.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


async def backfill_journal(*, apply: bool, season: str | None) -> dict:
    """Ядро скрипта. Возвращает `{"schema": bool, "season", "rows", "breakdown", "inserted"}`."""
    from database import amb_journal_db
    from database import db as _db
    from services.timeutil import msk_now

    if not await amb_journal_db.has_journal_schema():
        return {"schema": False, "season": season, "rows": 0, "breakdown": [], "inserted": 0}
    if season is None:
        season = ((await _db.get_setting("event_season")) or "").strip() or None
    candidates = await amb_journal_db.backfill_candidates(season)
    for row in candidates:
        row["season"] = season or row.get("season")

    by_referrer: dict[int, dict] = {}
    for row in candidates:
        entry = by_referrer.setdefault(
            int(row["referrer_id"]),
            {"referrer_id": int(row["referrer_id"]),
             "referrer_name": row.get("referrer_name") or "Без имени", "invitees": 0},
        )
        entry["invitees"] += 1
    breakdown = sorted(by_referrer.values(), key=lambda e: (-e["invitees"], e["referrer_id"]))

    inserted = 0
    if apply and candidates:
        inserted = await amb_journal_db.insert_backfill_rows(
            candidates, at=msk_now().strftime("%Y-%m-%d %H:%M:%S")
        )
    return {"schema": True, "season": season, "rows": len(candidates),
            "breakdown": breakdown, "inserted": inserted}


def main(apply: bool, season: str | None) -> int:
    summary = asyncio.run(backfill_journal(apply=apply, season=season))
    if not summary["schema"]:
        print("Сначала перезапустите бота на новой версии — схема журнала ещё не создана.")
        return 2
    print(f"Сезон: {summary['season'] or 'не задан'}")
    if summary["rows"] == 0:
        print("Дописывать нечего.")
        return 0
    print(f"Будет дописано строк: {summary['rows']}")
    print("\nПригласивший — сколько приглашённых:")
    for entry in summary["breakdown"]:
        print(f"  {entry['referrer_name']} (#{entry['referrer_id']}) — {entry['invitees']}")
    if not apply:
        print("\nЭто предпросмотр. Ничего не записано. Чтобы записать, добавьте --apply")
        return 0
    print(f"\nДописано строк: {summary['inserted']}. Баллы не начислялись, сообщения не отправлялись.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="реально дописать строки (без флага — только предпросмотр)")
    parser.add_argument("--season", default=None,
                        help="сезон (по умолчанию — текущий event_season)")
    args = parser.parse_args()
    raise SystemExit(main(args.apply, args.season))
