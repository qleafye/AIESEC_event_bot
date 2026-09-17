"""Квик 260917-en (уточнение владельца по пункту 1) — экран согласий Mini App (`pre_items`,
`miniapp/routers/form.py::_pre_items`) показывал подпись рядом с чекбоксом («Согласие на
обработку персональных данных») по-русски при `lang=en`, хотя это НАЗВАНИЕ документа, а не сам
юридический текст (тот же случай, что уже закрыт для чата бота — `handlers/registration.py`,
`handlers/reg_consent.py`, коммит «переводим название документа в карточке согласия»).

Фикс: `label` переводится через `services.i18n.tr(label, lang, {})` — ПУСТОЙ `tr_map`, срабатывает
ТОЛЬКО ярус A (`i18n_ui_en.UI_EN`, точный рукописный литерал). Менеджерский legal-override
(другой текст в настройке) машинным переводом не покрывается — LANG-09 не нарушается, делегат
видит его русским тем же fail-soft, что и раньше.

Харнесс — тот же `TestClient`, что `tests/test_miniapp_form.py`/`tests/test_i18n_miniapp_27.py`.
"""
from __future__ import annotations

from database import db as bot_db

from tests.test_miniapp_form import _fill, _run
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _standard_seed,
    _use_tmp_db,
)

DEFAULT_LABEL_RU = "Согласие на обработку персональных данных"
DEFAULT_LABEL_EN = "Consent to personal data processing"


def _make_client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_consent_label_i18n_260917.db")
    _standard_seed()
    _seed(settings={"consent_enabled": "on", "delegate_lang_enabled": "on"})
    return _client(_cfg(db_path))


def _consent_item(body):
    return next(i for i in body["pre_items"] if i["type"] == "consent")


def test_consent_label_stays_russian_at_ru(tmp_path):
    client = _make_client(tmp_path)
    _fill(DELEGATE_ID, lang="ru")

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()

    assert _consent_item(body)["label"] == DEFAULT_LABEL_RU


def test_consent_label_translates_default_literal_via_layer_a_at_en(tmp_path):
    client = _make_client(tmp_path)
    _fill(DELEGATE_ID, lang="en")

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()

    assert _consent_item(body)["label"] == DEFAULT_LABEL_EN


def test_consent_label_manager_override_stays_russian_at_en(tmp_path):
    """LANG-09: менеджерский legal-override (не дефолтный литерал из UI_EN) НЕ переводится
    машиной ни здесь, ни где-либо ещё в проекте — пустой `tr_map` в `_pre_items` не даёт ярусу B
    ни единого шанса, даже если бы для этого текста была запись в `translations`."""
    client = _make_client(tmp_path)
    _fill(DELEGATE_ID, lang="en")
    custom = "Согласие на обработку персональных данных делегата (редакция менеджера)"
    _run(bot_db.set_setting("consent_list", f"{custom}|personal_data"))

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()

    assert _consent_item(body)["label"] == custom


def test_consent_pdf_link_aria_label_uses_translated_title_at_en(tmp_path):
    """Ссылка на документ (иконка) переиспользует `item.label` как `aria-label` в
    `miniapp/static/js/screens/form.js::drawPre` — переведённый текст обязан долетать и туда
    без отдельного шва."""
    client = _make_client(tmp_path)
    _fill(DELEGATE_ID, lang="en")
    _run(bot_db.set_setting("consent_pdf_personal_data", "FAKE_FILE_ID"))

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    item = _consent_item(body)

    assert item["label"] == DEFAULT_LABEL_EN
    assert item["pdf_file_id"] == "FAKE_FILE_ID"
