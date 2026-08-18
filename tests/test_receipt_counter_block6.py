"""Block 6 M-02: receipt-queue position counter must show 1/N for the first card, not 51/N.

The old `total - len(visible) + 1` broke once the pending queue exceeded one 50-row batch:
len(visible) capped at 50, so the first card read e.g. 51/100. Drive the real
_show_current_receipt_card against a seeded >50 queue and assert the position.
"""
import asyncio

from config import config
from database import db
from handlers import admin
from handlers import admin_moderation  # Phase 13 (13-06): moderation moved out of admin.py


class _CapTarget:
    def __init__(self):
        self.texts = []

    async def answer(self, text=None, **k):
        self.texts.append(text)


class _Key:
    def __init__(self, user_id):
        self.user_id = user_id


class _State:
    def __init__(self, data, admin_id=999999):
        self._data = data
        # Phase 07.2 (CITY-02): _show_current_receipt_card reads the admin id from
        # state.key.user_id (target.message's from_user is the bot, not the admin).
        self.key = _Key(admin_id)

    async def get_data(self):
        return dict(self._data)


def test_m02_first_receipt_card_shows_position_one_on_large_queue(tmp_path):
    config.DB_PATH = str(tmp_path / "rcpt_counter.db")

    async def go():
        await db.init_db()
        n = 51  # > one 50-row batch → the exact case the old formula mis-counted
        for i in range(1, n + 1):
            await db.add_user({"telegram_id": i, "full_name": f"U{i}",
                               "registration_date": "2026-07-01 10:00:00"})
            await db.update_payment_status(i, "receipt_sent", receipt_file_id=f"F{i}")

        target = _CapTarget()
        state = _State({"rcpt_skipped": []})
        await admin_moderation._show_current_receipt_card(target, state)
        return target.texts

    texts = asyncio.run(go())
    assert texts, "no card rendered"
    assert "1/51" in texts[0]  # first card is position 1, not 51
    assert "51/51" not in texts[0]
