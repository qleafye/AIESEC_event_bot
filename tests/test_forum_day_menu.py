"""Идея №1 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): режим «день форума»
главного меню делегата (per_city) — `services/forum_day_menu.py::is_forum_day_menu_active_for_city`
+ `keyboards/builders.py::get_main_menu_kb` (реордер «🎟 Мой QR»/«📅 Программа»/«❗ Важное»/
«🆘 SOS» наверх, остальное сдвигается вниз, не прячется).

pytest-asyncio недоступна в этом окружении — каждый async-вызов через `asyncio.run()`,
`config.DB_PATH` указывает на файл в `tmp_path`, БД — `tests/_dbtpl.py::fast_init_db` (та же
форма, что `tests/test_sos_260924.py`, откуда позаимствован `_add_delegate`).

Два стиля времени в этом файле:
- Точные пороги окна (вечер накануне/конец форума) — монкипатч `services.forum_day_menu.
  msk_now` (модульный импорт, тот же приём, что `tests/test_checkin_qr_broadcast_260924.py`
  использует для `services.checkin_broadcast.msk_now`), юнит-тесты бьют напрямую в
  `is_forum_day_menu_active_for_city`, БЕЗ get_main_menu_kb.
- Интеграционные тесты меню (`get_main_menu_kb`) НЕ морозят время -- `forum_date` ставится на
  РЕАЛЬНОЕ «сегодня» (`msk_now()`), тот же приём, что `tests/test_sos_260924.py::
  test_menu_sos_button_shown_within_active_window`: при дефолтных `sos_active_days=2` окно
  «день форума» держит ЛЮБОЕ время сегодняшних и завтрашних суток (граница «вчера вечером»
  уже позади к моменту прогона теста), поэтому монкипатчить ещё и `keyboards.builders.msk_now`
  (независимый резолв `has_important_today`) не нужно."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
from keyboards.builders import get_main_menu_kb
from services import forum_day_menu as fdm
from services.timeutil import msk_now
from tests._dbtpl import fast_init_db

ADMIN_ID = 926001
DELEGATE_ID = 926010
DELEGATE2_ID = 926011


def _ready(tmp_path, name="test_forum_day_menu.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _enable_cities():
    _run(db.set_setting("event_city_enabled", "on"))


async def _add_delegate(tid: int, event_city: str | None = None, **extra):
    payload = {
        "telegram_id": tid, "full_name": "Тест Делегатов", "username": "testdel",
        "university": "ВШЭ", "phone": "+79990000000", "event_city": event_city,
        "registration_date": "2026-09-01 00:00:00", "status": "approved",
    }
    payload.update(extra)
    await db.add_user(payload)


async def _seed_important_delivery(admin_id: int, chat_id: int, text: str) -> None:
    """Важная рассылка ПРЯМО СЕЙЧАС (реальный msk_now через record_broadcast_delivery) --
    та же форма, что tests/test_broadcast_important_menu_260924.py, без переопределения
    sent_at (нужно ровно «сегодня», а не конкретный момент)."""
    bid = await db.create_broadcast(admin_id, text[:80], 1, important=True, full_text=text)
    await db.record_broadcast_delivery(bid, chat_id, 1)


def _menu_texts(kb):
    return [b.text for row in kb.keyboard for b in row]


def _freeze(monkeypatch, when: datetime):
    monkeypatch.setattr(fdm, "msk_now", lambda: when)


# ══════════════════════════════════════════════════════════════════════════════════════════
# services.forum_day_menu.is_forum_day_menu_active_for_city -- окно по времени (юнит)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_false_without_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _freeze(monkeypatch, datetime(2026, 10, 15, 19, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_false_when_toggle_off_even_within_window(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 16, 12, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_false_evening_before_prior_to_start_time_default_18(tmp_path, monkeypatch):
    """Накануне, ДО дефолтных 18:00 -- обычное меню."""
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 15, 17, 59))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_true_evening_before_at_and_after_start_time_default_18(tmp_path, monkeypatch):
    """Накануне, РОВНО в 18:00 и позже -- форумное меню."""
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 15, 18, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True
    _freeze(monkeypatch, datetime(2026, 10, 15, 23, 30))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True


def test_true_all_day_on_forum_start_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 16, 8, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True


def test_true_through_end_of_last_day_default_two_day_window(tmp_path, monkeypatch):
    """Дефолт sos_active_days=2 -- форум идёт «сегодня-завтра», окно держится до конца
    ВТОРОГО дня (23:59:59)."""
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 17, 23, 59))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True


def test_false_after_window_closes(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 18, 0, 1))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_respects_custom_sos_active_days(tmp_path, monkeypatch):
    """Один день форума (sos_active_days=1) -- окно закрывается в конце ПЕРВОГО дня, а не
    второго (byte-в-byte та же настройка, что использует services.sos)."""
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _run(db.set_setting("sos_active_days", "1"))
    _freeze(monkeypatch, datetime(2026, 10, 16, 23, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True
    _freeze(monkeypatch, datetime(2026, 10, 17, 8, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_respects_custom_start_time(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _run(db.set_setting("forum_day_menu_start_time", "20:30"))
    _freeze(monkeypatch, datetime(2026, 10, 15, 19, 59))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False
    _freeze(monkeypatch, datetime(2026, 10, 15, 20, 30))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True


def test_false_on_garbage_forum_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "не дата"))
    _freeze(monkeypatch, datetime(2026, 10, 16, 12, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False


def test_garbage_start_time_falls_back_to_default_18(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", "16.10.2026"))
    _run(db.set_setting("forum_day_menu_start_time", "не время"))
    _freeze(monkeypatch, datetime(2026, 10, 15, 17, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is False  # ещё дефолтные 18:00
    _freeze(monkeypatch, datetime(2026, 10, 15, 18, 30))
    assert _run(fdm.is_forum_day_menu_active_for_city(None)) is True


# ══════════════════════════════════════════════════════════════════════════════════════════
# По городам: СПб в окне форума, Москва -- нет (свои независимые forum_date/toggle)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_percity_spb_active_msk_not(tmp_path, monkeypatch):
    _ready(tmp_path)
    _enable_cities()
    _run(db.set_setting("forum_day_menu_enabled__city__spb", "on"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _run(db.set_setting("forum_day_menu_enabled__city__msk", "on"))
    _run(db.set_setting("forum_date__city__msk", "30.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city("spb")) is True
    assert _run(fdm.is_forum_day_menu_active_for_city("msk")) is False


def test_percity_msk_window_independent_of_spb(tmp_path, monkeypatch):
    _ready(tmp_path)
    _enable_cities()
    _run(db.set_setting("forum_day_menu_enabled__city__spb", "on"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _run(db.set_setting("forum_day_menu_enabled__city__msk", "on"))
    _run(db.set_setting("forum_date__city__msk", "30.10.2026"))
    _freeze(monkeypatch, datetime(2026, 10, 30, 9, 0))
    assert _run(fdm.is_forum_day_menu_active_for_city("msk")) is True
    assert _run(fdm.is_forum_day_menu_active_for_city("spb")) is False


# ══════════════════════════════════════════════════════════════════════════════════════════
# Меню: реордер get_main_menu_kb (реальное «сегодня», без монкипатча времени -- см. докстринг
# модуля)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_menu_order_unaffected_when_toggle_off(tmp_path):
    """Дефолт "off" -- порядок меню байт-в-байт как без фичи вовсе (никакой перестановки)."""
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", today))
    _run(_add_delegate(DELEGATE_ID))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = _menu_texts(kb)
    # menu_checkin_qr/menu_program/menu_important не прошли бы свои обычные гейты (нет
    # checkin_qr_enabled/программы/важного за сегодня) -- реферальная ссылка идёт первой,
    # как в MENU_BUTTONS.
    assert texts[0] == "🔗 Моя реферальная ссылка"


def test_menu_reorders_priority_buttons_to_top_within_forum_day_window(tmp_path):
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", today))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(_add_delegate(DELEGATE_ID))
    _run(_seed_important_delivery(ADMIN_ID, DELEGATE_ID, "Важная новость"))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = _menu_texts(kb)
    # SOS активен в то же окно (forum_date общий, дефолтные sos_active_days=2); программа без
    # сессий/фото не гейтится -- её в списке нет вовсе (свой обычный гейт первичен).
    expected_top = ["🎟 Мой QR", "❗ Важное", "🆘 SOS"]
    assert texts[: len(expected_top)] == expected_top
    assert "🔗 Моя реферальная ссылка" in texts[len(expected_top):]


def test_menu_reorder_keeps_relative_order_of_non_priority_buttons(tmp_path):
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_date", today))
    _run(_add_delegate(DELEGATE_ID))

    _run(db.set_setting("forum_day_menu_enabled", "off"))
    texts_off = _menu_texts(_run(get_main_menu_kb(DELEGATE_ID)))

    _run(db.set_setting("forum_day_menu_enabled", "on"))
    texts_on = _menu_texts(_run(get_main_menu_kb(DELEGATE_ID)))

    priority_texts = {"🎟 Мой QR", "📅 Программа форума", "❗ Важное", "🆘 SOS"}
    rest_off = [t for t in texts_off if t not in priority_texts]
    rest_on = [t for t in texts_on if t not in priority_texts]
    assert rest_off == rest_on
    assert rest_off  # список "остального" не пуст -- проверка не тривиальна


def test_menu_gate_still_hides_priority_button_without_own_toggle(tmp_path):
    """Режим «день форума» ничего не включает сам -- «🎟 Мой QR» остаётся скрытой, пока
    checkin_qr_enabled выключен (свой обычный гейт первичен)."""
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", today))
    _run(_add_delegate(DELEGATE_ID))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = _menu_texts(kb)
    assert "🎟 Мой QR" not in texts


def test_menu_percity_reorder_only_for_city_in_window(tmp_path):
    _ready(tmp_path)
    _enable_cities()
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_day_menu_enabled__city__spb", "on"))
    _run(db.set_setting("forum_date__city__spb", today))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(_add_delegate(DELEGATE_ID, event_city="spb"))
    _run(_add_delegate(DELEGATE2_ID, event_city="msk"))
    texts_spb = _menu_texts(_run(get_main_menu_kb(DELEGATE_ID)))
    texts_msk = _menu_texts(_run(get_main_menu_kb(DELEGATE2_ID)))
    assert texts_spb[0] == "🎟 Мой QR"
    assert texts_msk[0] != "🎟 Мой QR"


def test_menu_reorder_translates_to_english_labels(tmp_path):
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", today))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("delegate_lang_enabled", "on"))
    _run(_add_delegate(DELEGATE_ID))
    _run(db.set_user_lang(DELEGATE_ID, "en"))
    kb = _run(get_main_menu_kb(DELEGATE_ID))
    texts = _menu_texts(kb)
    assert texts[0] == "🎟 My QR"
    assert "🔗 My referral link" in texts


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр -- дефолты/формат/сторож TOGGLE_SECTION (см. tests/test_settings_ops.py для полного
# охвата "каждый ключ группы toggles покрыт ровно один раз" -- здесь только точечная сверка
# двух новых ключей, чтобы регрессия формата была видна прямо в файле фичи).
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_defaults_and_format():
    from domain.settings.schema import SETTINGS_SCHEMA
    import domain.settings.ops as settings_ops

    enabled = SETTINGS_SCHEMA["forum_day_menu_enabled"]
    assert enabled["default"] == "off"
    assert enabled["per_city"] is True
    assert enabled["type"] == "enum"

    start_time = SETTINGS_SCHEMA["forum_day_menu_start_time"]
    assert start_time["default"] == "18:00"
    assert start_time["format"] == "time"
    assert start_time["per_city"] is True

    assert settings_ops.TOGGLE_SECTION["forum_day_menu_enabled"] == "apps"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Экран «🎪 Форум: функции» -- строка + свой экран настройки (handlers/admin_forum_functions.py)
# Fake-объекты -- та же форма, что tests/test_admin_sos_settings_260924.py.
# ══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id=ADMIN_ID):
        self.from_user = _FakeUser(user_id)
        self.answers_sent = []
        self.text_edited = None
        self.edit_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = message if message is not None else _FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_hub_shows_forum_day_menu_row_and_button(tmp_path):
    from handlers import admin_forum_functions as aff
    _ready(tmp_path)
    text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "Меню «день форума»" in text
    assert "forumdaymenu_cfg:msk" in _cbs(kb)


def test_hub_status_line_reflects_active_window(tmp_path):
    from handlers import admin_forum_functions as aff
    _ready(tmp_path)
    today = msk_now().strftime("%d.%m.%Y")
    _run(db.set_setting("forum_day_menu_enabled", "on"))
    _run(db.set_setting("forum_date", today))
    text, _kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "сегодня форум — меню форумное" in text


def test_cfg_screen_toggle_flips_setting(tmp_path):
    from handlers import admin_forum_functions as aff
    _ready(tmp_path)
    callback = _FakeCallback("forumdaymenu_toggle:_all")
    _run(aff.forumdaymenu_toggle_go(callback))
    assert _run(db.get_setting("forum_day_menu_enabled")) == "on"
    callback2 = _FakeCallback("forumdaymenu_toggle:_all")
    _run(aff.forumdaymenu_toggle_go(callback2))
    assert _run(db.get_setting("forum_day_menu_enabled")) == "off"


def test_cfg_screen_writes_percity_key_when_module_on(tmp_path):
    from handlers import admin_forum_functions as aff
    from handlers.admin_caps import role_caps_key
    _ready(tmp_path)
    _enable_cities()
    _run(db.set_setting(role_caps_key("reg_manager"), "moderate_reg"))
    _run(db.add_staff(926099, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(926099, "spb"))
    callback = _FakeCallback("forumdaymenu_toggle:spb", user_id=926099)
    _run(aff.forumdaymenu_toggle_go(callback))
    assert _run(db.get_setting("forum_day_menu_enabled__city__spb")) == "on"
    assert _run(db.get_setting("forum_day_menu_enabled")) is None


def test_cfg_screen_warns_without_forum_date(tmp_path):
    from handlers import admin_forum_functions as aff
    _ready(tmp_path)
    text, _kb = _run(aff._forumdaymenu_cfg_text_kb(None))
    assert "не задана" in text
