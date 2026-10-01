"""Чтение таблицы ответов Google Формы сервисным аккаунтом (без скриптов на стороне Google).

Ключ ответа — `g:<отметка времени>#<n>`, где n — порядковый номер среди строк с той же меткой:
сортировка и удаление строк в таблице не создают дублей. Ключ вопроса — текст заголовка колонки.
В лог идут только id формы и причина, значения ячеек — никогда.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

import gspread

from config import config
from database import ext_forms_db as ef
from services.ext_forms_ingest import ingest_answer
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_URL_ID_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[#&?]gid=(\d+)")
_TS_HEADERS = {"отметка времени", "timestamp"}
_TS_FORMATS = ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S")


class GoogleFormError(Exception):
    """reason: no_credentials | no_access | not_found | unavailable | empty; human — фраза менеджеру."""

    def __init__(self, reason: str, human: str):
        super().__init__(reason)
        self.reason = reason
        self.human = human


def parse_sheet_url(text: str) -> tuple[str, int | None] | None:
    m = _URL_ID_RE.search(text or "")
    if not m:
        return None
    g = _GID_RE.search(text)
    return m.group(1), (int(g.group(1)) if g else None)


def service_account_email() -> str | None:
    try:
        with open(config.GOOGLE_CREDENTIALS_FILE, encoding="utf-8") as f:
            return json.load(f).get("client_email") or None
    except Exception:
        return None


def _parse_ts(value: str) -> str | None:
    v = (value or "").strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def rows_to_answers(values: list[list[str]]) -> tuple[list[tuple[str, str | None, list[dict]]], bool]:
    if not values:
        return [], False
    header = [str(h).strip() for h in values[0]]
    ts_idx = next((i for i, h in enumerate(header) if h.lower() in _TS_HEADERS), None)
    has_ts = ts_idx is not None
    key_idx = ts_idx if has_ts else 0

    seen: dict[str, int] = {}
    titles: list[str] = []
    for h in header:
        n = seen.get(h, 0) + 1
        seen[h] = n
        titles.append(h if n == 1 else f"{h} ({n})")

    out: list[tuple[str, str | None, list[dict]]] = []
    counters: dict[str, int] = {}
    for row in values[1:]:
        cells = [str(c) for c in row] + [""] * (len(header) - len(row))
        if not any(c.strip() for c in cells):
            continue
        base = cells[key_idx].strip() if key_idx < len(cells) else ""
        n = counters.get(base, 0)
        counters[base] = n + 1
        answered_at = _parse_ts(base) if has_ts else None
        items = [
            {"q": titles[i], "label": titles[i], "value": cells[i]}
            for i in range(len(header)) if i != ts_idx
        ]
        out.append((f"g:{base}#{n}", answered_at, items))
    return out, has_ts


def _open_spreadsheet_sync(spreadsheet_id: str):
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    return gc.open_by_key(spreadsheet_id)


def _no_access_error() -> GoogleFormError:
    email = service_account_email() or "сервисного аккаунта бота"
    return GoogleFormError(
        "no_access",
        f"Дайте доступ к таблице ответов для {email} с ролью «Читатель» и пришлите ссылку ещё раз",
    )


def _map_exc(e: Exception) -> GoogleFormError:
    if isinstance(e, GoogleFormError):
        return e
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return _no_access_error()
    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return GoogleFormError("not_found", "Вкладка не найдена — проверьте ссылку")
    if isinstance(e, gspread.exceptions.APIError):
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code == 403:
            return _no_access_error()
        if code == 404:
            return GoogleFormError("not_found", "Таблица не найдена — проверьте ссылку")
    return GoogleFormError("unavailable", "Google временно недоступен — попробую позже")


def _check_credentials() -> None:
    if not config.GOOGLE_CREDENTIALS_FILE or service_account_email() is None:
        raise GoogleFormError(
            "no_credentials",
            "Ключ сервисного аккаунта Google не настроен — подключить Google Форму пока нельзя",
        )


def _list_tabs_sync(spreadsheet_id: str) -> tuple[str, list[tuple[int, str]]]:
    sh = _open_spreadsheet_sync(spreadsheet_id)
    return sh.title, [(ws.id, ws.title) for ws in sh.worksheets()]


def _read_values_sync(spreadsheet_id: str, gid: int | None) -> list[list[str]]:
    sh = _open_spreadsheet_sync(spreadsheet_id)
    ws = sh.get_worksheet_by_id(gid) if gid is not None else sh.sheet1
    return ws.get_all_values()


async def list_tabs(spreadsheet_id: str) -> tuple[str, list[tuple[int, str]]]:
    _check_credentials()
    try:
        return await asyncio.to_thread(_list_tabs_sync, spreadsheet_id)
    except Exception as e:
        raise _map_exc(e) from None


async def read_values(spreadsheet_id: str, gid: int | None) -> list[list[str]]:
    _check_credentials()
    try:
        return await asyncio.to_thread(_read_values_sync, spreadsheet_id, gid)
    except Exception as e:
        raise _map_exc(e) from None


async def sync_google_form(form: dict) -> int:
    """Число новых анкет. Не бросает: ошибка уходит в sync_error формы."""
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        values = await read_values(form["external_id"], form.get("gsheet_gid"))
        answers, _ = rows_to_answers(values)
        known = await ef.known_answer_ids(form["id"])
        added = 0
        for answer_id, answered_at, items in answers:
            if answer_id in known:
                continue
            if await ingest_answer(
                form, answer_id=answer_id, answered_at=answered_at, items=items, raw=None,
            ):
                added += 1
        await ef.set_form_sync(form["id"], last_sync_at=now, sync_error=None)
        return added
    except GoogleFormError as e:
        logger.warning("google form %s sync: %s", form.get("id"), e.reason)
        await ef.set_form_sync(form["id"], last_sync_at=form.get("last_sync_at"), sync_error=e.human)
        return 0
    except Exception as e:
        logger.warning("google form %s sync failed: %s", form.get("id"), type(e).__name__)
        await ef.set_form_sync(
            form["id"], last_sync_at=form.get("last_sync_at"),
            sync_error="Не удалось обработать таблицу ответов — попробую позже",
        )
        return 0


async def poll_google_forms() -> int:
    total = 0
    for form in await ef.list_active_forms("google"):
        try:
            total += await sync_google_form(form)
        except Exception as e:
            logger.warning("google form %s poll failed: %s", form.get("id"), type(e).__name__)
    return total
