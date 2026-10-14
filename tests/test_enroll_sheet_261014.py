"""Вкладки Google-таблицы «Записи на сессии» / «Кто куда записан»: сборщики строк, читалки БД,
реестр, пересборка, автосинхрон и кнопка в админке."""
from __future__ import annotations

from database import db, session_enroll_db as edb
from shared import enroll_stats
from tests._enroll38 import CITY, DAY, add_user, ready, run, seed_msk_program

LABELS = {"msk": "Москва"}


# ── Чистые сборщики ──────────────────────────────────────────────────────────────────────────

def _row(**kw):
    base = {
        "city": "msk", "day": DAY, "start_time": "10:00", "end_time": "11:00",
        "title": "Сессия", "speaker": None, "hall_name": "Зал 1", "track_name": "Карьера",
        "enroll_limit": 30, "enrolled": 3, "arrived": None,
    }
    base.update(kw)
    return base


def test_sessions_rows_limit_free_and_time():
    [r] = enroll_stats.sessions_sheet_rows([_row()], LABELS)
    assert r == ["Москва", "30.10 (пт)", "10:00–11:00", "Сессия", "Зал 1", "Карьера", "", 3, 30, 27, ""]
    assert len(r) == len(enroll_stats.SESSION_HEADERS)


def test_sessions_rows_no_limit_and_overfull():
    [no_limit] = enroll_stats.sessions_sheet_rows([_row(enroll_limit=None)], LABELS)
    assert no_limit[8] == "" and no_limit[9] == ""
    [over] = enroll_stats.sessions_sheet_rows([_row(enroll_limit=2, enrolled=5, arrived=4)], LABELS)
    assert over[9] == 0 and over[10] == 4


def test_unknown_city_falls_back_to_code():
    [r] = enroll_stats.sessions_sheet_rows([_row(city="zzz")], LABELS)
    assert r[0] == "zzz"


def test_stamp_short():
    assert enroll_stats.stamp_short("2026-10-14 18:54:03") == "14.10 18:54"
    assert enroll_stats.stamp_short(None) == ""
    assert enroll_stats.stamp_short("мусор") == "мусор"


def test_people_rows_format_and_sort():
    base = {"city": "msk", "day": DAY, "start_time": "10:00", "end_time": "11:00",
            "title": "Сессия A", "hall_name": "Зал 1", "enrolled_at": "2026-10-14 18:54:03"}
    rows = [
        dict(base, telegram_id=2, full_name="борис", username="bor"),
        dict(base, telegram_id=1, full_name="Анна", username=None, start_time="12:00"),
        dict(base, telegram_id=3, full_name="Анна", username="anna", start_time="09:00"),
    ]
    out = enroll_stats.people_sheet_rows(rows, LABELS)
    assert [r[2] for r in out] == ["3", "1", "2"]
    assert out[0][:2] == ["Анна", "@anna"]
    assert out[1][1] == ""
    assert out[0][5] == "09:00–11:00" and out[0][8] == "14.10 18:54"
    assert len(out[0]) == len(enroll_stats.PEOPLE_HEADERS)


# ── Читалки БД ───────────────────────────────────────────────────────────────────────────────

async def _scan(tid, sid):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
            "VALUES (?, ?, '2026-10-30 10:05:00', 'scan', '2026-10-30 10:05:00', ?)",
            (tid, f"session:{sid}", DAY),
        )
        await conn.commit()


