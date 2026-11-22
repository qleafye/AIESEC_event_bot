"""Журнал на карточке /find: действия словами, без кодов. Пустой журнал карточку не меняет."""
import asyncio

import domain.cities as cities
from config import config
from database import db
from handlers.applications import admin_find
from services.applications.card_journal import journal_html
from tests._dbtpl import fast_init_db

DELEGATE = 330610

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_card_journal.db")
    fast_init_db()
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    return saved


def _restore(saved):
    cities.set_cities_for_test(saved)


async def _seed():
    await db.add_user({
        "telegram_id": DELEGATE,
        "username": "journaluser",
        "full_name": "Иван Иванов",
        "email": "ivan@example.com",
        "registration_date": "2026-01-01",
        "event_city": "spb",
    })
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?", ("pending", DELEGATE),
        )
        await conn.commit()


def test_empty_journal_is_blank(tmp_path):
    saved = _ready(tmp_path)

    async def go():
        await _seed()
        return await journal_html(DELEGATE)

    try:
        assert _run(go()) == ""
    finally:
        _restore(saved)


def test_city_move_line_uses_labels(tmp_path):
    saved = _ready(tmp_path)

    async def go():
        await _seed()
        await db.record_admin_action(
            actor_id=1,
            subject_id=DELEGATE,
            action="city_move",
            before={
                "event_city": "msk",
                "status": "approved",
                "participant_type": "full",
            },
            after={
                "event_city": "spb",
                "status": "pending",
                "participant_type": "short",
                "sheet_moved": True,
            },
        )
        await db.record_admin_action(
            actor_id=1,
            subject_id=DELEGATE,
            action="not_a_real_action",
            before={"resume_file_id": "secret-file"},
            after={"role": "reg_manager"},
        )
        await db.record_admin_action(
            actor_id=1,
            subject_id=DELEGATE,
            action="grant_role",
            after={"role": "reg_manager", "city": "msk"},
        )
        return await journal_html(DELEGATE)

    try:
        text = _run(go())
    finally:
        _restore(saved)

    assert "<b>Журнал</b>" in text
    assert "Перевод в город: Москва → Санкт-Петербург. Одобрена → На рассмотрении" in text
    assert "Выдана роль: 🛂 Менеджер регистраций" in text
    assert "Записано действие" in text
    for hidden in (
        "city_move", "msk", "spb", "approved", "pending", "full", "short",
        "sheet_moved", "participant_type", "not_a_real_action", "reg_manager",
        "resume_file_id", "secret-file",
    ):
        assert hidden not in text


def test_find_card_shows_journal_only_when_present(tmp_path):
    saved = _ready(tmp_path)

    class _Msg:
        def __init__(self):
            self.answers = []

        async def answer(self, text, parse_mode=None, reply_markup=None):
            self.answers.append(text)

    async def go():
        await _seed()
        user = await db.get_user(DELEGATE)
        before = _Msg()
        await admin_find._answer_users_card(before, user)
        await db.record_admin_action(
            actor_id=1,
            subject_id=DELEGATE,
            action="city_move_regional",
            before={"event_city": "msk", "status": "approved", "participant_type": "full"},
            after={"event_city": "spb", "status": "approved", "participant_type": "full"},
        )
        after = _Msg()
        await admin_find._answer_users_card(after, user)
        return before.answers[0], after.answers[0]

    try:
        empty, filled = _run(go())
    finally:
        _restore(saved)

    assert "Журнал" not in empty
    assert "Перевод в город: Москва → Санкт-Петербург" in filled
    assert "city_move_regional" not in filled
    assert "Одобрена →" not in filled
