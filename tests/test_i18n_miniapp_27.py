"""Phase 27 (27-04, LANG-02/LANG-06) — Mini App: анкета на английском (`_draft_response`/
`_pre_items`), канонизация ответа ДО `validate_answer` (`draft_patch`). Харнесс — тот же
`TestClient`, что `tests/test_miniapp_form.py` (реальный HTTP через ASGI-приложение, реальная
подпись initData) -- не вызов роутер-функций напрямую, чтобы гейты/зависимости FastAPI
оставались частью проверки.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db
import reg_engine
from services import i18n as i18n_mod

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

EN_PREFIX = "EN:"


def _fake_tr(text, lang, tr_map):
    """Детерминированный перевод -- НЕ похож на канон, чтобы round-trip проверял реальную
    работу canonical_option/tr, а не случайное совпадение подписи с каноном."""
    if not text or lang == "ru":
        return text
    return f"{EN_PREFIX}{text}"


@pytest.fixture
def db_path(tmp_path):
    path = _use_tmp_db(tmp_path, "i18n_miniapp_27.db")
    _standard_seed()
    return path


@pytest.fixture
def client(db_path):
    return _client(_cfg(db_path))


def _enable_lang_for_delegate(user_id=DELEGATE_ID, lang="en"):
    """Прямая запись в bot_settings/users -- та же идиома, что
    tests/test_i18n_enqueue_27.py::_enable_module, не запускает bulk_seed побочным эффектом."""
    _seed(settings={"delegate_lang_enabled": "on"})
    _fill(user_id, lang=lang)


# ── модуль выключен: ответ не меняется ни на байт ────────────────────────────────────────────

def test_module_off_draft_response_matches_reg_engine_directly(client):
    """«Байт-в-байт» = каждое переводимое поле (`prompt`/`help`/`label`/`options`) РАВНО тому,
    что вернул reg_engine напрямую, без единого прохода через tr() -- эталон "выключенный
    модуль" собирается на лету из независимого источника, а не хранится вторым файлом."""
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()

    async def go():
        user_row = await bot_db.get_user(DELEGATE_ID)
        season = (await bot_db.get_setting("event_season") or "").strip() or None
        answers = reg_engine.answers_from_user_row(user_row)
        prior = {}
        spec = await reg_engine.form_spec(answers, None, user_row.get("event_city") if user_row else None, prior=prior)
        return spec

    direct = _run(go())
    assert [s["prompt"] for s in body["steps"]] == [s["prompt"] for s in direct["steps"]]
    assert [s["help"] for s in body["steps"]] == [s["help"] for s in direct["steps"]]
    assert [s["label"] for s in body["steps"]] == [s["label"] for s in direct["steps"]]
    assert [s["options"] for s in body["steps"]] == [s["options"] for s in direct["steps"]]


# ── lang == "en": prompt/help/label/options переведены, контракт не меняется ───────────────

def test_english_lang_translates_step_spec_fields(client, monkeypatch):
    monkeypatch.setattr(i18n_mod, "tr", _fake_tr)
    monkeypatch.setattr(reg_engine, "_tr", _fake_tr)
    _enable_lang_for_delegate()

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert resp.status_code == 200, resp.text

    translated_any = False
    for step in body["steps"]:
        assert isinstance(step["prompt"], str) and step["prompt"].startswith(EN_PREFIX)
        if step["help"]:
            assert step["help"].startswith(EN_PREFIX)
        assert step["label"].startswith(EN_PREFIX)
        if step["options"] is not None:
            assert isinstance(step["options"], list)
            assert all(isinstance(o, str) for o in step["options"])
            assert all(o.startswith(EN_PREFIX) for o in step["options"] if o)
            translated_any = True
    assert translated_any  # хотя бы один select/multi/choice-chips шаг реально был проверен


# ── отсутствующий перевод -- fail-soft на русский, без ошибки ──────────────────────────────

def test_missing_translation_falls_back_to_russian(client):
    """Модуль включён, но `i18n.tr`/`reg_engine._tr` НЕ монкипатчены -- реальная лестница
    резолюции (ярус A -> tr_map -> русский as-is) отдаёт русский текст без единой ошибки."""
    _enable_lang_for_delegate()
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["steps"]
    assert all(isinstance(s["prompt"], str) and s["prompt"] for s in body["steps"])


# ── draft_patch: английская подпись варианта -> русский канон в черновике/users ─────────────

def test_draft_patch_canonicalizes_english_option_label(client, monkeypatch):
    monkeypatch.setattr(i18n_mod, "tr", _fake_tr)
    monkeypatch.setattr(reg_engine, "_tr", _fake_tr)
    _enable_lang_for_delegate()

    before = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    # attendance_format -- _MEMBERSHIP_STEPS, канон "Offline"/"Online", подпись под фейковым
    # tr() станет "EN:Offline"/"EN:Online".
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": before["version"], "answers": {"attendance_format": f"{EN_PREFIX}Offline"}},
    )
    assert resp.status_code == 200, resp.text

    async def go():
        return await bot_db.get_reg_draft(DELEGATE_ID)

    draft = _run(go())
    assert draft["answers"]["attendance_format"] == "Offline"  # русский канон, не EN:Offline


def test_draft_patch_other_allowed_free_text_saved_verbatim(client, monkeypatch):
    """Шаг с `other_allowed` -- свободный текст, которого нет ни среди вариантов, ни в ярусе A,
    сохраняется дословно (canonical_option -> None -> raw как есть)."""
    monkeypatch.setattr(i18n_mod, "tr", _fake_tr)
    monkeypatch.setattr(reg_engine, "_tr", _fake_tr)
    _enable_lang_for_delegate()

    before = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": before["version"], "answers": {"city": "My Special Town"}},
    )
    assert resp.status_code == 200, resp.text

    async def go():
        return await bot_db.get_reg_draft(DELEGATE_ID)

    draft = _run(go())
    assert draft["answers"]["city"] == "My Special Town"


# ── одна загрузка карты переводов на GET-запрос ─────────────────────────────────────────────

def test_single_fetch_translations_call_per_draft_response(client, monkeypatch):
    calls = []
    real_fetch = bot_db.fetch_translations

    async def counting_fetch(lang):
        calls.append(lang)
        return await real_fetch(lang)

    monkeypatch.setattr(bot_db, "fetch_translations", counting_fetch)
    _enable_lang_for_delegate()

    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
