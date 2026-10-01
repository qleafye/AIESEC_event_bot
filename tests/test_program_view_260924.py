"""Пакет C, п.3 (D-29, FORUM-CHECKIN.md «Решения владельца 24.09»): резолверы «одной кнопки
программы» в `services/program.py` — `resolve_program_photo`/`resolve_program_view`/
`has_program_content`/`build_delegate_program`. Общая точка правды для чата, гейта кнопки меню
и Mini App (см. докстринг модуля).

Та же конвенция, что `tests/test_program_service_260924.py`: БД — шаблонная копия
(`tests/_dbtpl.py::fast_init_db`), `asyncio.run()` — pytest-asyncio недоступен."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
from services import program
from tests._dbtpl import fast_init_db


def _use_tmp_db(tmp_path, name="test_program_view.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


# ── resolve_program_photo: per_city override -> общее -> None ──────────────────────────────

def test_resolve_program_photo_none_when_nothing_set(tmp_path):
    _use_tmp_db(tmp_path)
    assert _run(program.resolve_program_photo("msk")) is None


def test_resolve_program_photo_falls_back_to_global(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.set_setting("program_photo_file_id", "GLOBAL_FILE_ID"))
    assert _run(program.resolve_program_photo("msk")) == "GLOBAL_FILE_ID"
    assert _run(program.resolve_program_photo(None)) == "GLOBAL_FILE_ID"


def test_resolve_program_photo_own_city_overrides_global(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("program_photo_file_id", "GLOBAL_FILE_ID"))
    from cities import per_city_key
    _run(db.set_setting(per_city_key("program_photo_file_id", "msk"), "MSK_FILE_ID"))
    assert _run(program.resolve_program_photo("msk")) == "MSK_FILE_ID"
    # Другой город override не задан — видит общее.
    assert _run(program.resolve_program_photo("spb")) == "GLOBAL_FILE_ID"


def test_resolve_program_photo_ignores_override_when_cities_module_off(tmp_path):
    """Module-off parity (тот же контракт, что у `cities.get_setting_for_city`): при
    выключенном модуле городов per_city override не читается вовсе, даже если он есть в БД —
    иначе делегат мог бы получить чужое городское фото после того, как менеджер выключил
    модуль (переопределение "утекло" бы)."""
    _use_tmp_db(tmp_path)
    _run(db.set_setting("program_photo_file_id", "GLOBAL_FILE_ID"))
    from cities import per_city_key
    _run(db.set_setting(per_city_key("program_photo_file_id", "msk"), "MSK_FILE_ID"))
    assert _run(program.resolve_program_photo("msk")) == "GLOBAL_FILE_ID"


# ── resolve_program_view: per_city override -> общее -> дефолт по данным ───────────────────

def test_resolve_program_view_defaults_to_photo_without_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    assert _run(program.resolve_program_view("msk")) == "photo"


def test_resolve_program_view_defaults_to_table_when_sessions_exist(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    assert _run(program.resolve_program_view("msk")) == "table"


def test_resolve_program_view_explicit_global_wins_over_data_default(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    _run(db.set_setting("program_miniapp_view", "photo"))
    assert _run(program.resolve_program_view("msk")) == "photo"


def test_resolve_program_view_per_city_override_wins_over_global(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("program_miniapp_view", "photo"))
    from cities import per_city_key
    _run(db.set_setting(per_city_key("program_miniapp_view", "msk"), "table"))
    assert _run(program.resolve_program_view("msk")) == "table"
    assert _run(program.resolve_program_view("spb")) == "photo"


# ── has_program_content: фото ИЛИ сессии ────────────────────────────────────────────────────

def test_has_program_content_false_when_nothing(tmp_path):
    _use_tmp_db(tmp_path)
    assert _run(program.has_program_content("msk")) is False


def test_has_program_content_true_with_sessions_only(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    assert _run(program.has_program_content("msk")) is True


def test_has_program_content_true_with_photo_only(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.set_setting("program_photo_file_id", "GLOBAL_FILE_ID"))
    assert _run(program.has_program_content("msk")) is True


# ── build_delegate_program: слоты, параллельность, now/next ────────────────────────────────

def test_build_delegate_program_groups_parallel_sessions_into_one_slot(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Трек А"))
    _run(db.create_program_session("msk", "2026-10-30", "10:30", "11:30", "Трек Б"))
    days = _run(program.build_delegate_program("msk", at=datetime(2026, 10, 29, 9, 0)))
    assert len(days) == 1
    slots = days[0]["slots"]
    assert len(slots) == 1
    assert {s["title"] for s in slots[0]["sessions"]} == {"Трек А", "Трек Б"}


def test_build_delegate_program_marks_current_session_now(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "09:00", "10:00", "Утро"))
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Идёт сейчас"))
    days = _run(program.build_delegate_program("msk", at=datetime(2026, 10, 30, 10, 30)))
    slots = days[0]["slots"]
    now_slots = [s for s in slots if s["now"]]
    assert len(now_slots) == 1
    assert now_slots[0]["sessions"][0]["title"] == "Идёт сейчас"
    assert not any(s["next"] for s in slots)  # "now" есть -> "next" не нужна


def test_build_delegate_program_marks_nearest_future_slot_as_next_when_nothing_now(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "09:00", "10:00", "Прошедшая"))
    _run(db.create_program_session("msk", "2026-10-30", "14:00", "15:00", "Следующая"))
    _run(db.create_program_session("msk", "2026-10-31", "10:00", "11:00", "Завтра"))
    days = _run(program.build_delegate_program("msk", at=datetime(2026, 10, 30, 12, 0)))
    flagged = [s for day in days for s in day["slots"] if s["next"]]
    assert len(flagged) == 1
    assert flagged[0]["sessions"][0]["title"] == "Следующая"


def test_build_delegate_program_empty_city_returns_no_days(tmp_path):
    _use_tmp_db(tmp_path)
    assert _run(program.build_delegate_program("msk")) == []


# ── Интеграция: чат-кнопка «📅 Программа форума» берёт фото ГОРОДА делегата (D-29 «Е») ──────

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _PhotoTrackingMessage:
    """Тот же класс сообщения, что в `tests/test_delegate_texts_registry_260819.py`, плюс
    ЗАПОМИНАНИЕ переданного `photo` — та база не различает file_id по нему (пишет только
    подпись), а этому тесту нужно доказать, что ушёл именно городской file_id, а не общий."""

    def __init__(self, text=None, user_id=None):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.photos_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        pass

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.photos_sent.append(photo)


def test_show_program_chat_button_uses_delegate_city_photo(tmp_path):
    from handlers import user_actions as ua_mod

    _use_tmp_db(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("program_photo_file_id", "GLOBAL_FILE_ID"))
    from cities import per_city_key
    _run(db.set_setting(per_city_key("program_photo_file_id", "msk"), "MSK_FILE_ID"))

    delegate_id = 941924301
    _run(db.add_user({
        "telegram_id": delegate_id, "full_name": "Делегат МСК", "registration_date": "2026-08-01",
        "event_city": "msk",
    }))

    message = _PhotoTrackingMessage(text="📅 Программа форума", user_id=delegate_id)
    _run(ua_mod.show_program(message))
    assert message.photos_sent == ["MSK_FILE_ID"]


def test_build_delegate_program_parallel_sessions_have_own_now_next(tmp_path):
    """Приёмка 01.10: слот 03:00–06:00 из двух пересекающихся сессий идёт с 03:00, но
    мастер-класс с 04:30 в 04:00 ещё не начался — «идёт» только у первой, вторая «следующая»."""
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("spb", "2026-10-01", "03:00", "05:00", "Открытие"))
    _run(db.create_program_session("spb", "2026-10-01", "04:30", "06:00", "Мастер-класс"))
    days = _run(program.build_delegate_program("spb", at=datetime(2026, 10, 1, 4, 0)))
    slot = days[0]["slots"][0]
    assert slot["now"] is True
    by_title = {s["title"]: s for s in slot["sessions"]}
    assert by_title["Открытие"]["now"] is True and by_title["Открытие"]["next"] is False
    assert by_title["Мастер-класс"]["now"] is False and by_title["Мастер-класс"]["next"] is True

    later = _run(program.build_delegate_program("spb", at=datetime(2026, 10, 1, 4, 45)))
    assert all(s["now"] for s in later[0]["slots"][0]["sessions"])


def test_build_delegate_program_next_slot_marks_only_first_sessions(tmp_path):
    _use_tmp_db(tmp_path)
    _run(db.create_program_session("spb", "2026-10-01", "10:00", "12:00", "Первая"))
    _run(db.create_program_session("spb", "2026-10-01", "11:00", "12:00", "Вторая"))
    days = _run(program.build_delegate_program("spb", at=datetime(2026, 10, 1, 9, 0)))
    by_title = {s["title"]: s for s in days[0]["slots"][0]["sessions"]}
    assert by_title["Первая"]["next"] is True
    assert by_title["Вторая"]["next"] is False
