"""Админка записи на сессии: треки, компетенции, экран записи у сессии (кнопками)."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from database import db, session_enroll_db as edb
from handlers import admin_enroll, admin_program
from handlers.admin_caps import role_caps_key
from handlers.states import ProgramCompetencyEdit, ProgramEnrollLimit, ProgramTrackEdit
from tests._enroll38 import ADMIN_ID, CITY, add_user, ready, run, seed_msk_program

SPB_MANAGER = 900038002


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []
        self.answer_markups = []
        self.documents = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def answer_document(self, document, caption=None, **kw):
        self.documents.append((document, caption))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_tracks_list_and_add(tmp_path):
    ready(tmp_path)
    state = new_state()
    cb = FakeCallback("prog_trkl:msk")
    run(admin_enroll.prog_trkl(cb))
    assert "Треков пока нет" in cb.message.text_edited and "Карьера" in cb.message.text_edited
    assert "prog_trkn:msk" in cbs(cb.message.edit_markup)

    cb = FakeCallback("prog_trkn:msk")
    run(admin_enroll.prog_trkn(cb, state))
    assert run(state.get_state()) == ProgramTrackEdit.name.state
    assert "Например: Карьера" in cb.message.answers_sent[0]

    msg = FakeMessage("Карьера")
    run(admin_enroll.prog_trk_name_step(msg, state))
    assert run(state.get_state()) is None
    assert [t["name"] for t in run(edb.list_tracks(CITY))] == ["Карьера"]
    assert "Карьера" in texts(msg.answer_markups[0])[0]


def test_track_empty_name_rejected(tmp_path):
    ready(tmp_path)
    state = new_state()
    run(admin_enroll.prog_trkn(FakeCallback("prog_trkn:msk"), state))
    for bad in ("   ", "x" * 61):
        msg = FakeMessage(bad)
        run(admin_enroll.prog_trk_name_step(msg, state))
        assert "от 1 до 60" in msg.answers_sent[0]
        assert run(state.get_state()) == ProgramTrackEdit.name.state
    assert run(edb.list_tracks(CITY)) == []


def test_track_rename_and_duplicate(tmp_path):
    ready(tmp_path)
    tid = run(edb.create_track(CITY, "Карьера"))
    run(edb.create_track(CITY, "Бизнес"))
    state = new_state()
    run(admin_enroll.prog_trkr(FakeCallback(f"prog_trkr:{tid}"), state))
    msg = FakeMessage("бизнес")
    run(admin_enroll.prog_trk_name_step(msg, state))
    assert "уже есть" in msg.answers_sent[0]
    msg = FakeMessage("Наука")
    run(admin_enroll.prog_trk_name_step(msg, state))
    assert run(edb.get_track(tid))["name"] == "Наука"


def test_track_delete_confirm_names_consequences(tmp_path):
    ready(tmp_path)
    ids = run(_seed_two_enrolls())
    cb = FakeCallback(f"prog_trkx:{ids['career']}")
    run(admin_enroll.prog_trkx(cb))
    assert "станут общими" in cb.message.text_edited
    assert "записи на них удалятся: 2" in cb.message.text_edited
    assert f"prog_trkxgo:{ids['career']}" in cbs(cb.message.edit_markup)
    run(admin_enroll.prog_trkxgo(FakeCallback(f"prog_trkxgo:{ids['career']}")))
    assert run(edb.get_track(ids["career"])) is None
    assert run(edb.count_enrollments(ids["A"])) == 0


async def _seed_two_enrolls():
    ids = await seed_msk_program()
    for tid in (101, 102):
        await add_user(tid)
        await edb.enroll_tx(tid, ids["A"])
    return ids


def test_track_move(tmp_path):
    ready(tmp_path)
    a = run(edb.create_track(CITY, "A"))
    run(edb.create_track(CITY, "B"))
    cb = FakeCallback(f"prog_trkm:{a}:1")
    run(admin_enroll.prog_trkm(cb))
    assert [t["name"] for t in run(edb.list_tracks(CITY))] == ["B", "A"]


def test_competencies_crud(tmp_path):
    ready(tmp_path)
    state = new_state()
    cb = FakeCallback("prog_cmpl:msk")
    run(admin_enroll.prog_cmpl(cb))
    assert "Компетенций пока нет" in cb.message.text_edited
    run(admin_enroll.prog_cmpn(FakeCallback("prog_cmpn:msk"), state))
    assert run(state.get_state()) == ProgramCompetencyEdit.name.state
    run(admin_enroll.prog_cmp_name_step(FakeMessage("Лидерство"), state))
    cid = run(edb.list_competencies(CITY))[0]["id"]

    async def attach():
        ids = await seed_msk_program()
        await edb.toggle_session_competency(ids["A"], cid)
        await edb.toggle_session_competency(ids["B"], cid)

    run(attach())
    cb = FakeCallback(f"prog_cmpx:{cid}")
    run(admin_enroll.prog_cmpx(cb))
    assert "снимется с сессий: 2" in cb.message.text_edited
    run(admin_enroll.prog_cmpxgo(FakeCallback(f"prog_cmpxgo:{cid}")))
    assert run(edb.list_competencies(CITY)) == []


def test_foreign_city_denied(tmp_path):
    ready(tmp_path)

    async def setup():
        await db.set_setting(role_caps_key("reg_manager"), "settings")
        await db.add_staff(SPB_MANAGER, "reg_manager", ADMIN_ID)
        await db.set_staff_city(SPB_MANAGER, "spb")
        return await edb.create_track(CITY, "Карьера")

    tid = run(setup())
    cb = FakeCallback("prog_trkl:msk", user_id=SPB_MANAGER)
    run(admin_enroll.prog_trkl(cb))
    assert cb.answers and cb.answers[0][1] is True and cb.message.text_edited is None
    cb = FakeCallback(f"prog_trkx:{tid}", user_id=SPB_MANAGER)
    run(admin_enroll.prog_trkx(cb))
    assert cb.answers[0][1] is True and cb.message.text_edited is None


# ── Экран записи у сессии ─────────────────────────────────────────────────────────────────────

def test_card_shows_enroll_status(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    text, kb = run(admin_program.render_session_card(ids["A"]))
    assert "Трек: Карьера" in text
    assert "Компетенции: —" in text
    assert "Запись: открыта · без лимита · записано 0" in text
    assert f"prog_enrcard:{ids['A']}" in cbs(kb)


def test_pick_track_and_general(tmp_path):
    ready(tmp_path)
    ids = run(_seed_two_enrolls())
    sid = ids["A"]
    cb = FakeCallback(f"prog_enrtrk:{sid}:{ids['business']}")
    run(admin_enroll.prog_enrtrk(cb))
    assert run(get_session(sid))["track_id"] == ids["business"]
    assert run(edb.count_enrollments(sid)) == 2  # смена трека записи не трогает
    cb = FakeCallback(f"prog_enrtrk:{sid}:0")
    run(admin_enroll.prog_enrtrk(cb))
    assert run(get_session(sid))["track_id"] == ids["business"]  # ждёт подтверждения
    assert "записи на эту сессию удалятся: 2" in cb.message.text_edited
    run(admin_enroll.prog_enrtrkgo(FakeCallback(f"prog_enrtrkgo:{sid}:0")))
    assert run(get_session(sid))["track_id"] is None
    assert run(edb.count_enrollments(sid)) == 0


async def get_session(sid):
    return await admin_enroll.get_program_session(sid)


def test_pick_track_from_other_city_refused(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    other = run(edb.create_track("spb", "Чужой"))
    cb = FakeCallback(f"prog_enrtrk:{ids['A']}:{other}")
    run(admin_enroll.prog_enrtrk(cb))
    assert run(get_session(ids["A"]))["track_id"] == ids["career"]


def test_competency_toggle_and_empty_hint(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    cb = FakeCallback(f"prog_enrcard:{ids['P']}")
    run(admin_enroll.prog_enrcard(cb))
    assert "Компетенций пока нет" in cb.message.text_edited
    assert "prog_cmpl:msk" in cbs(cb.message.edit_markup)
    cid = run(edb.create_competency(CITY, "Лидерство"))
    run(admin_enroll.prog_enrcmp(FakeCallback(f"prog_enrcmp:{ids['A']}:{cid}")))
    assert run(edb.get_session_competency_ids(ids["A"])) == [cid]
    text, _ = run(admin_program.render_session_card(ids["A"]))
    assert "Компетенции: Лидерство" in text
    run(admin_enroll.prog_enrcmp(FakeCallback(f"prog_enrcmp:{ids['A']}:{cid}")))
    assert run(edb.get_session_competency_ids(ids["A"])) == []


def test_closed_toggle(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    cb = FakeCallback(f"prog_enrcl:{ids['A']}")
    run(admin_enroll.prog_enrcl(cb))
    assert run(get_session(ids["A"]))["enroll_closed"]
    assert "Запись: закрыта" in cb.message.text_edited


def test_limit_input(tmp_path):
    ready(tmp_path)
    ids = run(seed_msk_program())
    state = new_state()
    run(admin_enroll.prog_enrlim(FakeCallback(f"prog_enrlim:{ids['A']}"), state))
    assert run(state.get_state()) == ProgramEnrollLimit.value.state
    for bad in ("abc", "-5", "0"):
        msg = FakeMessage(bad)
        run(admin_enroll.prog_enrlim_step(msg, state))
        assert "Нужно целое число мест, например 30" in msg.answers_sent[0]
    msg = FakeMessage("30")
    run(admin_enroll.prog_enrlim_step(msg, state))
    assert run(get_session(ids["A"]))["enroll_limit"] == 30
    assert run(state.get_state()) is None
    run(admin_enroll.prog_enrlim0(FakeCallback(f"prog_enrlim0:{ids['A']}")))
    assert run(get_session(ids["A"]))["enroll_limit"] is None


def test_delete_confirm_mentions_enrollments(tmp_path):
    ready(tmp_path)
    ids = run(_seed_two_enrolls())
    cb = FakeCallback(f"prog_d:{ids['A']}")
    run(admin_program.prog_delete_confirm(cb))
    assert "Пропадут записи: 2" in cb.message.text_edited


def test_copy_day_maps_tracks(tmp_path):
    ready(tmp_path)
    from services.program import copy_program_day
    from tests._enroll38 import DAY
    ids = run(seed_msk_program())
    spb_career = run(edb.create_track("spb", "Карьера"))
    run(update_closed(ids["A"]))
    run(copy_program_day(CITY, "spb", DAY))
    sessions = run(db.list_program_sessions_for_city_day("spb", DAY))
    by_title = {s["title"]: s for s in sessions}
    assert by_title["Сессия A"]["track_id"] == spb_career
    assert by_title["Сессия B"]["track_id"] is None  # «Бизнеса» в spb нет
    assert by_title["Пленарка"]["track_id"] is None
    assert not by_title["Сессия A"]["enroll_closed"]


async def update_closed(sid):
    await db.update_program_session(sid, enroll_closed=1, enroll_limit=5)
