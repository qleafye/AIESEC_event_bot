import asyncio
import logging

import gspread
from config import config


def _append_to_sheet_sync(data: list):
    gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    sheet = sh.sheet1
    sheet.append_row(data)

async def append_to_sheet(data: list):
    """
    Appends a row of data to the configured Google Sheet.
    This function runs synchronously because gspread is sync, 
    so we should be careful or run it in an executor if high load.
    For this bot, direct call is acceptable or we can wrap in to_thread.
    """
    if not config.GOOGLE_SHEET_ID or not config.GOOGLE_CREDENTIALS_FILE:
        logging.warning("Google Sheet ID or Credentials not set. Skipping sheet export.")
        return

    try:
        await asyncio.to_thread(_append_to_sheet_sync, data)
        logging.info(f"Successfully appended to Google Sheet: {data}")
    except Exception as e:
        logging.error(f"Failed to append to Google Sheet: {e}")
