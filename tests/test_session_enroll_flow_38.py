"""Делегатский поток записи: слоты, выбор, замена, расписание, подтверждение, дедлайн, IDOR."""
from cities import per_city_key
from database import db, session_enroll_db
from handlers import session_enroll as h
from tests._enroll38 import CITY, add_user, ready, run
from tests._enroll38_chat import FakeCallback, FakeMessage, buttons, setup_world

U = 101


async def press(data, uid=U, message=None):
    cb = FakeCallback(data, uid, message)
    handler = {
        "se:t": h.se_track, "se:s": h.se_slot, "se:p": h.se_pick, "se:r": h.se_replace,
        "se:k": h.se_keep, "se:c": h.se_clear,
    }.get(data[:4])
    if handler is None:
        handler = {"se:my": h.se_my, "se:ok": h.se_confirm, "se:ed": h.se_edit}[data]
    await handler(cb)
    return cb


async def chosen(uid=U):
    return sorted(s["title"] for s in await session_enroll_db.list_user_enrollments(uid, CITY))


def test_slot_screen(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await db.update_program_session(ids["B"], enroll_closed=1)
        cb = await press(f"se:t:{ids['career']}")
        text, kb = cb.message.last
        # A, B, C, D связаны пересечениями в один слот (group_parallel транзитивен)
        assert text == "30.10, 10:00–12:30. Выбери одну сессию:"
        b = buttons(kb)
        assert b[0] == ("⭐ Сессия A", f"se:p:{ids['career']}:0:{ids['A']}")
        assert b[1][0] == "⭐ Сессия C"
        assert "Сессия B · 🔒 запись закрыта" in [t for t, _ in b]
    run(go())


def test_pick_ok_moves_next_and_ends_in_schedule(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        late = await db.create_program_session(CITY, "2026-10-30", "14:00", "15:00", "Поздняя")
        await db.update_program_session(late, track_id=ids["career"])
        cb = await press(f"se:p:0:0:{ids['A']}")
        assert await chosen() == ["Сессия A"]
        assert cb.message.last[0] == "30.10, 14:00–15:00. Выбери одну сессию:"
        cb = await press(f"se:p:0:1:{late}")
        assert "Моё расписание" in cb.message.last[0]
        assert await chosen() == ["Поздняя", "Сессия A"]
    run(go())


def test_pick_closed_alert(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await db.update_program_session(ids["A"], enroll_closed=1)
        cb = await press(f"se:p:0:0:{ids['A']}")
        assert cb.alerts[-1] == ("Запись на «Сессия A» закрыта — выбери другую сессию.", True)
        assert await chosen() == []
    run(go())


def test_replace_flow(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await press(f"se:p:0:0:{ids['A']}")
        cb = await press(f"se:p:0:0:{ids['B']}")
        text, kb = cb.message.last
        assert text == "Ты уже записан на «Сессия A» в это время. Поменять на «Сессия B»?"
        assert [t for t, _ in buttons(kb)] == ["Да, поменять", "Оставить как было"]
        assert await chosen() == ["Сессия A"]
        await press(f"se:k:0:0")
        assert await chosen() == ["Сессия A"]
        await press(f"se:r:0:0:{ids['B']}")
        assert await chosen() == ["Сессия B"]
    run(go())


def test_clear_choice(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await press(f"se:p:0:0:{ids['A']}")
        cb = await press(f"se:c:0:0:{ids['A']}")
        assert await chosen() == []
        assert cb.message.last[0].startswith("30.10, 10:00")
    run(go())


def test_my_schedule_and_confirm(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await press(f"se:p:0:0:{ids['A']}")
        cb = await press("se:my")
        text, kb = cb.message.last
        assert "09:00–10:00 Пленарка (для всех)" in text
        assert "10:00–11:00 Сессия A" in text and "Сессия B" not in text
        assert [t for t, _ in buttons(kb)] == ["✏️ Изменить", "✅ Подтвердить"]
        cb = await press("se:ok")
        assert await session_enroll_db.get_schedule_confirmed_at(U, CITY)
        assert "Готово, расписание подтверждено! Поменять выбор можно." in cb.message.last[0] \
            or "подтверждено!" in cb.message.last[0]
        assert "до ." not in cb.message.last[0] and "до  " not in cb.message.last[0]
        # выбор после подтверждения можно менять
        await press(f"se:p:0:0:{ids['B']}")
        cb2 = await press(f"se:r:0:0:{ids['B']}")
        assert await chosen() == ["Сессия B"]
        # с дедлайном — дата в тексте
        await db.set_setting(per_city_key("session_enroll_deadline", CITY), "28.10.2099 23:59")
        cb = await press("se:ok")
        assert "28.10.2099 23:59" in cb.message.last[0]
    run(go())


def test_after_deadline(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await press(f"se:p:0:0:{ids['A']}")
        await db.set_setting(per_city_key("session_enroll_deadline", CITY), "01.01.2020 10:00")
        cb = await press("se:my")
        text, kb = cb.message.last
        assert "10:00–11:00 Сессия A" in text
        assert "Запись на сессии закрылась 01.01.2020 10:00" in text
        assert buttons(kb) == []
        cb = await press(f"se:p:0:0:{ids['B']}")
        assert cb.alerts[-1][1] and "Запись на сессии закрылась" in cb.alerts[-1][0]
        assert await chosen() == ["Сессия A"]
        cb = await press("se:ed")
        assert cb.alerts[-1][1]
        cb = await press("se:ok")
        assert cb.alerts[-1][1]
        cb = await press(f"se:c:0:0:{ids['A']}")
        assert await chosen() == ["Сессия A"]
        # вход после дедлайна показывает расписание
        m = FakeMessage(U, "x")
        assert await h.open_enroll(m, U)
        assert "Моё расписание" in m.answers[-1][0]
    run(go())


def test_idor_foreign_city_session(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        spb_track = await session_enroll_db.create_track("spb", "Т")
        sid = await db.create_program_session("spb", "2026-10-30", "10:00", "11:00", "Чужая")
        await db.update_program_session(sid, track_id=spb_track)
        cb = await press(f"se:p:0:0:{sid}")
        assert cb.alerts and cb.alerts[-1][1]
        assert await chosen() == []
    run(go())


def test_stale_slot_index_and_garbage(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        cb = await press("se:s:0:99")
        assert "Моё расписание" in cb.message.last[0]
        cb = await press("se:s:0:-1")
        assert "Моё расписание" in cb.message.last[0]
        cb = await press("se:s:abc:1")
        assert "Моё расписание" in cb.message.last[0]
        cb = await press("se:p:x")
        assert "Моё расписание" in cb.message.last[0]
        assert await chosen() == []
    run(go())


def test_callbacks_gated_for_pending(tmp_path):
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        cb = await press(f"se:p:0:0:{ids['A']}", uid=104)
        assert cb.alerts[-1][1]
        assert await chosen(104) == []
    run(go())


def test_schedule_empty_text(tmp_path):
    ready(tmp_path)

    async def go():
        await setup_world()
        cb = await press("se:my")
        assert "Ты пока не выбрал ни одной сессии." in cb.message.last[0]
    run(go())


def test_slot_with_chain_shows_all_chosen_and_clears_all(tmp_path):
    """A/C/D в одном слоте: A и D совместимы — обе помечены, «Снять выбор» снимает обе."""
    ready(tmp_path)

    async def go():
        ids = await setup_world()
        await press(f"se:p:0:0:{ids['A']}")
        await press(f"se:p:0:0:{ids['D']}")
        assert await chosen() == ["Сессия A", "Сессия D"]
        cb = await press(f"se:s:0:0")
        marks = [t for t, _ in buttons(cb.message.last[1]) if t.startswith("✅")]
        assert marks == ["✅ Сессия A", "✅ Сессия D"]
        cb = await press(f"se:c:0:0:{ids['A']}")
        assert await chosen() == []
        assert not [t for t, _ in buttons(cb.message.last[1]) if t.startswith("✅")]
    run(go())
