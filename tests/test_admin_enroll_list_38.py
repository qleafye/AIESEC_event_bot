"""«📋 Записи на сессии», выгрузка, настройки модуля, тексты, строка хаба."""
from __future__ import annotations

import csv
import io

from database import db, session_enroll_db as edb
from handlers.forum import admin_enroll_list as el
from handlers.forum import admin_forum_functions as aff
from handlers.states import EditSetting
from services.session_enroll import ENROLL_TEXT_KEYS
from tests._enroll38 import ADMIN_ID, CITY, DAY, add_user, ready, run, seed_msk_program
from tests.test_admin_enroll_38 import FakeCallback, cbs, new_state, texts


async def _many_sessions():
    ids = await seed_msk_program()
    for i in range(6):  # 4 с треком из seed + 6 = 10
        sid = await db.create_program_session(CITY, DAY, f"{13 + i}:00", f"{13 + i}:30", f"Доп {i}")
        await db.update_program_session(sid, track_id=ids["career"])
    return ids


def test_list_paginated_with_counts(tmp_path):
    ready(tmp_path)

    async def setup():
        ids = await _many_sessions()
        await db.update_program_session(ids["A"], enroll_limit=30)
        for tid in (101, 102, 103):
            await add_user(tid)
            await edb.enroll_tx(tid, ids["A"])
        await edb.confirm_schedule(101, CITY)
        await db.update_program_session(ids["B"], enroll_closed=1)

    run(setup())
    cb = FakeCallback("prog_enrl:msk:0")
    run(el.prog_enrl(cb))
    text = cb.message.text_edited
    assert "Записались: 3 чел., подтвердили расписание: 1" in text
    labels = texts(cb.message.edit_markup)
    assert any("10:00 Карьера" in t and "3/30" in t for t in labels)
    assert any(t.startswith("🔒 ") and "Бизнес" in t for t in labels)
    session_rows = [c for c in cbs(cb.message.edit_markup) if c.startswith("prog_enrx:")]
    assert len(session_rows) == 8
    assert "prog_enrl:msk:1" in cbs(cb.message.edit_markup)
    cb2 = FakeCallback("prog_enrl:msk:1")
    run(el.prog_enrl(cb2))
    assert len([c for c in cbs(cb2.message.edit_markup) if c.startswith("prog_enrx:")]) == 2


def test_list_toggle_in_row(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    cb = FakeCallback(f"prog_enrlt:{ids['A']}:0")
    run(el.prog_enrlt(cb))
    assert run(db.get_program_session(ids["A"]))["enroll_closed"]
    assert any(t == "🔓" for t in texts(cb.message.edit_markup))
    run(el.prog_enrlt(FakeCallback(f"prog_enrlt:{ids['A']}:0")))
    assert not run(db.get_program_session(ids["A"]))["enroll_closed"]


def test_export_csv(tmp_path):
    ready(tmp_path)

    async def setup():
        ids = await seed_msk_program()
        await add_user(101)
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET full_name = '=1+1' WHERE telegram_id = 101")
            await conn.commit()
        await edb.enroll_tx(101, ids["A"])
        return ids

    ids = run(setup())
    cb = FakeCallback(f"prog_enrx:{ids['A']}")
    run(el.prog_enrx(cb))
    doc, _caption = cb.message.documents[0]
    raw = doc.data if hasattr(doc, "data") else doc.read()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 2 and rows[1][0] == "101" and rows[1][1] == "'=1+1"
    assert doc.filename == f"enroll_{ids['A']}.csv"

    empty = FakeCallback(f"prog_enrx:{ids['B']}")
    run(el.prog_enrx(empty))
    assert empty.answers[0] == ("На эту сессию пока никто не записан.", True)
    assert not empty.message.documents


def test_settings_screen_and_switch(tmp_path):
    ready(tmp_path)
    cb = FakeCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "Запись на сессии: ❌ Выкл" in cb.message.text_edited
    assert "дата не задана" in cb.message.text_edited
    run(el.prog_enrsw(FakeCallback("prog_enrsw:msk")))
    cb = FakeCallback("prog_enrset:msk")
    run(el.prog_enrset(cb))
    assert "Запись на сессии: ✅ Вкл" in cb.message.text_edited
    assert run(db.get_setting("session_enroll_enabled")) == "on"  # модуль городов выключен — голый ключ


def test_switch_uses_per_city_key_when_cities_on(tmp_path):
    ready(tmp_path)
    run(db.set_setting("event_city_enabled", "on"))
    run(el.prog_enrsw(FakeCallback("prog_enrsw:msk")))
    from domain.cities import per_city_key
    assert run(db.get_setting(per_city_key("session_enroll_enabled", "msk"))) == "on"


def test_deadline_edit_uses_general_input(tmp_path):
    ready(tmp_path)
    state = new_state()
    cb = FakeCallback("prog_enrdl:msk")
    run(el.prog_enrdl(cb, state))
    assert run(state.get_state()) == EditSetting.waiting_for_value.state
    assert (run(state.get_data()))["setting_key"] == "session_enroll_deadline"
    assert "28.10.2026 23:59" in cb.message.answers_sent[0]


def test_texts_list_and_edit(tmp_path):
    ready(tmp_path)
    cb = FakeCallback("prog_enrtx:msk:0")
    run(el.prog_enrtx(cb))
    labels = texts(cb.message.edit_markup)
    assert "вступление" in labels[0]
    assert not any("session_enroll" in t for t in labels)
    assert "prog_enrtx:msk:1" in cbs(cb.message.edit_markup)

    idx = ENROLL_TEXT_KEYS.index("session_enroll_slot_text")
    state = new_state()
    cb = FakeCallback(f"prog_enrte:msk:{idx}")
    run(el.prog_enrte(cb, state))
    assert run(state.get_state()) == EditSetting.waiting_for_value.state
    assert (run(state.get_data()))["setting_key"] == "session_enroll_slot_text"
    assert "{day}" in cb.message.text_edited
    bad = FakeCallback("prog_enrte:msk:999")
    run(el.prog_enrte(bad, new_state()))
    assert bad.answers[0][1] is True


def test_hub_line(tmp_path):
    ready(tmp_path)
    text, kb = run(aff._render_hub(ADMIN_ID, "msk"))
    assert "📅 Запись на сессии: ❌ Выкл" in text
    assert "prog_enrset:msk" in cbs(kb)
