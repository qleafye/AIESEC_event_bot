"""Quick 260902-vth: два журнальных листа В ТОЙ ЖЕ Google-таблице заявок (решение владельца
02.09) — «История правок» (кто что менял в уже поданной анкете, из `reg_answer_history`,
Phase 21) и «Вопросы» (вопросы делегатов боту, из `delegate_questions`, Phase 8).

Форма — буква в букву `services/polls.py`: полная пересборка листа (`sync_named_worksheet` —
clear + перезапись), не append по событию. Обрыв прокси даёт «лист не обновился», а не молча
потерянную строку (память проекта: sheet-append-no-retry-on-proxy-drop) — ровно то же
рассуждение, что уже отработано для вкладки опросов.

Автосинхрон (fire-and-forget точка врезки из `database/db.py`) — Task 2 этого же квика.
"""
import logging
from datetime import datetime

from database.db import (
    list_answer_history,
    list_questions,
    get_all_users_dicts,
    _csv_safe,
)
from settings_schema import get_setting_typed, SETTINGS_SCHEMA
import reg_engine

logger = logging.getLogger(__name__)

HISTORY_SHEET_HEADERS = [
    "Дата", "Telegram ID", "Делегат", "Username", "Источник", "Вопрос", "Было", "Стало", "Сезон",
]
QUESTIONS_SHEET_HEADERS = [
    "Дата", "Telegram ID", "Делегат", "Username", "Город", "Вопрос", "Кому ушло",
]

SOURCE_LABELS = {"bot": "бот", "miniapp": "приложение"}

# Обратная карта строится ЗДЕСЬ (reg_engine не трогаем — он на потолке правок этого квика):
# users-колонка -> step_key, чтобы отдать колонку в `reg_engine.label_for` и получить
# человеческую подпись вопроса, как в мастере анкеты и в профиле.
_COLUMN_TO_STEP = {col: step for step, col in reg_engine.STEP_TO_COLUMN.items()}


def _column_label(column: str) -> str:
    """Человеческая подпись поля анкеты по имени колонки `users`. Колонка вне
    `STEP_TO_COLUMN` (например служебное поле) печатается своим именем — без падения."""
    step = _COLUMN_TO_STEP.get(column)
    if step is None:
        return column
    return reg_engine.label_for(step)


def _fmt_dt(raw: str | None) -> str:
    """Два формата времени в одном хелпере — потому что `record_answer_history` пишет
    "%Y-%m-%d %H:%M:%S", а `create_question` пишет `datetime.utcnow().isoformat()`, и это факт
    кода (разные таблицы, разный возраст), а не недосмотр. Неразобранное отдаётся как есть
    (fail-soft, форма `polls._fmt_date`)."""
    if not raw:
        return ""
    for parser in (
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.fromisoformat(s),
    ):
        try:
            return parser(raw).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue
    return str(raw)


def _cell(value) -> str:
    """None -> пустая строка (не «None»); остальное — в строку, потом через `_csv_safe`."""
    return "" if value is None else str(value)


async def build_history_sheet_rows() -> list[list]:
    """Строки листа «История правок»: одна запись `reg_answer_history` -> N строк (по одной
    на изменённое поле, порядок как в `changes`); записи — хронологически (id ASC), как
    отдаёт `list_answer_history`. Один `get_all_users_dicts()` + индекс по telegram_id —
    никакого N+1 `get_user` в цикле; делегата, которого в `users` уже нет, строка не роняет
    (пустые ФИО/username, telegram_id на месте)."""
    users = await get_all_users_dicts()
    by_id = {u["telegram_id"]: u for u in users}
    entries = await list_answer_history()
    rows: list[list] = []
    for entry in entries:
        u = by_id.get(entry["telegram_id"])
        full_name = (u or {}).get("full_name")
        username = (u or {}).get("username")
        source_label = SOURCE_LABELS.get(entry["source"], entry["source"])
        stamp = _fmt_dt(entry.get("changed_at"))
        for change in entry.get("changes") or []:
            column = change.get("column")
            rows.append([
                stamp,
                entry["telegram_id"],
                _cell(full_name),
                _cell(username),
                source_label,
                _column_label(column) if column else "",
                _cell(change.get("old")),
                _cell(change.get("new")),
                _cell(entry.get("season")),
            ])
    return [[_csv_safe(v) for v in r] for r in rows]


async def build_questions_sheet_rows() -> list[list]:
    """Строки листа «Вопросы»: строка на вопрос, порядок id ASC (как отдаёт
    `list_questions`). «Кому ушло» — ВЫВОД по тому же правилу, что фан-аут в
    `handlers/user_actions.py::process_question` (город есть -> «Менеджеры города X», иначе
    «Все менеджеры»); отдельной колонки для этого в БД нет и не заводим — это факт вычисления
    на момент выгрузки, не сохранённый факт отправки."""
    from cities import cities_module_on, normalize_city, city_label  # cities -> database.db, обратной зависимости нет

    users = await get_all_users_dicts()
    by_id = {u["telegram_id"]: u for u in users}
    module_on = await cities_module_on()
    city_labels: dict[str, str] = {}

    async def _label_for_city(code: str) -> str:
        if code not in city_labels:
            city_labels[code] = await city_label(code)
        return city_labels[code]

    questions = await list_questions()
    rows: list[list] = []
    for q in questions:
        u = by_id.get(q["user_id"])
        full_name = (u or {}).get("full_name")
        username = (u or {}).get("username")
        raw_city = (u or {}).get("event_city") if u else None
        if module_on and raw_city:
            recipient = f"Менеджеры города {await _label_for_city(normalize_city(raw_city))}"
        else:
            recipient = "Все менеджеры"
        rows.append([
            _fmt_dt(q.get("asked_at")),
            q["user_id"],
            _cell(full_name),
            _cell(username),
            _cell(raw_city) if module_on and raw_city else "",
            _cell(q.get("question_text")),
            recipient,
        ])
    return [[_csv_safe(v) for v in r] for r in rows]


async def _resolve_tab(key: str) -> str:
    return (await get_setting_typed(key) or "").strip() or SETTINGS_SCHEMA[key]["default"]


async def export_history_to_sheet() -> int:
    """Полная перезапись листа «История правок» (название — настройка `history_sheet_tab`).
    -1 при ошибке/не настроенной таблице — буква в букву `polls.export_polls_to_sheet`."""
    from services.sheets import sync_named_worksheet  # sheets тянет gspread — держим лениво
    tab = await _resolve_tab("history_sheet_tab")
    try:
        rows = await build_history_sheet_rows()
        return await sync_named_worksheet(tab, HISTORY_SHEET_HEADERS, rows)
    except Exception as e:
        logger.error("export_history_to_sheet failed: %s", e)
        return -1


async def export_questions_to_sheet() -> int:
    """Полная перезапись листа «Вопросы» (название — настройка `questions_sheet_tab`)."""
    from services.sheets import sync_named_worksheet
    tab = await _resolve_tab("questions_sheet_tab")
    try:
        rows = await build_questions_sheet_rows()
        return await sync_named_worksheet(tab, QUESTIONS_SHEET_HEADERS, rows)
    except Exception as e:
        logger.error("export_questions_to_sheet failed: %s", e)
        return -1


async def sync_sheet_logs() -> tuple[int, int]:
    """Оба листа подряд. Возвращает (строк истории, строк вопросов); отрицательное значение —
    соответствующий лист не записался (см. export_*_to_sheet)."""
    history_n = await export_history_to_sheet()
    questions_n = await export_questions_to_sheet()
    return history_n, questions_n
