import asyncio
import logging
import sqlite3
import threading

import gspread
from config import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]

# Quick task 260813: sentinel distinct from the generic "-1 = unconfigured/API error" return
# used across this module, so callers (handlers/admin.py) can show the admin an ACTIONABLE
# message ("set GOOGLE_SHEET_TAB") instead of the generic "check logs" failure text.
REFUSED_UNPINNED_TAB = -2

# P0 audit T-dw1-02: bot injection hook for the exhausted-retry admin alert. sheets.py has no
# bot reference of its own today (services/scheduler.py holds its own via init_scheduler) —
# main.py wires this in at startup via set_alert_bot(bot).
_alert_bot = None
_alert_bot_warned = False


def set_alert_bot(bot):
    global _alert_bot
    _alert_bot = bot


def _tab_explicitly_configured() -> bool:
    """True only if GOOGLE_SHEET_TAB carries an explicit, non-empty value (after stripping the
    stray quotes/whitespace some .env parsers leave around it). Checked directly against
    config, NOT against whatever _sheet happens to be cached right now — so destructive-op
    refusal (rebuild/dedupe, see REFUSED_UNPINNED_TAB above) and the startup warning below both
    reflect the CURRENT config, not stale in-process cache state."""
    tab = (config.GOOGLE_SHEET_TAB or "").strip().strip('"').strip("'").strip()
    return bool(tab)


async def _send_admin_alert(text: str) -> None:
    """Shared low-level admin broadcast used by every fail-soft Sheets alert in this module
    (the exhausted-retry alert below, and warn_if_tab_unconfigured's startup warning — quick
    task 260813, hardening the main-tab resolution incident). NEVER raises — callers rely on
    that to stay fail-soft themselves."""
    global _alert_bot_warned
    try:
        if _alert_bot is None:
            if not _alert_bot_warned:
                logger.warning("_send_admin_alert: no alert bot set, skipping admin alert")
                _alert_bot_warned = True
            return
        for admin_id in config.ADMIN_IDS:
            try:
                await _alert_bot.send_message(admin_id, text)
            except Exception as e:
                logger.error(f"Sheets alert to {admin_id} failed: {e}")
    except Exception as e:
        logger.error(f"_send_admin_alert failed: {e}")


async def _alert_admins_sheet_failure(context: str) -> None:
    """Fail-soft admin alert fired after a Sheets append exhausts MAX_RETRIES (mirrors
    services/scheduler.py::allowlist_refresh_job's per-admin send pattern). Wrapped so this can
    NEVER raise or block the caller's retry loop."""
    text = (
        f"⚠️ Google Sheets: не удалось записать строку после {MAX_RETRIES} попыток "
        f"({context}). SQLite не затронут — синхронизация таблицы отстаёт. "
        "Проверьте квоту/доступ Google API."
    )
    await _send_admin_alert(text)


_startup_tab_warning_sent = False


async def warn_if_tab_unconfigured() -> None:
    """Startup check — call once from main.py, right after set_alert_bot(bot). If Sheets sync
    is active (GOOGLE_SHEET_ID + credentials set) but GOOGLE_SHEET_TAB is empty, the main tab
    resolves by position on first use (see _get_sheet's pinning comment above) instead of by an
    admin-confirmed name. Logs loudly and alerts admins ONCE per process start (mirrors
    _alert_bot_warned's one-shot pattern) — never spams on every write."""
    global _startup_tab_warning_sent
    if _startup_tab_warning_sent:
        return
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return  # Sheets integration off entirely — nothing to warn about
    if _tab_explicitly_configured():
        return
    _startup_tab_warning_sent = True
    logger.error(
        "GOOGLE_SHEET_TAB не задан: основная вкладка резолвится ПО ПОЗИЦИИ (первая слева), "
        "не по имени. Перетаскивание вкладок местами в Google Sheets молча перенаправит "
        "запись регистраций на другую вкладку. Укажите GOOGLE_SHEET_TAB в .env. До этого "
        "«Пересобрать таблицу» и «Убрать дубли из таблицы» отключены."
    )
    await _send_admin_alert(
        "⚠️ Google Sheets: GOOGLE_SHEET_TAB не задан в .env. Основная вкладка регистраций "
        "определяется по ПОЗИЦИИ (первая слева), а не по имени — перетаскивание вкладок "
        "местами в интерфейсе Google Sheets молча перенаправит запись данных на другую "
        "вкладку.\n\n"
        "Укажите GOOGLE_SHEET_TAB в .env (точное имя текущей основной вкладки) и "
        "перезапустите бота.\n\n"
        "Пока это не сделано, «♻️ Пересобрать таблицу» и «🧹 Убрать дубли из таблицы» "
        "отключены — чтобы случайно не стереть чужую вкладку."
    )


