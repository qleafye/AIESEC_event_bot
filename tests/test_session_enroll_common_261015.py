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


# ── Делегатский экран ────────────────────────────────────────────────────────────────────────

from handlers.forum import session_enroll as h  # noqa: E402
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons, setup_world  # noqa: E402

NOTE_START = "🏛 Общие сессии — открытие, закрытие и другие для всех. Записываться на них не обязательно"


async def _world(common: bool):
    ids = await setup_world()
    if common:
        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on")
    return ids


async def _press(data, uid=U):
    cb = FakeCallback(data, uid)
    handler = h.se_common if data == "se:g" else {
        "se:g:": h.se_common_toggle, "se:r:": h.se_replace, "se:k:": h.se_keep,
    }[data[:5]]
    await handler(cb)
    return cb


def test_tracks_screen_shows_common_block_only_when_on(tmp_path):
    ready(tmp_path)

    async def go():
        await _world(common=False)
        msg = FakeMessage(U)
        assert await h.open_enroll(msg, U, skip_quiz=True)
        text, kb = msg.last
        assert "Общие сессии" not in text and "se:g" not in [d for _, d in buttons(kb)]

        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on")
        msg = FakeMessage(U)
        assert await h.open_enroll(msg, U, skip_quiz=True)
        text, kb = msg.last
        assert NOTE_START in text
        assert ("🏛 Общие сессии", "se:g") in buttons(kb)
    run(go())


