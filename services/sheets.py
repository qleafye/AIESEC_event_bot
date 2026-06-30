import asyncio
import logging

import gspread
from config import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]


def _get_sheet():
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    return sh.sheet1


def _append_to_sheet_sync(data: list):
    sheet = _get_sheet()
    sheet.append_row(data)


def _ensure_header_sync(headers: list[str]):
    sheet = _get_sheet()
    col1 = sheet.col_values(1)
    if not col1:
        # empty sheet → header becomes the first row
        sheet.append_row(headers)
        return
    first = (col1[0] or "").strip()
    if first.lstrip("-").isdigit():
        # first row is data (a Telegram id) → no header yet, insert one on top
        sheet.insert_row(headers, 1)
    # else: a text header is already present — leave it untouched


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
        logger.warning(f"ensure_sheet_header failed (skipping): {e}")


async def get_existing_sheet_ids() -> set[int]:
    return await asyncio.to_thread(_get_existing_ids_sync)


async def append_rows_to_sheet(rows: list[list]):
    if not rows:
        return 0
    await asyncio.to_thread(_append_rows_sync, rows)
    return len(rows)
