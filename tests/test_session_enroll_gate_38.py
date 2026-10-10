"""Делегатский вход в запись на сессии: гейт, повестка, треки, deep-link."""
from domain.cities import per_city_key
from database import db
from handlers import forum_deeplinks, session_enroll as h
from tests._enroll38 import CITY, add_user, ready, run
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons, make_state, setup_world

U = 101


def test_menu_button_custom_caption(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        await db.set_setting(per_city_key("session_enroll_menu_label", CITY), "📅 Выбор сессий")
        from keyboards.menu_dynamic import DynamicMenuText
        for caption in ("📅 Выбор сессий", "📅 Запись на сессии"):
            assert await DynamicMenuText("menu_session_enroll")(FakeMessage(U, caption))
        m = FakeMessage(U, "📅 Выбор сессий")
        await h.session_enroll_menu(m)
        assert "Выбери трек" in m.answers[-1][0]
    run(go())


def test_gate_pending_and_legacy(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        m = FakeMessage(104, "x")
        await h.session_enroll_menu(m)
        assert m.answers and "Выбери трек" not in m.answers[-1][0]
        await add_user(150, status=None)
        m = FakeMessage(150, "x")
        assert not await h.open_enroll(m, 150)
        assert m.answers[-1][0] == "Запись на сессии откроется, когда твою заявку одобрят."
    run(go())


def test_gate_past_season(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        m = FakeMessage(105, "x")
        assert not await h.open_enroll(m, 105)
        assert "Выбери трек" not in m.answers[-1][0]
        assert "откроется, когда твою заявку" not in m.answers[-1][0]
    run(go())


def test_module_off_and_no_sessions(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world(enabled=False)
        m = FakeMessage(U, "x")
        assert not await h.open_enroll(m, U)
        assert m.answers[-1][0] == "Запись на сессии сейчас недоступна."
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        for key in ("A", "B", "C", "D"):
            await db.update_program_session(ids[key], track_id=None)
        m = FakeMessage(U, "x")
        assert not await h.open_enroll(m, U)
        assert m.answers[-1][0] == "Сессий для записи пока нет — загляни позже."
    run(go())


def test_agenda_photo_first_and_fail_soft(tmp_path, monkeypatch):
    ready(tmp_path)

    async def go():
        await setup_world()

        async def src(city):
            return {"file_id": "FID"}

        async def cap(city):
            return "Повестка <b>"

        async def none(city):
            return None
        monkeypatch.setattr(h, "resolve_program_photo_source", src)
        monkeypatch.setattr(h, "program_photo_caption", cap)
        m = FakeMessage(U, "x")
        assert await h.open_enroll(m, U)
        assert m.photos == [("FID", "Повестка &lt;b&gt;")]
        assert "Выбери трек" in m.answers[-1][0]
        m = FakeMessage(U, "x")
        m.fail_photo = True
        assert await h.open_enroll(m, U)
        assert not m.photos and "Выбери трек" in m.answers[-1][0]
        monkeypatch.setattr(h, "resolve_program_photo_source", none)
        m = FakeMessage(U, "x")
        assert await h.open_enroll(m, U)
        assert not m.photos
    run(go())


def test_track_buttons(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        m = FakeMessage(U, "x")
        await h.open_enroll(m, U)
        text, kb = m.answers[-1]
        assert "Выбери трек" in text
        b = buttons(kb)
        assert ("Карьера", f"se:t:{ids['career']}") in b
        assert ("Бизнес", f"se:t:{ids['business']}") in b
        assert ("🔀 Смешать треки", "se:t:0") in b
    run(go())


def test_deeplink_sessions(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        m = FakeMessage(U, "/start sessions")
        assert await forum_deeplinks.try_forum_deeplink(m, make_state(U), "sessions")
        assert "Выбери трек" in m.answers[-1][0]
        m = FakeMessage(999, "/start sessions")
        assert not await forum_deeplinks.try_forum_deeplink(m, make_state(999), "sessions")
        assert not m.answers
        assert not await forum_deeplinks.try_forum_deeplink(m, make_state(U), "quiz")
        assert not await forum_deeplinks.try_forum_deeplink(m, make_state(U), None)
    run(go())


def test_se_open_callback(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        cb = FakeCallback("se:open", U)
        await h.se_open(cb)
        assert "Выбери трек" in cb.message.answers[-1][0]
    run(go())
