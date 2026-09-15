"""Пересборка Google-таблицы из БД без бота — та же логика, что кнопка «♻️ Пересобрать таблицу»
(`handlers/admin_sheets.py::rebuild_sheet`): главная вкладка + именованные вкладки городов/
short/party по тому же строителю `build_sheet_batches`, что и живой хендлер и `sync_sheet`.

Квик 260915-4is: раньше скрипт строил СОБСТВЕННУЮ раскладку (одна шапка `active_sheet_headers()`
на ВСЕ вкладки, без учёта трека и даже без учёта города — хуже самого хендлера, который хотя бы
считал шапку по городу). Теперь единственный источник правды — `build_sheet_batches` из
`handlers/admin_sheets.py`; здесь никакой второй копии правил маршрутизации.

Запуск внутри контейнера бота:
    python tools/rebuild_sheet_headless.py            # сухой прогон: сколько строк куда ляжет
    python tools/rebuild_sheet_headless.py --apply    # перезаписать вкладки
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_KIND_LABEL = {"main": "полная", "short": "короткая", "party": "party"}


async def main(apply: bool) -> int:
    from handlers.admin_sheets import (  # noqa: WPS433 — те же имена, что у хендлера
        build_sheet_batches,
        get_all_users_dicts,
        rebuild_main_sheet,
        sync_named_worksheet,
    )

    all_users = await get_all_users_dicts()
    batches = await build_sheet_batches(all_users)
    main_batch, named_batches = batches[0], batches[1:]

    print(f"главная вкладка (полная): {len(main_batch.rows)} строк, {len(main_batch.headers)} колонок")
    for batch in named_batches:
        label = _KIND_LABEL.get(batch.kind, batch.kind)
        print(f"вкладка «{batch.tab}» ({label}): {len(batch.rows)} строк, {len(batch.headers)} колонок")

    if not apply:
        print("Сухой прогон — добавьте --apply для перезаписи.")
        return 0

    count = await rebuild_main_sheet(main_batch.headers, main_batch.rows)
    print(f"главная вкладка: результат {count}")
    if count < 0:
        print("Главная вкладка не перезаписана (ошибка/не закреплена) — города не трогаю.")
        return 1
    for batch in named_batches:
        res = await sync_named_worksheet(batch.tab, batch.headers, batch.rows)
        print(f"вкладка «{batch.tab}»: результат {res}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="перезаписать вкладки (иначе сухой прогон)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
