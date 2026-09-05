"""Пересборка Google-таблицы из БД без бота — та же логика, что кнопка «♻️ Пересобрать таблицу»
(`handlers/admin_settings.py::rebuild_sheet`): главная вкладка + вкладки городов по тому же
резолверу `city_row_tab`, что и живой append.

Запуск внутри контейнера бота:
    python tools/rebuild_sheet_headless.py            # сухой прогон: сколько строк куда ляжет
    python tools/rebuild_sheet_headless.py --apply    # перезаписать вкладки
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(apply: bool) -> int:
    from handlers.admin_settings import (  # noqa: WPS433 — те же имена, что у хендлера
        _sheet_value_map,
        active_sheet_headers,
        city_row_tab,
        get_all_users_dicts,
        rebuild_main_sheet,
        sync_named_worksheet,
    )

    headers = await active_sheet_headers()
    all_users = await get_all_users_dicts()
    main_rows: list[list] = []
    city_rows: dict[str, list[list]] = {}
    for u in all_users:
        row = [_sheet_value_map(u).get(h, "-") for h in headers]
        tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
        if tab is None:
            main_rows.append(row)
        else:
            city_rows.setdefault(tab, []).append(row)

    print(f"колонок: {len(headers)}; главная вкладка: {len(main_rows)} строк")
    for tab, trows in city_rows.items():
        print(f"вкладка «{tab}»: {len(trows)} строк")
    if not apply:
        print("Сухой прогон — добавьте --apply для перезаписи.")
        return 0

    count = await rebuild_main_sheet(headers, main_rows)
    print(f"главная вкладка: результат {count}")
    if count < 0:
        print("Главная вкладка не перезаписана (ошибка/не закреплена) — города не трогаю.")
        return 1
    for tab, trows in city_rows.items():
        res = await sync_named_worksheet(tab, headers, trows)
        print(f"вкладка «{tab}»: результат {res}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="перезаписать вкладки (иначе сухой прогон)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
