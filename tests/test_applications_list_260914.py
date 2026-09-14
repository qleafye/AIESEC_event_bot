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

import cities
from database import db

from tests.test_sheet_logs_260902 import _ready


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
