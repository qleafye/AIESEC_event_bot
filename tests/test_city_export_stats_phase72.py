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


# ── Task 2: stats screen with a per-city breakdown (row per city + total) ───────────────

def _expected_todays_literal(total, top_unis):
    import html as html_mod
    text = (
        f"📊 <b>Статистика:</b>\n"
        f"Всего регистраций: {total}\n"
        f"🏆 <b>Топ-3 ВУЗа:</b>\n"
    )
    for i, (uni, count) in enumerate(top_unis, 1):
        text += f"{i}. {html_mod.escape(str(uni))} — {count}\n"
    return text


def test_render_stats_text_module_off_matches_todays_literal(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, None)
    _seed_city(2, "spb")
    total, top_unis = asyncio.run(db.get_stats())
    expected = _expected_todays_literal(total, top_unis)
    actual = asyncio.run(admin_mod.render_stats_text())
    assert actual == expected
    assert "По городам" not in actual


def test_render_stats_text_module_on_row_per_city_plus_total(tmp_path):
    _seed_three_cities(tmp_path)
    text = asyncio.run(admin_mod.render_stats_text())
    assert "🏙 <b>По городам:</b>" in text
    for c in cities.CITIES:
        label = asyncio.run(cities.city_label(c["code"]))
        assert f"• {label} —" in text
    assert "• <b>Итого</b> —" in text

    total, _top = asyncio.run(db.get_stats())
    # sum of "всего N" numbers on city rows (excluding the Итого line) equals get_stats() total
    import re
    city_block = text.split("🏙 <b>По городам:</b>")[1]
    lines = [l for l in city_block.splitlines() if l.strip().startswith("•")]
    city_lines = [l for l in lines if "Итого" not in l]
    total_lines = [int(re.search(r"всего (\d+)", l).group(1)) for l in city_lines]
    assert sum(total_lines) == total
    itogo_line = [l for l in lines if "Итого" in l][0]
    assert int(re.search(r"всего (\d+)", itogo_line).group(1)) == total


def test_render_stats_text_null_and_unknown_collapse_into_default_city(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None)
    _seed_city(2, "__garbage_code__")
    text = asyncio.run(admin_mod.render_stats_text())
    default_code = cities.default_city_code()
    default_label = asyncio.run(cities.city_label(default_code))
    import re
    m = re.search(rf"• {re.escape(default_label)} — всего (\d+)", text)
    assert m is not None
    assert int(m.group(1)) == 2  # both the NULL row and the garbage-code row collapse here


def test_render_stats_text_identical_regardless_of_selected_admin_city(tmp_path):
    _seed_three_cities(tmp_path)
    asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    text_msk = asyncio.run(admin_mod.render_stats_text())
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    text_spb = asyncio.run(admin_mod.render_stats_text())
    assert text_msk == text_spb


def test_render_stats_text_escapes_city_label(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__msk", "<script>alert(1)</script>"))
    _seed_city(1, "msk")
    text = asyncio.run(admin_mod.render_stats_text())
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_cmd_stats_and_show_admin_stats_delegate_to_render_stats_text(tmp_path):
    _admin_ready(tmp_path)
    _seed_city(1, "spb")

    class _FakeAnswerMessage:
        def __init__(self, uid):
            self.from_user = FakeUser(uid)
            self.sent = []

        async def answer(self, text, parse_mode=None):
            self.sent.append(text)

    msg = _FakeAnswerMessage(ADMIN_ID)
    asyncio.run(admin_mod.cmd_stats(msg))
    expected = asyncio.run(admin_mod.render_stats_text())
    assert msg.sent[-1] == expected

    cb = FakeCallback("admin_stats")
    asyncio.run(admin_mod.show_admin_stats(cb))
    assert cb.message.text == expected
