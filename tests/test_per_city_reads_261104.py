"""Городское переопределение доходит до делегата; пустое и выключенный флаг оставляют общее.

Флаг per_city ставится на словаре SETTINGS_SCHEMA внутри теста. Файл реестра не меняется.
"""
import asyncio
from contextlib import contextmanager

from config import config
from database import db
import domain.cities as cities
import domain.regform.engine as reg_engine
import domain.settings.schema as schema
from handlers import payment
from tests._dbtpl import fast_init_db

_UID = 430104


def _ready(tmp_path, name: str) -> None:
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


@contextmanager
def _per_city(key: str):
    row = schema.SETTINGS_SCHEMA[key]
    had = "per_city" in row
    old = row.get("per_city")
    row["per_city"] = True
    try:
        yield
    finally:
        if had:
            row["per_city"] = old
        else:
            row.pop("per_city", None)


def test_payment_requisites_follow_delegate_city(tmp_path):
    _ready(tmp_path, "reads_pay.db")
    with _per_city("payment_requisites"):
        asyncio.run(_payment_requisites())


async def _payment_requisites():
    await db.set_setting("event_city_enabled", "on")
    await db.set_setting("payment_requisites", "ОБЩИЕ РЕКВИЗИТЫ")
    override = cities.per_city_key("payment_requisites", "spb")
    assert override
    await db.set_setting(override, "ГОРОДСКИЕ РЕКВИЗИТЫ")
    await db.add_user({
        "telegram_id": _UID,
        "full_name": "Делегат",
        "registration_date": "2026-11-04 00:00:00",
        "event_city": "spb",
    })
    assert await payment._resolve_requisites(_UID) == "ГОРОДСКИЕ РЕКВИЗИТЫ"
    await db.set_setting(override, "")
    assert await payment._resolve_requisites(_UID) == "ОБЩИЕ РЕКВИЗИТЫ"
    schema.SETTINGS_SCHEMA["payment_requisites"]["per_city"] = False
    await db.set_setting(override, "ГОРОДСКИЕ РЕКВИЗИТЫ")
    assert await payment._resolve_requisites(_UID) == "ОБЩИЕ РЕКВИЗИТЫ"


def test_consent_list_follows_city(tmp_path):
    _ready(tmp_path, "reads_consent.db")
    with _per_city("consent_list"):
        asyncio.run(_consent_list())


async def _consent_list():
    await db.set_setting("event_city_enabled", "on")
    await db.set_setting("consent_list", "Общее согласие | common_doc")
    override = cities.per_city_key("consent_list", "spb")
    assert override
    await db.set_setting(override, "Городское согласие | city_doc")
    assert [key for _label, key in await reg_engine.consent_entries("spb")] == ["city_doc"]
    await db.set_setting(override, "")
    assert [key for _label, key in await reg_engine.consent_entries("spb")] == ["common_doc"]
    schema.SETTINGS_SCHEMA["consent_list"]["per_city"] = False
    await db.set_setting(override, "Городское согласие | city_doc")
    assert [key for _label, key in await reg_engine.consent_entries("spb")] == ["common_doc"]
