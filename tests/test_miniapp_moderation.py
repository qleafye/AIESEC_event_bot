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


def test_vk_decision_letter_follows_city_and_party(tmp_path, monkeypatch):
    """Карточка только из ВК получает письмо своего города. Вечеринка берёт общий текст вечеринки."""
    config.DB_PATH = str(tmp_path / "vk_city_letter.db")
    fast_init_db()
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text))
        return True

    monkeypatch.setattr("services.vk.vk_api_client.send_vk_message", _send)

    async def _go():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("approve_text", "30-31 октября в Москве")
        await db.set_setting("approve_text__city__spb", "Форум в Санкт-Петербурге")
        await db.set_setting("reject_text", "Общий отказ")
        await db.set_setting("reject_text__city__spb", "Отказ Петербурга")
        await db.set_setting("approve_text__party", "Вечеринка")
        await db.set_setting("waitlist_delegate_text", "Общий лист")
        await db.create_vk_user(55, {
            "full_name": "Только ВК",
            "status": "approved",
            "event_city": "spb",
            "participant_type": "full",
            "registration_date": "2026-11-27 12:00:00",
        })
        await db.create_vk_user(77, {
            "full_name": "Вечеринка ВК",
            "status": "approved",
            "event_city": "spb",
            "participant_type": "party_overnight",
            "registration_date": "2026-11-27 12:00:01",
        })
        await applications.notify_vk_decision(55, "approved", None)
        await applications.notify_vk_decision(55, "rejected", "нет места")
        await applications.notify_vk_decision(55, "waitlist", None)
        await applications.notify_vk_decision(77, "approved", None)

    asyncio.run(_go())
    assert sent == [
        (55, "Форум в Санкт-Петербурге"),
        (55, "Отказ Петербурга\n\nнет места"),
        (55, "Общий лист"),
        (77, "Вечеринка"),
    ]
    assert "30-31 октября в Москве" not in sent[0][1]
