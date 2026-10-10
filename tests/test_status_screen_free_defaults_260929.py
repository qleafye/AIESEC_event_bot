"""Экран статуса «Одобрена»: пустой плейсхолдер схлопывается вместе с предлогом, а заводские
тексты при выключенном модуле оплаты не говорят об оплате.

Прод Юлида 17.09: `event_place_name` пуст → «увидимся 30-31 октября в .»; модуль оплаты
выключен, а тело плиты и шаги «Что дальше» — про оплату и чек. Своё значение менеджера в
`bot_settings` всегда сильнее обоих заводских наборов.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed
from services.comms.text_fill import fill_collapsing

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)


# ── A. Схлопывание пустого плейсхолдера ─────────────────────────────────────────────────────

_BODY = "Осталось оплатить участие — и увидимся {дата} в {город}."


def test_empty_city_collapses_with_preposition():
    assert fill_collapsing(_BODY, дата="30-31 октября", город="") == (
        "Осталось оплатить участие — и увидимся 30-31 октября."
    )


def test_none_city_collapses_like_empty():
    assert fill_collapsing(_BODY, дата="30-31 октября", город=None) == (
        "Осталось оплатить участие — и увидимся 30-31 октября."
    )


def test_empty_date_collapses():
    assert fill_collapsing(_BODY, дата="", город="Москве") == (
        "Осталось оплатить участие — и увидимся в Москве."
    )


def test_both_empty_collapse():
    assert fill_collapsing(_BODY, дата="", город="") == "Осталось оплатить участие — и увидимся."


def test_non_empty_values_substitute_unchanged():
    assert fill_collapsing(_BODY, дата="30-31 октября", город="Москве") == (
        "Осталось оплатить участие — и увидимся 30-31 октября в Москве."
    )


def test_preposition_vo_and_english_in_collapse():
    assert fill_collapsing("Ждём тебя во {город}!", город="") == "Ждём тебя!"
    assert fill_collapsing("See you {дата} in {город}.", дата="Oct 30", город="") == "See you Oct 30."


def test_placeholder_at_start_leaves_no_leading_space():
    assert fill_collapsing("{дата} — старт форума.", дата="") == "— старт форума."


def test_word_ending_in_v_is_not_eaten_as_preposition():
    # «в» — только отдельное слово: хвост «…ов» в «слов» не предлог.
    assert fill_collapsing("Пара слов {город}.", город="") == "Пара слов."


def test_unknown_placeholder_and_stray_braces_survive():
    assert fill_collapsing("Ссылка {id} {дата}", дата="") == "Ссылка {id}"


# ── B. Заводские тексты под бесплатное участие ──────────────────────────────────────────────

_PAYMENT_WORDS = ("оплат", "чек", "реквизит", "тариф")

_FREE_KEYS = (
    "reg_status_approved_body_text",
    "reg_status_approved_step1_title_text",
    "reg_status_approved_step1_body_text",
    "reg_status_approved_step2_title_text",
    "reg_status_approved_step2_body_text",
    "reg_status_review_step3_title_text",
    "reg_status_review_step3_body_text",
)


def test_free_defaults_registered_and_without_payment_words():
    for key in _FREE_KEYS:
        free = SETTINGS_SCHEMA[key].get("default_free")
        assert free, key
        low = free.lower()
        assert not any(w in low for w in _PAYMENT_WORDS), (key, free)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "status_free_defaults.db")
    _standard_seed()
    _set("reg_form_status_screen", "on")
    return _client(_cfg(db_path))


def _status(client) -> dict:
    return client.get("/app/api/hub/status", headers=_hdr(DELEGATE_ID)).json()


def _all_texts(body: dict) -> str:
    parts = [body["screen_body"] or ""]
    for step in body["next_steps"]:
        parts += [step["title"] or "", step["body"] or ""]
    return " ".join(parts).lower()


def test_payment_off_approved_screen_speaks_no_payment(client):
    _set("event_date", "30-31 октября")
    _set("event_place_name", "Москве")
    body = _status(client)
    assert body["screen_body"] == "Ждём тебя 30-31 октября в Москве."
    assert not any(w in _all_texts(body) for w in _PAYMENT_WORDS), _all_texts(body)
    assert len(body["next_steps"]) == 3


def test_payment_off_empty_place_collapses_on_screen(client):
    _set("event_date", "30-31 октября")
    body = _status(client)
    assert body["screen_body"] == "Ждём тебя 30-31 октября."


def test_payment_on_keeps_old_defaults(client):
    _set("payment_enabled", "on")
    _set("event_date", "30-31 октября")
    body = _status(client)
    assert body["screen_body"] == "Осталось оплатить участие — и увидимся 30-31 октября."
    assert body["next_steps"][0] == {
        "title": "Оплати участие", "body": "Реквизиты и чек — в одном экране.",
    }
    assert body["next_steps"][1] == {"title": "Пришли чек", "body": "Менеджер подтвердит за день."}


def test_manager_saved_value_wins_with_payment_off(client):
    _set("reg_status_approved_body_text", "Свой текст {дата} в {город}.")
    _set("reg_status_approved_step1_title_text", "Собирай монеты")
    _set("event_date", "30-31 октября")
    body = _status(client)
    assert body["screen_body"] == "Свой текст 30-31 октября."
    assert body["next_steps"][0]["title"] == "Собирай монеты"


def test_manager_saved_value_wins_with_payment_on(client):
    _set("payment_enabled", "on")
    _set("reg_status_approved_step2_title_text", "Позови друзей сам")
    body = _status(client)
    assert body["next_steps"][1]["title"] == "Позови друзей сам"


def _run(coro):
    return asyncio.run(coro)


def test_typed_accessor_picks_default_by_payment(client):
    key = "reg_status_approved_step1_title_text"
    assert _run(get_setting_typed(key)) == SETTINGS_SCHEMA[key]["default_free"]
    _set("payment_enabled", "on")
    assert _run(get_setting_typed(key)) == SETTINGS_SCHEMA[key]["default"]


def test_manager_saved_empty_string_is_respected(client):
    # Пустая строка — «менеджер стёр шаг»: не подменяем её заводским текстом.
    async def _save_empty():
        async with bot_db._connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                ("reg_status_approved_step1_body_text", ""),
            )
            await conn.commit()
    _run(_save_empty())
    assert _run(get_setting_typed("reg_status_approved_step1_body_text")) == ""


def test_free_defaults_have_manual_english():
    from services.i18n.i18n_miniapp_manual import MANUAL_EN

    for key in _FREE_KEYS:
        free = SETTINGS_SCHEMA[key]["default_free"]
        assert free in MANUAL_EN, (key, free)
        for token in ("{дата}", "{город}"):
            assert (token in free) == (token in MANUAL_EN[free]), (key, token)
