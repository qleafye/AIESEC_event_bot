"""Сопоставление анкет внешних форм с делегатами и единая точка приёма."""
import asyncio

import aiosqlite
import pytest

from config import config
from database import db
from database import ext_forms_db as ef
from services import ext_forms_match as m
from services.ext_forms_ingest import ingest_answer
from tests._dbtpl import fast_init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "ext_match.db")
    fast_init_db()


def _user(tid, username=None, phone=None):
    async def go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, phone, full_name) VALUES (?, ?, ?, ?)",
                (tid, username, phone, f"U{tid}"),
            )
            await conn.commit()
    asyncio.run(go())


def _form(**kw):
    fid = asyncio.run(ef.create_form(platform="yandex", external_id="f1", title="Ф", **kw))
    return asyncio.run(ef.get_form(fid))


def test_normalize_phone():
    for raw in ("+7 (999) 123-45-67", "89991234567", "79991234567"):
        assert m.normalize_phone(raw) == "9991234567"
    assert m.normalize_phone("12345") is None
    assert m.normalize_phone(None) is None


def test_username_from_value():
    assert m.username_from_value("@Ivan_Petrov") == "Ivan_Petrov"
    assert m.username_from_value("https://t.me/ivan_petrov") == "ivan_petrov"
    assert m.username_from_value("-") is None
    assert m.username_from_value("@") is None
    assert m.username_from_value("Иван Петров") is None


def test_guess_key_questions():
    assert m.guess_key_questions([("1", "ФИО"), ("2", "Ник в тг"), ("3", "Телефон")]) == ("2", "3")
    assert m.guess_key_questions([("a", "TELEGRAM username")]) == ("a", None)
    assert m.guess_key_questions([("a", "Город"), ("b", "Номер группы")]) == (None, None)


def test_match_by_username_case_insensitive(tmp_path):
    _ready(tmp_path)
    _user(10, "@ivan_petrov")
    form = _form(key_username_q="2")
    items = [{"q": "2", "label": "Ник", "value": "Ivan_Petrov"}]
    assert asyncio.run(m.match_answer(form, items)) == (10, "username")


def test_match_by_phone_and_tie(tmp_path):
    _ready(tmp_path)
    _user(10, None, "+7 999 123 45 67")
    form = _form(key_phone_q="3")
    items = [{"q": "3", "label": "Телефон", "value": "89991234567"}]
    assert asyncio.run(m.match_answer(form, items)) == (10, "phone")
    _user(11, None, "8 (999) 123-45-67")
    assert asyncio.run(m.match_answer(form, items)) == (None, None)


def test_reg_started_only_not_matched(tmp_path):
    _ready(tmp_path)

    async def go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO reg_started (telegram_id, username, started_at) VALUES (?, ?, ?)", (5, "@only_started", "2026-10-01 10:00:00")
            )
            await conn.commit()
    asyncio.run(go())
    form = _form(key_username_q="2")
    items = [{"q": "2", "label": "Ник", "value": "@only_started"}]
    assert asyncio.run(m.match_answer(form, items)) == (None, None)


def test_form_without_keys(tmp_path):
    _ready(tmp_path)
    _user(10, "@ivan_petrov")
    form = _form()
    assert asyncio.run(m.match_answer(form, [{"q": "2", "label": "Ник", "value": "@ivan_petrov"}])) == (None, None)


def _ingest(form, aid, items):
    return asyncio.run(ingest_answer(form, answer_id=aid, answered_at="2026-10-01 10:00:00",
                                     items=items, raw=None))


def test_ingest_dup_and_columns(tmp_path):
    _ready(tmp_path)
    form = _form()
    a = [{"q": "q1", "label": "Имя", "value": "Аня"}, {"q": "q2", "label": "Город", "value": "М"}]
    assert _ingest(form, "a1", a) is True
    assert _ingest(form, "a1", a) is False
    assert asyncio.run(ef.count_answers(form["id"])) == 1
    _ingest(form, "a2", a + [{"q": "q9", "label": "Новый", "value": "x"}])
    cols = {c["qkey"]: c["position"] for c in asyncio.run(ef.list_columns(form["id"]))}
    assert cols == {"q1": 1, "q2": 2, "q9": 3}


def test_late_rematch_marks_update(tmp_path):
    _ready(tmp_path)
    form = _form(key_username_q="q2")
    items = [{"q": "q2", "label": "Ник", "value": "@late_user"}]
    assert _ingest(form, "a1", items) is True
    rows = asyncio.run(ef.list_unmatched_answers(10))
    assert len(rows) == 1 and rows[0]["matched_telegram_id"] is None
    asyncio.run(ef.mark_sheet_state([rows[0]["id"]], "synced"))
    assert asyncio.run(m.rematch_unmatched()) == 0
    _user(77, "@late_user")
    assert asyncio.run(m.rematch_unmatched()) == 1
    assert asyncio.run(m.rematch_unmatched()) == 0

    async def state():
        async with db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT matched_telegram_id, sheet_state FROM external_form_answers"
            ) as c:
                return dict(await c.fetchone())
    assert asyncio.run(state()) == {"matched_telegram_id": 77, "sheet_state": "update"}


def test_rematch_skips_form_without_keys(tmp_path):
    _ready(tmp_path)
    form = _form()
    _ingest(form, "a1", [{"q": "q2", "label": "Ник", "value": "@someone"}])
    _user(5, "@someone")
    assert asyncio.run(m.rematch_unmatched()) == 0
