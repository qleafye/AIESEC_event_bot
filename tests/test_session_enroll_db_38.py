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


# ── enroll_tx ────────────────────────────────────────────────────────────────────────────────

def test_enroll_ok_and_already(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        r = await se.enroll_tx(5, ids["A"])
        assert r.status == "ok" and r.replaced == []
        assert (await se.enroll_tx(5, ids["A"])).status == "already"
        assert await se.count_enrollments(ids["A"]) == 1

    run(go())


def test_enroll_conflict_pairwise(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await se.enroll_tx(5, ids["A"])
        r = await se.enroll_tx(5, ids["B"])
        assert (r.status, r.conflicts) == ("conflict", [ids["A"]])
        assert (await se.enroll_tx(5, ids["D"])).status == "ok"  # 11:30, с A не пересекается
        r = await se.enroll_tx(5, ids["C"])  # C пересекается и с A, и с D
        assert r.status == "conflict" and set(r.conflicts) == {ids["A"], ids["D"]}
        assert await se.user_enrollment_ids(5) == {ids["A"], ids["D"]}

    run(go())


def test_enroll_replace(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await se.enroll_tx(5, ids["A"])
        r = await se.enroll_tx(5, ids["B"], allow_replace=True)
        assert (r.status, r.replaced) == ("ok", [ids["A"]])
        assert await se.user_enrollment_ids(5) == {ids["B"]}

    run(go())


def test_enroll_limit(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await db.update_program_session(ids["A"], enroll_limit=1)
        assert (await se.enroll_tx(1, ids["A"])).status == "ok"
        assert (await se.enroll_tx(2, ids["A"])).status == "full"
        assert (await se.enroll_tx(2, ids["A"], limit_check=False, source="scan",
                                   by_staff_id=77)).status == "ok"
        users = await se.list_enrolled_users(ids["A"])
        assert [(u["telegram_id"], u["source"], u["by_staff_id"]) for u in users] == [
            (1, "self", None), (2, "scan", 77)]

    run(go())


def test_enroll_plenary_not_enrollable(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        assert (await se.enroll_tx(5, ids["P"])).status == "not_enrollable"
        assert (await se.enroll_tx(5, 99999)).status == "no_session"

    run(go())


def test_other_day_and_city_ignored(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        track = ids["career"]
        other_day = await db.create_program_session(CITY, "2026-10-31", "10:00", "11:00", "Д2")
        other_city = await db.create_program_session("spb", "2026-10-30", "10:00", "11:00", "СПб")
        await db.update_program_session(other_day, track_id=track)
        await db.update_program_session(other_city, track_id=track)
        await se.enroll_tx(5, ids["A"])
        assert (await se.enroll_tx(5, other_day)).status == "ok"
        assert (await se.enroll_tx(5, other_city)).status == "ok"

    run(go())


def test_pleanary_enrollment_ignored_in_conflicts(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        # запись на сессию, потерявшую трек, не мешает новым записям
        await se.enroll_tx(5, ids["A"])
        await db.update_program_session(ids["A"], track_id=None)
        assert (await se.enroll_tx(5, ids["B"])).status == "ok"

    run(go())


def test_confirm_schedule(tmp_path):
    ready(tmp_path)

    async def go():
        assert await se.get_schedule_confirmed_at(5, CITY) is None
        stamp = await se.confirm_schedule(5, CITY)
        assert await se.get_schedule_confirmed_at(5, CITY) == stamp
        await se.confirm_schedule(5, CITY)
        await se.confirm_schedule(6, CITY)
        assert await se.count_schedule_confirms(CITY) == 2
        assert await se.count_schedule_confirms("spb") == 0

    run(go())


def test_counts_and_lists(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await se.enroll_tx(1, ids["A"])
        await se.enroll_tx(2, ids["A"])
        await se.enroll_tx(2, ids["D"])
        assert await se.enrollment_counts_for_city(CITY) == {ids["A"]: 2, ids["D"]: 1}
        assert await se.count_enrolled_users(CITY) == 2
        mine = await se.list_user_enrollments(2, CITY)
        assert [(s["id"], s["track_name"]) for s in mine] == [
            (ids["A"], "Карьера"), (ids["D"], "Бизнес")]
        assert await se.unenroll(2, ids["D"]) and not await se.unenroll(2, ids["D"])

    run(go())


# ── переезд делегата ─────────────────────────────────────────────────────────────────────────

def test_city_move_deletes_enrollments(tmp_path, monkeypatch):
    import domain.cities as cities
    from services.city_move import STATUS_MODE_KEEP, move_user_city
    from tests import test_city_move_260925 as cm

    ready(tmp_path)
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in cm._CITIES])
    store = cm._install_fake_sheets(monkeypatch)
    try:
        async def go():
            await cm._enable_cities_module()
            await cm._seed_user(cm.DELEGATE_ID, city="msk", participant_type="short")
            store.seed(await cm._resolve_tabs("msk", "short"), [[cm.DELEGATE_ID, "Тест"]])
            store.seed(await cm._resolve_tabs("spb", "short"), [])
            ids = await seed_msk_program()
            await se.enroll_tx(cm.DELEGATE_ID, ids["A"])
            await se.confirm_schedule(cm.DELEGATE_ID, CITY)
            dry = await move_user_city(cm.DELEGATE_ID, "spb", status_mode=STATUS_MODE_KEEP,
                                       by_admin=1, dry_run=True)
            assert dry["enrollments"] == 1
            assert await se.count_enrollments_for_user(cm.DELEGATE_ID) == 1
            from services.city_move import preview_city_move
            assert (await preview_city_move("short", "spb", cm.DELEGATE_ID))["enrollments"] == 1
            rep = await move_user_city(cm.DELEGATE_ID, "spb", status_mode=STATUS_MODE_KEEP,
                                       by_admin=1)
            assert rep["ok"] and "session_enrollments" in rep["db_changes"]
            assert await se.count_enrollments_for_user(cm.DELEGATE_ID) == 0
            assert await se.get_schedule_confirmed_at(cm.DELEGATE_ID, CITY) is None

        run(go())
    finally:
        cities.set_cities_for_test(saved)
