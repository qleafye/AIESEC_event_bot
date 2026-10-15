"""Запись на общие сессии (пленарки) «по желанию»: тумблер города, правила записи, пересечения,
расписание, статистика, листы и блок дашборда. Тумблер выключен — всё как раньше."""
from __future__ import annotations

import sqlite3

from config import config
from dashboard import queries
from database import db, session_enroll_db as edb
from domain.cities import per_city_key
from services.forum import session_enroll as se
from shared import enroll_stats
from tests._enroll38 import CITY, DAY, SEASON, ready, run, seed_delegates, seed_msk_program

U = 101  # одобренный делегат Москвы текущего сезона


async def _setup(common: bool):
    ids = await seed_msk_program()
    await seed_delegates()
    await db.set_setting("event_city_enabled", "on")
    await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
    if common:
        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on")
    # вторая общая сессия 10:30-11:30 — пересекается с A, B, C
    ids["Q"] = await db.create_program_session(CITY, DAY, "10:30", "11:30", "Закрытие")
    return ids


def test_toggle_off_keeps_old_behaviour(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=False)
        assert await se.common_enabled(CITY) is False
        assert (await se.enroll(U, ids["P"])).status == "not_enrollable"
        assert (await se.enroll_by_staff(U, ids["P"], by_staff_id=1)).status == "not_enrollable"
        assert (await edb.enroll_tx(U, ids["P"])).status == "not_enrollable"
        assert await se.common_sessions(CITY) == []
        assert await se.common_cities() == []
    run(go())


