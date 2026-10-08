"""Слой БД записи на сессии: схема, справочники треков и компетенций, связи, чистка."""
from __future__ import annotations

from database import db, session_enroll_db as se
from tests._enroll38 import CITY, ready, run, seed_msk_program


def test_schema_on_old_db(tmp_path):
    ready(tmp_path)

    async def go():
        async with db._connect() as conn:
            for table in ("program_tracks", "competencies", "program_session_competencies",
                          "session_enrollments", "session_schedule_confirms"):
                async with conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ) as cur:
                    assert await cur.fetchone(), table
            # «старая» сессия — вставлена без новых колонок
            await conn.execute(
                "INSERT INTO program_sessions (city, day, start_time, end_time, title, "
                "created_at, updated_at) VALUES ('msk','2026-10-30','10:00','11:00','Старая','x','x')"
            )
            await conn.commit()
        sess = (await db.list_program_sessions_for_city_day(CITY, "2026-10-30"))[0]
        assert sess["enroll_closed"] == 0
        assert sess["track_id"] is None and sess["enroll_limit"] is None
        # повторный ensure_schema не падает
        async with db._connect() as conn:
            await se.ensure_schema(conn)
            await conn.commit()

    run(go())


def test_tracks_crud_order(tmp_path):
    ready(tmp_path)

    async def go():
        a = await se.create_track(CITY, "Один")
        b = await se.create_track(CITY, "Два")
        c = await se.create_track(CITY, "Три")
        await se.create_track("spb", "Чужой")
        assert [t["name"] for t in await se.list_tracks(CITY)] == ["Один", "Два", "Три"]
        assert await se.move_track(c, -1)
        assert [t["name"] for t in await se.list_tracks(CITY)] == ["Один", "Три", "Два"]
        assert not await se.move_track(a, -1)
        assert await se.rename_track(b, "Дважды")
        assert (await se.get_track(b))["name"] == "Дважды"
        assert len(await se.list_tracks("spb")) == 1

    run(go())


def test_delete_track_detaches_sessions(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        assert (await se.enroll_tx(5, ids["A"])).status == "ok"
        assert await se.count_sessions_for_track(ids["career"]) == 2
        assert await se.count_enrollments_for_track(ids["career"]) == 1
        assert await se.delete_track(ids["career"]) == 1
        assert (await db.get_program_session(ids["A"]))["track_id"] is None
        assert await se.user_enrollment_ids(5) == set()
        assert await se.get_track(ids["career"]) is None

    run(go())


def test_competencies_crud_and_links(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        assert await se.list_competencies(CITY) == []
        assert await se.competency_names_for_sessions([ids["A"]]) == {ids["A"]: []}
        c1 = await se.create_competency(CITY, "Лидерство")
        c2 = await se.create_competency(CITY, "Коммуникация")
        assert await se.toggle_session_competency(ids["A"], c1) is True
        assert await se.toggle_session_competency(ids["A"], c2) is True
        assert await se.toggle_session_competency(ids["A"], c2) is False
        assert await se.get_session_competency_ids(ids["A"]) == [c1]
        assert (await se.competency_names_for_sessions([ids["A"], ids["B"]])) == {
            ids["A"]: ["Лидерство"], ids["B"]: []}
        assert await se.move_competency(c2, -1)
        assert [c["name"] for c in await se.list_competencies(CITY)] == [
            "Коммуникация", "Лидерство"]
        await se.delete_competency(c1)
        assert await se.get_session_competency_ids(ids["A"]) == []

    run(go())


def test_patch_fields_allow_list(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        assert await db.update_program_session(
            ids["P"], track_id=ids["career"], enroll_closed=1, enroll_limit=30)
        s = await db.get_program_session(ids["P"])
        assert (s["track_id"], s["enroll_closed"], s["enroll_limit"]) == (ids["career"], 1, 30)
        trackable = await se.list_trackable_sessions(CITY, "2026-10-30")
        assert ids["P"] in [t["id"] for t in trackable]
        assert all(t["track_name"] for t in trackable)

    run(go())


def test_list_trackable_excludes_plenary(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        got = [t["id"] for t in await se.list_trackable_sessions(CITY)]
        assert got == [ids["A"], ids["B"], ids["C"], ids["D"]]
        assert await se.list_trackable_sessions(CITY, "2026-01-01") == []

    run(go())


def test_delete_session_cleans_enrollments(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        cid = await se.create_competency(CITY, "К")
        await se.toggle_session_competency(ids["A"], cid)
        await se.enroll_tx(5, ids["A"])
        assert await db.delete_program_session(ids["A"])
        async with db._connect() as conn:
            for t in ("session_enrollments", "program_session_competencies"):
                async with conn.execute(f"SELECT COUNT(*) FROM {t}") as cur:
                    assert (await cur.fetchone())[0] == 0

    run(go())


def test_purge_tables_registered():
    tables = {t for t, _, _ in db.USER_PURGE_TABLES}
    assert {"session_enrollments", "session_schedule_confirms"} <= tables
