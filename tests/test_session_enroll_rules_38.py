"""Правила записи на сессии: гейты, дедлайн, замена, слоты, расписание, подсказка сканера."""
from datetime import datetime

from domain.cities import per_city_key
from database import db, session_enroll_db
from services.forum import session_enroll as se
from tests._enroll38 import CITY, add_user, ready, run, seed_delegates, seed_msk_program

U = 101  # approved, текущий сезон, msk


async def _setup(enabled=True):
    ids = await seed_msk_program()
    await seed_delegates()
    await db.set_setting("event_city_enabled", "on")
    if enabled:
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
    return ids


def test_module_disabled(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(enabled=False)
        r = await se.enroll(U, ids["A"])
        assert r.status == "disabled" and r.text_key == "session_enroll_disabled_text"
        assert await se.enroll_by_staff(U, ids["A"], by_staff_id=1)
    run(go())


def test_gate_not_approved_and_past_season(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        r = await se.enroll(104, ids["A"])
        assert r.status == "denied" and r.text_key == "session_enroll_not_approved_text"
        r = await se.enroll(105, ids["A"])
        assert r.status == "denied" and r.denial == "past_season"
    run(go())


def test_wrong_city(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await add_user(201, city="spb")
        assert (await se.enroll(201, ids["A"])).status == "wrong_city"
    run(go())


def test_closed_and_full(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await db.update_program_session(ids["A"], enroll_closed=1)
        r = await se.enroll(U, ids["A"])
        assert r.status == "closed" and r.text_key == "session_enroll_err_closed"
        await db.update_program_session(ids["B"], enroll_limit=1)
        assert (await se.enroll(102, ids["B"])).status == "ok"
        r = await se.enroll(U, ids["B"])
        assert r.status == "full" and r.text_key == "session_enroll_err_full"
    run(go())


def test_deadline_city_time(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await db.set_setting(per_city_key("session_enroll_deadline", CITY), "28.10.2026 23:59")
        now = {"v": datetime(2026, 10, 29, 0, 0)}

        async def fake_now(city):
            return now["v"]
        monkeypatch.setattr(se, "city_now", fake_now)
        assert (await se.enroll(U, ids["A"])).status == "deadline"
        assert (await se.unenroll(U, ids["A"])).status == "deadline"
        assert await se.my_schedule(U, CITY)
        now["v"] = datetime(2026, 10, 28, 12, 0)
        assert (await se.enroll(U, ids["A"])).status == "ok"
        assert await se.deadline_label(CITY) == "28.10.2026 23:59"
    run(go())


def test_garbage_deadline_means_none(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await db.set_setting(per_city_key("session_enroll_deadline", CITY), "мусор")
        assert await se.deadline_for(CITY) is None
        assert (await se.enroll(U, ids["A"])).status == "ok"
    run(go())


def test_conflict_then_confirm_replace(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        assert (await se.enroll(U, ids["A"])).status == "ok"
        r = await se.enroll(U, ids["B"])
        assert r.status == "conflict" and r.conflicts[0]["title"] == "Сессия A"
        r = await se.enroll(U, ids["B"], confirm_replace=True)
        assert r.status == "ok" and r.replaced[0]["title"] == "Сессия A"
        assert (await se.enroll(U, ids["B"])).status == "already"
    run(go())


def test_slots_exclude_plenary(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await session_enroll_db.enroll_tx(U, ids["A"])
        days = await se.slots_for_city(CITY)
        flat = [s for d in days for slot in d["slots"] for s in slot]
        assert ids["P"] not in {s["id"] for s in flat}
        first = days[0]["slots"][0]
        assert {s["id"] for s in first} >= {ids["A"], ids["B"]}
        a = next(s for s in flat if s["id"] == ids["A"])
        assert a["enrolled"] == 1 and a["open_state"] == "open" and a["track_name"] == "Карьера"
    run(go())


def test_my_schedule_common_and_chosen(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await se.enroll(U, ids["A"])
        sched = await se.my_schedule(U, CITY)
        items = sched[0]["items"]
        assert [(i["session"]["id"], i["kind"]) for i in items] == [
            (ids["P"], "common"), (ids["A"], "chosen")]
    run(go())


def test_confirm_after_change_allowed(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await se.enroll(U, ids["A"])
        assert await se.confirm(U, CITY)
        assert (await se.enroll(U, ids["D"])).status == "ok"
    run(go())


def test_scan_hint_variants(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        user = await db.get_user(U)
        await se.enroll(U, ids["A"])
        assert await se.scan_hint(user, ids["A"]) is None
        h = await se.scan_hint(user, ids["B"])
        assert "Сессия A" in h["hint"]
        assert h["enroll"]["action"] == "rebook" and h["enroll"]["session_id"] == ids["B"]
        assert h["enroll"]["label"] == "Перезаписать на эту"
        h = await se.scan_hint(user, ids["D"])
        assert h["enroll"]["action"] == "book"
        assert h["hint"] == "Не записан на сессию в это время"
        assert await se.scan_hint(user, ids["P"]) is None
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "off")
        assert await se.scan_hint(user, ids["B"]) is None
    run(go())


def test_enroll_by_staff_ignores_closed_and_limit(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup()
        await se.enroll(U, ids["A"])
        await db.update_program_session(ids["B"], enroll_closed=1, enroll_limit=1)
        await session_enroll_db.enroll_tx(102, ids["B"])
        r = await se.enroll_by_staff(U, ids["B"], by_staff_id=77)
        assert r.status == "ok" and r.replaced[0]["id"] == ids["A"]
        rows = await session_enroll_db.list_user_enrollments(U)
        assert rows[0]["enroll_source"] == "scan"
        async with db._connect() as conn:
            async with conn.execute("SELECT by_staff_id FROM session_enrollments "
                                    "WHERE telegram_id = ?", (U,)) as cur:
                assert (await cur.fetchone())[0] == 77
    run(go())


def test_no_aiogram_import():
    import os
    import subprocess
    import sys
    env = {**os.environ, "BOT_TOKEN": "123456:dummy-test-token", "ADMIN_IDS": "[1]"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import services.forum.session_enroll; print('aiogram' in sys.modules)"],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()
    assert out == "False"
