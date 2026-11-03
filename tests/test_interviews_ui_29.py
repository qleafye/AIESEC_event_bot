"""Экраны записи на интервью: менеджер, сетка делегата, галочка в рассылке.

Пока запись выключена, подтверждение рассылки остаётся прежним: галочки нет.
Имена записанных делегат не видит. Чужой город менеджер не удаляет.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from database import db, interview_db
from handlers.comms import admin_broadcast_interview as ivbc
from handlers.comms import admin_broadcasts
from handlers.settings import admin_sections as sec
from keyboards.builders import MENU_BUTTONS
from services import scheduler
from tests._enroll38 import add_user, ready, run

import handlers.admin_interviews as admin_iv
import handlers.interviews as delegate_iv

_HINT = "Пример: 28.10.2026 10:00. «-» — убрать дату, запись закроется."
_NEED_YES = "Записаться можно, если в анкете на кейс-чемпионат выбран ответ «Да»."
_ROOT = Path(__file__).resolve().parents[1]


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid=1, text=""):
        self.from_user = _User(uid)
        self.text = text
        self.chat = SimpleNamespace(id=uid)
        self.edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def delete(self):
        return None


class _Cb:
    def __init__(self, data, uid=1, message=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = message or _Msg(uid)
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))


class _State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.current = None

    async def clear(self):
        self.data = {}
        self.current = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, value):
        self.current = value


class _Jobs:
    def __init__(self):
        self.jobs = {}
        self.removed = []

    def add_job(self, *args, **kwargs):
        self.jobs[kwargs.get("id")] = True

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


class _Bot:
    def __init__(self, boom=False):
        self.boom = boom
        self.sent = []
        self._n = 1

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.boom:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, reply_markup))
        self._n += 1
        return SimpleNamespace(message_id=self._n)


def _buttons(kb):
    if kb is None:
        return []
    return [b for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for b in _buttons(kb)]


def _cbs(kb):
    return [b.callback_data for b in _buttons(kb)]


def _last(msg: _Msg):
    if msg.edits:
        return msg.edits[-1]
    return msg.answers[-1]


def _open(tmp_path):
    ready(tmp_path)
    run(db.set_setting("interview_booking_enabled", "on"))
    run(db.set_setting("interview_window_start", "01.01.2020 00:00"))
    run(db.set_setting("interview_window_end", "01.01.2100 00:00"))


async def _person(tid, *, case="Да", city="msk", name=None):
    await add_user(tid, status="approved", city=city, season=None)
    if name or case != "Да":
        sets = []
        args = []
        if name:
            sets.append("full_name = ?")
            args.append(name)
        if case != "Да":
            sets.append("case_optin = ?")
            args.append(case)
        else:
            sets.append("case_optin = ?")
            args.append("Да")
        args.append(tid)
        async with db._connect() as conn:
            await conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE telegram_id = ?", args,
            )
            await conn.commit()
    else:
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET case_optin = ? WHERE telegram_id = ?", ("Да", tid),
            )
            await conn.commit()


def _slot(city, starts, cap=1):
    return run(interview_db.create_slot(city, starts, cap, 1, "2026-11-03 12:00:00"))


def test_screen_listed_in_apps():
    rows = sec.visible_rows("apps", {"moderate_reg"}, False)
    assert "admin_interviews" in [sec.row_callback(r) for r in rows]


def test_screen_shows_human_rows(tmp_path):
    ready(tmp_path)
    cb = _Cb("admin_interviews")
    run(admin_iv.open_interviews(cb, _State()))
    text, kb = _last(cb.message)
    labels = _texts(kb)
    blob = text + "\n" + "\n".join(labels)
    assert "Запись: выключена" in labels
    assert any(x.startswith("С какого момента:") for x in labels)
    assert any(x.startswith("До какого момента:") for x in labels)
    assert any(x.startswith("Человек на одно время:") for x in labels)
    assert "interview_" not in blob
    assert "case_optin" not in blob


def test_add_slot_parses_example_format(tmp_path):
    ready(tmp_path)
    state = _State()
    run(admin_iv.interview_admin_tap(_Cb("ivadm:add"), state, _Bot()))
    bad = _Msg(text="вчера")
    run(admin_iv.interview_admin_value(bad, state))
    assert bad.answers[-1][0] == _HINT
    assert run(interview_db.list_slots(None)) == []

    state = _State()
    run(admin_iv.interview_admin_tap(_Cb("ivadm:add"), state, _Bot()))
    good = _Msg(text="28.10.2026 18:00")
    run(admin_iv.interview_admin_value(good, state))
    slots = run(interview_db.list_slots(None))
    assert len(slots) == 1
    assert slots[0]["starts_at"] == "2026-10-28 18:00"
    assert slots[0]["cap"] == 1
    assert slots[0]["event_city"] is None


def test_cap_plus_minus(tmp_path):
    ready(tmp_path)
    sid = _slot(None, "2026-12-28 18:00", cap=1)
    down = _Cb(f"ivadm:dn:{sid}")
    run(admin_iv.interview_admin_tap(down, _State(), _Bot()))
    assert down.alerts[-1][0] == "Ниже 1 нельзя."
    assert run(interview_db.get_slot(sid))["cap"] == 1
    run(admin_iv.interview_admin_tap(_Cb(f"ivadm:up:{sid}"), _State(), _Bot()))
    assert run(interview_db.get_slot(sid))["cap"] == 2
    run(admin_iv.interview_admin_tap(_Cb(f"ivadm:dn:{sid}"), _State(), _Bot()))
    assert run(interview_db.get_slot(sid))["cap"] == 1


def test_delete_confirms(tmp_path, monkeypatch):
    ready(tmp_path)
    run(_person(7, name="Звездочёт"))
    sid = _slot(None, "2026-12-28 18:00", cap=2)
    assert run(interview_db.book(7, sid, "2026-11-03 12:00:00")) == "ok"
    jobs = _Jobs()
    jobs.jobs["interview_rem_7"] = True
    monkeypatch.setattr(scheduler, "_scheduler", jobs)

    opened = _Cb(f"ivadm:open:{sid}")
    run(admin_iv.interview_admin_tap(opened, _State(), _Bot()))
    text, _kb = _last(opened.message)
    assert "Звездочёт" in text

    ask = _Cb(f"ivadm:del:{sid}")
    run(admin_iv.interview_admin_tap(ask, _State(), _Bot()))
    confirm, _kb = _last(ask.message)
    assert confirm == (
        "Запись пропадёт у 1 человек. Им уйдёт сообщение, что это время отменили."
    )
    assert run(interview_db.get_slot(sid)) is not None

    boom = _Bot(boom=True)
    run(admin_iv.interview_admin_tap(_Cb(f"ivadm:delok:{sid}"), _State(), boom))
    assert run(interview_db.get_slot(sid)) is None
    assert "interview_rem_7" in jobs.removed
    assert "interview_rem_7" not in jobs.jobs


def test_other_city_hidden(tmp_path):
    ready(tmp_path)
    run(db.set_setting("event_city_enabled", "on"))
    run(db.set_setting("admin_city__1", "spb"))
    msk = _slot("msk", "2026-12-01 10:00")
    _slot("spb", "2026-12-02 11:00")
    _slot(None, "2026-12-03 12:00")
    cb = _Cb("admin_interviews")
    run(admin_iv.open_interviews(cb, _State()))
    labels = "\n".join(_texts(_last(cb.message)[1]))
    assert "01.12 10:00" not in labels
    assert "02.12 11:00" in labels
    assert "03.12 12:00" in labels

    foreign = _Cb(f"ivadm:del:{msk}")
    run(admin_iv.interview_admin_tap(foreign, _State(), _Bot()))
    assert foreign.alerts[-1] == ("Это время другого города.", True)
    assert run(interview_db.get_slot(msk)) is not None


def test_grid_buttons_only_free_future_slots(tmp_path):
    _open(tmp_path)
    run(_person(10))
    run(_person(11, name="Звездочёт"))
    _slot("msk", "2026-12-28 18:00", cap=2)
    full = _slot("msk", "2026-12-29 19:00", cap=1)
    _slot("spb", "2026-12-30 20:00", cap=2)
    assert run(interview_db.book(11, full, "2026-11-03 12:00:00")) == "ok"
    cb = _Cb("iv_open", uid=10)
    run(delegate_iv.interview_open(cb))
    text, kb = cb.message.answers[-1]
    labels = _texts(kb)
    assert "28.12 18:00 · осталось 2" in labels
    assert all("29.12" not in x and "30.12" not in x for x in labels)
    assert "Часть времени уже занята." in text
    assert "Звездочёт" not in text
    assert "Звездочёт" not in "\n".join(labels)


def test_opt_out_gets_sentence_not_grid(tmp_path):
    _open(tmp_path)
    _slot(None, "2026-12-28 18:00", cap=2)
    for tid, case in ((20, "Нет"), (21, "1")):
        run(_person(tid, case=case))
        cb = _Cb("iv_open", uid=tid)
        run(delegate_iv.interview_open(cb))
        text, kb = cb.message.answers[-1]
        assert text == _NEED_YES
        assert kb is None


def test_book_confirms_with_move_button(tmp_path):
    _open(tmp_path)
    run(_person(30))
    first = _slot(None, "2026-12-28 18:00", cap=3)
    second = _slot(None, "2026-12-29 19:00", cap=3)
    pick = _Cb(f"iv_pick:{first}", uid=30)
    run(delegate_iv.interview_pick(pick))
    text, kb = _last(pick.message)
    assert "Ты записан на интервью 28.12.2026 18:00." in text
    assert "Перенести" in _texts(kb)
    assert "iv_move" in _cbs(kb)

    move = _Cb("iv_move", uid=30)
    run(delegate_iv.interview_move(move))
    _grid_text, grid = _last(move.message)
    assert f"iv_moveto:{second}" in _cbs(grid)
    assert f"iv_moveto:{first}" not in _cbs(grid)

    moved = _Cb(f"iv_moveto:{second}", uid=30)
    run(delegate_iv.interview_move_to(moved))
    moved_text, moved_kb = _last(moved.message)
    assert "Интервью перенесено на 29.12.2026 19:00." in moved_text
    assert moved_kb is None

    again = _Cb("iv_move", uid=30)
    run(delegate_iv.interview_move(again))
    spent, spent_kb = _last(again.message)
    assert spent == "Перенести можно было один раз."
    assert spent_kb is None


def test_broadcast_checkbox_off_by_default(tmp_path):
    ready(tmp_path)
    state = _State()
    assert run(ivbc.confirm_rows(state)) == []
    run(db.set_setting("interview_booking_enabled", "on"))
    rows = run(ivbc.confirm_rows(state))
    assert rows[0][0].callback_data == "bciv_toggle"
    assert rows[0][0].text == "🗓 Кнопка «Выбрать время»"
    assert ivbc.with_interview_button(None, False) is None
    on = ivbc.with_interview_button(None, True)
    assert _cbs(on) == ["iv_open"]
    assert ivbc.pack_filter_spec([{"field": "status"}], False) == json.dumps(
        [{"field": "status"}], ensure_ascii=False,
    )
    assert ivbc.pack_filter_spec(None, False) is None
    assert ivbc.peel_interview_flag([{"field": "status"}]) == (False, [{"field": "status"}])
    packed = json.loads(ivbc.pack_filter_spec([{"field": "status"}], True))
    assert ivbc.peel_interview_flag(packed) == (True, [{"field": "status"}])
    assert ivbc.peel_interview_flag({"filters": [], "interview_button": True}) == (True, None)

    bot = _Bot()
    run(admin_broadcasts._send_confirm_prompt(bot, 1, _State(), 1, [1]))
    assert "bciv_toggle" in _cbs(bot.sent[-1][2])
    run(db.set_setting("interview_booking_enabled", "off"))
    run(admin_broadcasts._send_confirm_prompt(bot, 1, _State(), 1, [1]))
    assert "bciv_toggle" not in _cbs(bot.sent[-1][2])

    run(_person(42))
    spec = json.dumps({"filters": [], "interview_button": True}, ensure_ascii=False)

    async def send(filter_spec):
        bid = await db.create_scheduled_broadcast(
            "привет", None, filter_spec, "2026-12-01 10:00:00", 1, important=True,
        )
        sender = _Bot()
        prev = scheduler._bot
        scheduler._bot = sender
        try:
            await scheduler.send_scheduled_broadcast(bid)
        finally:
            scheduler._bot = prev
        return sender

    flagged = run(send(spec))
    assert _cbs(flagged.sent[-1][2]) == ["iv_open"]
    plain = run(send(None))
    assert plain.sent[-1][2] is None


def test_no_menu_button():
    blob = " ".join(text for _key, text in MENU_BUTTONS)
    assert "iv_open" not in blob
    assert "Интервью" not in blob
    broadcasts = (_ROOT / "handlers" / "comms" / "admin_broadcasts.py").read_text(encoding="utf-8")
    assert re.search(r"^def .*interview", broadcasts, re.M) is None
    assert "/interview" not in broadcasts
