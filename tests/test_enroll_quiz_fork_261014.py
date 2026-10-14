"""Развилка при входе в запись на сессии: «пройти тест» или «сразу к записи»."""
from domain.cities import per_city_key
from database import db, quiz_db as qz
from handlers.forum import session_enroll as h
from tests._enroll38 import CITY, ready, run
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons
from tests.test_quiz_flow_38 import seed_quiz

U = 101


async def offer_on():
    await db.set_setting(per_city_key("session_enroll_quiz_offer", CITY), "on")


async def enter(msg=None):
    msg = msg or FakeMessage(U)
    await h.open_enroll(msg, U)
    return msg


def is_fork(msg):
    return [d for _, d in buttons(msg.answers[-1][1])] == ["se:quiz", "se:skipq"]


def test_toggle_off_shows_tracks(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        msg = await enter()
        assert not is_fork(msg)
        assert "Выбери трек" in msg.answers[-1][0]
    run(go())


def test_fork_shown(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        await offer_on()
        msg = await enter()
        assert is_fork(msg)
        assert "короткий тест" in msg.answers[-1][0]
        assert [t for t, _ in buttons(msg.answers[-1][1])] == ["🧭 Пройти тест", "➡️ Сразу к записи"]
        assert not msg.photos
    run(go())


def test_finished_quiz_no_fork(tmp_path):
    ready(tmp_path)

    async def go():
        quiz, _ = await seed_quiz()
        await offer_on()
        aid = await qz.create_attempt(U, quiz["id"], quiz["content_version"])
        assert await qz.finish_attempt(aid, {})
        msg = await enter()
        assert not is_fork(msg)
        assert "Выбери трек" in msg.answers[-1][0]
    run(go())


def test_skip_goes_to_tracks_without_second_fork(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        await offer_on()
        msg = await enter()
        cb = FakeCallback("se:skipq", U, msg)
        await h.se_skip_quiz(cb)
        assert "Выбери трек" in msg.answers[-1][0]
        assert not is_fork(msg)
    run(go())


def test_go_opens_quiz(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        await offer_on()
        msg = await enter()
        cb = FakeCallback("se:quiz", U, msg)
        await h.se_quiz(cb)
        assert "qz:go" in [d for _, d in buttons(msg.answers[-1][1])]
    run(go())


def test_after_deadline_no_fork(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz()
        await offer_on()
        await db.set_setting(per_city_key("session_enroll_deadline", CITY), "01.01.2020 10:00")
        msg = await enter()
        assert not is_fork(msg)
        assert "Моё расписание" in msg.answers[-1][0]
    run(go())


def test_no_quiz_no_fork(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_quiz(enabled=0)
        await offer_on()
        msg = await enter()
        assert not is_fork(msg)
        assert "Выбери трек" in msg.answers[-1][0]
    run(go())


def test_admin_toggle_and_hint(tmp_path):
    from handlers.forum import admin_enroll_list as el

    class _Msg:
        text_edited = None

        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.text_edited = text

    class _Cb:
        data = "prog_enrqo:msk"
        message = _Msg()
        from_user = type("U", (), {"id": 1})()

        async def answer(self, *a, **k):
            pass

    ready(tmp_path)

    async def go():
        cb = _Cb()
        await el.prog_enrqo(cb)
        assert "Предлагать тест перед записью: ✅ Вкл" in cb.message.text_edited
        assert "теста нет" in cb.message.text_edited
        await el.prog_enrqo(cb)
        assert "Предлагать тест перед записью: ❌ Выкл" in cb.message.text_edited
    run(go())
