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
    get_reject_rule,
    get_user,
    list_auto_reject_log,
    revert_user_to_pending,
    update_user_answers,
    upsert_auto_reject_log,
)
from services.reject_rules import rule_summary
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

    # Экран журнала (план 31-11): строка журнала считается ЖИВОЙ в БД
    # (`returned_to_moderation_at IS NULL`), но делегат мог сам поправить анкету — финализация
    # (`services/reg_finalize.py`, исход 2) снимает признак автоотказа с `users` и переводит
    # статус в `pending`, НЕ трогая строку журнала (это факт истории, не ошибка). Проверяется
    # ТОЛЬКО для ещё живой по БД строки (уже возвращённая идёт своим путём — «уже вернули»
    # ниже, вокруг claim_auto_reject_return, — иначе второй тап по своей же кнопке «вернуть»
    # ошибочно попал бы в эту ветку, статус делегата к тому моменту уже "pending"). Клавиатура
    # экрана журнала в чате не истекает — менеджер может тапнуть «вернуть» по старой карточке
    # уже после того, как делегат сам вернулся или решение принял человек; дружелюбный алерт
    # вместо попытки откатить статус, которого уже нет.
    if entry.get("returned_to_moderation_at") is None:
        status = (user or {}).get("status")
        if status != "rejected":
            if status == "pending":
                return None, "Делегат уже сам вернулся на модерацию — поправил анкету, возвращать нечего"
            return None, "По этой заявке уже есть решение человека — из журнала её больше не тронуть"

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

    # Квик 260923 (AUTOREJ-REPORT, D-G): лист таблицы после возврата продолжал показывать
    # «Отклонена» — return_to_moderation переводит делегата в БД, а строку листа никто не
    # трогал. Тот же fail-soft приём, что revert_user_to_pending выше: возврат УЖЕ зафиксирован
    # (claim выигран, статус в БД сменён) — сбой листа только логируется, не откатывает возврат.
    try:
        from reg_labels import STATUS_LABELS
        from services.sheets import update_status_in_sheet
        await update_status_in_sheet(telegram_id, STATUS_LABELS["pending"])
        from services.scheduler import sync_auto_reject_sheet_job
        await sync_auto_reject_sheet_job()
    except Exception as e:
        logger.warning(
            "reject_journal.return_to_moderation: лист не обновлён после возврата %s (%s) — "
            "возврат в БД уже зафиксирован, не откатывается", telegram_id, e,
        )

    return claimed, None


