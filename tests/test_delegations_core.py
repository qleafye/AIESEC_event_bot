"""Делегации вузов — ядро `services/delegations.py`: вопросы формы, поля ответа, поиск человека
по @нику, вердикт ЦА, решение о привязке, превращение в одобренного делегата Москвы без
анкеты, поздний вход через /start, хуки фазы внешних форм.

Фикстура `tests/fixtures/ext_forms/synthetic_delegation_answer.json` — JSON-список из девяти
элементов {q, label, value} в порядке колонок выгрузки Яндекса (ФИО, Возраст, Почта, Вуз,
Направление, Курс, Ник TG, Ник VK, Согласие). Все имена, почты и вузы вымышленные.

pytest-asyncio в проекте нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities as cities_mod
from config import config
from database import db
from database import delegations_db as ddb
from database import ext_forms_db as ef
from services import delegations as dlg
from settings_audit import set_setting_by_admin
from tests._dbtpl import fast_init_db

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ext_forms",
                   "synthetic_delegation_answer.json")
SEASON = "YL 26/2"
USERNAME = "IvanTest"
NEEDLE = "IvanTest"  # username_needle не меняет регистр, все поиски — COLLATE NOCASE
UNIVERSITY = "Тестовый университет"
MANAGER = 7


def _run(coro):
    return asyncio.run(coro)


def _fixture_items() -> list[dict]:
    with open(FIX, encoding="utf-8") as f:
        return json.load(f)


class FakeBot:
    id = 1

    def __init__(self):
        self.sent = []  # list[(chat_id, text, reply_markup)]

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return None


def _env(tmp_path, name="dlg.db"):
    """Чистая база + сезон + бот/хранилище FSM в модуле делегаций."""
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(set_setting_by_admin(None, "event_season", SEASON))
    bot = FakeBot()
    storage = MemoryStorage()
    dlg.init(bot, storage)
    return bot, storage


def _delegation_form(*, select: bool = True, username_q: str = "q7") -> int:
    """Подключённая форма делегаций с ключами вопросов из фикстуры."""
    async def go():
        fid = await ef.create_form(platform="yandex", external_id="dlg-form", title="Делегации",
                                   key_username_q=username_q)
        if select:
            await set_setting_by_admin(None, "delegation_form_id", str(fid))
        await set_setting_by_admin(None, "delegation_q_fullname", "q1")
        await set_setting_by_admin(None, "delegation_q_university", "q4")
        await set_setting_by_admin(None, "delegation_q_course", "q6")
        await set_setting_by_admin(None, "delegation_q_email", "q3")
        return fid
    return _run(go())


def _answer_from_fixture(fid: int, aid: str = "a1", *, username: str | None = "@" + USERNAME,
                         course: str | None = "1 бакалавриат",
                         answered_at: str = "2026-10-01 12:00:00", tid: int | None = None,
                         sheet_state: str | None = None, university: str = UNIVERSITY) -> dict:
    items = _fixture_items()
    for it in items:
        if it["q"] == "q7":
            it["value"] = username
        if it["q"] == "q6":
            it["value"] = course
        if it["q"] == "q4":
            it["value"] = university
    items = [it for it in items if it["value"] is not None]

    async def go():
        await ef.upsert_columns(fid, [(i["q"], i["label"]) for i in items])
        await ef.insert_answer(
            form_id=fid, answer_id=aid, answered_at=answered_at,
            received_at="2026-10-01 12:00:01", payload=items, raw=None,
            matched_telegram_id=tid, match_how="username" if tid else None,
        )
        row = await ef.get_answer(fid, aid)
        if sheet_state is not None:
            async with db._connect() as c:
                await c.execute("UPDATE external_form_answers SET sheet_state = ? WHERE id = ?",
                                (sheet_state, row["id"]))
                await c.commit()
            row = await ef.get_answer(fid, aid)
        return row
    return _run(go())


def _user(tid: int, status: str = "pending", *, username: str | None = USERNAME,
          full_name: str = "Старое Имя", email: str = "old@example.com",
          season: str | None = SEASON, city: str = "msk") -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO users (telegram_id, username, full_name, email, status, season, event_city, "
        "registration_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, db.store_username(username), full_name, email, status, season, city,
         "2026-09-20 10:00:00"),
    )
    conn.commit()
    conn.close()


def _reg_started(tid: int, username: str | None = USERNAME) -> None:
    _run(db.mark_reg_started(tid, username))


def _row(tid: int) -> dict | None:
    return _run(db.get_user(tid))


def _sheet_state(aid: str) -> str:
    conn = sqlite3.connect(config.DB_PATH)
    r = conn.execute("SELECT sheet_state FROM external_form_answers WHERE answer_id = ?",
                     (aid,)).fetchone()
    conn.close()
    return r[0]


def _journal(tid: int) -> list[tuple]:
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT decision, decided_by FROM application_decisions WHERE telegram_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    conn.close()
    return rows


def _reg_events(tid: int) -> list[tuple]:
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT id, event, source_tag FROM reg_events WHERE telegram_id = ? ORDER BY id", (tid,),
    ).fetchall()
    conn.close()
    return rows


def _fsm(storage, tid: int) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=tid, user_id=tid))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1 — слой оценки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_guess_delegation_questions():
    questions = [(i["q"], i["label"]) for i in _fixture_items()]
    guess = dlg.guess_delegation_questions(questions)
    assert guess == {"fullname": "q1", "university": "q4", "course": "q6", "email": "q3"}
    # Ник в телеграмме / ВКонтакте не попадает ни в один из четырёх ключей.
    assert "q7" not in guess.values() and "q8" not in guess.values()


def test_guess_skips_username_labels_and_uses_each_key_once():
    questions = [("a", "Имя в Telegram"), ("b", "Имя и фамилия"), ("c", "Ваш вуз"),
                 ("d", "Университет (второй)"), ("e", "E-mail")]
    guess = dlg.guess_delegation_questions(questions)
    assert guess == {"fullname": "b", "university": "c", "course": None, "email": "e"}


def test_extract_fields():
    form = {"key_username_q": "q7"}
    keys = {"fullname": "q1", "university": "q4", "course": "q6", "email": "q3"}
    items = _fixture_items()
    items[0]["value"] = "  Тест Делегат Тестович  "
    fields = dlg.extract_fields(form, items, keys)
    assert fields == {
        "full_name": "Тест Делегат Тестович",
        "university": UNIVERSITY,
        "course_raw": "1 бакалавриат",
        "email": "test-delegate@example.com",
        "username_needle": NEEDLE,
    }
    # Вопроса нет в ответе — None, а не падение.
    fields = dlg.extract_fields(form, items[:3], keys)
    assert fields["university"] is None and fields["course_raw"] is None
    assert fields["username_needle"] is None
    assert dlg.extract_fields({"key_username_q": None}, items, keys)["username_needle"] is None


def test_cutoff_dt():
    assert dlg.cutoff_dt("23.09.2026") == datetime(2026, 9, 23)
    assert dlg.cutoff_dt(datetime(2026, 9, 23)) == datetime(2026, 9, 23)
    assert dlg.cutoff_dt(None) is None
    assert dlg.cutoff_dt("вчера") is None


def test_delegation_form_id(tmp_path):
    _env(tmp_path)
    assert _run(dlg.delegation_form_id()) is None
    _run(set_setting_by_admin(None, "delegation_form_id", "7"))
    assert _run(dlg.delegation_form_id()) == 7
    _run(set_setting_by_admin(None, "delegation_form_id", "семь"))
    assert _run(dlg.delegation_form_id()) is None


def test_find_person(tmp_path):
    _env(tmp_path)
    _user(101, "pending", username="InUsers")
    _reg_started(102, "OnlyStarted")
    assert _run(dlg.find_person("inusers")) == (101, "users")
    assert _run(dlg.find_person("@OnlyStarted")) == (102, "reg_started")
    assert _run(dlg.find_person("nobody")) == (None, None)
    assert _run(dlg.find_person(None)) == (None, None)


def _evaluate(fid, aid):
    async def go():
        form = await ef.get_form(fid)
        answer = await ef.get_answer(fid, aid)
        return await dlg.evaluate(answer, form, await dlg.field_keys())
    return _run(go())


def test_evaluate_ok_no_check(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _run(set_setting_by_admin(None, "delegation_ta_cutoff", "23.09.2026"))
    _run(set_setting_by_admin(None, "delegation_not_ta_courses", "1\n2"))
    _answer_from_fixture(fid, "after", course="1 бакалавриат", answered_at="2026-10-01 12:00:00")
    _answer_from_fixture(fid, "before", course="1 бакалавриат", answered_at="2026-09-01 12:00:00")
    _answer_from_fixture(fid, "bare", course="2", answered_at="2026-10-01 12:00:00")

    row, fields, ta = _evaluate(fid, "after")
    assert ta == "no" and row["ta_status"] == "no"
    assert row["university"] == UNIVERSITY and row["course_raw"] == "1 бакалавриат"
    assert row["answered_at"] == "2026-10-01 12:00:00" and row["username_needle"] == NEEDLE
    assert fields["course_canonical"] == "1"
    assert _evaluate(fid, "before")[2] == "ok"
    assert _evaluate(fid, "bare")[2] == "check"
    assert _run(ddb.count_by_status(fid, "no", linked=None)) == 1


def test_evaluate_keeps_manual_decision(tmp_path):
    _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "bare", course="2")
    row, _, ta = _evaluate(fid, "bare")
    assert ta == "check"
    _run(ddb.set_decision(row["id"], "ok", MANAGER))
    row2, _, ta2 = _evaluate(fid, "bare")
    assert ta2 == "ok" and row2["ta_status"] == "ok" and row2["decided_by"] == MANAGER


def test_decide_link_rejected_auto_goes_check():
    row = {"linked_telegram_id": None, "decided_by": None}
    assert dlg.decide_link(row, {"status": "rejected"}, 5, how="username") == "check_rejected"


def test_decide_link_rejected_authorised_converts():
    assert dlg.decide_link({"linked_telegram_id": None, "decided_by": MANAGER},
                           {"status": "rejected"}, 5, how="username") == "convert"
    assert dlg.decide_link({"linked_telegram_id": None, "decided_by": None},
                           {"status": "rejected"}, 5, how="manual") == "convert"


def test_decide_link_regular():
    free = {"linked_telegram_id": None, "decided_by": None}
    assert dlg.decide_link(free, None, 5, how="username") == "convert"
    assert dlg.decide_link(free, {"status": "pending"}, 5, how="username") == "convert"
    assert dlg.decide_link(free, {"status": "approved"}, 5, how="username") == "convert"
    assert dlg.decide_link({"linked_telegram_id": 5}, {"status": "approved"}, 5,
                           how="username") == "already"
    assert dlg.decide_link({"linked_telegram_id": 6}, {"status": "approved"}, 5,
                           how="username") == "conflict"
