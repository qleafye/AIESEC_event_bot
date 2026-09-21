"""Phase 31 (31-05, D-18/D-19/D-24/D-29): журнал автоотказов как сервис.

Журнал (`database.db.auto_reject_log`) — отдельная сущность от `application_decisions`: у
решения человека есть окно отмены в 5 секунд и один финальный статус, у автоотказа его нет —
делегат может переподавать анкету сколько угодно раз (D-24: лимита попыток нет сознательно),
и менеджер может вернуть заявку на модерацию когда угодно, а не только в короткое окно после
срабатывания. Живая строка журнала (`returned_to_moderation_at IS NULL`) ровно одна на
делегата — это гарантирует частичный уникальный индекс на стороне БД (план 31-02).

Модуль aiogram-free: тот же разрез, что у `services/applications.py` против
`handlers/admin_moderation.py` — сообщение делегату о возврате на модерацию
(`reject_rules_return_text`) шлёт ВЫЗЫВАЮЩИЙ хендлер, не этот модуль. Свои копии `_short_stamp`/
`_username_label` (не импорт из `handlers/admin_app_list.py`) — тот модуль тянет aiogram на
уровне импорта, сервис обязан оставаться его свободным (сторож — grep в acceptance плана).

Инцидент 06.09 (тихое массовое автоодобрение): в этом модуле сознательно НЕТ ни одной функции,
применяющей правила или возврат пакетом — каждая запись журнала пишется и возвращается по
ОДНОМУ `telegram_id`/`entry_id` (D-19). Не «оптимизировать» `return_to_moderation` в цикл по
списку id — это прямое нарушение урока инцидента.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
from datetime import datetime

from cities import city_scope
from database.db import (
    claim_auto_reject_return,
    count_auto_reject_log,
    export_auto_reject_log_rows,
    get_auto_reject_log_entry,
    get_user,
    list_auto_reject_log,
    revert_user_to_pending,
    update_user_answers,
    upsert_auto_reject_log,
)
from services.timeutil import msk_now
from settings_ops import per_city_visible_codes

logger = logging.getLogger(__name__)

# Сентинел `application_decisions.decided_by` для решения, принятого ПРАВИЛОМ, а не человеком
# (планы 31-06/31-09 импортируют константу отсюда — второй копии литерала в проекте быть не
# должно). Ровно `-1`, не `0` и не `NULL`: `handlers/admin_app_list.py::_decision_suffix` уже
# трактует любое ложное значение (`0`, `None`) как «решения нет / решение отменено» и печатает
# «автоматически» без указания менеджера — автоотказ слился бы с этой веткой и стал неотличим
# от отменённого решения человека (T-31-05-07). Отрицательное значение никогда не совпадёт с
# реальным `telegram_id` (Telegram id положительны). `database.db.resolve_decision_managers`
# уже пропускает неположительные `decided_by` (план 31-02) — резолвить «менеджера» с id `-1`
# не нужно.
AUTO_DECIDED_BY = -1

# Пагинация экрана журнала (CLAUDE.md: при 1000+ заявках список обязан быть постраничным, а не
# сообщением на запись).
JOURNAL_PAGE = 10


async def record_auto_reject(telegram_id: int, rule_ids: list[int], reject_texts: list[str]) -> int:
    """Пишет/обновляет живую строку журнала для `telegram_id` и возвращает её id.

    D-24: лимита попыток нет — это осознанное решение владельца. Каждое срабатывание, пока
    строка живая (`returned_to_moderation_at IS NULL`), растит `attempt_count` той же строки
    (`upsert_auto_reject_log`, план 31-02); после возврата на модерацию следующее срабатывание
    заводит НОВУЮ строку со счётчиком 1 — история предыдущего автоотказа не теряется, но не
    смешивается со следующим.

    Метка времени — `msk_now()` (вся семья `now()`-меток проекта пишется по Москве), не наивный
    `datetime.now()`.
    """
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    rule_ids_json = json.dumps(rule_ids, ensure_ascii=False)
    reject_texts_json = json.dumps(reject_texts, ensure_ascii=False)
    return await upsert_auto_reject_log(telegram_id, rule_ids_json, reject_texts_json, now)


async def return_to_moderation(admin_id: int, entry_id: int) -> tuple[dict | None, str | None]:
    """Возврат ОДНОЙ заявки на модерацию. `(строка журнала, None)` при успехе;
    `(None, человеческая причина)` — запись недоступна, уже возвращена или вне города менеджера.

    Порядок шагов — фиксированный (T-31-05-01/T-31-05-02/T-31-05-03 threat register):
    1. запись существует;
    2. право на город делегата (город не задан у делегата -> доступен любому менеджеру);
    3. атомарный `claim_auto_reject_return` — второй тап по кнопке проигрывает;
    4. `revert_user_to_pending` — статус делегата назад в `pending`;
    5. узкий `update_user_answers` — признак автоотказа снят, сама строка делегата остаётся
       в базе (D-26: человек остаётся в базе ради охватов, не удаляется).

    Каждый шаг — по ОДНОМУ `telegram_id`/`entry_id`; в модуле нет ни одного массового `UPDATE`
    (D-19, урок инцидента 06.09) — не заводить здесь цикл по списку id."""
    entry = await get_auto_reject_log_entry(entry_id)
    if entry is None:
        return None, "Запись недоступна — обновите список"

    telegram_id = entry["telegram_id"]
    user = await get_user(telegram_id)
    delegate_city = (user or {}).get("event_city")
    if delegate_city:
        visible = await per_city_visible_codes(admin_id)
        if delegate_city not in visible:
            return None, "Эта заявка не из вашего города"

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = await claim_auto_reject_return(entry_id, admin_id, now)
    if claimed is None:
        return None, "Эту заявку уже вернули на модерацию"

    reverted = await revert_user_to_pending(telegram_id, "rejected")
    if not reverted:
        # Гонка статусов (например, делегата параллельно тронул другой менеджер) — claim
        # журнала уже выигран и не откатывается: строка закрыта, это факт истории. Признак
        # автоотказа всё равно снимается ниже — предупреждение только в лог, не исключение,
        # тот же fail-soft приём, что у `services.applications.undo_decision`.
        logger.warning(
            "reject_journal.return_to_moderation: revert_user_to_pending(%s) вернул False "
            "(запись журнала %s уже закрыта)", telegram_id, entry_id,
        )

    await update_user_answers(
        telegram_id,
        {"auto_reject_rule_ids": None, "auto_rejected_at": None, "auto_rule_note": None},
        allowed_columns=["auto_reject_rule_ids", "auto_rejected_at", "auto_rule_note"],
    )
    return claimed, None


async def _admin_scope(admin_id: int):
    """`per_city_visible_codes(admin_id)` -> `cities.city_scope` для SQL-фильтра журнала.
    `per_city_visible_codes` всегда возвращает либо ПОЛНЫЙ список кодов (суперадмин или
    непривязанный менеджер — «доступны все города»), либо список РОВНО из одного элемента
    (привязанный менеджер) — второго случая (несколько, но не все) функция не производит,
    поэтому единственная развилка здесь — «один код» -> точечный скоуп, иначе -> без фильтра."""
    codes = await per_city_visible_codes(admin_id)
    if len(codes) == 1:
        return city_scope(codes[0])
    return None


async def journal_page(admin_id: int, *, offset: int = 0, include_returned: bool = False
                        ) -> tuple[list[dict], int]:
    """Страница журнала + общий счётчик по ОДНОМУ набору фильтров (правило `services.
    applications.queue_page`: список и счётчик обязаны ходить по одному скоупу, иначе «Всего:
    N» разойдётся со списком под ним)."""
    scope = await _admin_scope(admin_id)
    total = await count_auto_reject_log(city_scope=scope, include_returned=include_returned)
    rows = await list_auto_reject_log(
        city_scope=scope, limit=JOURNAL_PAGE, offset=offset, include_returned=include_returned,
    )
    return rows, total


def _short_stamp(raw) -> str:
    """`ДД.ММ ЧЧ:ММ` из метки, которая УЖЕ московская (`last_triggered_at` пишется `msk_now`,
    второй сдвиг дал бы «будущее»). Собственная копия `handlers.admin_app_list._short_stamp`:
    тот модуль тянет aiogram на уровне импорта, сервис обязан оставаться его свободным.
    Фейл-софт: пустая или нераспознанная метка — «—»."""
    if not raw:
        return "—"
    text = str(raw)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            stamp = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return stamp.strftime("%d.%m %H:%M")
    return "—"


def _username_label(raw) -> str:
    """Собственная копия `handlers.admin_app_list._username` (тот же довод, что у
    `_short_stamp` выше)."""
    if not raw:
        return "(без ника)"
    return html.escape("@" + str(raw).strip().lstrip("@"))


def _rule_label(entry: dict) -> str:
    """Человеческое имя сработавшего правила для строки экрана. Журнал хранит СНИМОК —
    `rule_ids`/`reject_texts` на момент срабатывания, а не живую ссылку на `reject_rules`
    (правило могло быть переименовано или удалено к моменту, когда менеджер открыл журнал) —
    поэтому источник здесь один: `reject_texts` (у правила своего имени в снимке нет). Берётся
    ПЕРВЫЙ текст (первое сработавшее правило), обрезается до первого предложения, короткий
    остаток — до 60 символов с многоточием, чтобы строка списка не расползалась на нескольких
    делегатов подряд."""
    try:
        texts = json.loads(entry.get("reject_texts") or "[]")
    except (TypeError, ValueError):
        texts = []
    if not texts:
        return "—"
    first = str(texts[0]).strip()
    if not first:
        return "—"
    for sep in (".", "!", "?"):
        idx = first.find(sep)
        if idx != -1:
            first = first[: idx + 1]
            break
    if len(first) > 60:
        first = first[:57].rstrip() + "…"
    return html.escape(first)


def journal_line(entry: dict) -> str:
    """Одна строка экрана журнала: «Иванова Мария — @masha — 20.09 14:12 — «Москва: 1–2
    курс» — попыток: 2» (+ «— возвращена на модерацию», если строка закрыта). ВСЕ подставляемые
    значения экранированы через `html.escape` здесь — вызывающий печатает строку как есть с
    `parse_mode="HTML"` и повторно экранировать не должен (T-31-05-04, тот же контракт, что у
    `services.applications.prev_reject_line`/`edited_line`)."""
    name = html.escape(str(entry.get("full_name") or "") or "—")
    username = _username_label(entry.get("username"))
    stamp = _short_stamp(entry.get("last_triggered_at"))
    rule_label = _rule_label(entry)
    attempts = entry.get("attempt_count") or 0
    suffix = " — возвращена на модерацию" if entry.get("returned_to_moderation_at") else ""
    return f'{name} — {username} — {stamp} — «{rule_label}» — попыток: {attempts}{suffix}'


async def export_csv(admin_id: int) -> tuple[str, bytes]:
    """D-29: выгрузка журнала файлом для отчёта партнёрам. Разделитель «;» и BOM-кодировка
    ниже по коду — конвенция проекта для Excel-RU (тот же приём, что `handlers/
    admin_broadcasts.py::cmd_export`) — запятая сломала бы файл менеджеру в локали RU.
    Формульная инъекция уже обезврежена на стороне `export_auto_reject_log_rows` (`_csv_safe`,
    план 31-02, T-31-02-02) — второй копии защиты здесь не заводим."""
    scope = await _admin_scope(admin_id)
    headers, rows = await export_auto_reject_log_rows(city_scope=scope)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    output.seek(0)
    file_bytes = output.getvalue().encode('utf-8-sig')
    filename = f"auto_reject_journal_{msk_now().strftime('%Y-%m-%d')}.csv"
    return filename, file_bytes
