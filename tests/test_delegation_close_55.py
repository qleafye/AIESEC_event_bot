"""Дата закрытия делегаций раньше личного приёма.

Личная анкета живёт по своей дате. После дня делегаций экран «Делегации» говорит,
что приём закрыт, и новый ответ формы никого не превращает в делегата.
"""
import asyncio
from datetime import datetime

import domain.cities as cities
from config import config
from database import db
from domain.settings.ops import cross_setting_error, validate_batch_item
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.delegations import admin_delegations as dlg_admin
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db
from tests.test_delegations_admin import _FakeCallback
from tests.test_delegations_core import (
    _answer_from_fixture, _available, _delegation_form, _env, _reg_started, _row,
)


def _run(coro):
    return asyncio.run(coro)


def _db(tmp_path):
    config.DB_PATH = str(tmp_path / "delegation_close.db")
    fast_init_db()


def _save(key, value, *, cities_on=False, visible=None, selected=None):
    return _run(validate_batch_item(
        key, value, visible_codes=visible or [], selected_city=selected, cities_on=cities_on,
    ))


def test_delegation_close_earlier_day_is_accepted(tmp_path):
    _db(tmp_path)
    _run(db.set_setting("city_reg_close_date", "01.10.2026"))
    assert _run(cross_setting_error("delegation_close_date", "20.09.2026")) is None
    check = _save("delegation_close_date", "20.09.2026")
    assert check.error is None
    assert check.value == "20.09.2026"


def test_delegation_close_same_day_or_later_is_not_saved(tmp_path):
    _db(tmp_path)
    _run(db.set_setting("city_reg_close_date", "01.10.2026"))
    for raw in ("01.10.2026", "02.10.2026"):
        err = _run(cross_setting_error("delegation_close_date", raw))
        assert err
        assert "ранн" in err
        assert "2026-10-20" in err
        check = _save("delegation_close_date", raw)
        assert check.error
        assert "2026-10-20" in check.error
        assert _run(db.get_setting("delegation_close_date")) in (None, "")
    assert _run(cross_setting_error("delegation_close_date", "-")) is None
    _run(db.delete_setting("city_reg_close_date"))
    assert _run(cross_setting_error("delegation_close_date", "01.10.2026")) is None


def test_delegation_close_compares_with_that_city(tmp_path):
    _db(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    code = cities.default_city_code()
    others = [c for c in cities.city_codes() if c != code]
    assert others, "нужен второй город, чтобы дата одного не закрывала другой"
    _run(db.set_setting(cities.per_city_key("city_reg_close_date", code), "01.10.2026"))
    late = cities.per_city_key("delegation_close_date", code)
    err = _run(cross_setting_error(late, "01.10.2026"))
    assert err and "2026-10-20" in err and "ранн" in err
    other = cities.per_city_key("delegation_close_date", others[0])
    assert _run(cross_setting_error(other, "01.10.2026")) is None


def test_delegation_close_screen_says_closed_and_personal_stays_open(tmp_path, monkeypatch):
    _env(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 15, 12, 0))
    code = cities.default_city_code()
    _run(set_setting_by_admin(None, "city_reg_close_date", "01.11.2026"))
    _run(set_setting_by_admin(None, "delegation_close_date", "01.10.2026"))
    assert _run(cities.is_city_registration_open(code)) is True
    assert _run(cities.is_delegation_intake_open(code)) is False
    fid = _delegation_form()
    cb = _FakeCallback("admin_delegations")
    _run(dlg_admin.render_screen(cb))
    text = cb.message.edits[-1][0]
    assert cities.DELEGATION_INTAKE_CLOSED in text
    assert "delegation_close_date" not in text
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(502)
    res = _available(fid, "a1")
    assert res.get("closed") == "delegations"
    user = _row(502)
    assert user is None or user.get("status") != "approved"


def test_delegation_close_label_hides_the_key_code():
    entry = SETTINGS_SCHEMA["delegation_close_date"]
    assert entry["per_city"] is True and entry["type"] == "date_only"
    assert "delegation" not in entry["label"] and "delegation" not in entry["prompt"]
    assert "_" not in entry["label"]
