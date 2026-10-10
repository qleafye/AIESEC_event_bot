"""Сброс статусов амбассадора, оставшихся после миграции, с предпросмотром.

При первом старте новой версии бот переводит старые флаги в статус: кто ответил «да» в анкете,
становится кандидатом, действующие амбассадоры — амбассадорами. Если владелец или менеджер
решили, что эти статусы не нужны (кандидаты прошлых сезонов, «да» из старой анкеты), этот
инструмент сбрасывает их в «нет статуса».

Кого сбрасываем (флаги взаимоисключающие):
  --past-seasons    (по умолчанию) все, у кого есть статус, а сезон строки не равен
                    текущему сезону события;
  --candidates-all  все кандидаты текущего сезона.

Без --apply ничего не пишет — только показывает, кого сбросит, с разбивкой по статусу и сезону.
Запись идёт через единственного писателя статуса (`database.amb_status_db.set_status`) с
проверкой прежнего статуса: если статус человека успел смениться (менеджер нажал «Взять»),
строка пропускается. Сообщений никому не шлёт, баллов не начисляет и не снимает. Выданный
пакет не трогает: место за человеком с выданным пакетом остаётся.

Запуск (сначала предпросмотр):

    docker exec realtalk26-bot-1 python /app/tools/amb_status_reset.py
    docker exec realtalk26-bot-1 python /app/tools/amb_status_reset.py --candidates-all
    docker exec realtalk26-bot-1 python /app/tools/amb_status_reset.py --candidates-all --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows-консоль по умолчанию открывает stdout/stderr в cp1251 — кириллица в `--help` и в
# отчёте падает `UnicodeEncodeError` (тот же приём, что tools/backfill_referral_credits.py).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from services.amb_status_reset import (  # noqa: E402
    NO_COLUMN as _NO_COLUMN,
    SCOPE_CANDIDATES,
    SCOPE_PAST,
    breakdown as _breakdown,
    has_status_column as _has_status_column,
)
from services import amb_status_reset as _service  # noqa: E402


async def run(scope: str = SCOPE_PAST, apply: bool = False) -> tuple[int, list[str]]:
    """Ядро инструмента: (код выхода, строки отчёта). Не зовёт init_db — открывает
    существующую базу как есть. Логика — `services/amb_status_reset.py` (она же под кнопкой в админке)."""
    if not await _has_status_column():
        return 2, [_NO_COLUMN]

    plan = await _service.preview(scope)
    season, rows = plan["season"], plan["rows"]
    what = ("кандидаты текущего сезона" if scope == SCOPE_CANDIDATES
            else "статусы прошлых сезонов")
    lines = [
        f"Сезон события: {('«' + season + '»') if season else 'не задан'}",
        f"Что сбрасываем: {what}",
    ]
    if not rows:
        lines.append("Сбрасывать нечего.")
        return 0, lines

    if not apply:
        lines.append(f"Будет сброшено: {len(rows)}")
        lines += _breakdown(rows)
        lines.append("")
        lines.append("Это предпросмотр, в базе ничего не изменилось. Чтобы сбросить, добавьте --apply")
        return 0, lines

    result = await _service.apply(scope, plan)
    done, skipped = result["done"], result["skipped"]
    lines.append(f"Сброшено: {len(done)}")
    if done:
        lines += _breakdown(done)
    if skipped:
        lines.append(
            f"Пропущено (статус успел смениться, например менеджер нажал «Взять»): {len(skipped)}"
        )
    lines.append("Сообщений никому не отправлено.")
    return 0, lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--past-seasons", dest="scope", action="store_const", const=SCOPE_PAST,
        help="сбросить статусы строк прошлых сезонов (по умолчанию)",
    )
    group.add_argument(
        "--candidates-all", dest="scope", action="store_const", const=SCOPE_CANDIDATES,
        help="сбросить всех кандидатов текущего сезона",
    )
    parser.set_defaults(scope=SCOPE_PAST)
    parser.add_argument(
        "--apply", action="store_true",
        help="записать сброс (без флага — только предпросмотр)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, lines = asyncio.run(run(scope=args.scope, apply=args.apply))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
