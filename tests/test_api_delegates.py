"""Очередь заявок на текущем сайте: фильтр по номеру мессенджера, не по нику и не по ленте."""
import asyncio

from config import config
from database import db
from services.applications import applications
from tests._dbtpl import fast_init_db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "delegates_messenger.db")


def _run(coro):
    return asyncio.run(coro)


def _seed(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()

    async def _go():
        await db.add_user({
            "telegram_id": 100,
            "full_name": "Только Telegram",
            "registration_date": "2026-11-12 12:00:01",
            "vk_username": "@nick",
        })
        await db.set_user_status(100, "pending")
        await db.add_user({
            "telegram_id": 102,
            "full_name": "Склеен",
            "registration_date": "2026-11-12 12:00:02",
        })
        await db.set_user_status(102, "pending")
        await db.link_vk_account(102, 12)
        await db.create_vk_user(55, {
            "full_name": "Только ВК",
            "status": "pending",
            "registration_date": "2026-11-12 12:00:03",
        })

    _run(_go())


def _ids(messenger):
    rows = []

    async def _go():
        offset = 0
        while True:
            row, _total = await applications.queue_page(
                scope=None, offset=offset, messenger=messenger,
            )
            if row is None:
                return rows
            rows.append((row.get("telegram_id"), row.get("vk_id")))
            offset += 1

    return _run(_go())


def test_filter_vk(tmp_path):
    _seed(tmp_path)
    assert _ids("vk") == [(102, 12), (None, 55)]


def test_filter_telegram(tmp_path):
    _seed(tmp_path)
    assert _ids("telegram") == [(100, None), (102, 12)]


def test_filter_all(tmp_path):
    _seed(tmp_path)
    assert _ids(None) == [(100, None), (102, 12), (None, 55)]
    assert _ids("all") == _ids(None)
    row = _run(db.get_user(100))
    assert applications.messenger_kind(row) == "telegram"
    linked = _run(db.get_user(102))
    assert applications.messenger_kind(linked) == "both"
    vk_only = _run(db.get_vk_user(55))
    assert applications.messenger_kind(vk_only) == "vk"
    payload = _run(applications.card_payload(vk_only))
    badge = next(item for item in payload["badges"] if item["kind"] == "messenger")
    assert badge["text"] == "ВК"
