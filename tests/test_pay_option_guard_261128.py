"""Старая кнопка тарифа не возвращает оплатившего в «не оплачено».

У шага оплаты нет состояния FSM: фильтр на него вешать нельзя, иначе нажатие пропадёт.
"""
import asyncio

import database.db as db
import handlers.payment as pay
from tests._dbtpl import fast_init_db

UID = 261128


def _ready(tmp_path):
    db.config.DB_PATH = str(tmp_path / "pay-option-guard.db")
    fast_init_db()


class _Callback:
    def __init__(self, data="pay_option:0"):
        self.data = data
        self.bot = object()
        self.answers = []

        class _User:
            id = UID

        self.from_user = _User()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _State:
    pass


async def _seed(status, option="Старый тариф"):
    await db.add_user({
        "telegram_id": UID,
        "full_name": "Делегат",
        "registration_date": "2026-11-28",
        "participant_type": "full",
        "payment_status": status,
        "payment_option": option,
    })


def _run(tmp_path, monkeypatch, status):
    _ready(tmp_path)
    asyncio.run(db.set_setting("payment_options", "Новый тариф|5000"))
    asyncio.run(_seed(status))
    details = {}

    async def fake_show(*args, **kwargs):
        details["hit"] = True

    monkeypatch.setattr(pay, "_show_payment_details", fake_show)
    cb = _Callback()
    asyncio.run(pay.process_payment_option(cb, _State()))
    user = asyncio.run(db.get_user(UID))
    return cb.answers, user, details


def test_paid_tariff_button_keeps_paid_and_does_not_ask_for_a_receipt(tmp_path, monkeypatch):
    answers, user, details = _run(tmp_path, monkeypatch, "paid")
    assert user["payment_status"] == "paid"
    assert user["payment_option"] == "Старый тариф"
    assert details == {}
    assert answers == [("Оплата уже подтверждена.", True)]


def test_receipt_on_review_tariff_button_keeps_the_receipt(tmp_path, monkeypatch):
    answers, user, details = _run(tmp_path, monkeypatch, "receipt_sent")
    assert user["payment_status"] == "receipt_sent"
    assert user["payment_option"] == "Старый тариф"
    assert details == {}
    assert answers == [("Чек уже на проверке.", True)]


def test_unpaid_tariff_button_still_selects_the_option(tmp_path, monkeypatch):
    answers, user, details = _run(tmp_path, monkeypatch, "not_paid")
    assert user["payment_status"] == "not_paid"
    assert user["payment_option"] == "Новый тариф"
    assert details == {"hit": True}
    assert answers == [(None, False)]
