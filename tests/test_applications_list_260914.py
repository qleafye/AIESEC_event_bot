"""Квик 260914-rgq (RGQ-01): экран «📇 Список заявок» — менеджер видит постранично одобренных/
отклонённых/ожидающих делегатов: имя — ник — когда решили, без выгрузки в таблицу.

Задача 1 (этот блок) — аксессоры `database.db.list_applications_page`/`count_applications`:
страница + счётчики по трём статусам, city-scope, фолбэки даты решения. Задача 2 дописывает
сюда тесты экрана бота отдельным блоком ниже.

pytest-asyncio в проекте нет — каждый async-вызов через `asyncio.run()`. `_ready` — тот же
хелпер, что у `tests/test_sheet_logs_260902.py` (подменяет `config.DB_PATH` на tmp_path и
вызывает `db.init_db()`). У общего `_add_user` оттуда нет статуса и дат решения — здесь свой
локальный `_add` сырым INSERT в `users`.
"""
import asyncio
import html as html_escape_module

import cities
from database import db

from tests.test_sheet_logs_260902 import _ready


def html_escape(s):
    return html_escape_module.escape(str(s))


def _run(coro):
    return asyncio.run(coro)


async def _add(tid, *, name="Иван", username="@ivan", status="approved", city=None,
                registered="2026-09-01 10:00:00", approved_at=None, rejected_at=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city, "
            "registration_date, approved_at, rejected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, name, username, status, city, registered, approved_at, rejected_at),
        )
        await conn.commit()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1 — database/db.py: list_applications_page / count_applications
# ══════════════════════════════════════════════════════════════════════════════════════════

# ── фильтр по статусу ────────────────────────────────────────────────────────────────────────

def test_list_applications_page_approved_only_returns_approved(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="rejected")
        await _add(3, status="pending")
        rows = await db.list_applications_page(status="approved")
        assert [r["telegram_id"] for r in rows] == [1]

    _run(go())


def test_list_applications_page_rejected_only_returns_rejected(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="rejected")
        await _add(3, status="pending")
        rows = await db.list_applications_page(status="rejected")
        assert [r["telegram_id"] for r in rows] == [2]

    _run(go())


def test_list_applications_page_pending_only_returns_pending(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="rejected")
        await _add(3, status="pending")
        rows = await db.list_applications_page(status="pending")
        assert [r["telegram_id"] for r in rows] == [3]

    _run(go())


def test_list_applications_page_unknown_status_treated_as_approved(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="rejected")
        rows_bogus = await db.list_applications_page(status="bogus")
        rows_approved = await db.list_applications_page(status="approved")
        assert [r["telegram_id"] for r in rows_bogus] == [r["telegram_id"] for r in rows_approved]

    _run(go())


# ── дата решения: фолбэки approved ───────────────────────────────────────────────────────────

def test_decided_at_approved_uses_own_column_when_present(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", registered="2026-09-01 10:00:00",
                    approved_at="2026-09-05 12:00:00")
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_at"] == "2026-09-05 12:00:00"

    _run(go())


