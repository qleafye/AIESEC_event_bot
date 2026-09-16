"""Задача «делегатский интерфейс Mini App на английском» — аудит покрытия: для каждого
делегатского экрана (хаб/статус/задания/монеты/рейтинг/профиль/FAQ/обзор перед отправкой/
навигация приложения через `/app/api/me`), при lang=en, ключевые тексты интерфейса проходят
через `services.i18n.tr()`, а не уходят к клиенту русскими байт-в-байт.

Приём — тот же, что `tests/test_i18n_miniapp_27.py::_fake_tr`: подменяем `services.i18n.tr` на
детерминированный маркер `EN:<текст>` — тест проверяет ФАКТ прогона через `tr()` (wiring), а не
качество конкретного перевода (это отдельная забота ручных/машинных словарей). Харнесс — тот
же `TestClient`, что и остальные тесты Mini App (`tests/test_miniapp_routes.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db
from services import i18n as i18n_mod

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

EN_PREFIX = "EN:"


def _fake_tr(text, lang, tr_map):
    if not text or lang == "ru":
        return text
    return f"{EN_PREFIX}{text}"


def _run(coro):
    return asyncio.run(coro)


def _set_lang(telegram_id: int, lang: str = "en"):
    async def go():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE users SET lang = ? WHERE telegram_id = ?", (lang, telegram_id))
            await conn.commit()
    _run(go())


@pytest.fixture(autouse=True)
def _patched_tr(monkeypatch):
    monkeypatch.setattr(i18n_mod, "tr", _fake_tr)


@pytest.fixture
def make_client(tmp_path):
    def _make():
        path = _use_tmp_db(tmp_path, "miniapp_i18n_coverage.db")
        _standard_seed()
        _set("delegate_lang_enabled", "on")
        _set_lang(DELEGATE_ID, "en")
        _set_lang(PENDING_ID, "en")
        _set_lang(REJECTED_ID, "en")
        return _client(_cfg(path))
    return _make


def test_hub_texts_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/hub", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in ("balance_eyebrow", "balance_unit", "next_eyebrow", "sections_eyebrow",
                  "tasks_eyebrow", "rank_eyebrow"):
        assert body[field].startswith(EN_PREFIX), f"{field}: {body[field]!r}"


def test_hub_status_texts_translated_for_pending_and_rejected(make_client):
    client = make_client()
    _set("reg_form_status_screen", "on")

    resp = client.get("/app/api/hub/status", headers=_hdr(PENDING_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["heading"].startswith(EN_PREFIX)
    assert body["badge"].startswith(EN_PREFIX)
    assert body["title"].startswith(EN_PREFIX)

    resp = client.get("/app/api/hub/status", headers=_hdr(REJECTED_ID))
    body = resp.json()
    assert body["heading"].startswith(EN_PREFIX)
    assert body["cta_text"].startswith(EN_PREFIX)


def test_tasks_empty_text_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/tasks", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["empty_text"].startswith(EN_PREFIX)


def test_coins_balance_history_empty_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/coins/history", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["empty_text"].startswith(EN_PREFIX)


def test_leaderboard_empty_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/leaderboard", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["empty_text"].startswith(EN_PREFIX)


def test_faq_empty_text_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/faq", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["empty_text"].startswith(EN_PREFIX)


def test_profile_eyebrows_and_status_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contacts_eyebrow"].startswith(EN_PREFIX)
    assert body["form_eyebrow"].startswith(EN_PREFIX)
    assert body["status_label"].startswith(EN_PREFIX)
    assert body["edit_cta_text"].startswith(EN_PREFIX)


def test_me_shell_texts_translated(make_client):
    client = make_client()
    resp = client.get("/app/api/me", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["section_labels"], "section_labels пуст"
    assert all(v.startswith(EN_PREFIX) for v in body["section_labels"].values())
    assert body["screen_texts"]["retry"].startswith(EN_PREFIX)
    assert body["form_v2_texts"]["review_title"].startswith(EN_PREFIX)
    # Ключи `mgr_*` (экран менеджера «Анкета мероприятия») делегату не переводим и не отдаём
    # в этой карте вовсе — админская поверхность вне области задачи.
    assert not any(k.startswith("mgr_") for k in body["form_v2_texts"])


def test_form_draft_response_translates_new_gaps(make_client):
    """Дозор на регрессию конкретных находок владельца (17.09): «Продолжить в чате», кнопки
    обзора и прогресса анкеты — раньше читались из реестра БЕЗ прогона через tr()."""
    client = make_client()
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in ("continue_in_chat_text", "next_cta_text", "back_cta_text",
                  "questions_eyebrow", "draft_saved_text", "not_set_text",
                  "cancel_changes_text", "submit_cta_text"):
        value = body.get(field)
        assert value and value.startswith(EN_PREFIX), f"{field}: {value!r}"
