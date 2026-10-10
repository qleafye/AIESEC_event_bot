"""Делегации вузов — ядро `services/delegations/delegations.py`: вопросы формы, поля ответа, поиск человека
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

import domain.cities as cities_mod
from config import config
from database import db
from database import delegations_db as ddb
from database import ext_forms_db as ef
from services.delegations import delegations as dlg
from services.settings.audit import set_setting_by_admin
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
            await set_setting_by_admin(None, "delegation_armed_form_id", str(fid))
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2 — слой превращения
# ══════════════════════════════════════════════════════════════════════════════════════════

def _available(fid, aid, **kw):
    return _run(dlg.on_answer_available(fid, aid, **kw))


def _drow(fid, aid):
    return _run(ddb.get_by_answer(fid, aid))


def _assert_approved_delegate(tid: int, aid: str, *, course="1"):
    from services.forum.checkin import checkin_denial
    u = _row(tid)
    assert u is not None and u["status"] == "approved" and u["approved_at"]
    assert u["event_city"] == cities_mod.default_city_code()
    assert u["season"] == SEASON
    assert u["delegation"] == UNIVERSITY and u["delegation_answer_id"] == aid
    assert _run(checkin_denial(u)) is None
    return u


def test_noop_for_other_form(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form(select=False)
    _answer_from_fixture(fid, "a1", answered_at="2026-09-01 12:00:00")
    _reg_started(501)
    assert _available(fid, "a1") == {"skipped": "not_delegation_form"}
    other = _run(ef.create_form(platform="google", external_id="other", title="Другая"))
    _run(set_setting_by_admin(None, "delegation_form_id", str(other)))
    assert _available(fid, "a1") == {"skipped": "not_delegation_form"}
    assert _drow(fid, "a1") is None and bot.sent == []


def test_convert_new_user_from_reg_started(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(501)
    res = _available(fid, "a1")
    assert res.get("converted", {}).get("flipped") is True
    u = _assert_approved_delegate(501, "a1")
    assert u["source"] == "delegation" and u["source_from_tag"] == 1
    assert u["participant_type"] == "full" and u["course"] == "3"
    assert u["full_name"] == "Тест Делегат Тестович" and u["university"] == UNIVERSITY
    assert u["email"] == "test-delegate@example.com" and u["username"] == "@" + USERNAME
    assert _run(db.get_reg_started_by_username(USERNAME)) is None
    assert len(bot.sent) == 1
    chat_id, text, kb = bot.sent[0]
    assert chat_id == 501 and UNIVERSITY in text and kb is not None
    assert _drow(fid, "a1")["linked_telegram_id"] == 501
    assert _journal(501) == [("approved", dlg.DELEGATION_DECIDED_BY)]
    assert ("form_completed", "delegation") in [(e, s) for _, e, s in _reg_events(501)]


def test_convert_credits_inviting_ambassador(tmp_path, monkeypatch):
    """Одобрение делегата проходит через ту же точку «приглашённого одобрили», что обычная
    заявка (record_decision → on_invitees_approved): амбассадор получает зачёт ровно один раз,
    повторная обработка того же ответа второй раз не зачитывает."""
    _env(tmp_path)
    import services.amb.amb_journal as amb_journal
    calls = []

    async def fake(ids, *, changed_by=None, source="approval"):
        calls.append((list(ids), changed_by))
        return {"credited": 0, "coins": 0, "ambassadors": set()}

    monkeypatch.setattr(amb_journal, "on_invitees_approved", fake)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(503)
    _available(fid, "a1")
    assert calls == [([503], None)]
    _available(fid, "a1")
    assert calls == [([503], None)]


def test_convert_when_city_closed(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    code = cities_mod.default_city_code()
    _run(set_setting_by_admin(None, "event_city_enabled", "on"))
    _run(set_setting_by_admin(None, cities_mod.per_city_key("city_reg_close_date", code),
                              "01.01.2020"))
    assert _run(cities_mod.is_city_registration_open(code)) is False
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(502)
    _available(fid, "a1")
    _assert_approved_delegate(502, "a1")
    assert len(bot.sent) == 1


def test_existing_pending_keeps_answers(tmp_path, monkeypatch):
    bot, _ = _env(tmp_path)
    import services.sheets.sheets as sheets
    calls = []

    async def fake_update(tid, label):
        calls.append((tid, label))
    monkeypatch.setattr(sheets, "update_status_in_sheet", fake_update)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(503, "pending")
    _available(fid, "a1")
    u = _assert_approved_delegate(503, "a1")
    assert u["full_name"] == "Старое Имя" and u["email"] == "old@example.com"
    assert len(bot.sent) == 1 and UNIVERSITY in bot.sent[0][1]
    assert "одобрена" in bot.sent[0][1]
    assert calls == [(503, "Одобрена")]
    assert len(_journal(503)) == 1


def test_existing_approved_short_text(tmp_path, monkeypatch):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(504, "approved")
    before = _row(504)
    _available(fid, "a1")
    u = _row(504)
    assert u["status"] == "approved" and u["approved_at"] == before["approved_at"]
    assert u["delegation"] == UNIVERSITY and u["delegation_answer_id"] == "a1"
    assert _journal(504) == []
    assert len(bot.sent) == 1
    existing = _run(dlg.get_setting_typed("delegation_welcome_existing_text"))
    assert bot.sent[0][1] == existing.replace("{university}", UNIVERSITY)


def test_rejected_goes_to_check(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(505, "rejected")
    res = _available(fid, "a1")
    assert res.get("verdict") == "check_rejected"
    assert _row(505)["status"] == "rejected" and bot.sent == []
    row = _drow(fid, "a1")
    assert row["ta_status"] == "check" and row["note"] == dlg.NOTE_REJECTED_IN_BOT
    assert row["linked_telegram_id"] is None


def test_rejected_manual_ok_converts(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(506, "rejected")
    _available(fid, "a1")
    row = _drow(fid, "a1")
    _run(ddb.set_decision(row["id"], "ok", MANAGER))
    _available(fid, "a1", reason="manual")
    _assert_approved_delegate(506, "a1")
    assert _journal(506) == [("approved", MANAGER)]
    assert len(bot.sent) == 1
    assert _drow(fid, "a1")["linked_telegram_id"] == 506

    # Прямая ручная привязка отклонённого — тот же результат, автор — менеджер.
    _answer_from_fixture(fid, "a2", username="@second", course="3 бакалавриат")
    _user(507, "rejected", username="second")
    form = _run(ef.get_form(fid))
    row2, fields2, _ = _run(dlg.evaluate(_run(ef.get_answer(fid, "a2")), form,
                                         _run(dlg.field_keys())))
    res = _run(dlg.convert_to_delegate(507, row2, fields2, how="manual", by=MANAGER))
    assert res.get("flipped") is True
    assert _row(507)["status"] == "approved" and _journal(507) == [("approved", MANAGER)]


def test_rejected_unauthorised_direct_convert_refused(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(508, "rejected")
    form = _run(ef.get_form(fid))
    row, fields, _ = _run(dlg.evaluate(_run(ef.get_answer(fid, "a1")), form,
                                       _run(dlg.field_keys())))
    res = _run(dlg.convert_to_delegate(508, row, fields, how="username", by=None))
    assert res == {"refused": "rejected_in_bot"}
    assert _row(508)["status"] == "rejected" and bot.sent == []


def test_not_ta_silent(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="1 бакалавриат", answered_at="2026-10-01 12:00:00")
    _reg_started(509)
    res = _available(fid, "a1")
    assert res["ta"] == "no"
    assert _row(509) is None and bot.sent == []
    assert _drow(fid, "a1")["ta_status"] == "no"


def test_check_waits(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="2")
    _reg_started(510)
    res = _available(fid, "a1")
    assert res["ta"] == "check"
    assert _row(510) is None and bot.sent == []
    assert _drow(fid, "a1")["linked_telegram_id"] is None


def test_unmatched_waits(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    res = _available(fid, "a1")
    assert res["ta"] == "ok" and res.get("waiting") == "no_person"
    assert _drow(fid, "a1")["linked_telegram_id"] is None and bot.sent == []
    found = _run(ddb.find_pending_by_username(USERNAME))
    assert [r["answer_id"] for r in found] == ["a1"]


def test_idempotent(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(511)
    _available(fid, "a1")
    second = _available(fid, "a1")
    assert second.get("verdict") == "already"
    assert len(bot.sent) == 1 and len(_journal(511)) == 1


def test_conversion_cleans_registration_fsm(tmp_path):
    from handlers.states import Registration
    bot, storage = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(512)
    _reg_started(513, "someone_else")
    _run(_fsm(storage, 512).set_state(Registration.full_name))
    _run(_fsm(storage, 513).set_state("Payment:waiting_receipt"))
    _run(db.upsert_reg_draft(512, kind="new", source="bot", step="full_name",
                             patch={"full_name": "Черновик"}))
    assert _run(db.get_reg_draft(512)) is not None
    _available(fid, "a1")
    assert _run(_fsm(storage, 512).get_state()) is None
    assert _run(_fsm(storage, 513).get_state()) == "Payment:waiting_receipt"
    assert _run(db.get_reg_draft(512)) is None
    assert _run(db.get_reg_started_by_username("someone_else")) is not None


def test_no_payment_step(tmp_path, monkeypatch):
    import handlers.payment as payment
    import handlers.reg.reg_schema as reg_schema
    bot, storage = _env(tmp_path)
    _run(set_setting_by_admin(None, "payment_enabled", "on"))
    touched = []

    async def sentinel(*a, **k):
        touched.append(a)
    monkeypatch.setattr(reg_schema, "approve_user", sentinel)
    monkeypatch.setattr(payment, "start_payment_step", sentinel)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(514)
    _available(fid, "a1")
    u = _assert_approved_delegate(514, "a1")
    assert touched == []
    assert _run(_fsm(storage, 514).get_state()) is None
    assert u["payment_status"] == "not_paid"


def test_convert_marks_sheet_update(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    form = _run(ef.get_form(fid))
    keys = _run(dlg.field_keys())

    _answer_from_fixture(fid, "synced", course="3 бакалавриат", sheet_state="synced")
    _reg_started(515)
    row, fields, _ = _run(dlg.evaluate(_run(ef.get_answer(fid, "synced")), form, keys))
    _run(dlg.convert_to_delegate(515, row, fields, how="username", by=None))
    assert _sheet_state("synced") == "update"

    _answer_from_fixture(fid, "fresh", username="@fresh_one", course="3 бакалавриат")
    _reg_started(516, "fresh_one")
    row, fields, _ = _run(dlg.evaluate(_run(ef.get_answer(fid, "fresh")), form, keys))
    _run(dlg.convert_to_delegate(516, row, fields, how="username", by=None))
    assert _sheet_state("fresh") == "append"

    # Повтор по уже привязанному — ни второй отметки, ни второго письма.
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE external_form_answers SET sheet_state = 'synced' WHERE answer_id = 'synced'")
    conn.commit()
    conn.close()
    row = _drow(fid, "synced")
    res = _run(dlg.convert_to_delegate(515, row, fields, how="username", by=None))
    assert res == {"already": True}
    assert _sheet_state("synced") == "synced" and len(bot.sent) == 2


def test_evaluation_only_marks_sheet_when_status_changes(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="2", sheet_state="synced")
    _available(fid, "a1")
    assert _sheet_state("a1") == "update"
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE external_form_answers SET sheet_state = 'synced'")
    conn.commit()
    conn.close()
    _available(fid, "a1")
    assert _sheet_state("a1") == "synced"


def test_sweep_pending(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "n1", username="@n_one", course="3 бакалавриат")
    _answer_from_fixture(fid, "n2", username="@n_two", course="2")
    _answer_from_fixture(fid, "later", username="@late_one", course="3 бакалавриат")
    _available(fid, "later")
    assert _drow(fid, "later")["linked_telegram_id"] is None
    _reg_started(517, "late_one")
    res = _run(dlg.sweep_pending())
    assert res["evaluated"] == 2 and res["linked"] == 1
    assert _drow(fid, "n1")["ta_status"] == "ok" and _drow(fid, "n2")["ta_status"] == "check"
    assert _row(517)["status"] == "approved" and len(bot.sent) == 1
    again = _run(dlg.sweep_pending())
    assert again["evaluated"] == 0 and again["linked"] == 0 and len(bot.sent) == 1


def test_manual_decision_reevaluated(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="2")
    _reg_started(518)
    _available(fid, "a1")
    row = _drow(fid, "a1")
    assert row["ta_status"] == "check" and _row(518) is None
    _run(ddb.set_decision(row["id"], "ok", MANAGER))
    _available(fid, "a1", reason="manual")
    _assert_approved_delegate(518, "a1")
    after = _drow(fid, "a1")
    assert after["ta_status"] == "ok" and after["decided_by"] == MANAGER
    assert _journal(518) == [("approved", MANAGER)]


def test_on_first_entry_marks_update(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _reg_started(519)
    _available(fid, "a1")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE external_form_answers SET sheet_state = 'synced'")
    conn.commit()
    conn.close()
    _run(dlg.on_first_entry(bot, 519, "msk", "2026-10-30", source="miniapp", first_of_forum=True))
    assert _sheet_state("a1") == "update"
    _user(520, "approved", username="plain")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE external_form_answers SET sheet_state = 'synced'")
    conn.commit()
    conn.close()
    _run(dlg.on_first_entry(bot, 520, "msk", "2026-10-30", source="miniapp"))
    assert _sheet_state("a1") == "synced"


class _FakeFrom:
    def __init__(self, uid, username):
        self.id = uid
        self.username = username


class _FakeMessage:
    def __init__(self, uid, username):
        self.from_user = _FakeFrom(uid, username)
        self.chat = _FakeFrom(uid, None)
        self.answers = []

    async def answer(self, text=None, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


def test_try_delegate_start(tmp_path, monkeypatch):
    bot, storage = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат", sheet_state="synced")
    _available(fid, "a1")
    assert _row(521) is None
    msg = _FakeMessage(521, USERNAME)
    assert _run(dlg.try_delegate_start(msg, _fsm(storage, 521), bot)) is True
    _assert_approved_delegate(521, "a1")
    assert _sheet_state("a1") == "update" and len(bot.sent) == 1

    assert _run(dlg.try_delegate_start(_FakeMessage(522, "stranger"), _fsm(storage, 522),
                                       bot)) is False
    assert _row(522) is None and len(bot.sent) == 1

    queried = []
    orig = ddb.find_pending_by_username

    async def spy(needle):
        queried.append(needle)
        return await orig(needle)
    monkeypatch.setattr(ddb, "find_pending_by_username", spy)
    assert _run(dlg.try_delegate_start(_FakeMessage(523, None), _fsm(storage, 523), bot)) is False
    assert queried == []


def test_try_delegate_start_rejected_goes_check(tmp_path):
    bot, storage = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _user(524, "rejected")
    # Синк уже прошёл и отправил ответ в «проверить»; менеджер пока не решал — для чистоты
    # вернём строку в ok без автора, как если бы человек нажал /start раньше синка.
    _available(fid, "a1")
    row = _drow(fid, "a1")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE delegation_answers SET ta_status = 'ok', note = NULL WHERE id = ?",
                 (row["id"],))
    conn.commit()
    conn.close()
    msg = _FakeMessage(524, USERNAME)
    assert _run(dlg.try_delegate_start(msg, _fsm(storage, 524), bot)) is False
    assert _row(524)["status"] == "rejected" and bot.sent == []
    row = _drow(fid, "a1")
    assert row["ta_status"] == "check" and row["note"] == dlg.NOTE_REJECTED_IN_BOT

    _run(ddb.set_decision(row["id"], "ok", MANAGER))
    assert _run(dlg.try_delegate_start(msg, _fsm(storage, 524), bot)) is True
    assert _row(524)["status"] == "approved" and _journal(524) == [("approved", MANAGER)]
    assert len(bot.sent) == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3 — хуки фазы внешних форм, точка в cmd_start, хвост сверки
# ══════════════════════════════════════════════════════════════════════════════════════════

def _ingest(fid, aid, items=None, answered_at="2026-10-01 12:00:00"):
    from services.ext_forms.ext_forms_ingest import ingest_answer

    async def go():
        form = await ef.get_form(fid)
        return await ingest_answer(form, answer_id=aid, answered_at=answered_at,
                                   items=items or _fixture_items(), raw=None)
    return _run(go())


def test_hook_on_ingest(tmp_path):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    items = _fixture_items()
    for it in items:
        if it["q"] == "q6":
            it["value"] = "3 бакалавриат"
    _reg_started(601)
    assert _ingest(fid, "a1", items) is True
    assert _drow(fid, "a1")["ta_status"] == "ok"
    assert _row(601)["status"] == "approved" and len(bot.sent) == 1
    assert _ingest(fid, "a1", items) is False
    assert _run(ddb.count_by_status(fid, "ok", linked=None)) == 1 and len(bot.sent) == 1


def test_hook_survives_delegation_error(tmp_path, monkeypatch):
    bot, _ = _env(tmp_path)
    fid = _delegation_form()

    async def boom(*a, **k):
        raise RuntimeError("делегации упали")
    monkeypatch.setattr(dlg, "on_answer_available", boom)
    assert _ingest(fid, "a1") is True
    assert _run(ef.get_answer(fid, "a1")) is not None
    assert _drow(fid, "a1") is None and bot.sent == []


def test_rematch_hook(tmp_path):
    from services.ext_forms.ext_forms_match import rematch_unmatched
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")
    _available(fid, "a1")
    assert _drow(fid, "a1")["linked_telegram_id"] is None
    _user(602, "pending")
    assert _run(rematch_unmatched()) == 1
    assert _run(ef.get_answer(fid, "a1"))["matched_telegram_id"] == 602
    assert _drow(fid, "a1")["linked_telegram_id"] == 602
    assert _row(602)["status"] == "approved" and len(bot.sent) == 1


class _StartMessage(_FakeMessage):
    async def answer_photo(self, *a, reply_markup=None, **k):
        self.answers.append(("<photo>", reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _StartMessage(self.from_user.id, self.from_user.username)
        new.answers = self.answers
        return new


def test_cmd_start_late_entry(tmp_path, monkeypatch):
    from handlers import registration as reg
    bot, storage = _env(tmp_path)
    _run(set_setting_by_admin(None, "contact_tg", "@test_channel"))
    sub_calls = []

    async def fake_is_subscribed(bot_, channel, user_id):
        sub_calls.append(user_id)
        return True
    monkeypatch.setattr(reg, "is_subscribed", fake_is_subscribed)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат", sheet_state="synced")
    _available(fid, "a1")
    msg = _StartMessage(603, USERNAME)
    _run(reg.cmd_start(msg, _fsm(storage, 603), bot=bot, command=None))
    _assert_approved_delegate(603, "a1")
    assert _sheet_state("a1") == "update"
    events = _reg_events(603)
    kinds = [e for _, e, _ in events]
    assert kinds.index("start") < kinds.index("form_completed")
    assert sub_calls == []
    assert len(bot.sent) == 1


def test_cmd_start_order_guard():
    path = os.path.join(os.path.dirname(__file__), "..", "handlers", "registration.py")
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    funnel = next(i for i, l in enumerate(lines) if '"start", event_city=dl_event_city' in l)
    entry = next(i for i, l in enumerate(lines) if "try_delegate_start(message" in l)
    subscription = next(i for i, l in enumerate(lines)
                        if '_normalize_channel_ref(await get_setting("contact_tg"))' in l)
    assert funnel < entry < subscription
    assert sum("try_delegate_start" in l for l in lines) == 2


def test_reconcile_all_calls_sweep(tmp_path, monkeypatch):
    from services.ext_forms import ext_forms_yandex_sync as sync
    bot, _ = _env(tmp_path)
    fid = _delegation_form()
    _answer_from_fixture(fid, "a1", course="3 бакалавриат")

    async def no_forms(platform):
        return []
    monkeypatch.setattr(sync.ef, "list_active_forms", no_forms)
    res = _run(sync.reconcile_all())
    assert res["enqueued"] == 0 and res["rematched"] == 0
    assert res["swept"]["evaluated"] == 1
    assert _drow(fid, "a1")["ta_status"] == "ok"
