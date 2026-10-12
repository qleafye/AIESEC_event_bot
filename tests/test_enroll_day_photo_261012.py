"""Картинка повестки на каждый день программы: БД, админка (кнопки), поток записи."""
from __future__ import annotations

import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db, session_enroll_db as edb
from handlers.forum import admin_program, admin_program_view as av, session_enroll as h
from handlers.states import ProgramPhotoUpload
from services.forum import program as prog
from tests._enroll38 import ADMIN_ID, CITY, DAY, ready, run
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons, setup_world
from tests.test_session_enroll_flow_38 import press

U = 101
DAY2 = "2026-10-31"


# ── БД ──────────────────────────────────────────────────────────────────────────────────────

def test_db_roundtrip_replace_delete(tmp_path):
    ready(tmp_path)

    async def go():
        assert await edb.get_day_photo(CITY, DAY) is None
        await edb.set_day_photo(CITY, DAY, "F1", "подпись")
        assert await edb.get_day_photo(CITY, DAY) == {"file_id": "F1", "caption": "подпись"}
        await edb.set_day_photo(CITY, DAY, "F2", None)
        assert await edb.get_day_photo(CITY, DAY) == {"file_id": "F2", "caption": None}
        await edb.set_day_photo("spb", DAY2, "F3", None)
        assert await edb.day_photo_days(CITY) == {DAY}
        assert await edb.delete_day_photo(CITY, DAY) is True
        assert await edb.delete_day_photo(CITY, DAY) is False
        assert await edb.get_day_photo(CITY, DAY) is None
        assert await edb.day_photo_days("spb") == {DAY2}
    run(go())


def test_init_db_keeps_existing_rows(tmp_path):
    ready(tmp_path)
    run(edb.set_day_photo(CITY, DAY, "F1", "c"))
    run(db.init_db())
    assert run(edb.get_day_photo(CITY, DAY))["file_id"] == "F1"
    con = sqlite3.connect(config.DB_PATH)
    assert con.execute("SELECT COUNT(*) FROM program_day_photos").fetchone()[0] == 1
    con.close()


# ── Поток записи ────────────────────────────────────────────────────────────────────────────

async def two_days():
    ids = await setup_world()
    d2 = await db.create_program_session(CITY, DAY2, "10:00", "11:00", "Сессия Д2", hall_id=ids["hall1"])
    await db.update_program_session(d2, track_id=ids["career"])
    ids["D2"] = d2
    return ids


