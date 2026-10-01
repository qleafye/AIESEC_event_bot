"""Зеркало анкет внешних форм во вкладку таблицы события.

Колонки: «Дата ответа | Делегат | Статус заявки | ID ответа | вопросы…». Вопросы стоят по
позициям справа (колонка = 4 + position), новый вопрос дописывается ячейкой шапки справа —
вставка колонок в середину сдвинула бы строки. Вкладку зеркало само не создаёт: нет вкладки —
ошибка у формы «выберите вкладку заново»; создаёт только `create_mirror_tab` по действию
менеджера. Запись пакетная, value_input_option=RAW (значения чужой формы не исполняются как
формулы). Строку для позднего обновления находим по «ID ответа» (столбец D), а не по номеру.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

import gspread
from gspread.utils import rowcol_to_a1

from config import config
from database import ext_forms_db as ef
from secret_redact import redact_secrets
from services.sheet_arrival_sync import backoff_seconds
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

FIXED_HEADERS = ["Дата ответа", "Делегат", "Статус заявки", "ID ответа"]
_FIXED = len(FIXED_HEADERS)
_FMT = "%Y-%m-%d %H:%M:%S"
NOT_FOUND = "не найден"


def _raw():
    from services.sheets import _RAW
    return _RAW


def _configured() -> bool:
    return bool(config.GOOGLE_SHEET_ID and config.GOOGLE_CREDENTIALS_FILE)


def _open_tab_sync(tab: str):
    """Единственная точка доступа к листу. None = вкладки нет (не создаём)."""
    from services.sheets import _open_named_or_main_sync
    return _open_named_or_main_sync(tab)


def _create_tab_sync(title: str, cols: int):
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    return sh.add_worksheet(title=title, rows=1000, cols=cols)


def _status_label(status: str | None) -> str:
    from services.delegate_card import STATUS_LABELS
    return STATUS_LABELS.get(status) or str(status or "—")


def _delegate_cell(user: dict | None) -> str:
    if not user:
        return NOT_FOUND
    name = (user.get("full_name") or "").strip()
    uname = (user.get("username") or "").strip().lstrip("@")
    parts = [name] if name else []
    if uname and uname != "-":
        parts.append("@" + uname)
    return " ".join(parts) or NOT_FOUND


def _status_cell(user: dict | None) -> str:
    return _status_label(user.get("status")) if user else "—"


def build_row(answer: dict, columns: list[dict], user: dict | None) -> list[str]:
    """4 фиксированных значения + ответы по position (пропуск = пустая строка)."""
    pos_by_q = {c["qkey"]: int(c["position"]) for c in columns}
    width = max([int(c["position"]) for c in columns], default=0)
    row = [
        str(answer.get("answered_at") or ""),
        _delegate_cell(user),
        _status_cell(user),
        str(answer.get("answer_id") or ""),
    ] + [""] * width
    for item in answer.get("payload") or []:
        pos = pos_by_q.get(str(item.get("q")))
        if pos:
            row[_FIXED + pos - 1] = str(item.get("value") if item.get("value") is not None else "")
    return row


def _header_row(columns: list[dict]) -> list[str]:
    width = max([int(c["position"]) for c in columns], default=0)
    row = list(FIXED_HEADERS) + [""] * width
    for c in columns:
        row[_FIXED + int(c["position"]) - 1] = str(c.get("label") or "")
    return row


async def create_mirror_tab(form_id: int, title: str) -> str:
    """'ok' | 'exists' | 'unavailable'. Единственное место, где вкладка создаётся."""
    if not _configured():
        return "unavailable"
    columns = await ef.list_columns(form_id)
    try:
        if await asyncio.to_thread(_open_tab_sync, title) is not None:
            return "exists"
        header = _header_row(columns)
        ws = await asyncio.to_thread(_create_tab_sync, title, len(header) + 10)
        await asyncio.to_thread(ws.update, "A1", [header], value_input_option=_raw())
    except Exception as exc:
        logger.warning("ext_forms_mirror: не создать вкладку: %s", redact_secrets(str(exc)))
        return "unavailable"
    await ef.mark_headers_written(form_id, [c["qkey"] for c in columns])
    await ef.set_form_mirror(form_id, title, None)
    return "ok"


class ForeignTabError(Exception):
    """В выбранной вкладке уже лежат чужие данные (шапка не наша) — писать туда нельзя."""


def _foreign_tab_text(tab: str) -> str:
    return f"Во вкладке «{tab}» уже есть чужие данные — выберите пустую или создайте новую"


def _tab_missing_text(tab: str) -> str:
    return f"Вкладка «{tab}» не найдена — выберите вкладку заново в разделе «📝 Внешние формы»"


def _ensure_cols(ws, need: int) -> None:
    """Выбранная менеджером вкладка может быть уже шапки — без расширения сетки запись
    за её пределы падает («exceeds grid limits»)."""
    have = getattr(ws, "col_count", None)
    if isinstance(have, int) and have < need:
        ws.add_cols(need - have)


def _write_form_sync(tab, columns, new_cols, appends, updates):
    """Один проход по листу. appends/updates — списки (row_id, answer, user). Возврат:
    (n_appended, n_updated) или None, если вкладки нет."""
    ws = _open_tab_sync(tab)
    if ws is None:
        return None
    raw = _raw()
    head = [str(c).strip() for c in (ws.row_values(1) or [])]
    blank = not any(head)
    if not blank and head[:_FIXED] != FIXED_HEADERS:
        raise ForeignTabError(tab)  # чужая шапка: ничего не пишем
    _ensure_cols(ws, len(_header_row(columns)))
    if blank:
        ws.update("A1", [_header_row(columns)], value_input_option=raw)
    else:
        if new_cols:
            # RAW, не update_cell (тот пишет USER_ENTERED): подпись вопроса чужой формы
            # вида «=IMPORTXML(...)» не должна исполняться как формула.
            ws.batch_update(
                [{"range": rowcol_to_a1(1, _FIXED + int(c["position"])),
                  "values": [[str(c.get("label") or "")]]} for c in new_cols],
                value_input_option=raw,
            )
    n_app = n_upd = 0
    index: dict[str, int] = {}
    if appends or updates:
        index = {v: i + 1 for i, v in enumerate(ws.col_values(4)) if v}
    if appends:
        # Строка могла уже попасть в лист (сбой после append_rows, привязка во время записи):
        # такую не дописываем второй раз, а обновляем по «ID ответа».
        already = [it for it in appends if str(it[1].get("answer_id")) in index]
        appends = [it for it in appends if str(it[1].get("answer_id")) not in index]
        updates = list(updates) + already
    if appends:
        ws.append_rows(
            [build_row(a, columns, u) for _, a, u in appends], value_input_option=raw,
        )
        n_app = len(appends)
    if updates:
        batch, missing = [], []
        for item in updates:
            _, a, u = item
            r = index.get(str(a.get("answer_id")))
            if r is None:
                missing.append(item)
            else:
                batch.append({"range": f"B{r}:C{r}", "values": [[_delegate_cell(u), _status_cell(u)]]})
        if batch:
            ws.batch_update(batch, value_input_option=raw)
        if missing:
            ws.append_rows(
                [build_row(a, columns, u) for _, a, u in missing], value_input_option=raw,
            )
        n_upd = len(updates)
    return n_app, n_upd


async def _users_for(answers: list[dict]) -> dict[int, dict]:
    ids = sorted({a["matched_telegram_id"] for a in answers if a.get("matched_telegram_id")})
    if not ids:
        return {}
    from database import db as _db
    marks = ",".join("?" * len(ids))
    async with _db._connect() as conn:
        import aiosqlite
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT * FROM users WHERE telegram_id IN ({marks})", tuple(ids)
        ) as cur:
            return {r["telegram_id"]: dict(r) for r in await cur.fetchall()}


async def drain_mirror(limit: int = 200) -> dict:
    counts = {"appended": 0, "updated": 0, "failed": 0, "not_found": 0}
    now = msk_now()
    due = await ef.list_sheet_due(now.strftime(_FMT), limit)
    if not due:
        return counts
    if not _configured():
        await ef.mark_sheet_state([a["id"] for a in due], "skip")
        return counts

    users = await _users_for(due)
    by_form: dict[int, list[dict]] = defaultdict(list)
    for a in due:
        by_form[a["form_id"]].append(a)

    for form_id, answers in by_form.items():
        tab = answers[0]["mirror_tab"]
        columns = await ef.list_columns(form_id)
        new_cols = [c for c in columns if not c.get("header_written")]
        appends = [(a["id"], a, users.get(a.get("matched_telegram_id"))) for a in answers
                   if a["sheet_state"] == "append"]
        updates = [(a["id"], a, users.get(a.get("matched_telegram_id"))) for a in answers
                   if a["sheet_state"] == "update"]
        try:
            res = await asyncio.to_thread(
                _write_form_sync, tab, columns, new_cols, appends, updates
            )
        except ForeignTabError:
            await ef.set_form_mirror(form_id, tab, _foreign_tab_text(tab))
            counts["not_found"] += len(answers)
            continue
        except Exception as exc:
            logger.warning("ext_forms_mirror: сбой записи формы %s: %s", form_id,
                           redact_secrets(str(exc)))
            attempts = max(int(a.get("sheet_attempts") or 0) for a in answers) + 1
            nxt = (now + timedelta(seconds=backoff_seconds(attempts))).strftime(_FMT)
            await ef.fail_sheet([a["id"] for a in answers], nxt)
            counts["failed"] += len(answers)
            continue
        if res is None:
            await ef.set_form_mirror(form_id, tab, _tab_missing_text(tab))
            counts["not_found"] += len(answers)
            continue
        await ef.mark_headers_written(form_id, [c["qkey"] for c in new_cols])
        await ef.mark_sheet_synced(
            [(r[0], "append", r[1].get("matched_telegram_id")) for r in appends]
            + [(r[0], "update", r[1].get("matched_telegram_id")) for r in updates])
        counts["appended"] += res[0]
        counts["updated"] += res[1]
    return counts
