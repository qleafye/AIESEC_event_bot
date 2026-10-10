"""«📨 Отправить решение заново» на карточке /find: строка о недоставке, подтверждение с началом
текста, результат «доставлено / не доставлено», права и город менеджера.

Паттерн — tests/test_card_actions_260925.py: fake callback/bot на слое хендлера, БД —
шаблонная копия, `asyncio.run()` на каждый сценарий."""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError

import domain.cities as cities
from config import config
from database import db
from handlers.admin_caps import ADMIN_CAPS, required_capability, role_caps_key
from services import decision_delivery
from tests._dbtpl import fast_init_db

ADMIN_ID = 261009001
BOUND_MSK_ID = 261009002
DELEGATE_ID = 261009101

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resend_decision_261009.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    config.GOOGLE_SHEET_ID = ""
    config.GOOGLE_CREDENTIALS_FILE = ""


async def _seed(status="approved", city="msk"):
    await db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Иван Иванов", "registration_date": "2026-01-01",
        "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, DELEGATE_ID))
        await conn.commit()


class _Bot:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return object()


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _Cb:
    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.bot = bot or _Bot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _blocked():
    return TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")


# ── строка о недоставке ──────────────────────────────────────────────────────────────────────

def test_failure_line_only_for_failed_decided():
    base = {"status": "approved", "decision_delivery_status": "failed",
            "decision_delivery_error": "бот заблокирован делегатом"}
    assert "Письмо о решении не дошло: бот заблокирован делегатом" in decision_delivery.failure_line(base)
    assert decision_delivery.failure_line({**base, "decision_delivery_status": "delivered"}) == ""
    assert decision_delivery.failure_line({**base, "status": "pending"}) == ""
    assert decision_delivery.failure_line(None) == ""
    assert "причина неизвестна" in decision_delivery.failure_line({**base, "decision_delivery_error": None})


def test_failure_line_escapes_html():
    line = decision_delivery.failure_line({
        "status": "rejected", "decision_delivery_status": "failed",
        "decision_delivery_error": "ошибка отправки: <b>x</b>",
    })
    assert "<b>x</b>" not in line and "&lt;b&gt;" in line


# ── карточка /find ───────────────────────────────────────────────────────────────────────────

def _find_card():
    import handlers.admin as admin_mod

    class _M:
        text = f"/find @{DELEGATE_ID}"
        captured = {}

        async def answer(self, text, parse_mode=None, reply_markup=None):
            _M.captured = {"text": text, "kb": reply_markup}

    async def _get(username):
        return await db.get_user(DELEGATE_ID)

    orig = admin_mod.get_user_by_username
    admin_mod.get_user_by_username = _get
    try:
        _run(admin_mod.cmd_find_user(_M()))
    finally:
        admin_mod.get_user_by_username = orig
    return _M.captured


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_card_has_resend_button_for_decided(tmp_path, status):
    _db_ready(tmp_path)
    _run(_seed(status=status))
    card = _find_card()
    assert f"decresend_start:{DELEGATE_ID}" in _cbs(card["kb"])
    assert "Письмо о решении не дошло" not in card["text"]


def test_card_no_resend_button_for_pending(tmp_path):
    _db_ready(tmp_path)
    _run(_seed(status="pending"))
    assert f"decresend_start:{DELEGATE_ID}" not in _cbs(_find_card()["kb"])


def test_card_shows_failure_reason(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="approved")
        await db.record_decision_delivery(DELEGATE_ID, "approved", "failed", "бот заблокирован делегатом")

    _run(scenario())
    assert "Письмо о решении не дошло: бот заблокирован делегатом" in _find_card()["text"]


# ── подтверждение ────────────────────────────────────────────────────────────────────────────

def test_start_shows_confirm_with_text_start(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="approved")
        await db.set_setting("approve_text", "Поздравляем! " + "Очень длинный текст одобрения. " * 10)
        cb = _Cb(f"decresend_start:{DELEGATE_ID}", ADMIN_ID)
        await h.decresend_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Отправить <b>Иван Иванов</b> письмо о решении ещё раз?" in text
    assert "Он(а) получит: «Поздравляем!" in text and "…»" in text
    assert [b.text for row in kb.inline_keyboard for b in row] == ["✅ Отправить", "Отмена"]
    assert _cbs(kb) == [f"decresend_go:{DELEGATE_ID}", f"decresend_cancel:{DELEGATE_ID}"]


def test_start_rejected_preview_has_reject_text_and_reason(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="rejected")
        await db.set_setting("reject_text", "К сожалению, заявка отклонена.")
        cb = _Cb(f"decresend_start:{DELEGATE_ID}", ADMIN_ID)
        await h.decresend_start(cb)
        return cb

    text, _ = _run(scenario()).message.edits[0]
    assert "К сожалению, заявка отклонена." in text