def test_overview_rows_skip_plenary_and_count(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await seed_msk_program()
        await add_user(11)
        await add_user(12)
        await edb.enroll_tx(11, ids["A"])
        await edb.enroll_tx(12, ids["A"])
        await _scan(11, ids["A"])
        rows = await edb.enroll_overview_rows()
        by_id = {r["id"]: r for r in rows}
        assert ids["P"] not in by_id
        assert set(by_id) == {ids["A"], ids["B"], ids["C"], ids["D"]}
        assert by_id[ids["A"]]["enrolled"] == 2
        assert by_id[ids["A"]]["arrived"] == 1
        assert by_id[ids["B"]]["arrived"] == 0
        assert len(await edb.enroll_overview_rows(CITY)) == 4
        assert await edb.enroll_overview_rows("spb") == []
        people = await edb.enroll_people_rows()
        assert {p["telegram_id"] for p in people} == {11, 12}
        assert await edb.enroll_people_rows("spb") == []

    run(go())


# ── Реестр ───────────────────────────────────────────────────────────────────────────────────

def test_registry_keys():
    from domain.settings import ops
    from domain.settings.schema import SETTINGS_SCHEMA

    for key, default in (("enroll_sessions_tab", "Записи на сессии"),
                         ("enroll_people_tab", "Кто куда записан")):
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "sheets" and entry["type"] == "text"
        assert entry["default"] == default
        assert ops.SHEET_TAB_WRITE_MODE[key] == "rewrite"
        assert key in ops.SHEET_TAB_NAME_KEYS


# ── Пересборка вкладок ───────────────────────────────────────────────────────────────────────

def _fake_sync(calls, fail_title=None):
    async def fake(title, headers, rows):
        calls.append((title, headers, rows))
        return -1 if title == fail_title else len(rows)
    return fake


def test_rebuild_writes_both_tabs_with_default_names(tmp_path, monkeypatch):
    from services.forum import enroll_sheets

    ready(tmp_path)
    calls = []
    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", _fake_sync(calls))

    async def go():
        ids = await seed_msk_program()
        await add_user(11)
        await edb.enroll_tx(11, ids["A"])
        return await enroll_sheets.rebuild_enroll_sheets()

    assert run(go()) == (4, 1)
    assert [c[0] for c in calls] == ["Записи на сессии", "Кто куда записан"]
    assert calls[0][1] == enroll_stats.SESSION_HEADERS
    assert calls[1][1] == enroll_stats.PEOPLE_HEADERS
    assert calls[1][2][0][0] == "Делегат 11"


def test_rebuild_uses_renamed_tabs_and_survives_one_failure(tmp_path, monkeypatch):
    from services.forum import enroll_sheets

    ready(tmp_path)
    calls = []
    monkeypatch.setattr("services.sheets.sheets.sync_named_worksheet", _fake_sync(calls, "Люди"))

    async def go():
        await seed_msk_program()
        await db.set_setting("enroll_sessions_tab", "Сессии")
        await db.set_setting("enroll_people_tab", "Люди")
        return await enroll_sheets.rebuild_enroll_sheets()

    assert run(go()) == (4, -1)
    assert [c[0] for c in calls] == ["Сессии", "Люди"]


# ── Хуки автосинхрона ────────────────────────────────────────────────────────────────────────

def test_hooks_fire_only_on_real_changes(tmp_path, monkeypatch):
    ready(tmp_path)
    fired = []
    monkeypatch.setattr(
        "services.forum.enroll_sheets.request_enroll_sheet_resync", lambda: fired.append(1)
    )

    async def go():
        ids = await seed_msk_program()
        await add_user(11)
        fired.clear()
        assert (await edb.enroll_tx(11, ids["A"])).status == "ok"
        assert len(fired) == 1
        assert (await edb.enroll_tx(11, ids["A"])).status == "already"
        assert (await edb.enroll_tx(11, ids["B"])).status == "conflict"
        assert len(fired) == 1
        assert await edb.unenroll(11, ids["A"])
        assert len(fired) == 2
        assert not await edb.unenroll(11, ids["A"])
        assert len(fired) == 2
        assert await db.update_program_session(ids["A"], title="Новое")
        assert len(fired) == 3
        assert await db.delete_program_session(ids["D"])
        assert len(fired) == 4

    run(go())


# ── Кнопка в админке ─────────────────────────────────────────────────────────────────────────

def test_settings_screen_has_sheet_button(tmp_path):
    from handlers.forum import admin_enroll_list as el
    from tests.test_admin_enroll_38 import FakeCallback, cbs

    ready(tmp_path)
    cb = FakeCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "prog_enrsh:msk" in cbs(cb.message.edit_markup)
    assert "Записи на сессии" in cb.message.text_edited


def test_confirm_and_report(tmp_path, monkeypatch):
    from handlers.forum import admin_enroll_list as el
    from tests.test_admin_enroll_38 import FakeCallback, cbs

    ready(tmp_path)
    cb = FakeCallback("prog_enrsh:msk")
    run(el.prog_enrsh(cb))
    text = cb.message.text_edited
    assert "Записи на сессии" in text and "Кто куда записан" in text
    assert "перезапишутся целиком" in text
    assert "prog_enrshgo:msk" in cbs(cb.message.edit_markup)

    async def ok():
        return 4, 7

    monkeypatch.setattr(el, "rebuild_enroll_sheets", ok)
    cb = FakeCallback("prog_enrshgo:msk")
    run(el.prog_enrshgo(cb))
    assert "«Записи на сессии»: 4 строк." in cb.message.text_edited
    assert "«Кто куда записан»: 7 строк." in cb.message.text_edited

    async def bad():
        return -1, 2

    monkeypatch.setattr(el, "rebuild_enroll_sheets", bad)
    cb = FakeCallback("prog_enrshgo:msk")
    run(el.prog_enrshgo(cb))
    assert "не удалось записать" in cb.message.text_edited
    assert "«Кто куда записан»: 2 строк." in cb.message.text_edited
