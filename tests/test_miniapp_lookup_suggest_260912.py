"""Phase 30 (30-02, A2-03) — `GET /app/api/reg/suggest`: гейты (`form_gate`, не `delegate_gate`
— Pitfall 9, незарегистрированный делегат с черновиком `kind='new'` обязан пройти), лимит
длины `q`, экранирование `%`/`_`/кавычек (T-30-03/T-30-04), закреплённые чипы первыми,
пустой ответ на не-lookup шаге.

Харнесс — `tests/test_miniapp_routes.py` (`TestClient` + временная БД), подпись initData —
`tests/test_miniapp_auth.py::make_init_data` (тот же приём, что у `tests/test_miniapp_form.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


async def _insert_entry(kind, canonical, alias, *, pinned=0):
    async with bot_db._connect() as conn:
        from services.lookup import normalize_alias

        await conn.execute(
            "INSERT OR IGNORE INTO lookup_entries "
            "(kind, canonical, alias, alias_norm, source, pinned, added_by, created_at) "
            "VALUES (?, ?, ?, ?, 'test', ?, NULL, '2026-09-13 00:00:00')",
            (kind, canonical, alias, normalize_alias(alias), pinned),
        )
        await conn.commit()


async def _clear_lookup(kind):
    async with bot_db._connect() as conn:
        await conn.execute("DELETE FROM lookup_entries WHERE kind = ?", (kind,))
        await conn.commit()


@pytest.fixture
def db_path(tmp_path):
    path = _use_tmp_db(tmp_path, "miniapp_lookup_suggest.db")
    _standard_seed()
    _run(_clear_lookup("university"))
    _run(_clear_lookup("city"))
    return path


@pytest.fixture
def client(db_path):
    return _client(_cfg(db_path))


# ── Гейты ────────────────────────────────────────────────────────────────────────────────

def test_suggest_requires_initdata_401(client):
    resp = client.get("/app/api/reg/suggest", params={"step": "university", "q": "лэти"})
    assert resp.status_code == 401


def test_suggest_403_when_section_off(client, db_path):
    from tests.test_miniapp_routes import _set

    _set("miniapp_section_form", "off")
    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "лэти"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 403


def test_suggest_unregistered_delegate_with_new_draft_passes(client):
    """Pitfall 9: `form_gate`, не `delegate_gate` — незарегистрированный делегат ищет ВУЗ на
    своём первом прохождении анкеты, до какого-либо решения менеджера."""
    _run(_insert_entry("university", "ИТМО", "ИТМО"))
    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "итмо"},
        headers=_hdr(UNREGISTERED_ID),
    )
    assert resp.status_code == 200


# ── Контракт ответа ──────────────────────────────────────────────────────────────────────

def test_suggest_finds_university_by_alias(client):
    _run(_insert_entry("university", "СПбГЭТУ", "СПбГЭТУ"))
    _run(_insert_entry("university", "СПбГЭТУ", "ЛЭТИ"))

    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "лэти"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any(r["canonical"] == "СПбГЭТУ" for r in body["results"])
    assert "chips" in body and "other_allowed" in body


def test_suggest_pinned_chips_come_first(client):
    _run(_insert_entry("city", "Закреплённый", "закреплённый", pinned=1))
    _run(_insert_entry("city", "Обычный", "обычный"))

    resp = client.get(
        "/app/api/reg/suggest", params={"step": "city", "q": ""},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    chips = resp.json()["chips"]
    assert chips[0] == "Закреплённый"


def test_suggest_empty_response_for_non_lookup_step(client):
    resp = client.get(
        "/app/api/reg/suggest", params={"step": "age", "q": "20"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    assert resp.json() == {"chips": [], "results": [], "other_allowed": False}


def test_suggest_empty_response_for_unknown_step(client):
    resp = client.get(
        "/app/api/reg/suggest", params={"step": "not_a_real_step", "q": "x"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    assert resp.json() == {"chips": [], "results": [], "other_allowed": False}


def test_suggest_short_query_returns_only_chips(client):
    _run(_insert_entry("university", "ИТМО", "ИТМО", pinned=1))

    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "и"},
        headers=_hdr(DELEGATE_ID),
    )
    body = resp.json()
    assert body["results"] == []
    assert body["chips"] == ["ИТМО"]


# ── T-30-03/T-30-04: лимиты и инъекция ───────────────────────────────────────────────────

def test_suggest_q_percent_does_not_return_everything(client):
    for i in range(20):
        _run(_insert_entry("university", f"ВУЗ {i}", f"вуз {i}"))

    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "%"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) <= 10
    assert len(results) < 20


def test_suggest_q_with_quote_and_sql_keywords_does_not_crash(client):
    _run(_insert_entry("university", "ИТМО", "ИТМО"))

    resp = client.get(
        "/app/api/reg/suggest",
        params={"step": "university", "q": "'; DROP TABLE lookup_entries; --"},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []

    # Таблица не пострадала.
    resp2 = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": "итмо"},
        headers=_hdr(DELEGATE_ID),
    )
    assert len(resp2.json()["results"]) == 1


def test_suggest_long_q_is_truncated_not_500(client):
    _run(_insert_entry("university", "ИТМО", "ИТМО"))
    long_q = "и" * 5000

    resp = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": long_q},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200


def test_suggest_other_allowed_true_for_city_false_for_university(client):
    resp_city = client.get(
        "/app/api/reg/suggest", params={"step": "city", "q": ""},
        headers=_hdr(DELEGATE_ID),
    )
    resp_uni = client.get(
        "/app/api/reg/suggest", params={"step": "university", "q": ""},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp_city.json()["other_allowed"] is True
    assert resp_uni.json()["other_allowed"] is False
