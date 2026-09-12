"""Quick 260911-5ij (W2 «Ошибки видно») — контракт между реестром и оболочкой Mini App для
пяти новых текстов состояний (Пилар 6: отказ первичной загрузки экрана; Пилар 5 + известная
находка №5: гейт закрытой сдачи по `task.can_submit`).

Задача 1 — реестр (`SETTINGS_SCHEMA`/`SETTINGS_SYNONYMS`) и доставка в оболочку
(`page.SCREEN_TEXT_KEYS` -> `data-screen-texts` на `<body>`, тот же приём, что
`APPLICATIONS_TEXT_KEYS` -> `data-applications-texts`).
Задача 3 добавляет сюда серверный контракт `can_submit`/`status` на `GET /app/api/tasks/{id}`
— гейт `submit.js` опирается на уже существующее поле (`submission_state`), новых серверных
полей не заводит.

Харнесс — `tests/test_miniapp_routes.py` (`_client`, `_cfg`, `_seed`, `_set`, `_standard_seed`,
`_use_tmp_db`), `tests/test_miniapp_submissions.py::_task` (задача 3) для заведения задания.
"""
from __future__ import annotations

import html
import json

from database import db as bot_db

from miniapp.routers import page as page_module
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS

from tests.test_miniapp_frontend import _client
from tests.test_miniapp_routes import (
    ADMIN_ID,
    DELEGATE_ID,
    _cfg,
    _client as _client_cfg,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

NEW_KEYS = [
    "miniapp_load_error_text",
    "miniapp_retry_button",
    "miniapp_submit_pending_text",
    "miniapp_submit_approved_text",
    "miniapp_submit_limit_text",
    # Квик 12.09 (UI-аудит, пункт 3): шестой ключ — обрыв сети на любом шаге анкеты.
    "miniapp_network_error_text",
]


def _screen_texts_from_html(text: str) -> dict:
    marker = 'data-screen-texts="'
    start = text.index(marker) + len(marker)
    end = text.index('"', start)
    return json.loads(html.unescape(text[start:end]))


# ── реестр: пять новых ключей ────────────────────────────────────────────────────────────

def test_five_new_keys_are_group_miniapp_text_with_human_defaults_and_prompts():
    assert len(NEW_KEYS) == 6
    for key in NEW_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "miniapp", key
        assert entry["type"] == "text", key
        assert isinstance(entry["default"], str) and entry["default"].strip(), key
        assert isinstance(entry["prompt"], str) and entry["prompt"].strip(), key
        assert "miniapp_" not in entry["label"], key


def test_five_new_keys_have_at_least_two_lowercase_synonyms():
    for key in NEW_KEYS:
        synonyms = SETTINGS_SYNONYMS[key]
        assert len(synonyms) >= 2, key
        for synonym in synonyms:
            assert synonym == synonym.lower(), (key, synonym)


# ── page.SCREEN_TEXT_KEYS: доставка без седьмого errorText ──────────────────────────────

def test_screen_text_keys_has_exactly_six_entries_matching_new_keys():
    assert len(page_module.SCREEN_TEXT_KEYS) == 6
    assert set(page_module.SCREEN_TEXT_KEYS.values()) == set(NEW_KEYS)


# ── доставка в оболочку: data-screen-texts ───────────────────────────────────────────────

def test_shell_delivers_screen_texts_matching_registry_defaults(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_error_states_shell.db")
    _standard_seed()
    resp = _client(db_path).get("/app")
    assert resp.status_code == 200
    texts = _screen_texts_from_html(resp.text)
    for name, key in page_module.SCREEN_TEXT_KEYS.items():
        assert texts[name] == SETTINGS_SCHEMA[key]["default"], name


def test_shell_delivers_manager_edited_screen_text(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_error_states_shell_edit.db")
    _standard_seed()
    _set("miniapp_load_error_text", "Что-то пошло не так — менеджер поправил текст сам")
    resp = _client(db_path).get("/app")
    texts = _screen_texts_from_html(resp.text)
    assert texts["load_error"] == "Что-то пошло не так — менеджер поправил текст сам"


def test_disabled_page_also_carries_screen_texts_attribute(tmp_path):
    """503 (`miniapp_enabled = off`) несёт тот же атрибут — фолбэк `{}` только когда БД
    физически недоступна (ветка `except` `render_disabled_page`), а не всегда."""
    db_path = _use_tmp_db(tmp_path, "miniapp_error_states_disabled.db")
    _seed(settings={"miniapp_enabled": "off"})
    resp = _client(db_path).get("/app")
    assert resp.status_code == 503
    texts = _screen_texts_from_html(resp.text)
    assert set(texts) == set(page_module.SCREEN_TEXT_KEYS)
    assert texts["load_error"] == SETTINGS_SCHEMA["miniapp_load_error_text"]["default"]


def test_render_disabled_page_fallback_dict_has_screen_texts_key():
    """Ветка `except` `render_disabled_page` (БД недоступна) — фолбэк-словарь несёт
    `screen_texts` пустым JSON, атрибут в шаблоне присутствует всегда (сторож против
    `KeyError`/`UndefinedError` в Jinja на реальном обрыве БД)."""
    import inspect

    source = inspect.getsource(page_module.render_disabled_page)
    assert '"screen_texts": "{}"' in source


# ── задача 3: серверный контракт can_submit/status на GET /app/api/tasks/{id} ────────────
#
# Гейт `submit.js` опирается на поле, которое сервер уже отдаёт (`submission_state`,
# `routers/tasks.py:67-89`) — этот файл НЕ трогаем, только читаем контракт.

def _task_route_client(db_path: str):
    return _client_cfg(_cfg(db_path))


def test_can_submit_true_and_status_new_when_no_submission_yet(tmp_path):
    from tests.test_miniapp_submissions import _task

    db_path = _use_tmp_db(tmp_path, "miniapp_can_submit_new.db")
    _standard_seed()
    task_id = _task()
    resp = _task_route_client(db_path).get(f"/app/api/tasks/{task_id}", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_submit"] is True
    assert body["status"] == "new"


def test_can_submit_false_and_status_pending_when_submission_awaits_review(tmp_path):
    from tests.test_miniapp_submissions import _run, _task

    db_path = _use_tmp_db(tmp_path, "miniapp_can_submit_pending.db")
    _standard_seed()
    task_id = _task()
    _run(bot_db.create_submission(task_id, DELEGATE_ID, "text", "мой ответ", "2026-01-01 00:00:00"))
    resp = _task_route_client(db_path).get(f"/app/api/tasks/{task_id}", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_submit"] is False
    assert body["status"] == "pending"


def test_can_submit_false_and_status_approved_when_submission_accepted(tmp_path):
    from tests.test_miniapp_submissions import _run, _task

    db_path = _use_tmp_db(tmp_path, "miniapp_can_submit_approved.db")
    _standard_seed()
    task_id = _task()
    sub_id = _run(bot_db.create_submission(task_id, DELEGATE_ID, "text", "мой ответ", "2026-01-01 00:00:00"))
    _run(bot_db.claim_submission(sub_id, ADMIN_ID, "approved", coins_awarded=5))
    resp = _task_route_client(db_path).get(f"/app/api/tasks/{task_id}", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_submit"] is False
    assert body["status"] == "approved"


def test_can_submit_false_and_status_rejected_when_resubmit_limit_exhausted(tmp_path):
    from tests.test_miniapp_submissions import _run, _task

    db_path = _use_tmp_db(tmp_path, "miniapp_can_submit_limit.db")
    _standard_seed()
    _set("game_resubmit_limit", "1")
    task_id = _task()
    sub_id = _run(bot_db.create_submission(task_id, DELEGATE_ID, "text", "попытка 1", "2026-01-01 00:00:00"))
    _run(bot_db.claim_submission(sub_id, ADMIN_ID, "rejected", reject_reason="не подходит"))
    resp = _task_route_client(db_path).get(f"/app/api/tasks/{task_id}", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_submit"] is False
    assert body["status"] == "rejected"


def test_post_submissions_still_returns_409_already_submitted_on_closed_task(tmp_path):
    """Серверная защита 409 остаётся — гейт клиента её не заменяет, дублирующий POST по уже
    поданному заданию по-прежнему отвергается (Пилар 5, обе линии обороны)."""
    from tests.test_miniapp_submissions import _run, _task

    db_path = _use_tmp_db(tmp_path, "miniapp_can_submit_409.db")
    _standard_seed()
    task_id = _task()
    _run(bot_db.create_submission(task_id, DELEGATE_ID, "text", "первая", "2026-01-01 00:00:00"))
    resp = _task_route_client(db_path).post(
        "/app/api/submissions",
        json={"task_id": task_id, "parts": [{"kind": "text", "content": "вторая"}]},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 409
    assert resp.json() == {"reason": "already_submitted"}
