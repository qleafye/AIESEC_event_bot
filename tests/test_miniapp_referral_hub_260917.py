"""Приёмка 17.09 (п.1/2): постоянное место реф-ссылки в хабе делегата (`GET /app/api/hub` ->
`referral`) + пояснение под ссылкой амбассадора, упоминающее это место (`{section}` подставляет
`miniapp_hub_referral_label_text`).

Правило видимости — ТО ЖЕ, что у кнопки чата «🔗 Моя реферальная ссылка»
(`keyboards/builders.py::MENU_BUTTONS`, тумблер `menu_referral`): сам маршрут уже требует
`delegate_gate` (аналог `ensure_registered`), здесь дополнительно проверяется тот же тумблер.
Число приглашённых — второй, независимый тумблер `menu_invites` (кнопка «👥 Мои приглашённые»).

Харнесс — тот же приём, что `tests/test_miniapp_hub_status_260904.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_referral_hub.db")
    _standard_seed()
    return _client(_cfg(db_path))


def test_referral_present_by_default_and_uses_amb_link_format(client):
    """Формат ссылки — тот же, что у кнопки меню «🔗 Моя реферальная ссылка»
    (`handlers/user_actions.py::my_referral_link`): `?start=<telegram_id>`, БЕЗ префикса
    `amb_` — тот формат принадлежит отдельному потоку «Хочу свою ссылку» финального экрана
    анкеты (`handlers/reg_ambassador.py`) и сюда не переносится."""
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    referral = body["referral"]
    assert referral is not None
    assert referral["link"] == f"https://t.me/YouLead_test_bot?start=amb_{DELEGATE_ID}"
    assert "amb_" not in referral["link"]
    assert referral["label"]
    assert referral["copy_button"]
    assert referral["copied_toast"]
    # Никто ещё не зарегистрировался по ссылке — count=0, строка всё равно есть (тумблер
    # menu_invites включён по умолчанию).
    assert referral["invites_text"]


def test_referral_hidden_when_menu_referral_toggle_off(client):
    """Тот же тумблер, что прячет кнопку в чате, прячет и постоянное место в приложении —
    одно правило видимости на обе поверхности."""
    _set("menu_referral", "off")
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    assert body["referral"] is None


def test_referral_invites_text_hidden_when_menu_invites_toggle_off(client):
    """Второй, независимый тумблер (кнопка «👥 Мои приглашённые») — выключен -> ссылка
    остаётся, счётчика нет."""
    _set("menu_invites", "off")
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    referral = body["referral"]
    assert referral is not None
    assert referral["link"]
    assert referral["invites_text"] is None


def test_referral_invites_text_counts_real_referrals(client):
    async def seed():
        await bot_db.add_user({
            "telegram_id": DELEGATE_ID + 500, "full_name": "Приглашённый",
            "registration_date": "2026-09-17", "referrer_id": DELEGATE_ID,
        })

    _run(seed())
    body = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()
    tpl = body["referral"]["invites_text"]
    assert "1" in tpl


def test_referral_denied_for_pending_delegate_via_gate(client):
    """`delegate_gate` (аналог `ensure_registered`) уже отдаёт 403 всему маршруту — до чтения
    `menu_referral` дело не доходит, ровно как у соседних плиток хаба."""
    resp = client.get("/app/api/hub", headers=_hdr(PENDING_ID))
    assert resp.status_code == 403


# ── Ссылка + пояснение на финальном экране анкеты — упоминает постоянное место в приложении ──

def test_ambassador_note_mentions_hub_referral_section_label(client):
    from settings_schema import SETTINGS_SCHEMA

    resp = client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    note = resp.json()["note"]
    assert note
    assert "{section}" not in note
    assert SETTINGS_SCHEMA["miniapp_hub_referral_label_text"]["default"] in note


# ── Структурные сторожа фронта: постоянное место переиспользует блок финального экрана ──────

def test_hub_js_renders_referral_block_via_shared_helper():
    from tests.test_miniapp_frontend import MINIAPP_STATIC, _js_without_comments

    text = _js_without_comments(MINIAPP_STATIC / "js" / "screens" / "hub.js")
    assert "ambassadorLinkBlock" in text
    assert "hub.referral" in text


def test_ui_js_exports_ambassador_link_block():
    from tests.test_miniapp_frontend import MINIAPP_STATIC, _js_without_comments

    text = _js_without_comments(MINIAPP_STATIC / "js" / "ui.js")
    assert "export function ambassadorLinkBlock(" in text
    assert 'h("div", { class: "ambassador-link-box", text: res.link' in text


def test_form_js_delegates_to_shared_ambassador_link_block():
    """`screens/form.js` больше не несёт вторую копию рендера — вызывает импортированную
    функцию (не дублирует `.ambassador-link-box`)."""
    from tests.test_miniapp_frontend import MINIAPP_STATIC, _js_without_comments

    text = _js_without_comments(MINIAPP_STATIC / "js" / "screens" / "form.js")
    assert "ambassadorLinkBlock(h, res, { haptic, say })" in text
    assert "ambassador-link-box" not in text
