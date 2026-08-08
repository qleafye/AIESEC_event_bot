"""Phase 07.2 Plan 03 (per-city admin panels, CITY-02) tests: city-scoped CSV export
(`show_admin_export`) and the per-city stats screen (`render_stats_text`).

Stats are the deliberate EXCEPTION to city scoping — one row per city + a total on ONE
screen, never filtered by the admin's selected city (07.2-CONTEXT.md). CSV export IS
scoped (opposite behavior, both by design).

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_admin_phase72.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import inspect

from config import config
from database import db
from handlers import admin as admin_mod
import cities


ADMIN_ID = 930101
NON_ADMIN_ID = 930102


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_city_export_stats72.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeExportMessage:
    """Stand-in for the aiogram Message the export callback answers on — captures
    answer_document calls (filename/caption/bytes) instead of edit_text."""

    def __init__(self):
        self.documents = []

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeExportMessage()
        self.bot = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _seed_city(telegram_id, event_city, status="pending"):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": f"2026-01-01 09:{telegram_id:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))


def _seed_three_cities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None)
    _seed_city(2, "msk")
    _seed_city(3, "spb")
    _seed_city(4, "spb")
    _seed_city(5, "tyumen")


# ── Task 1: CSV export scoped to the admin's selected city ──────────────────────────────

def test_show_admin_export_module_off_byte_identical_filename_caption(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    assert len(cb.message.documents) == 1
    document, caption = cb.message.documents[0]
    assert document.filename == "users.csv"
    assert caption == "База данных пользователей"


def test_show_admin_export_scoped_spb_filename_and_rows(tmp_path):
    _seed_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    document, caption = cb.message.documents[0]
    assert document.filename == "users_spb.csv"
    spb_label = asyncio.run(cities.city_label("spb"))
    assert spb_label in caption

    headers, rows = asyncio.run(db.export_users_csv())
    tid_idx = headers.index("Telegram ID") if "Telegram ID" in headers else None
    # Decode the exported CSV bytes and check contained telegram_ids instead of relying on
    # a specific header label — parse via csv module for robustness.
    import csv as csv_mod
    import io as io_mod
    text = document.data.decode("utf-8-sig")
    reader = csv_mod.reader(io_mod.StringIO(text), delimiter=";")
    all_rows = list(reader)
    body = all_rows[1:]
    # telegram_id is always the first column of `users` (SELECT *), so first CSV cell.
    ids_in_export = {row[0] for row in body}
    assert ids_in_export == {"3", "4"}


def test_show_admin_export_default_city_includes_null(tmp_path):
    _seed_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    cb = FakeCallback("admin_export_csv")
    asyncio.run(admin_mod.show_admin_export(cb))
    document, _caption = cb.message.documents[0]
    import csv as csv_mod
    import io as io_mod
    text = document.data.decode("utf-8-sig")
    reader = csv_mod.reader(io_mod.StringIO(text), delimiter=";")
    body = list(reader)[1:]
    ids_in_export = {row[0] for row in body}
    assert ids_in_export == {"1", "2"}  # NULL row (1) collapses into default city (msk, 2)


def test_show_admin_export_rejects_non_admin(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("admin_export_csv", user_id=NON_ADMIN_ID)
    asyncio.run(admin_mod.show_admin_export(cb))
    assert cb.answers[-1] == ("Недостаточно прав", True)
    assert cb.message.documents == []


def test_export_incomplete_calls_batches_helper_without_city_arg():
    src = inspect.getsource(admin_mod.export_incomplete)
    assert "incomplete_city_batches()" in src
