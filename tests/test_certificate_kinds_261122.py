"""Свои виды сертификата: кому положен, рядом с четырьмя старыми, без кнопок админки."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import timedelta

from config import config
from database import certificates_db as cdb
from database import db
from services.forum import certificates as certs
from services.infra.timeutil import msk_now
from tests.test_certificate_autosend_261104 import _DocBot, _arm
from tests.test_certificates_261028 import _checkin

UID = 261122001
DAY = "2026-10-30"


def _run(coro):
    return asyncio.run(coro)


def _use_db(tmp_path):
    config.DB_PATH = str(tmp_path / "kinds.db")
    _run(db.init_db())
    _run(db.add_user({
        "telegram_id": UID, "full_name": "Иванова Анна", "username": "speaker",
        "registration_date": "2026-01-01", "event_city": "msk",
    }))


def _pairs(uid: int = UID, username: str | None = "speaker"):
    return _run(certs.entitlements(uid, username))


def test_list_kinds_without_table_is_empty(tmp_path):
    config.DB_PATH = str(tmp_path / "bare.db")
    sqlite3.connect(config.DB_PATH).close()
    assert _run(cdb.list_kinds()) == []


def test_delete_refuses_four_old_codes(tmp_path):
    _use_db(tmp_path)
    for code in cdb.KINDS:
        assert _run(cdb.delete_kind(code)) is False
    row = _run(cdb.insert_kind("Спикер", "checkin"))
    assert _run(cdb.delete_kind(row["code"])) is True
    assert _run(cdb.get_kind(row["code"])) is None


def test_checkin_kind_sits_beside_participant(tmp_path):
    _use_db(tmp_path)
    _checkin(UID, DAY)
    row = _run(cdb.insert_kind("Спикер", "checkin", by_day=True))
    got = _pairs()
    assert ("participant", DAY) in got
    assert (row["code"], DAY) in got
    assert got.index(("participant", DAY)) < got.index((row["code"], DAY))


def test_checkin_kind_without_day_flag_is_all(tmp_path):
    _use_db(tmp_path)
    _checkin(UID, DAY)
    row = _run(cdb.insert_kind("Гость", "checkin", by_day=False))
    got = _pairs()
    assert ("participant", DAY) in got
    assert (row["code"], "all") in got


def test_test_kind_needs_finished_test_and_keeps_participant(tmp_path, monkeypatch):
    _use_db(tmp_path)
    _checkin(UID, DAY)
    row = _run(cdb.insert_kind("Тест", "test"))

    async def _none(_uid):
        return None

    monkeypatch.setattr(certs, "competency_percents", _none)
    got = _pairs()
    assert ("participant", DAY) in got
    assert all(code != row["code"] for code, _variant in got)

    async def _done(_uid):
        return [80, 50, 100, 60, 90]

    monkeypatch.setattr(certs, "competency_percents", _done)
    got = _pairs()
    assert ("participant", DAY) in got
    assert (row["code"], "all") in got


def test_nicks_kind_matches_only_listed_nick(tmp_path):
    _use_db(tmp_path)
    other = UID + 1
    _run(db.add_user({
        "telegram_id": other, "full_name": "Петров Пётр", "username": "other",
        "registration_date": "2026-01-01", "event_city": "msk",
    }))
    row = _run(cdb.insert_kind("Ведущий", "nicks"))
    _run(cdb.add_people(row["code"], ["speaker"]))
    assert (row["code"], "all") in _pairs(UID, "speaker")
    assert all(code != row["code"] for code, _variant in _pairs(other, "other"))


def test_role_kind_respects_registry_and_expiry(tmp_path):
    _use_db(tmp_path)
    assert _run(cdb.insert_kind("Чужой", "role", role_key="not-a-role")) is None
    assert _run(cdb.list_kinds()) == []
    row = _run(cdb.insert_kind("Волонтёрский", "role", role_key="volunteer"))
    assert row is not None
    _run(db.add_staff(UID, "volunteer", 1))
    assert (row["code"], "all") in _pairs()
    yesterday = (msk_now().date() - timedelta(days=1)).isoformat()
    _run(db.set_staff_expiry(UID, "volunteer", yesterday))
    assert all(code != row["code"] for code, _variant in _pairs())


def test_recipients_include_role_only_person(tmp_path):
    _use_db(tmp_path)
    _run(cdb.insert_kind("Стойка", "role", role_key="volunteer"))
    _run(db.add_staff(UID, "volunteer", 1))
    assert UID in _run(certs.recipients())


def test_entitlements_text_has_no_gender_phrase():
    src = inspect.getsource(certs.entitlements) + inspect.getsource(certs.kind_row_variants)
    assert "прошёл" not in src
    assert "прошла" not in src


def test_autosend_reaches_nicks_kind_and_skips_role(tmp_path, monkeypatch):
    bot = _DocBot()
    _arm(tmp_path, monkeypatch, bot, username="speaker", full_name="Иванова Анна")
    role = _run(cdb.insert_kind("Стойка", "role", role_key="volunteer"))
    nicks = _run(cdb.insert_kind("Ведущий", "nicks"))
    _run(db.add_staff(UID, "volunteer", 1))
    _run(cdb.add_people(nicks["code"], ["speaker"]))
    _run(cdb.set_template(nicks["code"], "all", "FILE", 1))
    _run(cdb.set_template(role["code"], "all", "FILE", 1))
    sent = _run(certs.deliver_role_pdfs(bot))
    assert sent == 1
    assert len(bot.docs) == 1
    assert "Ведущий" in bot.docs[0][1]
    assert "Стойка" not in bot.docs[0][1]


def test_four_old_variants_stay():
    variants = ["all", "2026-10-30"]
    assert certs.kind_variants("participant", variants) == variants
    assert certs.kind_variants("competency", variants) == ["all"]
    assert certs.kind_variants("volunteer", variants) == ["all"]
    assert certs.kind_variants("ambassador", variants) == ["all"]
    assert certs.kind_row_variants({"by_day": 0}, variants) == ["all"]
    assert certs.kind_row_variants({"by_day": 1}, variants) == variants
