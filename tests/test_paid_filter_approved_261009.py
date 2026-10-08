"""Рассылка «оплатившим» уходит только одобренным: возврат на модерацию и отказ оставляют
`payment_status = paid` (факт оплаты нужен для возврата денег), но писать такому делегату
«вы оплатили — вот детали форума» нельзя."""
import asyncio
import sqlite3

from config import config
from database import db
from tests._dbtpl import fast_init_db


def test_paid_filter_takes_only_approved(tmp_path):
    config.DB_PATH = str(tmp_path / "paid_filter.db")
    fast_init_db()
    rows = [
        (1, "approved", "paid"),
        (2, "rejected", "paid"),
        (3, "pending", "paid"),
        (4, "approved", "not_paid"),
    ]
    with sqlite3.connect(config.DB_PATH) as conn:
        for tid, status, pay in rows:
            conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, payment_status) VALUES (?, 'Т', ?, ?)",
                (tid, status, pay),
            )
    ids = asyncio.run(db.count_and_list_filtered([{"field": "payment_status", "value": "paid"}]))
    assert sorted(ids) == [1]
    not_paid = asyncio.run(db.count_and_list_filtered([{"field": "payment_status", "value": "not_paid"}]))
    assert sorted(not_paid) == [4]
