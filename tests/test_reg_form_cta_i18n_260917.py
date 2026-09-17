"""Квик 260917-en (живая проверка 17.09, находка 2) — регрессия: inline-кнопка «📱 Заполнить в
приложении» под приветствием (`handlers/registration.py::_reg_form_cta_kb`) оставалась русской
при lang=en, потому что подпись бралась напрямую из `get_setting_typed("reg_form_cta_text")`
без единого прохода через `reg_i18n.tr_text`/UI_EN — в отличие от остальных кнопок делегатского
чата, у этой не было шва перевода вовсе (не баг шва, баг отсутствия шва).

Тот же приём, что `tests/test_miniapp_entry.py` использует для DASHBOARD_PUBLIC_URL/DB, и что
`tests/test_i18n_bot_render_27.py` использует для монкипатча `reg_i18n.ctx_for` — фиксированный
`(lang, tr_map)` без похода в БД за языком делегата.
"""
import asyncio

from config import config
from database import db
from handlers import reg_i18n
from handlers import registration as reg

UID = 260917001


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_form_cta_i18n_260917.db")
    asyncio.run(db.init_db())
    config.DASHBOARD_PUBLIC_URL = "https://yl26.example.com"
    asyncio.run(db.set_setting("miniapp_enabled", "on"))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat_id=UID):
        self.chat = _FakeChat(chat_id)


def _patch_ctx(monkeypatch, lang, tr_map):
    async def _ctx(_target):
        return lang, dict(tr_map)

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)


def test_cta_button_stays_russian_at_ru(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_ctx(monkeypatch, "ru", {})

    kb = asyncio.run(reg._reg_form_cta_kb(_FakeMessage()))

    assert kb.inline_keyboard[0][0].text == "📱 Заполнить в приложении"


def test_cta_button_translates_default_text_via_layer_a_at_en(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_ctx(monkeypatch, "en", {})

    kb = asyncio.run(reg._reg_form_cta_kb(_FakeMessage()))

    assert kb.inline_keyboard[0][0].text == "📱 Fill in the app"


def test_cta_button_manager_override_falls_back_to_tr_map_at_en(tmp_path, monkeypatch):
    """Кастомная подпись менеджера (не дефолт из UI_EN) идёт через ярус B (машинный перевод в
    `translations`, здесь — фиктивная карта) — тот же контур, что и у любого другого текста
    корпуса группы `reg`."""
    _db_ready(tmp_path)
    custom = "Жми сюда, чтобы заполнить анкету"
    asyncio.run(db.set_setting("reg_form_cta_text", custom))
    tr_map = {reg_i18n.i18n_service.src_hash(custom): "Tap here to fill in the form"}
    _patch_ctx(monkeypatch, "en", tr_map)

    kb = asyncio.run(reg._reg_form_cta_kb(_FakeMessage()))

    assert kb.inline_keyboard[0][0].text == "Tap here to fill in the form"


def test_cta_button_web_app_url_untouched_by_translation(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_ctx(monkeypatch, "en", {})

    kb = asyncio.run(reg._reg_form_cta_kb(_FakeMessage()))

    assert kb.inline_keyboard[0][0].web_app.url == "https://yl26.example.com/app"


def test_cta_button_none_when_miniapp_disabled(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_enabled", "off"))
    _patch_ctx(monkeypatch, "en", {})

    assert asyncio.run(reg._reg_form_cta_kb(_FakeMessage())) is None
