"""Phase 07.3 Plan 06 (RET-04) — «📥 Импорт прошлого события» (forum.db import).

Style matches tests/test_season_reset_073.py: pytest-asyncio is unavailable in this env, every
async call goes through asyncio.run(), config.DB_PATH points at a tmp_path file, no conftest.py
(project convention). A second, throwaway sqlite file (the "imported forum.db") is built with
raw stdlib sqlite3 to emulate a file uploaded by the manager.
"""
import asyncio
import io
import os
import sqlite3
import tempfile

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from database.db import bulk_insert_users_if_absent
from handlers import admin as admin_mod
from handlers.admin_caps import ADMIN_CAPS
# NOTE: `from handlers.states import SeasonImport` is added by Task 2, once the class exists —
# importing it here before then would break collection of this whole file's Task 1 tests.

ADMIN_ID = 900401
OTHER_ID = 900402


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_import_073.db")


def _db_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _FakeDocument:
    def __init__(self, file_id="doc1", file_size=1024):
        self.file_id = file_id
        self.file_size = file_size


class _FakeMessage:
    def __init__(self, user_id, text=None, document=None):
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.document = document
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeBot:
    """`await bot.download(file_id)` — same idiom services/nextcloud.py::upload_resume uses."""

    def __init__(self, content: bytes):
        self.content = content
        self.downloaded_file_ids = []

    async def download(self, file_id):
        self.downloaded_file_ids.append(file_id)
        return io.BytesIO(self.content)


def _flat_callback_data(kb: InlineKeyboardMarkup):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _seed_user(telegram_id, **overrides):
    data = {
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": "2026-01-01T00:00:00",
    }
    data.update(overrides)
    asyncio.run(db.add_user(data))


def _make_foreign_db(tmp_path, rows, name="forum_src.db", table="users"):
    """Builds a throwaway sqlite file emulating an uploaded forum.db. `rows` is a list of
    dicts — the union of their keys becomes the foreign table's columns (TEXT-typed, simplest
    schema that lets sqlite3 accept any Python value)."""
    path = str(tmp_path / name)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    if table:
        cols = set()
        for r in rows:
            cols.update(r.keys())
        cols = sorted(cols) if cols else ["telegram_id"]
        col_def = ", ".join(f'"{c}"' for c in cols)
        con.execute(f"CREATE TABLE {table} ({col_def})")
        for r in rows:
            keys = list(r.keys())
            placeholders = ", ".join("?" for _ in keys)
            col_list = ", ".join(f'"{k}"' for k in keys)
            con.execute(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", [r[k] for k in keys])
        con.commit()
    con.close()
    with open(path, "rb") as f:
        return f.read()


def _make_garbage_bytes() -> bytes:
    return b"this is not a sqlite file, just random garbage bytes 0123456789"


def _snapshot_row(telegram_id):
    return asyncio.run(db.get_user(telegram_id))


# ── Task 1: bulk_insert_users_if_absent — батч-вставка без побочных эффектов ────────────────

def test_bulk_insert_adds_only_absent(tmp_path):
    _db_ready(tmp_path)
    _seed_user(1, full_name="Existing One", university="MGU")
    before = _snapshot_row(1)

    rows = [
        {"telegram_id": 1, "full_name": "Imported One (should not overwrite)"},
        {"telegram_id": 2, "full_name": "New Two"},
        {"telegram_id": 3, "full_name": "New Three"},
    ]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 2
    after = _snapshot_row(1)
    assert after == before  # not one column changed
    assert _snapshot_row(2) is not None
    assert _snapshot_row(3) is not None


def test_bulk_insert_sets_season(tmp_path):
    _db_ready(tmp_path)
    rows = [{"telegram_id": 10}, {"telegram_id": 11}]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 2
    assert _snapshot_row(10)["season"] == "YL'25"
    assert _snapshot_row(11)["season"] == "YL'25"


def test_bulk_insert_drops_excluded_columns(tmp_path):
    _db_ready(tmp_path)
    rows = [{
        "telegram_id": 20,
        "payment_status": "paid",
        "paid_at": "2026-01-01T00:00:00",
        "referrer_id": 42,
    }]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 1
    row = _snapshot_row(20)
    assert row["payment_status"] != "paid"
    assert row["paid_at"] is None
    assert row["referrer_id"] is None


def test_bulk_insert_ignores_unknown_columns(tmp_path):
    _db_ready(tmp_path)
    rows = [{"telegram_id": 30, "full_name": "Someone", "some_old_field": "legacy junk"}]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 1
    row = _snapshot_row(30)
    assert "some_old_field" not in row
    assert row["full_name"] == "Someone"


def test_bulk_insert_missing_columns_are_null(tmp_path):
    _db_ready(tmp_path)
    rows = [{"telegram_id": 40, "full_name": "Only Name And Id"}]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 1
    row = _snapshot_row(40)
    assert row["full_name"] == "Only Name And Id"
    assert row["university"] is None
    assert row["email"] is None


def test_bulk_insert_skips_rows_without_id(tmp_path):
    _db_ready(tmp_path)
    rows = [
        {"telegram_id": None, "full_name": "No id"},
        {"full_name": "Missing id key entirely"},
        {"telegram_id": "not-a-number", "full_name": "Garbage id"},
        {"telegram_id": 50, "full_name": "Valid"},
    ]
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))

    assert inserted == 1
    assert _snapshot_row(50) is not None


def test_bulk_insert_empty_rows(tmp_path):
    _db_ready(tmp_path)
    inserted = asyncio.run(bulk_insert_users_if_absent([], "YL'25"))
    assert inserted == 0
    total, _ = asyncio.run(db.get_stats())
    assert total == 0


def test_bulk_insert_no_side_effects(tmp_path):
    _db_ready(tmp_path)
    rows = [{"telegram_id": 60, "full_name": "Sixty"}]
    before_coins = asyncio.run(db.get_balance(60))
    inserted = asyncio.run(bulk_insert_users_if_absent(rows, "YL'25"))
    after_coins = asyncio.run(db.get_balance(60))

    assert inserted == 1
    assert before_coins == after_coins == 0