def test_decided_at_approved_falls_back_to_live_decision_when_column_null(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", registered="2026-09-01 10:00:00", approved_at=None)
        await db.record_application_decision(
            1, "approved", None, 999, "2026-09-03 08:00:00", "2026-09-03 08:05:00",
        )
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_at"] == "2026-09-03 08:00:00"

    _run(go())


def test_decided_at_approved_falls_back_to_registration_date_when_decision_undone(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", registered="2026-09-01 10:00:00", approved_at=None)
        decision_id = await db.record_application_decision(
            1, "approved", None, 999, "2026-09-03 08:00:00", "2026-09-03 08:05:00",
        )
        undone = await db.claim_application_undo(decision_id)
        assert undone is not None  # sanity: отмена действительно прошла
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_at"] == "2026-09-01 10:00:00"

    _run(go())


# ── дата решения: фолбэки rejected / pending ─────────────────────────────────────────────────

def test_decided_at_rejected_uses_own_column_when_present(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="rejected", registered="2026-09-01 10:00:00",
                    rejected_at="2026-09-06 09:00:00")
        rows = await db.list_applications_page(status="rejected")
        assert rows[0]["decided_at"] == "2026-09-06 09:00:00"

    _run(go())


def test_decided_at_rejected_falls_back_to_live_decision_when_column_null(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="rejected", registered="2026-09-01 10:00:00", rejected_at=None)
        await db.record_application_decision(
            1, "rejected", "не подошёл", 999, "2026-09-02 11:00:00", "2026-09-02 11:05:00",
        )
        rows = await db.list_applications_page(status="rejected")
        assert rows[0]["decided_at"] == "2026-09-02 11:00:00"

    _run(go())


def test_decided_at_rejected_falls_back_to_registration_date_without_column_or_decision(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="rejected", registered="2026-09-01 10:00:00", rejected_at=None)
        rows = await db.list_applications_page(status="rejected")
        assert rows[0]["decided_at"] == "2026-09-01 10:00:00"

    _run(go())


def test_decided_at_pending_always_uses_registration_date(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="pending", registered="2026-09-01 10:00:00")
        rows = await db.list_applications_page(status="pending")
        assert rows[0]["decided_at"] == "2026-09-01 10:00:00"

    _run(go())


# ── decided_by: кто принял решение (владелец 16.09) ──────────────────────────────────────────

def test_list_applications_page_decided_by_from_live_decision(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", approved_at="2026-09-05 12:00:00")
        await db.record_application_decision(
            1, "approved", None, 777, "2026-09-05 12:00:00", "2026-09-05 12:05:00",
        )
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_by"] == 777

    _run(go())


def test_list_applications_page_decided_by_null_without_decision_row(tmp_path):
    _ready(tmp_path)

    async def go():
        # Автоодобрение (services/reg_finalize.py::post_finalize) НЕ пишет строку в
        # application_decisions вовсе — этот случай и легаси-строки без журнала неотличимы,
        # оба читаются экраном как «автоматически».
        await _add(1, status="approved", approved_at="2026-09-05 12:00:00")
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_by"] is None

    _run(go())


def test_list_applications_page_decided_by_null_when_decision_undone(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", approved_at=None, registered="2026-09-01 10:00:00")
        decision_id = await db.record_application_decision(
            1, "approved", None, 777, "2026-09-03 08:00:00", "2026-09-03 08:05:00",
        )
        await db.claim_application_undo(decision_id)
        rows = await db.list_applications_page(status="approved")
        assert rows[0]["decided_by"] is None

    _run(go())


def test_list_applications_page_decided_by_pending_always_null(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="pending")
        rows = await db.list_applications_page(status="pending")
        assert rows[0]["decided_by"] is None

    _run(go())


def test_resolve_decision_managers_prefers_full_name(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(777, name="Марина Иванова", username="marina", status="approved")
        labels = await db.resolve_decision_managers([777])
        assert labels == {777: "Марина Иванова"}

    _run(go())


def test_resolve_decision_managers_falls_back_to_username_without_full_name(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(777, name="", username="marina", status="approved")
        labels = await db.resolve_decision_managers([777])
        assert labels == {777: "@marina"}

    _run(go())


def test_resolve_decision_managers_unknown_id_gets_numbered_placeholder(tmp_path):
    _ready(tmp_path)

    async def go():
        labels = await db.resolve_decision_managers([999])
        assert labels == {999: "менеджер #999"}

    _run(go())


def test_resolve_decision_managers_ignores_falsy_ids_and_empty_list(tmp_path):
    _ready(tmp_path)

    async def go():
        assert await db.resolve_decision_managers([]) == {}
        assert await db.resolve_decision_managers([None, 0]) == {}

    _run(go())


# ── порядок, пагинация, city-scope ───────────────────────────────────────────────────────────

def test_list_applications_page_order_newest_decision_first(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved", approved_at="2026-09-01 10:00:00")
        await _add(2, status="approved", approved_at="2026-09-03 10:00:00")
        await _add(3, status="approved", approved_at="2026-09-02 10:00:00")
        rows = await db.list_applications_page(status="approved")
        assert [r["telegram_id"] for r in rows] == [2, 3, 1]

    _run(go())


def test_list_applications_page_tiebreak_by_telegram_id_desc(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(10, status="approved", approved_at="2026-09-01 10:00:00")
        await _add(20, status="approved", approved_at="2026-09-01 10:00:00")
        rows = await db.list_applications_page(status="approved")
        assert [r["telegram_id"] for r in rows] == [20, 10]

    _run(go())


def test_list_applications_page_limit_offset_slices_in_sql(tmp_path):
    _ready(tmp_path)

    async def go():
        for i in range(20):
            await _add(i, status="approved", approved_at=f"2026-09-01 10:{i:02d}:00")
        rows = await db.list_applications_page(status="approved", limit=15, offset=15)
        assert len(rows) == 5

    _run(go())


def test_list_applications_page_city_scope_hides_other_city(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _add(1, status="approved", city="spb", approved_at="2026-09-01 10:00:00")
        await _add(2, status="approved", city="tyumen", approved_at="2026-09-01 10:00:00")

        scope = cities.city_scope("spb")
        rows = await db.list_applications_page(status="approved", city_scope=scope)
        ids = {r["telegram_id"] for r in rows}
        assert 1 in ids
        assert 2 not in ids

    _run(go())


def test_list_applications_page_city_scope_none_returns_all_cities(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _add(1, status="approved", city="spb", approved_at="2026-09-01 10:00:00")
        await _add(2, status="approved", city="tyumen", approved_at="2026-09-01 10:00:00")

        rows = await db.list_applications_page(status="approved", city_scope=None)
        ids = {r["telegram_id"] for r in rows}
        assert {1, 2}.issubset(ids)

    _run(go())


# ── count_applications ───────────────────────────────────────────────────────────────────────

def test_count_applications_returns_three_status_counts(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="approved")
        await _add(3, status="rejected")
        await _add(4, status="pending")
        counts = await db.count_applications()
        assert counts == {"approved": 2, "rejected": 1, "pending": 1}

    _run(go())


def test_count_applications_respects_city_scope_same_as_list(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _add(1, status="approved", city="spb")
        await _add(2, status="approved", city="tyumen")
        await _add(3, status="rejected", city="spb")

        scope = cities.city_scope("spb")
        counts = await db.count_applications(city_scope=scope)
        assert counts == {"approved": 1, "rejected": 1, "pending": 0}

    _run(go())


def test_count_applications_empty_db_returns_zeros(tmp_path):
    _ready(tmp_path)

    async def go():
        counts = await db.count_applications()
        assert counts == {"approved": 0, "rejected": 0, "pending": 0}

    _run(go())


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2 — экран «📇 Список заявок» бота (handlers/admin_app_list.py) + права
#
# FakeCallback — из tests/test_admin_sections_ia20.py (простой dummy для прямого вызова
# хендлера); ADMIN_ID/STRANGER_ID/_roles_ready — из tests/test_roles_phase8.py; хендлеры зовутся
# напрямую, без полного dispatch через Router (тот же приём, что у test_questions_journal_260904).
# ══════════════════════════════════════════════════════════════════════════════════════════

import cities as cities_mod
import handlers.admin_sections as sec
from handlers import admin_app_list
from handlers.admin_caps import ADMIN_CAPS, required_capability
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_roles_phase8 import ADMIN_ID, STRANGER_ID, _roles_ready


def _btn_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ── render_app_list_screen: состав строки, экранирование, фолбэки ───────────────────────────

def test_render_default_shows_approved_page_zero(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="approved", approved_at="2026-09-14 18:30:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "📇" in text
        assert "Показаны: ✅ Одобренные" in text

    _run(go())


def test_row_format_link_username_date(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, name="Вася", username="ivanov", status="approved",
                    approved_at="2026-09-14 18:30:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert f'<a href="tg://user?id={STRANGER_ID}">Вася</a>' in text
        assert "@ivanov" in text
        assert "14.09 18:30" in text

    _run(go())


def test_row_empty_username_shows_placeholder(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, name="Вася", username=None, status="approved",
                    approved_at="2026-09-14 18:30:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "(без ника)" in text

    _run(go())


def test_row_username_normalizes_single_at_regardless_of_storage(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(1, name="A", username="ivan", status="approved",
                    approved_at="2026-09-01 10:00:00")
        await _add(2, name="B", username="@ivan", status="approved",
                    approved_at="2026-09-02 10:00:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "@@ivan" not in text
        assert text.count("@ivan") == 2

    _run(go())


def test_row_escapes_html_in_full_name(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, name="<b>Вася</b>", status="approved",
                    approved_at="2026-09-14 18:30:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "&lt;b&gt;" in text
        assert "<b>Вася</b> —" not in text

    _run(go())


def test_row_broken_or_empty_timestamp_shows_dash(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="pending", registered="мусор")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID, status="pending")
        assert " — —" in text

    _run(go())


# ── строка: кто принял решение (владелец 16.09) ──────────────────────────────────────────────

def test_row_approved_shows_manager_who_accepted(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(777, name="Марина Иванова", username="marina", status="approved")
        await _add(STRANGER_ID, name="Вася", status="approved",
                    approved_at="2026-09-12 10:00:00")
        await db.record_application_decision(
            STRANGER_ID, "approved", None, 777,
            "2026-09-12 10:00:00", "2026-09-12 10:05:00",
        )
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        row_line = next(line for line in text.split("\n") if line.startswith("1."))
        assert "· приняла Марина Иванова" in row_line

    _run(go())


def test_row_rejected_shows_manager_who_declined(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(777, name="Марина Иванова", username="marina", status="approved")
        await _add(STRANGER_ID, name="Вася", status="rejected",
                    rejected_at="2026-09-12 10:00:00")
        await db.record_application_decision(
            STRANGER_ID, "rejected", "не подошёл", 777,
            "2026-09-12 10:00:00", "2026-09-12 10:05:00",
        )
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID, status="rejected")
        row_line = next(line for line in text.split("\n") if line.startswith("1."))
        assert "· отклонил(а) Марина Иванова" in row_line

    _run(go())


def test_row_pending_has_no_decision_suffix(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, name="Вася", status="pending")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID, status="pending")
        row_line = next(line for line in text.split("\n") if line.startswith("1."))
        assert "·" not in row_line

    _run(go())


def test_row_auto_approved_without_decision_row_shows_automatically(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        # Ни одной строки в application_decisions -- короткий трек/автоодобрение
        # (services/reg_finalize.py::post_finalize), решение принял не человек.
        await _add(STRANGER_ID, name="Вася", status="approved",
                    approved_at="2026-09-12 10:00:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        row_line = next(line for line in text.split("\n") if line.startswith("1."))
        assert "· автоматически" in row_line

    _run(go())


def test_row_manager_without_users_row_shows_numbered_placeholder(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, name="Вася", status="approved",
                    approved_at="2026-09-12 10:00:00")
        await db.record_application_decision(
            STRANGER_ID, "approved", None, 555,
            "2026-09-12 10:00:00", "2026-09-12 10:05:00",
        )
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        row_line = next(line for line in text.split("\n") if line.startswith("1."))
        assert "· приняла менеджер #555" in row_line

    _run(go())


# ── шапка: счётчики, подписи даты, город, пагинация, пустой список ──────────────────────────

def test_header_shows_all_three_counters(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(1, status="approved")
        await _add(2, status="approved")
        await _add(3, status="rejected")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "✅ 2" in text
        assert "❌ 1" in text
        assert "⏳ 0" in text

    _run(go())


def test_header_date_caption_pending_says_podana_decided_says_reshenie(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(1, status="pending")
        text_pending, _ = await admin_app_list.render_app_list_screen(ADMIN_ID, status="pending")
        assert "(подана)" in text_pending

        await _add(2, status="approved", approved_at="2026-09-01 10:00:00")
        text_approved, _ = await admin_app_list.render_app_list_screen(ADMIN_ID, status="approved")
        assert "(решение)" in text_approved

    _run(go())


def test_header_city_label_shown_when_city_selected(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await cities_mod.set_admin_city(ADMIN_ID, "spb")
        await _add(STRANGER_ID, status="approved", city="spb", approved_at="2026-09-01 10:00:00")
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        label = await cities_mod.city_label("spb")
        assert html_escape(label) in text

    _run(go())


def test_empty_list_shows_page_1_of_1_and_human_empty_text(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        text, _ = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "Страница 1 из 1" in text
        assert "Пока пусто." in text

    _run(go())


def test_pagination_numbering_continues_across_pages(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        for i in range(20):
            await _add(i, status="approved", approved_at=f"2026-09-01 10:{i:02d}:00")
        text_page2, _ = await admin_app_list.render_app_list_screen(
            ADMIN_ID, status="approved", offset=15,
        )
        assert "16." in text_page2
        assert "Страница 2 из 2" in text_page2

    _run(go())


# ── клавиатура: статусы, навигация, callback_data ────────────────────────────────────────────

def test_status_buttons_mark_active_and_always_target_offset_zero(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="rejected", rejected_at="2026-09-01 10:00:00")
        _, kb = await admin_app_list.render_app_list_screen(ADMIN_ID, status="rejected", offset=0)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert "• ❌ Отклонённые" in texts
        callbacks = _btn_callbacks(kb)
        assert "apl:approved:0" in callbacks
        assert "apl:rejected:0" in callbacks
        assert "apl:pending:0" in callbacks

    _run(go())


def test_nav_buttons_absent_on_first_and_last_page(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="approved", approved_at="2026-09-01 10:00:00")
        _, kb = await admin_app_list.render_app_list_screen(ADMIN_ID, status="approved", offset=0)
        callbacks = _btn_callbacks(kb)
        assert not any(c and c.startswith("apl:approved:") and c != "apl:approved:0"
                        for c in callbacks)

    _run(go())


def test_nav_buttons_present_on_second_of_two_pages(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        for i in range(20):
            await _add(i, status="approved", approved_at=f"2026-09-01 10:{i:02d}:00")
        _, kb = await admin_app_list.render_app_list_screen(ADMIN_ID, status="approved", offset=15)
        callbacks = _btn_callbacks(kb)
        # offset=15, total=20: ⬅️ назад на страницу 1 есть, ➡️ дальше нет (последняя страница) —
        # единственный оффсет среди apl:approved:* callback'ов на этом рендере — 0.
        assert "apl:approved:0" in callbacks
        assert not any(c.startswith("apl:approved:") and c != "apl:approved:0"
                        for c in callbacks)

    _run(go())


def test_back_button_targets_apps_section(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        _, kb = await admin_app_list.render_app_list_screen(ADMIN_ID)
        callbacks = _btn_callbacks(kb)
        assert "admin_sec:apps" in callbacks

    _run(go())


def test_all_callback_data_under_64_bytes(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        for i in range(20):
            await _add(i, status="approved", approved_at=f"2026-09-01 10:{i:02d}:00")
        _, kb = await admin_app_list.render_app_list_screen(ADMIN_ID, status="approved", offset=15)
        for cb in _btn_callbacks(kb):
            assert len(cb.encode("utf-8")) < 64, cb

    _run(go())


# ── хендлеры: открытие, страница/статус, битый offset ────────────────────────────────────────

def test_admin_app_list_open_handler_renders_screen(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="approved", approved_at="2026-09-01 10:00:00")
        cb = FakeCallback("admin_app_list", user_id=ADMIN_ID)
        await admin_app_list.admin_app_list_open(cb)
        assert cb.message.edit_calls == 1
        assert "📇" in cb.message.text

    _run(go())


def test_apl_page_switches_status(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(STRANGER_ID, status="rejected", rejected_at="2026-09-01 10:00:00")
        cb = FakeCallback("apl:rejected:0", user_id=ADMIN_ID)
        await admin_app_list.apl_page(cb)
        assert "Показаны: ❌ Отклонённые" in cb.message.text

    _run(go())


def test_apl_page_invalid_offset_shows_alert(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        cb = FakeCallback("apl:approved:abc", user_id=ADMIN_ID)
        await admin_app_list.apl_page(cb)
        assert cb.answers == [("Некорректная страница", True)]

    _run(go())


def test_apl_page_negative_offset_shows_alert(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        cb = FakeCallback("apl:approved:-5", user_id=ADMIN_ID)
        await admin_app_list.apl_page(cb)
        assert cb.answers == [("Некорректная страница", True)]

    _run(go())


# ── права и принадлежность разделу ───────────────────────────────────────────────────────────

def test_required_capability_moderate_reg_for_both_callbacks():
    assert required_capability(callback_data="admin_app_list") == "moderate_reg"
    assert required_capability(callback_data="apl:approved:15") == "moderate_reg"


def test_visible_rows_apps_gated_by_moderate_reg():
    rows_without = sec.visible_rows("apps", {"stats"}, False)
    rows_with = sec.visible_rows("apps", {"moderate_reg"}, False)
    assert ("screen", "admin_app_list", "📇 Список заявок") not in rows_without
    assert ("screen", "admin_app_list", "📇 Список заявок") in rows_with


def test_section_of_admin_app_list_is_apps_declared_once():
    assert sec.section_of("admin_app_list") == "apps"
    occurrences = sum(
        1
        for _token, _label, rows in sec.SECTIONS
        for row in rows
        if sec.row_callback(row) == "admin_app_list"
    )
    assert occurrences == 1


def test_admin_caps_has_entries_for_both_new_callbacks():
    assert ADMIN_CAPS.get("admin_app_list") == "moderate_reg"
    assert ADMIN_CAPS.get("apl:*") == "moderate_reg"
