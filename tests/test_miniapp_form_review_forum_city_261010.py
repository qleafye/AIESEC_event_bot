"""Приёмка 09.10 (Mini App): в обзоре анкеты перед отправкой нет «Город форума».

Город форума выбирается до анкеты (развилка), шагом не является — сводка чата его показывает
(`reg_engine.summary_fields`), обзор приложения — нет. Черновик теперь отдаёт готовую строку
`forum_city` ({label, value}), экран ставит её первой в группе «Мероприятие». Модуль городов
выключен или город не выбран — строки нет.
"""
from __future__ import annotations

import domain.regform.engine as reg_engine
from domain.cities import CITIES

from tests.test_miniapp_form import (  # noqa: F401 — фикстуры подтягиваются по имени
    client,
    db_path,
    _seed_draft,
)
from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between
from tests.test_miniapp_routes import UNREGISTERED_ID, _hdr, _set


def _draft(client):
    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _code() -> str:
    return CITIES[0]["code"]


def test_draft_carries_forum_city_row(client):
    _set("event_city_enabled", "on")
    code = _code()
    _set(f"city_label__{code}", "Москва, 3 октября")
    _seed_draft(UNREGISTERED_ID, kind="new", event_city=code, patch={"full_name": "Иван Иванов"})
    body = _draft(client)
    assert body["forum_city"] == {"label": reg_engine.FORUM_CITY_LABEL, "value": "Москва, 3 октября"}


def test_no_row_when_cities_module_off(client):
    _set("event_city_enabled", "off")
    _seed_draft(UNREGISTERED_ID, kind="new", event_city=_code(), patch={"full_name": "Иван Иванов"})
    assert _draft(client)["forum_city"] is None


def test_no_row_without_city(client):
    _set("event_city_enabled", "on")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"full_name": "Иван Иванов"})
    assert _draft(client)["forum_city"] is None


def test_chat_summary_uses_same_label():
    fields = reg_engine.summary_fields({reg_engine.SUMMARY_EVENT_CITY_KEY: "Казань"})
    assert fields[0] == (reg_engine.FORUM_CITY_LABEL, "Казань")


def test_review_puts_forum_city_first_in_event_group():
    body = _between(_js_without_comments(FORM_SCREEN_JS), "function drawReview()", "async function submitForm()")
    assert "d.forum_city" in body
    section = body[body.index("function groupSection("):]
    assert 'groupKey === "event" ? forumCityRow : null' in section
    assert "[lead, ...items.map(reviewRow)]" in section
