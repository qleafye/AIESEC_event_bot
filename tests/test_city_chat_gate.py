"""Чат города: кто свой, кого показать в сводке, можно ли принять заявку.

pytest-asyncio нет — корутины через asyncio.run. Схема — копия fast_init_db.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import contextmanager

import domain.cities as cities
from config import config
from database import city_chat_db, db
from services import chat_tracking, city_chat_gate
from tests._dbtpl import fast_init_db

SUPER = 12001
CHAT = -10012011
OTHER_CHAT = -10012012
STRAY = -10012099

NO_BOT = 22001
REJECTED = 22002
OTHER = 22003
PENDING = 22004
OURS = 22005


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="city-gate.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [SUPER]
    fast_init_db()


@contextmanager
def _registry():
    saved = list(cities.CITIES)
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": ""},
            {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб"},
        ])
        yield
    finally:
        cities.set_cities_for_test(saved)


async def _user(tid, status, event_city, full_name="Имя", username=None):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, full_name, username, status, event_city),
        )
        await conn.commit()


async def _member(chat_id, tid):
    await db.upsert_chat_member(chat_id, tid, "member", source="gate")


async def _bind_msk():
    await db.set_setting("event_city_enabled", "on")
    await chat_tracking.bind_chat(SUPER, CHAT, "Делегаты", "msk")


def test_gate_does_not_import_aiogram():
    env = os.environ.copy()
    env.setdefault("BOT_TOKEN", "123456:dummy-test-token")
    env.setdefault("ADMIN_IDS", "[1]")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import services.city_chat_gate, sys; assert 'aiogram' not in sys.modules"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_reasons_order(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _member(CHAT, NO_BOT)
            await _user(REJECTED, "rejected", "spb", full_name="Отказ")
            await _member(CHAT, REJECTED)
            await _user(OTHER, "approved", "spb", full_name="Чужой")
            await _member(CHAT, OTHER)
            await _user(PENDING, "pending", "msk", full_name="Ждёт")
            await _member(CHAT, PENDING)
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(CHAT, OURS)
            rows = await city_chat_gate.outsider_rows(CHAT)
        reasons = [row["reason"] for row in rows]
        assert reasons == [
            city_chat_gate.REASON_NO_BOT,
            city_chat_gate.REASON_REJECTED,
            city_chat_gate.REASON_OTHER_CITY,
            city_chat_gate.REASON_NOT_APPROVED,
        ]
        assert [row["telegram_id"] for row in rows] == [NO_BOT, REJECTED, OTHER, PENDING]
        assert OURS not in [row["telegram_id"] for row in rows]

    _run(go())


def test_module_off_approved_is_ours(tmp_path):
    _ready(tmp_path)

    async def go():
        await chat_tracking.bind_chat(SUPER, CHAT, "Делегаты", None)
        bound = await chat_tracking.bound_chats()
        assert bound == [{"city": None, "chat_id": CHAT, "title": "Делегаты"}]
        await _user(OURS, "approved", "spb", full_name="Свой")
        await _member(CHAT, OURS)
        await _user(PENDING, "pending", "tyumen", full_name="Ждёт")
        await _member(CHAT, PENDING)
        rows = await city_chat_gate.outsider_rows(CHAT)
        assert [row["telegram_id"] for row in rows] == [PENDING]
        assert rows[0]["reason"] == city_chat_gate.REASON_NOT_APPROVED
        assert city_chat_gate.REASON_OTHER_CITY not in [row["reason"] for row in rows]

    _run(go())


def test_allow_hides_next_summary(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(REJECTED, "rejected", "spb", full_name="Отказ")
            await _member(CHAT, REJECTED)
            assert any(row["telegram_id"] == REJECTED for row in await city_chat_gate.outsider_rows(CHAT))
            await city_chat_db.allow(CHAT, REJECTED, added_by=SUPER)
            assert await city_chat_db.is_allowed(CHAT, REJECTED) is True
            assert all(row["telegram_id"] != REJECTED for row in await city_chat_gate.outsider_rows(CHAT))

    _run(go())


def test_may_approve_only_ours(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk")
            await _user(OTHER, "approved", "spb")
            await _user(PENDING, "pending", "msk")
            await _user(REJECTED, "rejected", "msk")
            assert await city_chat_gate.may_approve(CHAT, OURS) is True
            assert await city_chat_gate.may_approve(CHAT, OTHER) is False
            assert await city_chat_gate.may_approve(CHAT, PENDING) is False
            assert await city_chat_gate.may_approve(CHAT, REJECTED) is False
            assert await city_chat_gate.may_approve(CHAT, NO_BOT) is False
            assert await city_chat_gate.may_approve(STRAY, OURS) is False

    _run(go())


def test_names_hide_empty_and_skip_contacts(tmp_path):
    _ready(tmp_path)
    source = open("services/city_chat_gate.py", encoding="utf-8").read()
    assert "phone" not in source
    assert "email" not in source

    async def go():
        with _registry():
            await _bind_msk()
            await _user(NO_BOT, "pending", "msk", full_name="   ", username="@Nick")
            await db.upsert_chat_username(NO_BOT, "other", "  ")
            await _member(CHAT, NO_BOT)
            rows = await city_chat_gate.outsider_rows(CHAT)
        assert rows[0]["name"] == "без имени и ника"
        assert rows[0]["username"] == "Nick"

    _run(go())


def test_unbound_chat_is_not_global(tmp_path):
    _ready(tmp_path)

    async def go():
        with _registry():
            await _bind_msk()
            await _user(OURS, "approved", "msk", full_name="Свой")
            await _member(OTHER_CHAT, OURS)
            await _member(STRAY, PENDING)
            assert await city_chat_gate.outsider_rows(STRAY) == []
            assert await city_chat_gate.outsider_rows(OTHER_CHAT) == []
            assert await city_chat_gate.may_approve(STRAY, OURS) is False

    _run(go())