# Cache the authorized client + worksheet handle: gspread.service_account() re-auths
# (token fetch) and open_by_key() is a network call, so rebuilding on every append cost
# ~3 round-trips per registration. google-auth refreshes the token on the cached client,
# so the handle stays valid; _reset_sheet_cache() drops it after a failure to force re-auth.
_sheet = None
# WR-05: sheet ops run across worker threads (asyncio.to_thread), so the check-then-set in
# _get_sheet is a TOCTOU race — two concurrent ops could both build a fresh client. Guard with
# a threading.Lock (NOT asyncio.Lock, which is single-thread only).
_sheet_lock = threading.Lock()

# Quick task 260813: real production incident — with GOOGLE_SHEET_TAB empty, the main tab used
# to resolve by POSITION (sh.sheet1) on every single _get_sheet() call. A manager dragging a
# tab to the front of the spreadsheet (completely normal in the Sheets UI) silently redirected
# every registration write into it, and "♻️ Пересобрать таблицу" (rebuild_main_sheet) then
# cleared and overwrote that unrelated tab. Fix: resolve by position AT MOST ONCE — the title
# picked that first time is persisted (bot_settings, survives restarts) and every subsequent
# resolution — including after _reset_sheet_cache() or a process restart — targets that SAME
# title by name. Reordering tabs afterwards can no longer redirect writes.
_PINNED_MAIN_TAB_SETTING_KEY = "sheets_main_tab_pinned_title"


def _load_pinned_tab_title() -> str | None:
    """Read the persisted pin from bot_settings via a PLAIN sync sqlite3 connection — NOT
    aiosqlite/database.db.get_setting. _get_sheet() runs inside asyncio.to_thread (a worker
    thread, off the asyncio event loop aiosqlite needs), so an async DB call here would have no
    running loop to attach to. sqlite3.connect() is a stdlib blocking call, safe from any
    thread; a fresh connection is opened and closed per call (this only runs when the sheet
    cache is empty — at most once per process per cache-reset, not per write). Fails soft: a
    missing DB file/table (e.g. _get_sheet() called before init_db() has run once) returns None
    rather than raising, so the in-memory positional fallback below still works."""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            cur = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (_PINNED_MAIN_TAB_SETTING_KEY,),
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_load_pinned_tab_title failed (falling back to unpinned): {e}")
        return None


def _save_pinned_tab_title(title: str) -> None:
    """Persist the pin. Same sync-sqlite3 rationale as _load_pinned_tab_title. Fail-soft: if
    this write fails (e.g. table not yet created), the bot keeps working off the in-memory
    _sheet cache for this process — it just won't survive a restart, which is exactly the
    pre-fix behaviour, not a regression."""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                (_PINNED_MAIN_TAB_SETTING_KEY, title),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_save_pinned_tab_title({title!r}) failed: {e}")


def _get_sheet():
    global _sheet
    if _sheet is not None:
        return _sheet
    with _sheet_lock:
        # Re-check inside the lock — another thread may have built it while we waited.
        if _sheet is not None:
            return _sheet
        gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        # Defensively strip stray quotes some .env parsers keep around the value.
        tab = (config.GOOGLE_SHEET_TAB or "").strip().strip('"').strip("'").strip()
        if tab:
            # Target the configured tab by name; auto-create it if the spreadsheet doesn't
            # have it yet (mirrors the «Незавершённые» tab behaviour) so a fresh work-sheet
            # doesn't silently drop every write with WorksheetNotFound.
            try:
                _sheet = sh.worksheet(tab)
            except gspread.WorksheetNotFound:
                _sheet = sh.add_worksheet(title=tab, rows=1000, cols=30)
            return _sheet

        # No explicit tab configured. Never resolve by raw position more than once — first
        # check whether a previous resolution (this process or an earlier one) already pinned
        # a title.
        pinned_title = _load_pinned_tab_title()
        if pinned_title:
            try:
                _sheet = sh.worksheet(pinned_title)
                return _sheet
            except gspread.WorksheetNotFound:
                # The pinned tab was renamed/deleted since we last pinned it. There is no safe
                # "same tab" target left — fall through and re-pin via position, same as a
                # first-ever run. Loud, because this is exactly the kind of drift the pin
                # exists to prevent.
                logger.warning(
                    f"Pinned main tab {pinned_title!r} no longer exists on the spreadsheet; "
                    "re-resolving by position and re-pinning."
                )

        # Historical default: first tab by position — used ONLY to establish the pin.
        _sheet = sh.sheet1
        _save_pinned_tab_title(_sheet.title)
        return _sheet


