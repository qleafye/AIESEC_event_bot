"""Quick 260911-805 (W4-03): экран «🌙 Тихие часы» — тумблер и «с»/«до» в одном месте.

УАТ-находка ночи 10-11.09: раздел «📋 Заявки» показывал только тумблер, поле времени лежало
внутри обезличенной группы «⚙️ Тексты и настройки», недостижимой прямым тапом. Этот файл
покрывает новый шов `handlers/admin_quiet_hours.py`.

Стиль и фейки — `tests/test_admin_sections_ia20.py` (FakeCallback/FakeMessage, `asyncio.run`,
tmp_path, `_roles_ready`).
"""
import asyncio

import pytest

from config import config
from database import db
from handlers import admin_quiet_hours as qh_screen
from handlers.admin_caps import ADMIN_CAPS

from tests.test_roles_phase8 import ADMIN_ID, _roles_ready


@pytest.fixture
def _restore_cities_cache():
    """`cities.reload_cities()` мутирует `cities.CITIES` НА МЕСТЕ (см. `cities.py` докстринг —
    другие модули держат `from cities import CITIES`, ребинд сломал бы их алиасы), а
    conftest.py этого проекта намеренно не сбрасывает состояние между тестами/файлами. Без
    восстановления города, дописанные этим тестом, продолжают жить и в следующих тестовых
    файлах того же процесса (форма `tests/test_faq_260906.py::_restore_cities_cache`)."""
    import cities
    snapshot = list(cities.CITIES)
    yield
    cities.CITIES.clear()
    cities.CITIES.extend(snapshot)


def _run(coro):
    return asyncio.run(coro)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return {b.callback_data: b.text for row in kb.inline_keyboard for b in row}


# ── Право доступа ───────────────────────────────────────────────────────────────────────────

def test_admin_quiet_hours_requires_settings_capability():
    assert ADMIN_CAPS["admin_quiet_hours"] == "settings"


# ── Состояние: тумблер выключен ─────────────────────────────────────────────────────────────

def test_toggle_off_says_silence_disabled_and_still_offers_edit(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "выключена" in text.lower()
    cbs = _cbs(kb)
    assert "settings_edit:quiet_hours_start" in cbs
    assert "settings_edit:quiet_hours_end" in cbs
    assert "settings_edit:quiet_hours_manager_notice_text" in cbs


# ── Состояние: окно есть ────────────────────────────────────────────────────────────────────

def test_toggle_on_valid_window_shows_window_and_what_is_deferred(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "22:00" in text and "09:00" in text
    assert "тишину" in text.lower() and "не ждёт" in text.lower()
    assert "В очереди уведомлений" in text


def test_queue_count_reflects_pending_notifications(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))
    _run(db.add_user({"telegram_id": 111, "full_name": "D", "registration_date": "2026-09-01"}))
    from datetime import datetime
    _run(db.enqueue_delayed_notification(
        111, "text_html", {"text": "hi"},
        "2026-09-12 09:00:00", "2026-09-11 23:00:00", replace=False,
    ))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    assert "1" in text.split("В очереди уведомлений:")[1].split("\n")[0]


# ── Честные состояния «окна нет» ────────────────────────────────────────────────────────────

def test_start_equals_end_says_no_window_and_what_to_do(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "10:00"))
    _run(db.set_setting("quiet_hours_end", "10:00"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "окна тишины нет" in text.lower() or "окна нет" in text.lower()
    assert "10:00" in text


def test_unparsable_value_says_that_exactly_not_empty_window(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "вечером"))
    _run(db.set_setting("quiet_hours_end", "09:00"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "не разобрал" in text.lower()
    assert "вечером" in text


# ── Кнопки: значения в подписи, тумблер — та же кнопка, что в разделе ───────────────────────

def test_edit_buttons_carry_current_values_in_label(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:30"))
    _run(db.set_setting("quiet_hours_end", "08:15"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    labels = _labels(kb)

    assert "22:30" in labels["settings_edit:quiet_hours_start"]
    assert "08:15" in labels["settings_edit:quiet_hours_end"]


def test_toggle_row_is_same_button_as_settings_toggle_rows(tmp_path):
    _roles_ready(tmp_path)
    from handlers.admin_settings import settings_toggle_rows

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    labels = _labels(kb)

    expected = _run(settings_toggle_rows(ADMIN_ID))["toggle_quiet_hours"][0][0]
    assert labels["toggle_quiet_hours"] == expected.text


def test_back_button_uses_back_button_helper_not_literal(tmp_path):
    _roles_ready(tmp_path)
    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    cbs = _cbs(kb)
    assert "admin_sec:apps" in cbs


# ── Экранирование ────────────────────────────────────────────────────────────────────────────

def test_manager_notice_html_is_escaped_in_screen_text(tmp_path):
    _roles_ready(tmp_path)
    _run(db.set_setting("quiet_hours_manager_notice_text", "🌙 <b>тест</b> в {time}"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "<b>тест</b>" not in text
    assert "&lt;b&gt;" in text


# ── Город (модуль включён) ──────────────────────────────────────────────────────────────────

def test_city_module_off_no_city_line(tmp_path):
    _roles_ready(tmp_path)
    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    assert "Город:" not in text


def test_bound_city_header_shows_city_line_and_per_city_hours(tmp_path, _restore_cities_cache):
    _roles_ready(tmp_path)
    import cities as cities_mod
    _run(db.insert_city("msk", "Москва", "", 0))
    _run(db.insert_city("spb", "Питер", "", 1))
    _run(db.set_setting("event_city_enabled", "on"))
    _run(cities_mod.reload_cities())
    _run(cities_mod.set_admin_city(ADMIN_ID, "spb"))
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "22:00"))
    _run(db.set_setting("quiet_hours_end", "09:00"))
    from cities import per_city_key
    _run(db.set_setting(per_city_key("quiet_hours_start", "spb"), "23:00"))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))

    assert "Питер" in text
    assert "23:00" in text


def test_all_cities_header_no_city_line(tmp_path, _restore_cities_cache):
    _roles_ready(tmp_path)
    import cities as cities_mod
    _run(db.insert_city("msk", "Москва", "", 0))
    _run(db.set_setting("event_city_enabled", "on"))
    _run(cities_mod.reload_cities())
    _run(cities_mod.set_admin_city(ADMIN_ID, cities_mod.ALL_CITIES))

    text, kb = _run(qh_screen.render_quiet_hours_screen(ADMIN_ID))
    assert "Город:" not in text


# ── Хендлер: колбэк рендерит и отвечает ─────────────────────────────────────────────────────

def test_admin_quiet_hours_callback_renders_screen(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("admin_quiet_hours")

    _run(qh_screen.admin_quiet_hours(cb))

    assert cb.message.text is not None
    assert "Тихие часы" in cb.message.text
    assert cb.answers