def test_toggle_on_enroll_and_unenroll(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        assert await se.common_enabled(CITY) is True
        assert (await se.enroll(U, ids["P"])).status == "ok"
        assert (await se.enroll(U, ids["P"])).status == "already"
        assert (await se.unenroll(U, ids["P"])).status == "ok"
        assert await edb.user_enrollment_ids(U) == set()
        assert (await se.enroll_by_staff(U, ids["P"], by_staff_id=7)).status == "ok"
        shown = await se.common_sessions(CITY)
        assert [s["id"] for s in shown] == [ids["P"], ids["Q"]]
        assert shown[0]["enrolled"] == 1 and shown[0]["open_state"] == "open"
        assert await se.common_cities() == [CITY]
    run(go())


def test_closed_and_limit_work_for_common(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        await db.update_program_session(ids["P"], enroll_closed=1)
        r = await se.enroll(U, ids["P"])
        assert r.status == "closed" and r.text_key == "session_enroll_err_closed"
        await db.update_program_session(ids["Q"], enroll_limit=1)
        assert (await se.enroll(102, ids["Q"])).status == "ok"
        r = await se.enroll(U, ids["Q"])
        assert r.status == "full" and r.text_key == "session_enroll_err_full"
        # самостоятельный выход из закрытой — запрещён, как у трековых
        await edb.enroll_tx(U, ids["P"])
        assert (await se.unenroll(U, ids["P"])).status == "closed"
    run(go())


def test_overlap_common_vs_track(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        assert (await se.enroll(U, ids["A"])).status == "ok"          # 10:00-11:00 трековая
        r = await se.enroll(U, ids["Q"])                               # 10:30-11:30 общая
        assert r.status == "conflict" and [c["id"] for c in r.conflicts] == [ids["A"]]
        r = await se.enroll(U, ids["Q"], confirm_replace=True)
        assert r.status == "ok" and [s["id"] for s in r.replaced] == [ids["A"]]
        # и наоборот: трековая вытесняет общую
        r = await se.enroll(U, ids["B"])
        assert r.status == "conflict" and [c["id"] for c in r.conflicts] == [ids["Q"]]
        # непересекающиеся (P 09:00-10:00 и A 10:00-11:00) совместимы
        assert (await se.enroll(102, ids["P"])).status == "ok"
        assert (await se.enroll(102, ids["A"])).status == "ok"
    run(go())


def test_overlap_ignores_common_when_toggle_off(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=False)
        # запись на пленарку «осталась в данных» (раньше включали тумблер)
        assert (await edb.enroll_tx(U, ids["Q"], common_ok=True)).status == "ok"
        assert (await se.enroll(U, ids["A"])).status == "ok"          # конфликта нет
        assert await edb.user_enrollment_ids(U) == {ids["Q"], ids["A"]}
    run(go())


def test_my_schedule_common_chosen_and_hidden(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        await se.enroll(U, ids["P"])
        day = (await se.my_schedule(U, CITY))[0]["items"]
        kinds = {i["session"]["id"]: i["kind"] for i in day}
        assert kinds[ids["P"]] == "chosen" and kinds[ids["Q"]] == "common"
        # тумблер выключили: запись скрыта, пленарка снова «для всех»
        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "off")
        day = (await se.my_schedule(U, CITY))[0]["items"]
        kinds = {i["session"]["id"]: i["kind"] for i in day}
        assert kinds[ids["P"]] == "common" and kinds[ids["Q"]] == "common"
    run(go())


def test_scan_hint_for_common(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=False)
        user = {"telegram_id": U}
        assert await se.scan_hint(user, ids["P"]) is None
        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on")
        hint = await se.scan_hint(user, ids["P"])
        assert hint and hint["enroll"]["action"] == "book" and hint["enroll"]["session_id"] == ids["P"]
        await se.enroll(U, ids["P"])
        assert await se.scan_hint(user, ids["P"]) is None             # уже записан
    run(go())


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_stats_sheets_and_dashboard_include_common(tmp_path):
    ready(tmp_path)

    async def go():
        await db.set_setting("event_season", SEASON)
        ids = await _setup(common=True)
        await se.enroll(U, ids["P"])
        await se.enroll(102, ids["P"])
        await se.enroll(U, ids["A"])
        return ids

    ids = run(go())
    common = run(se.common_cities())
    rows = run(edb.enroll_overview_rows(None, common=common))
    by_id = {r["id"]: r for r in rows}
    assert by_id[ids["P"]]["enrolled"] == 2 and by_id[ids["Q"]]["enrolled"] == 0
    sheet = enroll_stats.sessions_sheet_rows(rows, {CITY: "Москва"})
    plen = next(r for r in sheet if r[3] == "Пленарка")
    assert plen[5] == "Общая" and plen[7] == 2
    people = run(edb.enroll_people_rows(None, common=common))
    assert len(people) == 3
    # дашборд читает те же настройки из сырой bot_settings
    with _conn() as conn:
        block = queries.enroll_block(conn, queries.Scope())
    got = {s["id"]: s for s in block["sessions"]}
    assert got[ids["P"]]["enrolled"] == 2 and got[ids["P"]]["track_name"] == "Общая"
    assert block["enrolled"] == 2
    # без тумблера общих сессий нет ни в листах, ни на дашборде
    run(db.set_setting(per_city_key("session_enroll_common_optional", CITY), "off"))
    assert ids["P"] not in {r["id"] for r in run(edb.enroll_overview_rows(None, common=run(se.common_cities())))}
    assert len(run(edb.enroll_people_rows(None, common=[]))) == 1
    with _conn() as conn:
        assert ids["P"] not in {s["id"] for s in queries.enroll_block(conn, queries.Scope())["sessions"]}


def test_dashboard_block_only_common_sessions(tmp_path):
    """Нет ни одной сессии с треком, есть только общие и тумблер включён — блок не None."""
    ready(tmp_path)

    async def go():
        sid = await db.create_program_session(CITY, DAY, "09:00", "10:00", "Открытие")
        await db.set_setting("event_city_enabled", "on")
        return sid

    sid = run(go())
    with _conn() as conn:
        assert queries.enroll_block(conn, queries.Scope()) is None
    run(db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on"))
    with _conn() as conn:
        block = queries.enroll_block(conn, queries.Scope())
    assert block is not None and [s["id"] for s in block["sessions"]] == [sid]


def test_common_cities_ladder():
    f = enroll_stats.common_cities
    s = {"session_enroll_common_optional": "on", "session_enroll_common_optional__city__spb": "off"}
    assert f(s, ["msk", "spb"], cities_on=True) == ["msk"]
    assert f(s, ["msk", "spb"], cities_on=False) == ["msk", "spb"]   # модуль городов выключен
    assert f({}, ["msk"], cities_on=True) == []
