"""Квик 260919 (находка 08-sheets-dashboard, review-260919-sections): гвардия против регресса
CWE-1236-фикса — раньше `_csv_safe` (database/db.py) приписывал ВИДИМЫЙ апостроф к телефонам/
юзернеймам, уходящим в Google Sheets, хотя gspread пишет RAW и Sheets формулу так не считает.
385+ строк на проде несли `'+79991234567` / `'@username` — ВПР/фильтр их не находили.

Три сторожа:
(а) AST — ни один пишущий вызов gspread в services/sheets.py не остаётся без явного
    value_input_option=RAW (не полагаемся на дефолт библиотеки — апгрейд gspread может его
    незаметно сменить); update_cell (который RAW не поддерживает вовсе, gspread хардкодит
    USER_ENTERED) запрещён совсем.
(б) Поведенческий — строка листа делегата с телефоном/юзернеймом не содержит ведущего
    апострофа.
(в) Поведенческий — CSV-экспорт (Excel-путь, database.db.export_users_csv) по-прежнему
    нейтрализует через _csv_safe — та защита была и остаётся нужна, потому что Excel/LibreOffice
    ОТКРЫВАЯ .csv файл реально парсит ведущий =/+/-/@ как формулу.

pytest-asyncio недоступно в окружении — асинхронные помощники гоняются через asyncio.run(),
как в соседних файлах (tests/test_block7_low.py и т.п.).
"""
import ast
import asyncio
from pathlib import Path

from config import config
from database import db
from handlers import registration as reg

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHEETS_PY = _REPO_ROOT / "services" / "sheets.py"

# Методы gspread.Worksheet, которые пишут ЗНАЧЕНИЯ ячеек (а не структуру/форматирование) и
# принимают value_input_option.
_VALUE_WRITE_METHODS = {"append_row", "append_rows", "update", "insert_row", "batch_update"}
# update_cell хардкодит USER_ENTERED в самом gspread — параметра value_input_option у него нет
# вовсе, так что «добавить RAW» невозможно; единственный безопасный путь — не звать его.
_FORBIDDEN_METHODS = {"update_cell"}


def _calls_missing_raw(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        name = func.attr
        if name in _FORBIDDEN_METHODS:
            offenders.append((node.lineno, f"{name}() запрещён — gspread хардкодит USER_ENTERED"))
            continue
        if name not in _VALUE_WRITE_METHODS:
            continue
        # Spreadsheet.batch_update (структурные запросы — форматирование/валидация) зовётся с
        # ОДНИМ dict-литералом {"requests": [...]}, а не со списком value-range обновлений —
        # это не запись значений, value_input_option там не применим. Единственный такой вызов
        # в модуле — _apply_status_formatting_sync's sheet.spreadsheet.batch_update(...).
        if name == "batch_update" and node.args and isinstance(node.args[0], ast.Dict):
            continue
        has_raw_kw = any(kw.arg == "value_input_option" for kw in node.keywords)
        if not has_raw_kw:
            offenders.append((node.lineno, f"{name}() без явного value_input_option"))
    return offenders


def test_every_gspread_value_write_has_explicit_raw():
    offenders = _calls_missing_raw(_SHEETS_PY)
    assert not offenders, (
        "services/sheets.py: каждый пишущий вызов gspread обязан нести явный "
        f"value_input_option=RAW (находка 08-sheets-dashboard): {offenders}"
    )


def _use_tmp_db(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)


def test_delegate_row_phone_and_username_have_no_leading_apostrophe(tmp_path):
    """Живой сценарий из находки: телефон «+7…» и юзернейм «@…» в строке главного листа."""
    _use_tmp_db(tmp_path, "raw_guard_row.db")

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_phone", "on")
        return await reg.active_sheet_row({
            "telegram_id": 1,
            "full_name": "Иван Иванов",
            "username": "@ivan_ivanov",
            "phone": "+79991234567",
            "registration_date": "2026-09-19 10:00:00",
        })

    row = asyncio.run(go())
    assert "+79991234567" in row
    assert "'+79991234567" not in row
    for cell in row:
        if isinstance(cell, str):
            assert not cell.startswith("'"), f"ведущий апостроф выжил в строке листа: {cell!r}"


def test_csv_export_still_neutralizes_formula_injection(tmp_path):
    """export_users_csv — настоящий CSV-файл, открываемый в Excel/LibreOffice, где ведущий
    =/+/-/@ реально триггерит формулу. Этот путь _csv_safe трогать было нельзя и не тронули."""
    _use_tmp_db(tmp_path, "raw_guard_csv.db")

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 1,
            "full_name": "=cmd()",
            "username": "@evil",
            "phone": "+79991234567",
            "registration_date": "2026-09-19 10:00:00",
        })
        return await db.export_users_csv()

    headers, rows = asyncio.run(go())
    row = rows[0]
    full_name_idx = headers.index("ФИО")
    username_idx = headers.index("Username")
    phone_idx = headers.index("Телефон")
    assert row[full_name_idx] == "'=cmd()"
    assert row[username_idx] == "'@evil"
    assert row[phone_idx] == "'+79991234567"
