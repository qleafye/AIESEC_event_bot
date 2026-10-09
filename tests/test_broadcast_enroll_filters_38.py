"""Мастер фильтров рассылки «Записан на сессию…», «Не записался ни на одну», «Не прошёл тест»."""
from __future__ import annotations

from database import db, quiz_db, session_enroll_db
from handlers import admin_broadcast_enroll_filter as ef
from handlers import admin_broadcasts as ab
from handlers.states import Broadcast
from tests._enroll38 import ADMIN_ID, CITY, DAY, add_user, ready, run, seed_delegates, seed_msk_program
from tests.test_roles_phase8 import FakeCallback, FakeMessage, _fresh_state


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _menu(filters=None):
    msg = FakeMessage()
    run(ab._render_filter_menu(msg, filters or [], edit=False))
    return _flat(msg.answers[-1][2])


def test_menu_shows_enroll_buttons_only_with_tracks(tmp_path):
    ready(tmp_path)
    flat = _menu()
    assert "enrf_start:in" not in flat and "enrf_start:none" not in flat
    assert "enrf_start:quiz" not in flat
    # сессия без трека — кнопок записи по-прежнему нет
    run(db.create_program_session(CITY, DAY, "09:00", "10:00", "Пленарка"))
    assert "enrf_start:in" not in _menu()
    run(seed_msk_program())
    flat = _menu()
    assert "enrf_start:in" in flat and "enrf_start:none" in flat
    assert "enrf_start:quiz" not in flat
    run(quiz_db.get_or_create_quiz(CITY))
    assert "enrf_start:quiz" in _menu()


def _state(filters=None):
    state = _fresh_state(ADMIN_ID)
    run(state.update_data(filters=filters or []))
    run(state.set_state(Broadcast.filter_field))
    return state


def test_enrf_in_flow(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    state = _state()
    cb = FakeCallback("enrf_start:in", ADMIN_ID)
    run(ef.enrf_start(cb, state))
    assert any(f"enrf_day:{DAY}" == d for d in _flat(cb.message.markup))
    cb2 = FakeCallback(f"enrf_day:{DAY}", ADMIN_ID)
    run(ef.enrf_day_pick(cb2, state))
    picks = [d for d in _flat(cb2.message.markup) if d.startswith("enrf_pick:")]
    # только сессии с треком: A B C D, пленарки нет
    assert sorted(picks) == sorted(f"enrf_pick:{ids[k]}" for k in "ABCD")
    cb3 = FakeCallback(f"enrf_pick:{ids['A']}", ADMIN_ID)
    run(ef.enrf_session_pick(cb3, state))
    f = run(state.get_data())["filters"]
    assert len(f) == 1
    assert f[0]["field"] == "session_enroll" and f[0]["value"] == "in"
    assert f[0]["session_id"] == ids["A"] and f[0]["label"].startswith("Записан на «Сессия A»")


def test_enrf_none_and_quiz(tmp_path):
    ready(tmp_path)
    run(seed_msk_program())
    run(quiz_db.get_or_create_quiz(CITY))
    state = _state()
    run(ef.enrf_start(FakeCallback("enrf_start:none", ADMIN_ID), state))
    run(ef.enrf_start(FakeCallback("enrf_start:quiz", ADMIN_ID), state))
    f = run(state.get_data())["filters"]
    assert f[0]["field"] == "session_enroll" and f[0]["value"] == "none" and f[0]["city"] == CITY
    assert f[1]["field"] == "quiz" and f[1]["value"] == "not_passed" and f[1]["city"] == CITY
    assert all(x["label"] for x in f)


def test_enrf_pick_bad_and_deleted(tmp_path):
    ready(tmp_path)
    run(seed_msk_program())
    state = _state()
    run(ef.enrf_start(FakeCallback("enrf_start:in", ADMIN_ID), state))
    cb = FakeCallback("enrf_pick:abc", ADMIN_ID)
    run(ef.enrf_session_pick(cb, state))
    assert cb.answers[-1][1] is True
    cb2 = FakeCallback("enrf_pick:99999", ADMIN_ID)
    run(ef.enrf_session_pick(cb2, state))
    assert cb2.answers[-1][1] is True and run(state.get_data())["filters"] == []


def test_enrf_wrong_state_is_refused(tmp_path):
    ready(tmp_path)
    run(seed_msk_program())
    state = _state()
    cb = FakeCallback("enrf_pick:1", ADMIN_ID)
    run(ef.enrf_session_pick(cb, state))
    assert cb.answers[-1][1] is True


def test_end_to_end_count(tmp_path):
    ready(tmp_path)
    sess = run(seed_msk_program())
    ids = run(seed_delegates())
    run(session_enroll_db.enroll_tx(ids["cur1"], sess["A"]))
    state = _state()
    run(ef.enrf_start(FakeCallback("enrf_start:in", ADMIN_ID), state))
    run(ef.enrf_day_pick(FakeCallback(f"enrf_day:{DAY}", ADMIN_ID), state))
    run(ef.enrf_session_pick(FakeCallback(f"enrf_pick:{sess['A']}", ADMIN_ID), state))
    filters = run(state.get_data())["filters"]
    expected = run(db.count_and_list_filtered(filters))
    assert expected == [ids["cur1"]]
    cb = FakeCallback("filter_count", ADMIN_ID)
    run(ab.filter_count(cb, state))
    assert f"<b>{len(expected)}</b>" in cb.message.text
    assert "Запись на сессии" in cb.message.text

    run(state.update_data(filters=[]))
    run(ef.enrf_start(FakeCallback("enrf_start:none", ADMIN_ID), state))
    filters = run(state.get_data())["filters"]
    none_ids = run(db.count_and_list_filtered(filters))
    assert ids["cur1"] not in none_ids and ids["cur2"] in none_ids


def test_callback_bytes(tmp_path):
    ready(tmp_path)
    run(seed_msk_program())
    run(quiz_db.get_or_create_quiz(CITY))
    run(add_user(1))
    state = _state()
    seen = []
    for data, fn in (("enrf_start:in", ef.enrf_start),):
        cb = FakeCallback(data, ADMIN_ID)
        run(fn(cb, state))
        seen += _flat(cb.message.markup)
    cb = FakeCallback(f"enrf_day:{DAY}", ADMIN_ID)
    run(ef.enrf_day_pick(cb, state))
    seen += _flat(cb.message.markup)
    seen += _menu()
    assert seen and all(len(s.encode()) <= 64 for s in seen)
