"""Программа форума в админке перед форумами 03.10: фото программы можно убрать, общее фото у
города с сессиями честно названо невидимым, подсказки дней — только дни форума города, двойной
тап по залу в мастере сессии не задаёт вопрос «Спикер» дважды.

Конвенция соседей: Fake-объекты из `tests/test_admin_program_260924.py` и
`tests/test_program_photo_city_261003.py`, БД — tmp_path (`fast_init_db`), `asyncio.run()`."""
from __future__ import annotations

import asyncio

from domain.cities import per_city_key
from database import db
from handlers import admin_program, admin_program_view
from handlers.states import ProgramSessionField
from tests import test_admin_program_260924 as tap
from tests import test_program_photo_city_261003 as tpp


def _run(coro):
    return asyncio.run(coro)


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_remove_city_photo_with_confirmation(tmp_path):
    tpp._ready(tmp_path)
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB"))
    _run(db.set_setting(per_city_key("program_caption", "spb"), "Подпись"))
    _status, rows = _run(admin_program_view.program_rows("spb", "program"))
    assert "prog_photo_del:spb:program" in [b.callback_data for row in rows for b in row]

    ask = tpp._Callback("prog_photo_del:spb:program")
    _run(admin_program_view.prog_photo_del_ask(ask))
    assert "Убрать фото программы" in ask.message.edited
    assert _run(db.get_setting(per_city_key("program_photo_file_id", "spb"))) == "SPB"  # ещё не удалено

    go = tpp._Callback("prog_photo_delgo:spb:program")
    _run(admin_program_view.prog_photo_del_go(go))
    assert not _run(db.get_setting(per_city_key("program_photo_file_id", "spb")))
    assert not _run(db.get_setting(per_city_key("program_caption", "spb")))
    _status, rows = _run(admin_program_view.program_rows("spb", "program"))
    assert not any((b.callback_data or "").startswith("prog_photo_del") for row in rows for b in row)


def test_bound_manager_cannot_remove_other_city_photo(tmp_path):
    tpp._ready(tmp_path)
    _run(db.add_staff(tpp.MANAGER_ID, "reg_manager", tpp.SUPERADMIN_ID))
    _run(db.set_staff_city(tpp.MANAGER_ID, "msk"))
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB"))
    go = tpp._Callback("prog_photo_delgo:spb:program", user_id=tpp.MANAGER_ID)
    _run(admin_program_view.prog_photo_del_go(go))
    assert go.answers and go.answers[0][1] is True
    assert _run(db.get_setting(per_city_key("program_photo_file_id", "spb"))) == "SPB"


def test_shared_photo_hidden_by_sessions_is_named(tmp_path):
    tpp._ready(tmp_path)
    _run(db.set_setting("program_photo_file_id", "SHARED"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    status, _rows = _run(admin_program_view.program_rows("spb", "program"))
    assert "загрузите фото для города" in status


def test_one_day_forum_suggests_only_its_day(tmp_path):
    tpp._ready(tmp_path)
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _run(db.set_setting("sos_active_days__city__spb", "1"))
    _text, kb = _run(admin_program.render_city_program_screen(tpp.SUPERADMIN_ID, "spb"))
    days = [c for c in _cbs(kb) if c.startswith("prog_day:")]
    assert days == ["prog_day:spb:2026-10-03"]
    _run(db.set_setting("sos_active_days__city__spb", "2"))
    _text, kb = _run(admin_program.render_city_program_screen(tpp.SUPERADMIN_ID, "spb"))
    assert [c for c in _cbs(kb) if c.startswith("prog_day:")] == [
        "prog_day:spb:2026-10-03", "prog_day:spb:2026-10-04",
    ]


def test_double_tap_on_hall_asks_speaker_once(tmp_path):
    tap._ready(tmp_path)
    state = tap._new_state(tap.SUPERADMIN_ID)
    _hall_text, _kb, first = _run(tap._create_session_via_wizard(state))
    assert _run(state.get_state()) == ProgramSessionField.speaker.state
    second = tap._FakeCallback("prog_hp:w:none", user_id=tap.SUPERADMIN_ID)
    _run(admin_program.prog_hp_pick(second, state))
    assert second.message.answers_sent == []  # второй «Спикер…» не задан
    assert _run(state.get_state()) == ProgramSessionField.speaker.state
