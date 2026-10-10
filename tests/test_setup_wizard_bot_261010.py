"""«🚀 Первая настройка» в боте (`handlers/admin_setup_wizard.py`): тот же список шагов, что в
приложении, кнопки полей — в обычные экраны правки, после сохранения — назад в шаг мастера.

pytest-asyncio в окружении нет — async через `asyncio.run()`, БД — `tests/_dbtpl.fast_init_db`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from config import config
from database import db
from handlers import admin_sections, admin_settings, admin_setup_wizard as wiz
from handlers.admin_caps import required_capability
from miniapp.setup_wizard import STEPS
from tests._dbtpl import fast_init_db

ADMIN = 900261031
MANAGER = 900261032


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="setup_wizard_bot.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]
    wiz._return_to.clear()


def _state(uid=ADMIN):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _CB:
    def __init__(self, data, uid=ADMIN):
        self.data = data
        self.from_user = User(id=uid, is_bot=False, first_name="Админ")
        self.alerts = []
        self.edited = []
        outer = self

        class _Message:
            async def edit_text(self, text, **kwargs):
                outer.edited.append((text, kwargs.get("reply_markup")))

            async def answer(self, text, **kwargs):
                outer.edited.append((text, kwargs.get("reply_markup")))

        self.message = _Message()

    async def answer(self, text=None, show_alert=False, **kwargs):
        if show_alert:
            self.alerts.append(text)

    def model_copy(self, update):
        clone = _CB(update.get("data", self.data), self.from_user.id)
        clone.message = self.message
        return clone


def _callbacks(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_overview_lists_same_steps_as_app_with_progress(tmp_path):
    _ready(tmp_path)
    text, kb = _run(wiz.overview_screen())
    assert "🚀 Первая настройка" in text
    assert "из" in text and "готово" in text
    cbs = _callbacks(kb)
    # Тип события по умолчанию — форум: шаги СкиллАпа и конференции скрыты, как в приложении.
    assert "setupw_s:event_type" in cbs and "setupw_s:event_info" in cbs
    assert "setupw_s:su_options" not in cbs and "setupw_s:conf_lc" not in cbs
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any(label.startswith("⬜") and "Тип события" in label for label in labels)  # явный выбор не сделан


def test_event_type_step_done_after_explicit_choice(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_type", "forum"))
    _, kb = _run(wiz.overview_screen())
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any(label.startswith("✅") and "Тип события" in label for label in labels)


def test_step_screen_buttons_lead_to_bot_editors_and_navigation(tmp_path):
    _ready(tmp_path)
    text, kb = _run(wiz.step_screen("event_info"))
    assert "Шаг 3 из" in text
    cbs = _callbacks(kb)
    assert "setupw_f:event_info:event_name" in cbs
    assert "setupw_s:botfather" in cbs and "setupw_s:contacts" in cbs  # «Назад» / «Дальше»
    assert cbs[-1] == "admin_setup_wizard"


def test_app_only_fields_are_labelled_and_menu_goes_to_menu_buttons(tmp_path):
    _ready(tmp_path)
    text, kb = _run(wiz.step_screen("theme"))
    assert "в приложении" in text and wiz.APP_HINT in text
    _, kb = _run(wiz.step_screen("menu"))
    assert "setupw_scr:menu:admin_menu_buttons" in _callbacks(kb)
    _, kb = _run(wiz.step_screen("reg_prompts"))
    assert "admin_sec:form" in _callbacks(kb)


def test_field_tap_opens_bot_editor_and_save_returns_to_step(tmp_path, monkeypatch):
    _ready(tmp_path)
    opened = []

    async def _fake_edit(callback, state):
        opened.append(callback.data)

    monkeypatch.setattr(admin_settings, "settings_edit_start", _fake_edit)
    _run(wiz.setup_wizard_field(_CB("setupw_f:event_info:event_name"), _state()))
    assert opened == ["settings_edit:event_name"]

    # После сохранения редактор зовёт settings_return_screen — мастер перехватывает возврат.
    text, kb = _run(admin_sections.settings_return_screen(ADMIN, setting_key="event_name"))
    assert "Информация о событии" in text and "setupw_f:event_info:event_name" in _callbacks(kb)
    # Возврат разовый: следующая правка вне мастера идёт обычным путём, на экран группы.
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, setting_key="event_name"))
    assert "Информация о событии" not in text


def test_photo_field_uses_photo_editor(tmp_path, monkeypatch):
    _ready(tmp_path)
    opened = []

    async def _fake_photo(callback, state):
        opened.append(callback.data)

    monkeypatch.setattr(admin_settings, "settings_photo_start", _fake_photo)
    _run(wiz.setup_wizard_field(_CB("setupw_f:welcome:start"), _state()))
    assert opened == ["settings_photo:start"]


def test_only_superadmin(tmp_path):
    _ready(tmp_path)
    for data in ("admin_setup_wizard", "setupw_s:event_info"):
        cb = _CB(data, uid=MANAGER)
        handler = wiz.setup_wizard_overview if data == "admin_setup_wizard" else wiz.setup_wizard_step
        _run(handler(cb))
        assert cb.alerts == [wiz.ONLY_SUPERADMIN] and cb.edited == []
    cb = _CB("setupw_f:event_info:event_name", uid=MANAGER)
    _run(wiz.setup_wizard_field(cb, _state(MANAGER)))
    assert cb.alerts == [wiz.ONLY_SUPERADMIN]
    assert wiz.pop_return(MANAGER) is None


def test_entry_row_in_manage_for_superadmin_only():
    rows = admin_sections.section_rows("manage")
    assert ("screen_admin", "admin_setup_wizard", "🚀 Первая настройка") in rows
    assert required_capability(callback_data="admin_setup_wizard") == "settings"
    assert required_capability(callback_data="setupw_s:event_info") == "settings"
    assert required_capability(callback_data="setupw_f:event_info:event_name") == "settings"


def test_every_wizard_step_renders(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_type", "skillup"))
    for step in STEPS:
        screen = _run(wiz.step_screen(step.key))
        if screen is None:
            continue  # шаг не виден при этом типе/модулях — как в приложении
        text, kb = screen
        assert step.title in text and kb.inline_keyboard


# ── Ревью 10.10 ───────────────────────────────────────────────────────────────────────────

def test_other_saves_are_not_hijacked_into_wizard(tmp_path, monkeypatch):
    _ready(tmp_path, "setup_wizard_hijack.db")

    async def _fake_edit(callback, state):
        return None

    monkeypatch.setattr(admin_settings, "settings_edit_start", _fake_edit)
    _run(wiz.setup_wizard_field(_CB("setupw_f:event_info:event_name"), _state()))
    # Другое сохранение и тумблер в это время идут своим путём, а не в мастер.
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, setting_key="approve_text"))
    assert "Информация о событии" not in text
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, callback_data="settings_toggle_reg"))
    assert "Информация о событии" not in text
    # Сохранение своего поля (в том числе городского значения) — назад в шаг мастера.
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, setting_key="event_name__city__msk"))
    assert "Информация о событии" in text


def test_city_header_value_counts_as_filled(tmp_path, monkeypatch):
    import domain.cities as cities

    _ready(tmp_path, "setup_wizard_city.db")
    _run(db.set_setting("event_city_enabled", "on"))
    code = cities.city_codes()[0]

    async def _header(_admin):
        return code

    monkeypatch.setattr(cities, "admin_selected_city", _header)
    key = cities.per_city_key("event_date", code)
    assert key is not None
    _run(db.set_setting(key, "30 октября"))
    _, kb = _run(wiz.step_screen("event_info", ADMIN))
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any(label.startswith("✅") and "Дата" in label for label in labels), labels


def test_conference_lc_question_opens_reg_questions_not_app(tmp_path):
    _ready(tmp_path, "setup_wizard_lc.db")
    _run(db.set_setting("event_type", "conference"))
    text, kb = _run(wiz.step_screen("conf_lc", ADMIN))
    assert "в приложении" not in text
    assert "setupw_scr:conf_lc:admin_reg_questions" in _callbacks(kb)


def test_screen_from_wizard_returns_on_back_only(tmp_path, monkeypatch):
    from handlers.regform import admin_reg_config

    _ready(tmp_path, "setup_wizard_screen.db")
    opened = []

    async def _fake_menu(callback):
        opened.append(callback.data)

    monkeypatch.setattr(admin_reg_config, "show_menu_buttons", _fake_menu)
    _run(wiz.setup_wizard_screen(_CB("setupw_scr:menu:admin_menu_buttons")))
    assert opened == ["admin_menu_buttons"]
    # Чужой экран не уводит в мастер, «Назад» с «🔘 Кнопки меню» — уводит в шаг «Главное меню».
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, callback_data="admin_reg_questions"))
    assert "Главное меню" not in text
    text, _ = _run(admin_sections.settings_return_screen(ADMIN, callback_data="admin_menu_buttons"))
    assert "Главное меню" in text
