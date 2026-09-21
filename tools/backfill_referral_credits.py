"""Разовый бэкафилл баллов за приглашённых задним числом (Phase 32, план 32-05, D-23).

До запуска амбассадорского слоя часть приглашённых уже была одобрена — эта операция находит
их и начисляет баллы пригласившим-амбассадорам через ту же идемпотентную точку
(`services.referrals.backfill_approved`), что использует и «живое» одобрение
(`database.db.claim_referral_credit_atomic` — `INSERT OR IGNORE` по PRIMARY KEY, повтор
запуска безопасен). `wave_id` у бэкафилла ВСЕГДА `None` и `source='backfill'` — задним числом
баллы идут ТОЛЬКО в общий зачёт, ни в одну волну (правило волны применимо только к моменту
самого события одобрения).

WR-17 (32-REVIEW.md): кандидаты ограничены ТЕКУЩИМ сезоном (`bot_settings.event_season`) —
без этого фильтра под бэкафилл попадали и легаси-строки без сезона, и делегаты, импортированные
из прошлого события. `--season` переопределяет сезон явно (например, для разового прогона по
архивным данным).

НЕОБРАТИМО: как и «живое» начисление, баллы бэкафилла не отзываются задним числом (D-22).

Запуск (по умолчанию — предпросмотр, ничего не пишет):

    docker exec youlead26-bot-1 python /app/tools/backfill_referral_credits.py
    docker exec youlead26-bot-1 python /app/tools/backfill_referral_credits.py --apply
    docker exec youlead26-bot-1 python /app/tools/backfill_referral_credits.py --season "YL'26"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль по умолчанию открывает stdout/stderr в cp1251 — кириллица в `--help`
# (argparse печатает докстринг модуля) и в отчёте падает `UnicodeEncodeError` (тот же приём,
# что `tools/requeue_auto_approved.py`).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


async def _init_and_run(dry_run: bool, season: str | None) -> dict:
    """`database.db.init_db()` НЕ зовём — таблицы/колонки уже существуют на боевой БД
    (план 32-01 их создал миграцией при обычном старте бота); этот скрипт открывает
    существующий файл БД как есть и ничего не запускает поверх него (ни polling, ни
    планировщик) — только `services.referrals.backfill_approved`."""
    from services.referrals import backfill_approved

    return await backfill_approved(dry_run=dry_run, season=season)


def main(apply: bool, season: str | None) -> int:
    dry_run = not apply
    summary = asyncio.run(_init_and_run(dry_run, season))

    season_label = summary["season"] or "не задан (сезон не сконфигурирован)"
    print(f"Сезон: {season_label}")
    print(f"Кандидатов (одобрен приглашённый, пригласивший сейчас амбассадор): {summary['candidates']}")

    if summary["breakdown"]:
        print("\nАмбассадор — приглашённых — баллов:")
        for entry in summary["breakdown"]:
            print(
                f"  {entry['referrer_name']} (#{entry['referrer_id']}) — "
                f"{entry['invitees']} — {entry['coins']}"
            )

    if dry_run:
        print(
            f"\nПРЕДПОКАЗ («что будет» при --apply): начислено бы {summary['credited']} "
            f"приглашённым, суммарно {summary['coins']} баллов, {summary['ambassadors']} "
            "амбассадорам."
        )
        print("\nЭто предпросмотр. Ничего не записано. Чтобы записать, добавь --apply")
        return 0

    print(
        f"\nНачислено: {summary['credited']} приглашённых, суммарно {summary['coins']} баллов, "
        f"{summary['ambassadors']} амбассадорам."
    )
    print("Операция необратима — баллы бэкафилла, как и «живое» начисление, не отзываются.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="только показать, что будет начислено (по умолчанию — без --apply запись не идёт)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="реально начислить баллы (необратимо — без этого флага только отчёт)",
    )
    parser.add_argument(
        "--season", default=None,
        help=(
            "фильтр по сезону (WR-17): по умолчанию — текущий bot_settings.event_season; "
            "укажи явно, чтобы прогнать по архивному сезону"
        ),
    )
    args = parser.parse_args()
    raise SystemExit(main(args.apply, args.season))
