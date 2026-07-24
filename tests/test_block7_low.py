"""BLOCK 7 (LOW) regressions.

- Main-tab formula-injection parity: active_sheet_row must neutralize a crafted cell via
  _csv_safe, matching the party path.
- Negative-amount guard: _parse_options must clamp a negative price to 0.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg
from handlers.payment import _parse_options


def test_main_tab_active_sheet_row_neutralizes_formula(tmp_path):
    config.DB_PATH = str(tmp_path / "csv_main.db")

    async def go():
        await db.init_db()
        data = {
            "telegram_id": 1,
            "full_name": "=HYPERLINK(\"http://evil\",\"click\")",
            "registration_date": "2026-07-01 10:00:00",
        }
        return await reg.active_sheet_row(data)

    row = asyncio.run(go())
    # The crafted ФИО must be stored as text (leading apostrophe), never left to evaluate.
    assert any(isinstance(c, str) and c.startswith("'=HYPERLINK") for c in row), row
    assert not any(isinstance(c, str) and c.startswith("=HYPERLINK") for c in row), row


def test_parse_options_clamps_negative_price():
    opts = _parse_options("Скидка|-500")
    assert opts == [("Скидка", 0, None)]  # negative clamped to 0


def test_parse_options_keeps_valid_price():
    assert _parse_options("Участие|3000") == [("Участие", 3000, None)]
