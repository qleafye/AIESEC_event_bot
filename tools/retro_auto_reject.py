"""Разовое применение действующих правил автоотказа к уже поданным заявкам.

Решение владельца 23.09: правило «Курс из списка» завели 22.09 вечером, а заявки под него шли с
понедельника. Всем, кто подал анкету с даты `--since` и подходит под ВКЛЮЧЁННЫЕ сейчас правила
(своего города и трека), — тот же исход, что дал бы движок на подаче:

- статус «на рассмотрении» -> автоотказ целиком: колонки автоотказа, статус `rejected`, строка
  журнала автоотказов, письмо делегату с текстом правила (через `apply_decision_effects` —
  тихие часы соблюдаются: ночью письмо встаёт в очередь), статус в листе, решение
  `application_decisions` от имени автоотказа;
- уже отклонён вручную -> только пометка (колонки + журнал), чтобы статистика «какое правило
  сколько отсеяло» и «реальные заявки» считались верно; второго письма нет, ручное решение
  в `application_decisions` не трогается;
- одобрен -> не трогается.

Уже помеченные автоотказом пропускаются — повторный запуск ничего не удвоит.

Запуск на сервере (по умолчанию — только отчёт, без единой записи):
    docker exec -w /app -e PYTHONPATH=/app youlead26-bot-1 python tools/retro_auto_reject.py --since 2026-09-21
    ... --apply     # применить
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from tools.send_resume_recovery import build_bot  # noqa: E402 — тот же Bot/прокси, что у main.py


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, help="дата подачи анкеты, с которой применять (ГГГГ-ММ-ДД)")
    parser.add_argument("--apply", action="store_true", help="применить (без флага — только отчёт)")
    args = parser.parse_args()

    from services import reject_retro

    report = await reject_retro.preview(args.since)
    print(f"Под правила с {args.since}: pending={report['pending']}, rejected={report['rejected']}, "
          f"approved={report['approved']}")
    print(f"Будет обработано: {report['pending'] + report['rejected']} "
          f"(одобренные не трогаются: {report['approved']})")
    if not args.apply:
        print("Отчёт без изменений. Для применения добавьте --apply.")
        return
    bot = await build_bot()
    try:
        done = await reject_retro.apply(bot, args.since)
    finally:
        await bot.session.close()
    print(f"Готово: автоотказ {done['pending']}, пометка у отклонённых вручную {done['rejected']}, "
          f"сбоев {done['failed']}")


if __name__ == "__main__":
    asyncio.run(main())