def test_start_refuses_pending(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="pending")
        cb = _Cb(f"decresend_start:{DELEGATE_ID}", ADMIN_ID)
        await h.decresend_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == [] and cb.answers[0][1] is True


def test_start_denied_for_other_city_manager(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(status="approved", city="spb")
        cb = _Cb(f"decresend_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await h.decresend_start(cb)
        go = _Cb(f"decresend_go:{DELEGATE_ID}", BOUND_MSK_ID)
        await h.decresend_go(go)
        return cb, go

    cb, go = _run(scenario())
    assert cb.message.edits == [] and go.message.edits == []
    assert go.bot.sent == []
    assert cb.answers[0][1] is True and go.answers[0][1] is True


def test_cancel_sends_nothing(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)
    cb = _Cb(f"decresend_cancel:{DELEGATE_ID}", ADMIN_ID)
    _run(h.decresend_cancel(cb))
    assert cb.bot.sent == [] and "отменена" in cb.message.edits[0][0]


# ── отправка ─────────────────────────────────────────────────────────────────────────────────

def test_go_delivered_records_status_and_sends_decision_text(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="approved")
        await db.record_decision_delivery(DELEGATE_ID, "approved", "failed", "чат не найден")
        cb = _Cb(f"decresend_go:{DELEGATE_ID}", ADMIN_ID)
        await h.decresend_go(cb)
        return cb, await db.get_user(DELEGATE_ID)

    cb, user = _run(scenario())
    assert cb.message.edits[0][0] == "✅ Доставлено"
    assert len(cb.bot.sent) == 1 and cb.bot.sent[0][0] == DELEGATE_ID
    assert user["decision_delivery_status"] == "delivered"
    assert user["status"] == "approved"


def test_go_rejected_sends_reject_text_with_reason(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="rejected")
        await db.set_setting("reject_text", "Заявка отклонена.")
        cb = _Cb(f"decresend_go:{DELEGATE_ID}", ADMIN_ID)
        await h.decresend_go(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits[0][0] == "✅ Доставлено"
    assert "Заявка отклонена." in cb.bot.sent[0][1]


def test_go_blocked_reports_reason_and_keeps_failed_status(tmp_path):
    from handlers import admin_resend_decision as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="approved")
        cb = _Cb(f"decresend_go:{DELEGATE_ID}", ADMIN_ID, bot=_Bot(error=_blocked()))
        await h.decresend_go(cb)
        return cb, await db.get_user(DELEGATE_ID)

    cb, user = _run(scenario())
    assert cb.message.edits[0][0] == (
        "❌ Не доставлено: бот заблокирован делегатом — свяжитесь с делегатом напрямую"
    )
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_error"] == "бот заблокирован делегатом"


def test_resend_one_refuses_pending(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed(status="pending")
        return await decision_delivery.resend_one_decision(_Bot(), DELEGATE_ID)

    result = _run(scenario())
    assert result["ok"] is False


# ── права ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["decresend_start", "decresend_go", "decresend_cancel"])
def test_resend_callbacks_require_moderate_reg(name):
    assert required_capability(callback_data=f"{name}:{DELEGATE_ID}") == "moderate_reg"
    assert f"{name}:*" in ADMIN_CAPS


# ── «📱 Приложение в браузере» рядом с «🌐 Открыть дашборд» ──────────────────────────────────

def _stats_kb(miniapp: str, url: str):
    import handlers.admin as admin_mod

    async def scenario():
        await db.set_setting("miniapp_enabled", miniapp)
        return await admin_mod._stats_keyboard_for(ADMIN_ID)

    old = config.DASHBOARD_PUBLIC_URL
    config.DASHBOARD_PUBLIC_URL = url
    try:
        return _run(scenario())
    finally:
        config.DASHBOARD_PUBLIC_URL = old


def test_browser_app_button_when_enabled_and_url_set(tmp_path):
    _db_ready(tmp_path)
    kb = _stats_kb("on", "https://yl26.alekseev.info/")
    assert kb.inline_keyboard[0][0].text == "🌐 Открыть дашборд"
    second = kb.inline_keyboard[1][0]
    assert second.text == "📱 Приложение в браузере"
    assert second.url == "https://yl26.alekseev.info/app"


def test_browser_app_button_kept_when_miniapp_off(tmp_path):
    """Выключенное приложение закрыто только делегатам — менеджерский запасной вход остаётся."""
    _db_ready(tmp_path)
    texts = [b.text for row in _stats_kb("off", "https://yl26.alekseev.info").inline_keyboard for b in row]
    assert "📱 Приложение в браузере" in texts
    assert "🌐 Открыть дашборд" in texts


def test_browser_app_button_hidden_without_dashboard_url(tmp_path):
    _db_ready(tmp_path)
    texts = [b.text for row in _stats_kb("on", "").inline_keyboard for b in row]
    assert "📱 Приложение в браузере" not in texts