def _reset_sheet_cache():
    global _sheet
    _sheet = None


def _append_to_sheet_sync(data: list):
    sheet = _get_sheet()
    sheet.append_row(data)


def _ensure_header_sync(headers: list[str]):
    sheet = _get_sheet()
    # Grow the grid FIRST. Unlike append_row/append_rows (which auto-expand), sheet.update()
    # and insert_row() raise "exceeds grid limits" when the target range is wider than the
    # worksheet's current column count (default 26). Enabling enough registration questions to
    # push the header past that width made the reconcile below fail silently (ensure_sheet_header
    # is fail-soft) — so data rows kept appending wider while the header row stayed stuck at its
    # old, narrower width. Widening the grid up front keeps header and data aligned.
    if sheet.col_count < len(headers):
        sheet.add_cols(len(headers) - sheet.col_count)
    col1 = sheet.col_values(1)
    if not col1:
        # empty sheet → header becomes the first row
        sheet.append_row(headers)
        return
    first = (col1[0] or "").strip()
    if first.lstrip("-").isdigit():
        # first row is data (a Telegram id) → no header yet, insert one on top
        sheet.insert_row(headers, 1)
        return
    # a text header is already present — reconcile it if it drifted from the
    # current schema (columns added/reordered in code). Overwrite row 1 in place
    # so data rows below keep their positions.
    current = [h.strip() for h in sheet.row_values(1)]
    if current != headers:
        end = gspread.utils.rowcol_to_a1(1, len(headers))
        sheet.update(values=[headers], range_name=f"A1:{end}")


def _get_existing_ids_sync() -> set[int]:
    sheet = _get_sheet()
    col_values = sheet.col_values(1)
    ids = set()
    for v in col_values[1:]:
        try:
            ids.add(int(v))
        except (ValueError, TypeError):
            continue
    return ids


def _append_rows_sync(rows: list[list]):
    sheet = _get_sheet()
    sheet.append_rows(rows)


def _get_allowlist_rows_sync(tab_name: str) -> list[str]:
    """Read column 1 of a non-sheet1 tab (the pre-selection allowlist, D-09).
    Raises WorksheetNotFound if the tab is missing — caller (refresh_allowlist) is fail-soft."""
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.worksheet(tab_name)
    return ws.col_values(1)


