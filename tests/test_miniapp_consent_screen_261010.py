"""Приёмка 09.10 (Mini App): экран согласия.

1. В приложении была только галочка с названием документа — текст согласия, который менеджер
   задаёт для чата (`reg_prompt_consent_<ключ>`), до приложения не доезжал. Теперь карточка
   несёт его в `text` (перевод — только ярусом A, как в чате: юридический текст машинно не
   переводится) и рисует над галочкой.
2. Ошибка без галочки «Нужно подтвердить согласия — вернитесь к шагу «Согласия».» — на «вы» и
   отсылает к шагу, на котором делегат уже стоит. Новый текст на «ты» и говорит, что сделать.
"""
from __future__ import annotations

from services.i18n_form_manual import _REGISTRY_TEXTS_EN
from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_miniapp_form import client, db_path  # noqa: F401 — фикстуры подтягиваются по имени
from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between
from tests.test_miniapp_routes import DELEGATE_ID, _hdr, _set

LEGAL = "Я соглашаюсь на обработку персональных данных АЙСЕК в России."


def _consent_item(client):
    body = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    return next(i for i in body["pre_items"] if i["type"] == "consent")


def test_consent_card_carries_manager_text(client):
    _set("consent_enabled", "on")
    _set("reg_prompt_consent_personal_data", LEGAL)
    assert _consent_item(client)["text"] == LEGAL


def test_consent_card_without_manager_text_has_none(client):
    _set("consent_enabled", "on")
    assert _consent_item(client)["text"] is None


def test_consent_required_text_is_on_ty_and_says_what_to_do():
    text = SETTINGS_SCHEMA["reg_form_consent_required_text"]["default"]
    assert "вернитесь" not in text
    assert "галочк" in text
    assert text in _REGISTRY_TEXTS_EN


def test_screen_draws_consent_text_above_checkbox():
    body = _between(_js_without_comments(FORM_SCREEN_JS), "function drawPre(consentItems, otherItems)", "async function next()")
    assert "item.text" in body
    card = body[body.index("const card = "):]
    assert card.index("item.text") < card.index('h("label", { class: "check" }')


# ── Ревью 10.10: текст согласия у английского делегата ────────────────────────────────────

LEGAL_EN = "I agree to the processing of my personal data by AIESEC in Russia."


def _en(client):
    _set("consent_enabled", "on")
    _set("delegate_lang_enabled", "on")
    _set("reg_prompt_consent_personal_data", LEGAL)
    resp = client.post("/app/api/reg/lang", headers=_hdr(DELEGATE_ID), json={"lang": "en"})
    assert resp.status_code == 200, resp.text


def test_en_delegate_gets_manual_translation(client):
    from database import db as bot_db
    from services import i18n
    from tests.test_miniapp_form import _run
    _en(client)
    _run(bot_db.upsert_translation("en", i18n.src_hash(LEGAL), LEGAL, LEGAL_EN, manual=1))
    assert _consent_item(client)["text"] == LEGAL_EN


def test_en_delegate_never_gets_machine_translation(client):
    from database import db as bot_db
    from services import i18n
    from tests.test_miniapp_form import _run
    _en(client)
    _run(bot_db.upsert_translation("en", i18n.src_hash(LEGAL), LEGAL, "machine text", manual=0))
    assert _consent_item(client)["text"] == LEGAL
