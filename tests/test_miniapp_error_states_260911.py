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

from miniapp.routers import page as page_module
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS

from tests.test_miniapp_frontend import _client
from tests.test_miniapp_routes import (
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
]


def _screen_texts_from_html(text: str) -> dict:
    marker = 'data-screen-texts="'
    start = text.index(marker) + len(marker)
    end = text.index('"', start)
    return json.loads(html.unescape(text[start:end]))


# ── реестр: пять новых ключей ────────────────────────────────────────────────────────────

def test_five_new_keys_are_group_miniapp_text_with_human_defaults_and_prompts():
    assert len(NEW_KEYS) == 5
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

def test_screen_text_keys_has_exactly_five_entries_matching_new_keys():
    assert len(page_module.SCREEN_TEXT_KEYS) == 5
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
