"""Две анкеты с одним именем и телефоном: менеджер оставляет одну, вторая уходит из очереди."""
import asyncio
from datetime import datetime
from pathlib import Path

from config import config
from database import db
from services.applications import applications
from tests._dbtpl import fast_init_db

_JS = Path(__file__).resolve().parents[1] / "miniapp" / "static" / "js" / "screens" / "applications.js"
_NOW = datetime(2026, 11, 12, 12, 0, 0)


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "duplicates.db")


def _run(coro):
    return asyncio.run(coro)


async def _person(tid, name, phone, status="pending"):
    await db.add_user({
        "telegram_id": tid,
        "full_name": name,
        "phone": phone,
        "registration_date": "2026-11-12 12:00:00",
    })
    await db.set_user_status(tid, status)


def test_detect_duplicates(tmp_path):
    _use_tmp_db(tmp_path)
    fast_init_db()

    async def _go():
        await _person(1, "Анна Иванова", "+7999")
        await _person(2, "Анна Иванова", "+7999")
        await _person(3, "Анна Иванова", "+7000")
        await _person(4, "Анна Иванова", "")
        await _person(5, "Анна Иванова", "")
        first = await db.get_user(1)
        other_phone = await db.get_user(3)
        blank = await db.get_user(4)
        return (
            await applications.duplicate_hint(first, city=None),
            await applications.duplicate_hint(other_phone, city=None),
            await applications.duplicate_hint(blank, city=None),
        )

    same, other, blank = _run(_go())
    assert same["warning"] == "Возможный дубль"
    assert same["keep_button"] == "Оставить эту"
    assert "1" in same["confirm"]
    assert "отклонены" in same["confirm"]
    assert other is None
    assert blank is None
    src = _JS.read_text(encoding="utf-8")
    assert "/applications/keep" in src
    assert "dup.keep_button" in src


def test_delete_duplicate(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    fast_init_db()
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text))
        return True

    monkeypatch.setattr("services.vk.vk_api_client.send_vk_message", _send)

    async def _go():
        await _person(1, "Анна Иванова", "+7999")
        await _person(2, "Анна Иванова", "+7999")
        await db.create_vk_user(55, {
            "full_name": "Анна Иванова",
            "phone": "+7999",
            "status": "pending",
            "registration_date": "2026-11-12 12:00:02",
        })
        keeper = await db.get_user(1)
        result = await applications.reject_other_applications(
            keeper, city=None, by=7, now=_NOW,
        )
        again = await applications.reject_other_applications(
            keeper, city=None, by=7, now=_NOW,
        )
        kept = await db.get_user(1)
        other = await db.get_user(2)
        vk = await db.get_vk_user(55)
        decision = await db.get_last_application_decision(2)
        return result, again, kept["status"], other["status"], vk["status"], decision

    result, again, kept, other, vk_status, decision = _run(_go())
    assert result["decision_ids"]
    assert result["vk_rejected"] == 1
    assert again == {"decision_ids": [], "vk_rejected": 0}
    assert kept == "pending"
    assert other == "rejected"
    assert vk_status == "rejected"
    assert decision["reason"] == "Оставили другую анкету"
    assert sent and "Оставили другую анкету" in sent[0][1]
    assert sent[0][0] == 55
