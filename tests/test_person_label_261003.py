"""Подпись человека в «👥 Роли и доступы»: у делегата без username в анкете лежит «-»."""
import asyncio
from datetime import datetime

from config import config
from database import db
from services.person_label import person_label
from tests._dbtpl import fast_init_db


def _add(uid: int, username: str):
    asyncio.run(db.add_user({
        "telegram_id": uid, "full_name": "Иван Петров", "username": username,
        "university": "ВШЭ", "phone": "+79990000000",
        "registration_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "approved",
    }))


def test_dash_username_is_treated_as_missing(tmp_path):
    config.DB_PATH = str(tmp_path / "pl.db")
    fast_init_db()
    _add(9101, "-")
    assert asyncio.run(person_label(9101)) == "Иван Петров"


def test_real_username_is_kept(tmp_path):
    config.DB_PATH = str(tmp_path / "pl.db")
    fast_init_db()
    _add(9102, "ivan")
    assert asyncio.run(person_label(9102)) == "Иван Петров (@ivan)"
