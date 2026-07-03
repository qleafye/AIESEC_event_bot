import asyncio
import logging

import gspread
from config import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]


# Cache the authorized client + worksheet handle: gspread.service_account() re-auths
# (token fetch) and open_by_key() is a network call, so rebuilding on every append cost
# ~3 round-trips per registration. google-auth refreshes the token on the cached client,
# so the handle stays valid; _reset_sheet_cache() drops it after a failure to force re-auth.
_sheet = None


def _get_sheet():
    global _sheet
    if _sheet is not None:
        return _sheet
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    _sheet = gc.open_by_key(config.GOOGLE_SHEET_ID).sheet1
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
            logger.info(f"Successfully appended to Google Sheet: {data}")
            return
        except Exception as e:
            _reset_sheet_cache()  # drop possibly-stale client/handle before retrying
            delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            logger.warning(f"Sheet append attempt {attempt + 1}/{MAX_RETRIES} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

    logger.error(f"Failed to append to Google Sheet after {MAX_RETRIES} attempts: {data}")


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
