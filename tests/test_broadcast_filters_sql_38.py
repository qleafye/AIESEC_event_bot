"""Фильтры рассылки по записи на сессию, тесту и сентинел «текущий сезон»."""
from __future__ import annotations

import json

from database import db, quiz_db as qz, session_enroll_db as se
from tests._enroll38 import CITY, PAST_SEASON, SEASON, add_user, ready, run, seed_delegates, \
    seed_msk_program


def _filter_in(sid):
    return [{"field": "session_enroll", "value": db.SESSION_ENROLL_IN, "session_id": sid}]


async def _ids(filters):
    return sorted(await db.count_and_list_filtered(filters))


def test_filter_session_enroll_in(tmp_path):
    ready(tmp_path)

    async def go():
        p = await seed_msk_program()
        d = await seed_delegates()
        for key in ("cur1", "pending", "past"):
            await se.enroll_tx(d[key], p["A"])
        assert await _ids(_filter_in(p["A"])) == [d["cur1"]]
        await db.delete_program_session(p["B"])
        assert await _ids(_filter_in(p["B"])) == []  # удалённая сессия — fail-closed
        assert await _ids(_filter_in("1; DROP")) == []

    run(go())


def test_filter_session_enroll_none(tmp_path):
    ready(tmp_path)

    async def go():
        p = await seed_msk_program()
        d = await seed_delegates()
        await add_user(201, city="spb")
        await se.enroll_tx(d["cur2"], p["A"])
        spec = [{"field": "session_enroll", "value": db.SESSION_ENROLL_NONE, "city": CITY}]
        got = await _ids(spec)
        assert d["cur1"] in got and d["empty"] in got
        assert d["cur2"] not in got  # записан
        assert d["pending"] not in got and d["past"] not in got  # гард approved+сезон
        assert 201 not in got  # другой город
        assert await _ids([{"field": "session_enroll", "value": "none"}]) == []  # без города

    run(go())


def test_filter_quiz_not_passed(tmp_path):
    ready(tmp_path)

    async def go():
        d = await seed_delegates()
        quiz = await qz.get_or_create_quiz(CITY)
        done = await qz.create_attempt(d["cur1"], quiz["id"], 1)
        await qz.finish_attempt(done, {})
        await qz.create_attempt(d["cur2"], quiz["id"], 1)  # начал, не закончил
        got = await _ids([{"field": "quiz", "value": db.QUIZ_NOT_PASSED, "city": CITY}])
        assert d["cur1"] not in got
        assert d["cur2"] in got and d["empty"] in got
        assert d["past"] not in got and d["pending"] not in got
        assert await _ids([{"field": "quiz", "value": "bogus", "city": CITY}]) == []

    run(go())


def test_season_current_sentinel(tmp_path):
    ready(tmp_path)

    async def go():
        d = await seed_delegates()
        spec = [{"field": "season", "value": db.SEASON_CURRENT}]
        got = await _ids(spec)
        assert d["cur1"] in got and d["empty"] in got and d["pending"] in got
        assert d["past"] not in got
        again = json.loads(json.dumps(spec))  # переживает отложенную рассылку
        assert await _ids(again) == got
        # сезон не заморожен в спеке: смена настройки меняет результат
        await db.set_setting("event_season", PAST_SEASON)
        swapped = await _ids(again)
        assert d["past"] in swapped and d["cur1"] not in swapped
        assert "event_season" not in again[0]

    run(go())


def test_season_current_without_event_season(tmp_path):
    ready(tmp_path)

    async def go():
        d = await seed_delegates()
        await db.set_setting("event_season", "")
        got = await _ids([{"field": "season", "value": db.SEASON_CURRENT}])
        assert d["past"] not in got and d["cur1"] not in got  # сезон не задан — не «всем подряд»
        assert d["empty"] in got

    run(go())


def test_split_ids_by_season(tmp_path):
    ready(tmp_path)

    async def go():
        d = await seed_delegates()
        ids = [d["past"], d["cur1"], d["empty"], d["pending"]]
        cur, past = await db.split_ids_by_season(ids)
        assert cur == [d["cur1"], d["empty"], d["pending"]] and past == [d["past"]]
        await db.set_setting("event_season", "")
        assert await db.split_ids_by_season(ids) == (ids, [])
        assert await db.split_ids_by_season([]) == ([], [])

    run(go())


def test_fields_registered():
    assert {"session_enroll", "quiz"} <= db._FILTER_COLUMNS
    assert {"session_enroll", "quiz"} <= db._FILTER_VIRTUAL_FIELDS
    assert db.SEASON_CURRENT == "__current__"
    assert SEASON  # фикстура