def test_common_screen_toggle_enroll_and_unenroll(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world(common=True)
        cb = await _press("se:g")
        text, kb = cb.message.last
        assert NOTE_START in text
        labels = dict((d, t) for t, d in buttons(kb))
        assert labels[f"se:g:{ids['P']}"] == "09:00 Пленарка"
        cb = await _press(f"se:g:{ids['P']}")
        assert await edb.user_enrollment_ids(U) == {ids["P"]}
        labels = dict((d, t) for t, d in buttons(cb.message.last[1]))
        assert labels[f"se:g:{ids['P']}"] == "✅ 09:00 Пленарка"
        cb = await _press(f"se:g:{ids['P']}")
        assert await edb.user_enrollment_ids(U) == set()
    run(go())


def test_common_conflict_goes_through_replace_and_returns_to_block(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world(common=True)
        q = await db.create_program_session(CITY, "2026-10-30", "10:30", "11:30", "Закрытие")
        await se.enroll(U, ids["A"])
        cb = await _press(f"se:g:{q}")
        text, kb = cb.message.last
        assert "Сессия A" in text and "Закрытие" in text            # вопрос о замене
        assert await edb.user_enrollment_ids(U) == {ids["A"]}
        yes = next(d for t, d in buttons(kb) if d.startswith("se:r:"))
        assert yes == f"se:r:0:-1:{q}"
        cb = await _press(yes)
        assert await edb.user_enrollment_ids(U) == {q}
        assert NOTE_START in cb.message.last[0]                      # вернулись в блок общих
        # «Оставить как было» тоже возвращает в блок общих
        cb = await _press("se:k:0:-1")
        assert NOTE_START in cb.message.last[0]
    run(go())


def test_common_toggle_off_refuses(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world(common=False)
        cb = await _press(f"se:g:{ids['P']}")
        assert cb.alerts[-1][1] is True
        assert await edb.user_enrollment_ids(U) == set()
    run(go())


def test_schedule_text_marks_common(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _world(common=True)
        await se.enroll(U, ids["P"])
        ctx, _ = await h._ctx_for(FakeMessage(U), U)
        text = await h._schedule_text(ctx)
        assert "09:00–10:00 Пленарка" in text and "Пленарка (для всех)" not in text
    run(go())


# ── Админка ──────────────────────────────────────────────────────────────────────────────────

from handlers.forum import admin_enroll, admin_enroll_list as el  # noqa: E402
from handlers.forum import admin_program  # noqa: E402
from tests.test_admin_enroll_38 import FakeCallback as AdminCallback, cbs, texts  # noqa: E402


def test_admin_toggle_button_switches_setting(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        await db.set_setting("event_city_enabled", "on")
    run(go())
    cb = AdminCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "🏛 Общие сессии: запись по желанию — ❌ выкл" in texts(cb.message.edit_markup)
    assert "prog_enrco:msk" in cbs(cb.message.edit_markup)
    run(el.prog_enrco(AdminCallback("prog_enrco:msk")))
    assert run(se.common_enabled(CITY)) is True
    cb = AdminCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "🏛 Общие сессии: запись по желанию — ✅ вкл" in texts(cb.message.edit_markup)
    run(el.prog_enrco(AdminCallback("prog_enrco:msk")))
    assert run(se.common_enabled(CITY)) is False


def test_admin_note_text_is_editable(tmp_path):
    from services.forum.session_enroll import ENROLL_TEXT_KEYS
    assert "session_enroll_common_note" in ENROLL_TEXT_KEYS


def test_admin_card_and_list_for_common_session(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=False)
        return ids
    ids = run(go())
    text, _kb = run(admin_program.render_session_card(ids["P"]))
    assert "Запись: открыта · без лимита · записано 0" in text      # как раньше
    assert not [t for t in texts(run(el.render_enroll_list(CITY, 0))[1]) if "Пленарка" in t]

    run(db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on"))
    run(se.enroll(U, ids["P"]))
    text, _kb = run(admin_program.render_session_card(ids["P"]))
    assert "Запись по желанию: открыта · без лимита · 1 записались" in text
    _head, kb = run(el.render_enroll_list(CITY, 0))
    assert any("Пленарка" in t and "🏛 общая" in t and t.endswith("— 1") for t in texts(kb))
    # закрытие записи и лимит доступны в карточке общей сессии
    _t, card_kb = run(admin_enroll.render_enroll_card(run(db.get_program_session(ids["P"]))))
    assert f"prog_enrcl:{ids['P']}" in cbs(card_kb) and f"prog_enrlim:{ids['P']}" in cbs(card_kb)


def test_admin_switch_track_to_common_keeps_enrollments_when_on(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        await se.enroll(U, ids["A"])
        return ids
    ids = run(go())
    cb = AdminCallback(f"prog_enrtrk:{ids['A']}:0")
    run(admin_enroll.prog_enrtrk(cb))
    session = run(db.get_program_session(ids["A"]))
    assert session["track_id"] is None                              # без подтверждения
    assert run(edb.count_enrollments(ids["A"])) == 1                # запись осталась

    # тумблер выключен — прежнее поведение: подтверждение и удаление записей
    run(db.set_setting(per_city_key("session_enroll_common_optional", CITY), "off"))
    run(se.common_enabled(CITY))
    run(db.update_program_session(ids["B"], track_id=ids["business"]))
    run(edb.enroll_tx(102, ids["B"]))
    cb = AdminCallback(f"prog_enrtrk:{ids['B']}:0")
    run(admin_enroll.prog_enrtrk(cb))
    assert "записи на эту сессию удалятся: 1" in cb.message.text_edited
    assert run(db.get_program_session(ids["B"]))["track_id"] == ids["business"]


# ── Фильтры рассылки ─────────────────────────────────────────────────────────────────────────

from handlers.comms import admin_broadcast_enroll_filter as ef  # noqa: E402
from handlers.comms import admin_broadcasts as ab  # noqa: E402
from handlers.states import Broadcast  # noqa: E402
from tests._enroll38 import ADMIN_ID  # noqa: E402
from tests.test_roles_phase8 import FakeCallback as RolesCallback, FakeMessage as RolesMessage, _fresh_state  # noqa: E402


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_broadcast_filter_lists_common_sessions_only_when_on(tmp_path):
    ready(tmp_path)

    async def go():
        sid = await db.create_program_session(CITY, DAY, "09:00", "10:00", "Открытие")
        await db.set_setting("event_city_enabled", "on")

        async def menu():
            msg = RolesMessage()
            await ab._render_filter_menu(msg, [], edit=False)
            return _flat(msg.answers[-1][2])

        assert "enrf_start:in" not in await menu()               # тумблер выключен
        await db.set_setting(per_city_key("session_enroll_common_optional", CITY), "on")
        assert "enrf_start:in" in await menu()
        state = _fresh_state(ADMIN_ID)
        await state.update_data(filters=[])
        await state.set_state(Broadcast.filter_field)
        await ef.enrf_start(RolesCallback("enrf_start:in", ADMIN_ID), state)
        cb = RolesCallback(f"enrf_day:{DAY}", ADMIN_ID)
        await ef.enrf_day_pick(cb, state)
        assert f"enrf_pick:{sid}" in _flat(cb.message.markup)
        return sid
    run(go())


def test_filter_none_ignores_common_enrollments(tmp_path):
    from tests.test_broadcast_filters_sql_38 import _ids

    ready(tmp_path)

    async def go():
        ids = await _setup(common=True)
        await se.enroll(U, ids["P"])                              # только на общую
        await se.enroll(102, ids["A"])                            # на трековую
        spec = [{"field": "session_enroll", "value": db.SESSION_ENROLL_NONE, "city": CITY}]
        got = await _ids(spec)
        assert U in got and 102 not in got
        # фильтр «записан на сессию» по общей сессии работает
        got_in = await _ids([{"field": "session_enroll", "value": db.SESSION_ENROLL_IN,
                              "session_id": ids["P"]}])
        assert U in got_in
    run(go())
