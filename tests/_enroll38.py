"""Общая фикстура тестов записи на сессии и теста компетенций: Москва с двумя треками,
параллельными сессиями, пленаркой и делегатами разных статусов/сезонов.

pytest-asyncio нет — корутины гоняются через `asyncio.run` (`run`)."""
from __future__ import annotations

import asyncio

from config import config
from database import db, session_enroll_db
from tests._dbtpl import fast_init_db

ADMIN_ID = 1
CITY = "msk"
DAY = "2026-10-30"
SEASON = "YL 26/2"
PAST_SEASON = "YL 26/1"


def run(coro):
    return asyncio.run(coro)


def ready(tmp_path, name="enroll38.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


async def seed_msk_program() -> dict:
    """Сессии: A 10:00-11:00 Карьера, B 10:00-11:00 Бизнес, C 10:30-12:00 Карьера (пересекается
    с A и B), D 11:30-12:30 Бизнес (с A не пересекается, с C пересекается), P 09:00-10:00 без
    трека (пленарка)."""
    hall1 = await db.create_program_hall(CITY, "Зал 1")
    hall2 = await db.create_program_hall(CITY, "Зал 2")
    career = await session_enroll_db.create_track(CITY, "Карьера")
    business = await session_enroll_db.create_track(CITY, "Бизнес")

    async def sess(title, start, end, hall, track):
        sid = await db.create_program_session(CITY, DAY, start, end, title, hall_id=hall)
        if track:
            await db.update_program_session(sid, track_id=track)
        return sid

    return {
        "hall1": hall1, "hall2": hall2, "career": career, "business": business,
        "A": await sess("Сессия A", "10:00", "11:00", hall1, career),
        "B": await sess("Сессия B", "10:00", "11:00", hall2, business),
        "C": await sess("Сессия C", "10:30", "12:00", hall1, career),
        "D": await sess("Сессия D", "11:30", "12:30", hall2, business),
        "P": await sess("Пленарка", "09:00", "10:00", hall1, None),
    }


async def add_user(tid, *, status="approved", city=CITY, season=SEASON):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city, season) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, f"Делегат {tid}", f"user{tid}", status, city, season),
        )
        await conn.commit()


async def seed_delegates(event_season=SEASON) -> dict:
    """Ставит event_season и заводит делегатов: cur1/cur2 — approved текущего сезона, empty —
    approved с пустым сезоном, pending — не одобрен, past — approved прошлого сезона."""
    await db.set_setting("event_season", event_season)
    ids = {"cur1": 101, "cur2": 102, "empty": 103, "pending": 104, "past": 105}
    await add_user(ids["cur1"])
    await add_user(ids["cur2"])
    await add_user(ids["empty"], season=None)
    await add_user(ids["pending"], status="pending")
    await add_user(ids["past"], season=PAST_SEASON)
    return ids
