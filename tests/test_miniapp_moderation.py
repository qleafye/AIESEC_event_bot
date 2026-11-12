"""Чипы мессенджера на экране заявок и решение по карточке только из ВК."""
import asyncio
from pathlib import Path

from config import config
from database import db
from services.applications import applications
from tests._dbtpl import fast_init_db

_JS = Path(__file__).resolve().parents[1] / "miniapp" / "static" / "js" / "screens" / "applications.js"


def _src() -> str:
    return _JS.read_text(encoding="utf-8")


def test_vk_filter_button():
    src = _src()
    assert '["messenger_vk", "vk"]' in src
    assert '["messenger_telegram", "telegram"]' in src
    assert '["messenger_all", "all"]' in src


def test_vk_filter_fetch():
    """Клик «ВК» кладёт messenger=vk в запрос очереди заявок, не в отдельный сайт."""
    src = _src()
    assert 'query.set("messenger", messenger)' in src
    assert "/applications/next?" in src
    assert "/api/delegates" not in src
    assert "/applications/vk/" in src


def test_vk_only_decision_does_not_touch_linked(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "vk_decide.db")
    fast_init_db()
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text))
        return True

    monkeypatch.setattr("services.vk.vk_api_client.send_vk_message", _send)

    async def _go():
        await db.add_user({
            "telegram_id": 102,
            "full_name": "Склеен",
            "registration_date": "2026-11-12 12:00:00",
        })
        await db.set_user_status(102, "pending")
        await db.link_vk_account(102, 12)
        await db.set_setting("approve_text", "<b>Привет</b>")
        await db.create_vk_user(55, {
            "full_name": "Только ВК",
            "status": "pending",
            "registration_date": "2026-11-12 12:00:01",
        })
        linked = await applications.claim_vk_decision(12, "approved", None)
        only = await applications.claim_vk_decision(55, "approved", None)
        row = await db.get_user(102)
        vk = await db.get_vk_user(55)
        return linked, only, row["status"], vk["status"]

    linked, only, linked_status, vk_status = asyncio.run(_go())
    assert linked is False
    assert linked_status == "pending"
    assert only is True
    assert vk_status == "approved"
    assert sent == [(55, "Привет")]