async def append_to_sheet(data: list):
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        logger.warning("Google Sheet ID or Credentials not set. Skipping sheet export.")
        return

    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.to_thread(_append_to_sheet_sync, data)
            # WR-06: log only the id, not the full row — data carries PII (phone/email/name)
            # and the file handler retains 5×10MB rotated logs on disk.
            logger.info(f"Successfully appended row for telegram_id={(data[0] if data else '?')!r} to Google Sheet")
            return
        except Exception as e:
            _reset_sheet_cache()  # drop possibly-stale client/handle before retrying
            delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            logger.warning(f"Sheet append attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

    logger.error(f"Failed to append to Google Sheet after {MAX_RETRIES} attempts for telegram_id={(data[0] if data else '?')!r}")
    await _alert_admins_sheet_failure(f"основная вкладка, telegram_id={(data[0] if data else '?')!r}")


async def ensure_sheet_header(headers: list[str]):
    """Make sure row 1 of the sheet is the column-name header. Fail-soft: a missing
    sheet/credentials or API error never blocks the bot."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return
    try:
        await asyncio.to_thread(_ensure_header_sync, headers)
    except Exception as e:
        _reset_sheet_cache()
        logger.warning(f"ensure_sheet_header failed (skipping): {e}")


async def get_existing_sheet_ids() -> set[int]:
    return await asyncio.to_thread(_get_existing_ids_sync)


async def append_rows_to_sheet(rows: list[list]):
    if not rows:
        return 0
    await asyncio.to_thread(_append_rows_sync, rows)
    return len(rows)


def _sync_named_worksheet_sync(title: str, headers: list[str], rows: list[list]) -> int:
    """Overwrite a dedicated tab (create if missing) with header + rows. Used for the
    «Незавершённые» dropout export — a full refresh, not an append."""
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(len(rows) + 10, 100), cols=max(len(headers), 4))
    ws.clear()
    ws.update(values=[headers] + [list(r) for r in rows], range_name="A1")
    return len(rows)


def _dedupe_sheet_sync() -> int:
    """Remove duplicate rows on the data tab that share a Telegram id (col 1), keeping the
    LAST occurrence (freshest data). Deletes whole rows (bottom-up so indices don't shift),
    which preserves any manual columns on the kept row. Row 1 (header) is never touched.
    Returns the number of rows deleted."""
    sheet = _get_sheet()
    col1 = sheet.col_values(1)
    rows_by_id: dict[str, list[int]] = {}
    for offset, val in enumerate(col1[1:], start=2):  # row numbers are 1-based; skip header
        v = (val or "").strip()
        if not v or not v.lstrip("-").isdigit():
            continue
        rows_by_id.setdefault(v, []).append(offset)
    to_delete = [r for rowlist in rows_by_id.values() if len(rowlist) > 1 for r in rowlist[:-1]]
    for r in sorted(to_delete, reverse=True):
        sheet.delete_rows(r)
    return len(to_delete)


async def dedupe_sheet_by_id() -> int:
    """Fail-soft wrapper. Returns rows deleted, REFUSED_UNPINNED_TAB (-2) if GOOGLE_SHEET_TAB
    isn't explicitly set (see rebuild_main_sheet's docstring — same reasoning: this permanently
    DELETES rows on whatever tab _get_sheet() resolves, and an unpinned/positional resolution
    is not a safe target for a destructive op), or -1 for unconfigured / API error."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return -1
    if not _tab_explicitly_configured():
        logger.warning("dedupe_sheet_by_id refused: GOOGLE_SHEET_TAB not set (positional main tab)")
        return REFUSED_UNPINNED_TAB
    try:
        return await asyncio.to_thread(_dedupe_sheet_sync)
    except Exception as e:
        _reset_sheet_cache()
        logger.error(f"dedupe_sheet_by_id failed: {e}")
        return -1


async def sync_named_worksheet(title: str, headers: list[str], rows: list[list]) -> int:
    """Fail-soft full-refresh of a named tab. Returns the number of data rows written,
    or -1 when the sheet is not configured / an API error occurs."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return -1
    try:
        return await asyncio.to_thread(_sync_named_worksheet_sync, title, headers, rows)
    except Exception as e:
        logger.error(f"sync_named_worksheet('{title}') failed: {e}")
        return -1


# --- Статус заявки в таблице (Таня п.5) -------------------------------------------------
# Список значений выпадашки в колонке «Статус». Совпадает с registration.STATUS_LABELS.
STATUS_HEADER = "Статус"
STATUS_DROPDOWN = ["Новая", "Одобрена", "Отклонена"]
# ярлык → фон (light): жёлтый / зелёный / красный.
_STATUS_COLORS = [
    ("Новая", (1.0, 0.95, 0.6)),
    ("Одобрена", (0.72, 0.88, 0.70)),
    ("Отклонена", (0.96, 0.78, 0.78)),
]


def _status_col_index(sheet) -> int:
    """0-based индекс колонки «Статус» по фактической шапке (row 1). -1 если её нет."""
    try:
        return [h.strip() for h in sheet.row_values(1)].index(STATUS_HEADER)
    except ValueError:
        return -1


def _apply_status_formatting_sync(sheet, num_rows: int):
    """Выпадашка (data validation ONE_OF_LIST) + условное форматирование (цвета) на
    колонку «Статус». strict=False — ручные/IMPORTRANGE-значения не блокируются.
    IN-08: удаляем существующие conditional-format правила перед добавлением, чтобы
    повторные «Пересобрать таблицу» не копили дубли."""
    col0 = _status_col_index(sheet)
    if col0 < 0:
        return
    end_row = 1 + max(num_rows, 1)  # данные с row 2 (row 1 — шапка)
    grid = {
        "sheetId": sheet.id,
        "startRowIndex": 1,
        "endRowIndex": end_row,
        "startColumnIndex": col0,
        "endColumnIndex": col0 + 1,
    }
    # IN-08: count existing conditional-format rules on this sheet and delete them (highest
    # index first) in the same batch before re-adding, so rules don't accumulate over rebuilds.
    existing_rules = 0
    try:
        meta = sheet.spreadsheet.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("sheetId") == sheet.id:
                existing_rules = len(s.get("conditionalFormats", []) or [])
                break
    except Exception:
        existing_rules = 0
    requests = [
        {"deleteConditionalFormatRule": {"sheetId": sheet.id, "index": i}}
        for i in range(existing_rules - 1, -1, -1)
    ]
    requests.append({
        "setDataValidation": {
            "range": grid,
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in STATUS_DROPDOWN],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    })
    for idx, (val, (r, g, b)) in enumerate(_STATUS_COLORS):
        requests.append({
            "addConditionalFormatRule": {
                "index": idx,
                "rule": {
                    "ranges": [grid],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": val}]},
                        "format": {"backgroundColor": {"red": r, "green": g, "blue": b}},
                    },
                },
            }
        })
    sheet.spreadsheet.batch_update({"requests": requests})


def _update_status_in_sheet_sync(telegram_id: int, label: str) -> bool:
    """Найти строку по col1==telegram_id и записать label в колонку «Статус». False если
    колонки/строки нет."""
    sheet = _get_sheet()
    status_col = _status_col_index(sheet)  # 0-based
    if status_col < 0:
        return False
    target = str(telegram_id)
    col1 = sheet.col_values(1)
    for row_idx, val in enumerate(col1[1:], start=2):  # skip header
        if (val or "").strip() == target:
            sheet.update_cell(row_idx, status_col + 1, label)  # update_cell 1-based col
            return True
    return False


def _bulk_update_status_sync(id_to_label: dict[str, str]) -> int:
    """Один проход: прочитать col1, записать статусы для всех telegram_id из mapping
    одним batch_update (quota-friendly для массового одобрения). 0 если колонки нет."""
    sheet = _get_sheet()
    status_col = _status_col_index(sheet)  # 0-based
    if status_col < 0:
        return 0
    col1 = sheet.col_values(1)
    updates = []
    for row_idx, val in enumerate(col1[1:], start=2):  # skip header
        key = (val or "").strip()
        if key in id_to_label:
            a1 = gspread.utils.rowcol_to_a1(row_idx, status_col + 1)
            updates.append({"range": a1, "values": [[id_to_label[key]]]})
    if updates:
        sheet.batch_update(updates)
    return len(updates)


def _rebuild_main_sheet_sync(headers: list[str], rows: list[list]) -> int:
    """Полная пересборка листа данных: очистить, записать шапку + все строки в текущем
    порядке колонок, применить выпадашку/цвета к «Статус». Выравнивает старые строки после
    смены порядка колонок."""
    sheet = _get_sheet()
    if sheet.col_count < len(headers):
        sheet.add_cols(len(headers) - sheet.col_count)
    sheet.clear()
    values = [headers] + [list(r) for r in rows]
    sheet.update(values=values, range_name="A1")
    try:
        _apply_status_formatting_sync(sheet, len(rows))
    except Exception as e:
        logger.warning(f"apply_status_formatting failed (skipping): {e}")
    return len(rows)


async def update_status_in_sheet(telegram_id: int, label: str) -> bool:
    """Fail-soft автосинк статуса заявки в таблицу. True если ячейка обновлена."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return False
    try:
        return await asyncio.to_thread(_update_status_in_sheet_sync, telegram_id, label)
    except Exception as e:
        _reset_sheet_cache()
        logger.warning(f"update_status_in_sheet({telegram_id}) failed: {e}")
        return False


async def bulk_update_status_in_sheet(id_to_label: dict[str, str]) -> int:
    """Fail-soft массовый автосинк статусов (одобрить все). Возвращает число обновлённых
    ячеек или -1 при ошибке."""
    if not id_to_label or not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return -1
    try:
        return await asyncio.to_thread(_bulk_update_status_sync, id_to_label)
    except Exception as e:
        _reset_sheet_cache()
        logger.warning(f"bulk_update_status_in_sheet failed: {e}")
        return -1


async def rebuild_main_sheet(headers: list[str], rows: list[list]) -> int:
    """Fail-soft полная пересборка листа данных. Возвращает число строк, REFUSED_UNPINNED_TAB
    (-2) если GOOGLE_SHEET_TAB не задан явно, или -1 при ошибке.

    Quick task 260813: this is the operation that CLEARS the resolved worksheet before
    rewriting it (_rebuild_main_sheet_sync -> sheet.clear()) — the single most destructive
    Sheets call in this module. When GOOGLE_SHEET_TAB is empty, _get_sheet() resolves the main
    tab by position-then-pin (see its docstring): the FIRST time it ever ran, it picked
    whatever tab happened to be sh.sheet1 at that moment. That is not an admin-confirmed
    target — it is an accident of tab order on some earlier day. Wiping it is refused until an
    admin explicitly names the tab, at which point _get_sheet() targets it by name unambiguously."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return -1
    if not _tab_explicitly_configured():
        logger.warning("rebuild_main_sheet refused: GOOGLE_SHEET_TAB not set (positional main tab)")
        return REFUSED_UNPINNED_TAB
    try:
        return await asyncio.to_thread(_rebuild_main_sheet_sync, headers, rows)
    except Exception as e:
        _reset_sheet_cache()
        logger.error(f"rebuild_main_sheet failed: {e}")
        return -1


# --- Phase 5 (D-11/D-12, plan 05-06): incremental append to a SECOND, admin-named tab ------
# `append_to_sheet` above is hardcoded to the single cached `_sheet` global (the main tab); the
# party tab needs the exact same incremental-append shape but keyed by an arbitrary tab name.
# `sync_named_worksheet` (above) already targets an arbitrary tab but does a FULL OVERWRITE —
# wrong shape for per-registration appends. Do not repurpose either — add a parallel cache.

# Second cache, keyed by tab name, mirroring _sheet/_sheet_lock exactly (same TOCTOU rationale
# at WR-05 above — sheet ops run across worker threads via asyncio.to_thread).
_named_sheets: dict[str, object] = {}
_named_sheets_lock = threading.Lock()


def _get_named_sheet(tab_name: str):
    """Lazy double-checked-lock cache keyed by tab name (mirrors _get_sheet). Auto-creates the
    tab on WorksheetNotFound (D-11: the party tab needs no manual setup)."""
    if tab_name in _named_sheets:
        return _named_sheets[tab_name]
    with _named_sheets_lock:
        if tab_name in _named_sheets:
            return _named_sheets[tab_name]
        gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=1000, cols=30)
        _named_sheets[tab_name] = ws
        return ws


def _reset_named_sheet_cache(tab_name: str):
    _named_sheets.pop(tab_name, None)


def _append_to_named_sheet_sync(tab_name: str, data: list):
    _get_named_sheet(tab_name).append_row(data)


async def append_to_named_sheet(tab_name: str, data: list):
    """Fire-and-forget row append to a named (non-main) tab. Mirrors append_to_sheet's
    retry/backoff loop verbatim — same MAX_RETRIES/RETRY_DELAYS, same fail-soft posture.
    WR-06: log only the id + tab name, never the full row (party rows carry phone/allergy PII)."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        logger.warning(f"Google Sheet ID or Credentials not set. Skipping named sheet export (tab={tab_name!r}).")
        return

    for attempt in range(MAX_RETRIES):
        try:
            await asyncio.to_thread(_append_to_named_sheet_sync, tab_name, data)
            logger.info(f"Successfully appended row for telegram_id={(data[0] if data else '?')!r} to tab {tab_name!r}")
            return
        except Exception as e:
            _reset_named_sheet_cache(tab_name)
            delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            logger.warning(f"Named sheet ({tab_name!r}) append attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

    logger.error(f"Failed to append to tab {tab_name!r} after {MAX_RETRIES} attempts for telegram_id={(data[0] if data else '?')!r}")
    await _alert_admins_sheet_failure(f"вкладка {tab_name!r}, telegram_id={(data[0] if data else '?')!r}")


def _ensure_named_header_sync(tab_name: str, headers: list[str]):
    """Mirror _ensure_header_sync exactly, targeting the named tab."""
    sheet = _get_named_sheet(tab_name)
    if sheet.col_count < len(headers):
        sheet.add_cols(len(headers) - sheet.col_count)
    col1 = sheet.col_values(1)
    if not col1:
        sheet.append_row(headers)
        return
    first = (col1[0] or "").strip()
    if first.lstrip("-").isdigit():
        sheet.insert_row(headers, 1)
        return
    current = [h.strip() for h in sheet.row_values(1)]
    if current != headers:
        end = gspread.utils.rowcol_to_a1(1, len(headers))
        sheet.update(values=[headers], range_name=f"A1:{end}")


async def ensure_named_sheet_header(tab_name: str, headers: list[str]):
    """Fail-soft, mirrors ensure_sheet_header but targets a named tab."""
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        return
    try:
        await asyncio.to_thread(_ensure_named_header_sync, tab_name, headers)
    except Exception as e:
        _reset_named_sheet_cache(tab_name)
        logger.warning(f"ensure_named_sheet_header({tab_name!r}) failed (skipping): {e}")