async def journal_entry_detail(admin_id: int, entry_id: int) -> tuple[dict | None, str | None]:
    """Одна строка журнала + делегатские поля для экрана подтверждения возврата (`arj_back`,
    план 31-11) — та же проверка права на город, что `return_to_moderation` делает перед
    `claim` (T-31-11-01/05): подтверждение обязано перепроверить право САМО, клавиатура в чате
    не истекает. `(None, причина)` — запись недоступна или вне scope менеджера; при успехе
    `entry` несёт `full_name`/`username`/`event_city` (та же форма, что строки `journal_page`)
    плюс `rule_display` (см. `_resolve_rule_display`) и `returnable` (живая по БД строка И
    делегат всё ещё `rejected` — иначе экрану нечего подтверждать, см. `journal_page`)."""
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
    entry = dict(entry)
    entry["full_name"] = (user or {}).get("full_name")
    entry["username"] = (user or {}).get("username")
    entry["event_city"] = delegate_city
    entry["rule_display"] = await _resolve_rule_display(entry)
    entry["returnable"] = (
        entry.get("returned_to_moderation_at") is None and (user or {}).get("status") == "rejected"
    )
    return entry, None


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
    N» разойдётся со списком под ним). Каждая строка дополнительно резолвит `rule_display`
    (см. `_resolve_rule_display`) и, для ЖИВЫХ по БД строк, `stale_reason` — правку 31-11,
    закрывающую находку «стейл-«живые» записи»: живая по `returned_to_moderation_at` строка,
    чей делегат уже не в статусе `rejected` (поправил анкету сама/решение принял человек),
    экрану журнала нечего предлагать вернуть."""
    scope = await _admin_scope(admin_id)
    total = await count_auto_reject_log(city_scope=scope, include_returned=include_returned)
    rows = await list_auto_reject_log(
        city_scope=scope, limit=JOURNAL_PAGE, offset=offset, include_returned=include_returned,
    )
    for row in rows:
        row["rule_display"] = await _resolve_rule_display(row)
        if not row.get("returned_to_moderation_at"):
            # `_AUTO_REJECT_LOG_SELECT` не несёт `users.status` (только full_name/username/
            # event_city) — отдельный запрос за статусом, тот же N+1-компромисс, что у
            # `_resolve_rule_display` ниже, приемлем на странице из JOURNAL_PAGE=10 строк.
            user = await get_user(row["telegram_id"])
            status = (user or {}).get("status")
            if status == "pending":
                row["stale_reason"] = "делегат поправил анкету — автоотказ снялся сам"
            elif status not in (None, "rejected"):
                row["stale_reason"] = "решение по заявке уже принял человек"
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


async def _resolve_rule_display(entry: dict) -> str:
    """План 31-11 (orchestrator finding 1): имя правила для строки журнала — если правило
    ЕЩЁ существует, собственное имя (D-10) или его автоописание `services.reject_rules.
    rule_summary` («Москва · Курс — один из: 1, 2 → отказ»), резолвится ЗАНОВО через
    `database.db.get_reject_rule` (та же дисциплина, что `services.applications.
    _resolve_rule_label` для бейджей карточки) — правило могло быть переименовано после
    срабатывания. Правило удалено (или снимок вообще не нёс id — записи, посеянные до этой
    правки) -> единственный оставшийся источник `_rule_label` (снимок `reject_texts`)."""
    try:
        rule_ids = json.loads(entry.get("rule_ids") or "[]")
    except (TypeError, ValueError):
        rule_ids = []
    if not rule_ids:
        return _rule_label(entry)
    try:
        row = await get_reject_rule(rule_ids[0])
    except Exception:
        row = None
    if not row:
        return _rule_label(entry)
    name = str(row.get("name") or "").strip()
    if name:
        label = name
    else:
        rule = dict(row)
        try:
            rule["conditions"] = json.loads(row.get("conditions") or "[]") or []
        except (TypeError, ValueError):
            rule["conditions"] = []
        label = await rule_summary(rule)
    label = html.escape(label)
    if len(label) > 60:
        label = label[:57].rstrip() + "…"
    return label


def journal_line(entry: dict) -> str:
    """Одна строка экрана журнала: «Иванова Мария — @masha — 20.09 14:12 — «Москва: 1–2
    курс» — попыток: 2» (+ «— возвращена на модерацию»/причина «стейл»-закрытия, если строке
    больше нечего предлагать). ВСЕ подставляемые значения экранированы через `html.escape`
    здесь — вызывающий печатает строку как есть с `parse_mode="HTML"` и повторно экранировать
    не должен (T-31-05-04, тот же контракт, что у `services.applications.prev_reject_line`/
    `edited_line`). `rule_display` (если строка пришла из `journal_page`, план 31-11) уже
    готова и экранирована — второй раз не обрабатывается; прямые вызовы (тесты плана 31-05,
    строка без `rule_display`) падают в прежний `_rule_label`."""
    name = html.escape(str(entry.get("full_name") or "") or "—")
    username = _username_label(entry.get("username"))
    stamp = _short_stamp(entry.get("last_triggered_at"))
    rule_label = entry.get("rule_display") or _rule_label(entry)
    attempts = entry.get("attempt_count") or 0
    if entry.get("returned_to_moderation_at"):
        suffix = " — возвращена на модерацию"
    elif entry.get("stale_reason"):
        suffix = f" — {entry['stale_reason']}"
    else:
        suffix = ""
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
