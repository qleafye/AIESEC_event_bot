"""One-shot cleanup: снять ведущий апостроф, приписанный старым `_csv_safe` (database/db.py) к
ячейкам, уходившим в Google Sheets (находка 08-sheets-dashboard, квик 260919).

Фон: до фикса каждая ячейка листа (телефоны, юзернеймы, ФИО, поле «-» и т.п.) проходила через
CSV-инъекционный нейтрализатор, который приписывает ВИДИМЫЙ апостроф к строкам, начинающимся с
= + - @ \\t \\r. Google Sheets пишется через gspread RAW (явно — services/sheets.py::_RAW) и
RAW-ячейку Google НИКОГДА не считает формулой, так что апостроф не защищал ни от чего — только
портил данные: `'+79991234567`, `'@username`, `'-` не находятся фильтром/ВПР/копированием в
звонилку. Код теперь пишет БЕЗ апострофа (см. database.db._sheet_safe); этот скрипт чистит уже
записанные на лист старые строки — новые строки апострофа не несут и без него.

НЕ трогает БД — только ячейки Google Sheets. Запись в прод-таблицу решает владелец, поэтому
скрипт НИКОГДА не запускается автоматически и по умолчанию работает в режиме предпросмотра.

Запуск на СЕРВЕРЕ (тот же .env, что у бота):

    python -m scripts.strip_sheet_apostrophes --tab "Реги бот"            # предпросмотр (default)
    python -m scripts.strip_sheet_apostrophes --tab "Реги бот" --apply    # реальная запись
    python -m scripts.strip_sheet_apostrophes --tab "Реги бот" --tab "Party" --apply
    python -m scripts.strip_sheet_apostrophes --all-tabs --apply          # ВСЕ вкладки таблицы

--tab (можно указать несколько раз) — имя конкретной вкладки, которую пишет бот: основная
(настройка main_sheet_tab / GOOGLE_SHEET_TAB), «Party», «Краткая», вкладки городов, «История
правок», «Вопросы», «Опросы», «Незавершённые»/dropout-вкладки. Точный набор зависит от того, что
настроено у конкретной установки — скрипт эти имена не угадывает, их нужно передать явно.

--all-tabs — пройти по ВСЕМ вкладкам таблицы без разбора. Осторожно: если на листе есть
вкладка, которую менеджер ведёт вручную формулами (например «STATISTICS»), --all-tabs может
задеть и её ведущие апострофы, если они там осмысленно стоят (маловероятно, но проверьте вывод
предпросмотра перед --apply).

Без --apply ничего не пишется — только печатается, что было бы изменено. Реальная запись —
один batch_update на вкладку, явный value_input_option=RAW (та же защита, что и во всём
services/sheets.py), так что освобождённое от апострофа значение не начинает вдруг
интерпретироваться как формула.
"""
import argparse

import gspread

from config import config

# Тот же набор триггеров CSV-инъекции, что и в database.db._CSV_INJECTION_PREFIXES, только
# с уже приписанным апострофом впереди — именно так `_csv_safe` записывал их на лист.
_PREFIXES = ("'=", "'+", "'-", "'@")


def find_fixes(values: list[list[str]]) -> list[dict]:
    """values — worksheet.get_all_values() (2D список строк, строка 1 — заголовок, 1-based
    нумерация ячеек листа). Возвращает список dict'ов для gspread batch_update:
    {"range": "<A1>", "values": [[<без апострофа>]]} — по одному на каждую задетую ячейку.
    Заголовок (row 1) никогда не трогаем."""
    updates = []
    for row_idx, row in enumerate(values, start=1):
        if row_idx == 1:
            continue
        for col_idx, cell in enumerate(row, start=1):
            if isinstance(cell, str) and cell.startswith(_PREFIXES):
                a1 = gspread.utils.rowcol_to_a1(row_idx, col_idx)
                updates.append({"range": a1, "values": [[cell[1:]]]})
    return updates


def _process_tab(ws, apply: bool) -> int:
    values = ws.get_all_values()
    updates = find_fixes(values)
    if not updates:
        print(f"  «{ws.title}»: апострофов не найдено, чисто")
        return 0
    print(f"  «{ws.title}»: {len(updates)} ячеек с ведущим апострофом")
    for u in updates[:10]:
        print(f"    {u['range']}: {u['values'][0][0]!r}")
    if len(updates) > 10:
        print(f"    ... и ещё {len(updates) - 10}")
    if apply:
        ws.batch_update(updates, value_input_option=gspread.utils.ValueInputOption.raw)
        print(f"  «{ws.title}»: записано.")
    return len(updates)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Снять ведущий апостроф (артефакт старого _csv_safe) с вкладок Google-таблицы бота."
    )
    parser.add_argument(
        "--tab", action="append", default=[],
        help='Имя вкладки (можно указать несколько раз: --tab "Реги бот" --tab Party).',
    )
    parser.add_argument("--all-tabs", action="store_true", help="Пройти по ВСЕМ вкладкам таблицы.")
    parser.add_argument("--apply", action="store_true", help="Реально писать (без флага — только предпросмотр).")
    return parser.parse_args()


def main():
    args = _parse_args()
    if not args.tab and not args.all_tabs:
        print('Нужно указать хотя бы один --tab "Имя вкладки" или --all-tabs. См. докстринг файла.')
        return

    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        print("GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS_FILE не настроены — выход.")
        return

    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)

    if args.all_tabs:
        worksheets = sh.worksheets()
    else:
        worksheets = []
        for name in args.tab:
            try:
                worksheets.append(sh.worksheet(name))
            except gspread.WorksheetNotFound:
                print(f'Вкладка "{name}" не найдена — пропуск')

    mode = "ПРИМЕНЯЮ (--apply)" if args.apply else "ПРЕДПРОСМОТР (по умолчанию, ничего не пишу)"
    print(f"{mode}: {len(worksheets)} вкладок")
    print()

    total = 0
    for ws in worksheets:
        total += _process_tab(ws, args.apply)

    print()
    suffix = "" if args.apply else " (ничего не записано — добавьте --apply для реальной записи)"
    print(f"Итого ячеек с апострофом: {total}{suffix}")


if __name__ == "__main__":
    main()
