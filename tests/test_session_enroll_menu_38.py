"""Кнопки меню «Запись на сессии» и «Тест»: вторые гейты, подпись из настройки, причины."""
from cities import per_city_key
from database import db, quiz_db
from keyboards.builders import get_main_menu_kb, menu_hidden_reason
from tests._enroll38 import CITY, add_user, ready, run

U = 301
CAPTION = "📅 Запись на сессии"
QUIZ = "🧭 Тест"


async def _texts():
    kb = await get_main_menu_kb(U)
    return [b.text for row in kb.keyboard for b in row]


async def _base():
    await db.set_setting("event_city_enabled", "on")
    await add_user(U)


async def _make_quiz(enabled=1, questions=1):
    quiz = await quiz_db.get_or_create_quiz(CITY)
    await quiz_db.update_quiz(quiz["id"], enabled=enabled)
    for i in range(questions):
        await quiz_db.create_question(quiz["id"], f"Вопрос {i}")


def test_menu_hidden_when_module_off(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        assert CAPTION not in await _texts()
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        assert CAPTION in await _texts()
    run(go())


def test_custom_caption_in_keyboard(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        await db.set_setting(per_city_key("session_enroll_menu_label", CITY), "📅 Выбор сессий")
        texts = await _texts()
        assert "📅 Выбор сессий" in texts and CAPTION not in texts
    run(go())


def test_quiz_button_needs_active_quiz(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        assert QUIZ not in await _texts()
        await _make_quiz(enabled=0)
        assert QUIZ not in await _texts()
        await quiz_db.update_quiz((await quiz_db.get_quiz_for_city(CITY))["id"], enabled=1)
        assert QUIZ in await _texts()
    run(go())


def test_quiz_without_questions_hidden(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        await _make_quiz(enabled=1, questions=0)
        assert QUIZ not in await _texts()
    run(go())


def test_menu_off_toggle(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        await db.set_setting(per_city_key("menu_session_enroll", CITY), "off")
        assert CAPTION not in await _texts()
    run(go())


def test_hidden_reasons(tmp_path):
    ready(tmp_path)

    async def go():
        await _base()
        assert "выключен" in await menu_hidden_reason("menu_session_enroll", CITY)
        assert await menu_hidden_reason("menu_quiz", CITY) == \
            "тест не включён или в нём нет вопросов"
        await db.set_setting(per_city_key("session_enroll_enabled", CITY), "on")
        assert await menu_hidden_reason("menu_session_enroll", CITY) is None
    run(go())