def test_entry_sends_first_day_photo_not_shared(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        await two_days()
        await edb.set_day_photo(CITY, DAY, "DAY1", "Повестка <b>1</b>")
        await edb.set_day_photo(CITY, DAY2, "DAY2", None)

        async def shared(city):
            return {"file_id": "SHARED"}
        monkeypatch.setattr(prog, "resolve_program_photo_source", shared)
        m = FakeMessage(U, "x")
        assert await h.open_enroll(m, U)
        assert m.photos == [("DAY1", "Повестка <b>1</b>")]
        before = len(m.photos)
        await press("se:t:0", message=m)
        assert len(m.photos) == before
    run(go())


def test_forward_to_new_day_sends_photo_once(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await two_days()
        await edb.set_day_photo(CITY, DAY, "DAY1", None)
        await edb.set_day_photo(CITY, DAY2, "DAY2", "Д2")
        tid = ids["career"]
        m = FakeMessage(U, "x")
        await h.open_enroll(m, U)
        await press(f"se:t:{tid}", message=m)
        assert [p[0] for p in m.photos] == ["DAY1"]
        # «Дальше» внутри дня 1 не бывает (один слот) — переход сразу на день 2
        answers_before = len(m.answers)
        cb = await press(f"se:s:{tid}:1", message=m)
        assert [p[0] for p in m.photos] == ["DAY1", "DAY2"] and m.photos[1][1] == "Д2"
        assert len(m.answers) == answers_before + 1  # слот дня 2 — новое сообщение
        assert "Выбери одну сессию" in m.answers[-1][0] and m.answers[-1][0].startswith("31.10")
        assert len(m.markup_edits) == 1 and m.markup_edits[0] is None  # у старого снята клавиатура
        # на месте: «Оставить как было» и «Снять выбор» — без картинок
        await press(f"se:k:{tid}:1", message=m)
        await press(f"se:p:{tid}:1:{ids['D2']}", message=m)
        await press(f"se:c:{tid}:1:{ids['D2']}", message=m)
        assert [p[0] for p in m.photos] == ["DAY1", "DAY2"]
        assert cb.alerts is not None
    run(go())


def test_auto_advance_after_pick_sends_photo(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await two_days()
        await edb.set_day_photo(CITY, DAY2, "DAY2", None)
        tid = ids["career"]
        m = FakeMessage(U, "x")
        await press(f"se:t:{tid}", message=m)
        assert m.photos == []
        await press(f"se:p:{tid}:0:{ids['A']}", message=m)
        assert [p[0] for p in m.photos] == ["DAY2"]
        assert m.answers[-1][0].startswith("31.10")
    run(go())


def test_no_day_photos_behaves_as_before(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        ids = await two_days()

        async def shared(city):
            return {"file_id": "SHARED"}

        async def nocap(city):
            return None
        monkeypatch.setattr(prog, "resolve_program_photo_source", shared)
        monkeypatch.setattr(prog, "program_photo_caption", nocap)
        m = FakeMessage(U, "x")
        await h.open_enroll(m, U)
        assert [p[0] for p in m.photos] == ["SHARED"]
        await press(f"se:t:{ids['career']}", message=m)
        n = len(m.photos)
        await press(f"se:s:{ids['career']}:1", message=m)
        assert len(m.photos) == n and m.edits[-1][0].startswith("31.10")  # правка на месте
    run(go())


def test_only_day2_has_photo(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        ids = await two_days()
        await edb.set_day_photo(CITY, DAY2, "DAY2", None)

        async def shared(city):
            return {"file_id": "SHARED"}

        async def nocap(city):
            return None
        monkeypatch.setattr(prog, "resolve_program_photo_source", shared)
        monkeypatch.setattr(prog, "program_photo_caption", nocap)
        m = FakeMessage(U, "x")
        await h.open_enroll(m, U)
        assert [p[0] for p in m.photos] == ["SHARED"]
        await press(f"se:t:{ids['career']}", message=m)
        await press(f"se:s:{ids['career']}:1", message=m)
        assert [p[0] for p in m.photos] == ["SHARED", "DAY2"]
    run(go())


def test_photo_failure_does_not_break_flow(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await two_days()
        await edb.set_day_photo(CITY, DAY2, "DAY2", None)
        m = FakeMessage(U, "x")
        m.fail_photo = True
        await press(f"se:s:{ids['career']}:1", message=m)
        assert not m.photos and m.edits[-1][0].startswith("31.10")
    run(go())


def test_module_off_sends_nothing(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world(enabled=False)
        await edb.set_day_photo(CITY, DAY, "DAY1", None)
        m = FakeMessage(U, "x")
        assert not await h.open_enroll(m, U)
        assert m.photos == []
    run(go())


# ── Админка ─────────────────────────────────────────────────────────────────────────────────

class _Photo:
    def __init__(self, fid):
        self.file_id = fid


class _AdminMsg(FakeMessage):
    def __init__(self, uid, text=None, photo=None, caption=None):
        super().__init__(uid, text)
        self.photo = photo
        self.caption = caption
        self.html_text = caption or text
        self.texts: list[str] = []
        self.shown: list[tuple] = []

    async def answer(self, text=None, reply_markup=None, **kw):
        self.texts.append(text)
        return await super().answer(text, reply_markup)

    async def answer_photo(self, photo, caption=None, **kw):
        self.shown.append((photo, caption))
        return self


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


async def _tap(handler, data, uid=ADMIN_ID, *args):
    cb = FakeCallback(data, uid, _AdminMsg(uid))
    await handler(cb, *args)
    return cb


def test_day_screen_has_button(tmp_path):
    ready(tmp_path)

    async def go():
        await two_days()
        _t, kb = await admin_program.render_day_screen(CITY, DAY)
        assert ("🖼 Картинка дня", f"prog_agd:{CITY}:{DAY}") in buttons(kb)
        await edb.set_day_photo(CITY, DAY, "F", None)
        _t, kb = await admin_program.render_day_screen(CITY, DAY)
        assert ("🖼 Картинка дня ✅", f"prog_agd:{CITY}:{DAY}") in buttons(kb)
    run(go())


def test_admin_upload_show_replace_delete(tmp_path):
    ready(tmp_path)

    async def go():
        await two_days()
        st = _state(ADMIN_ID)
        cb = await _tap(av.prog_agd_open, f"prog_agd:{CITY}:{DAY}")
        text, kb = cb.message.edits[-1]
        assert "ещё не загружена" in text
        assert [b[0] for b in buttons(kb)] == ["📷 Загрузить картинку", "← К дню"]

        await _tap(av.prog_agd_upload, f"prog_agdup:{CITY}:{DAY}", ADMIN_ID, st)
        assert await st.get_state() == ProgramPhotoUpload.day_waiting.state
        # не фото -> подсказка
        bad = _AdminMsg(ADMIN_ID, text="привет")
        await av.prog_agd_not_photo(bad, st)
        assert "Пришлите картинку" in bad.texts[-1]
        assert await st.get_state() == ProgramPhotoUpload.day_waiting.state

        ph = _AdminMsg(ADMIN_ID, photo=[_Photo("a"), _Photo("BIG")], caption="Повестка")
        await av.prog_agd_receive(ph, st)
        assert await edb.get_day_photo(CITY, DAY) == {"file_id": "BIG", "caption": "Повестка"}
        assert await st.get_state() is None
        assert "✅ Картинка дня 30.10.2026 сохранена." in ph.texts[0]
        assert "✅ загружена" in ph.texts[1]

        cb = await _tap(av.prog_agd_open, f"prog_agd:{CITY}:{DAY}")
        names = [b[0] for b in buttons(cb.message.edits[-1][1])]
        assert "📷 Заменить картинку" in names and "👁 Показать" in names and "🗑 Убрать" in names

        cb = await _tap(av.prog_agd_show, f"prog_agdshow:{CITY}:{DAY}")
        assert cb.message.shown == [("BIG", "Повестка")]

        cb = await _tap(av.prog_agd_del_ask, f"prog_agddel:{CITY}:{DAY}")
        ask = cb.message.edits[-1][0]
        assert "удалятся" in ask and "общее фото" in ask
        assert await edb.get_day_photo(CITY, DAY)  # пока не удалено
        await _tap(av.prog_agd_del_go, f"prog_agddelgo:{CITY}:{DAY}")
        assert await edb.get_day_photo(CITY, DAY) is None
    run(go())


def test_admin_cancel_clears_state(tmp_path):
    ready(tmp_path)

    async def go():
        await two_days()
        st = _state(ADMIN_ID)
        await _tap(av.prog_agd_upload, f"prog_agdup:{CITY}:{DAY}", ADMIN_ID, st)
        cb = await _tap(av.prog_agd_back, f"prog_agdx:{CITY}:{DAY}", ADMIN_ID, st)
        assert await st.get_state() is None
        assert "30.10" in cb.message.edits[-1][0]
        await _tap(av.prog_agd_upload, f"prog_agdup:{CITY}:{DAY}", ADMIN_ID, st)
        msg = _AdminMsg(ADMIN_ID, text="Отмена")
        await av.prog_agd_not_photo(msg, st)
        assert await st.get_state() is None and msg.texts
    run(go())


def test_admin_foreign_city_and_bad_day(tmp_path):
    ready(tmp_path)
    MANAGER = 961012001

    async def go():
        await two_days()
        await db.add_staff(MANAGER, "reg_manager", ADMIN_ID)
        await db.set_staff_city(MANAGER, "msk")
        st = _state(MANAGER)
        cb = await _tap(av.prog_agd_upload, f"prog_agdup:spb:{DAY}", MANAGER, st)
        assert cb.alerts[0][1] is True and await st.get_state() is None
        cb = await _tap(av.prog_agd_upload, f"prog_agdup:{CITY}:не-день", MANAGER, st)
        assert cb.alerts[0][1] is True and await st.get_state() is None
        # состояние подделано/устарело: фото для чужого города не пишется
        await st.set_state(ProgramPhotoUpload.day_waiting)
        await st.set_data({"code": "spb", "day": DAY})
        await av.prog_agd_receive(_AdminMsg(MANAGER, photo=[_Photo("EVIL")]), st)
        assert await edb.get_day_photo("spb", DAY) is None
        cb = await _tap(av.prog_agd_del_go, f"prog_agddelgo:spb:{DAY}", MANAGER)
        assert cb.alerts[0][1] is True
    run(go())
