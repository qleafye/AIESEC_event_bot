"""Quick 260904-aup Task 2 (UAT D4/D5/D6): резюме текстом доезжает до `users.resume_text`,
служебные колонки («Источник» из метки, невыставленный «Амбассадор») не попадают в профиль,
короткий трек в профиле остаётся отфильтрованным после D16. Харнесс — тот же приём, что
`tests/test_miniapp_delegate.py`/`tests/test_miniapp_form.py` (`TestClient` + временная БД).
"""
from __future__ import annotations

import asyncio

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _set,
    _use_tmp_db,
)


def _run(coro):
    return asyncio.run(coro)


async def _sql(query: str, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _fill(telegram_id: int, **columns):
    sets = ", ".join(f"{c} = ?" for c in columns)
    _run(_sql(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*columns.values(), telegram_id)))


def _draft_row(telegram_id: int) -> dict | None:
    return _run(bot_db.get_reg_draft(telegram_id))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_profile_service_fields.db")
    _standard_seed()
    return _client(_cfg(db_path))


# ── D6: резюме текстом -> users.resume_text -> профиль ──────────────────────────────────────

def test_resume_text_patch_reaches_users_and_profile(client):
    _set("reg_q_resume", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": {"text": "Моё резюме"}}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(DELEGATE_ID)
    assert row["answers"]["resume_text"] == "Моё резюме"

    submit_resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert submit_resp.status_code == 200, submit_resp.text
    user = _run(bot_db.get_user(DELEGATE_ID))
    assert user["resume_text"] == "Моё резюме"

    profile = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    by_key = {f["key"]: f for f in profile["fields"]}
    assert by_key["reg_q_resume"]["value"] == "Моё резюме"


def test_resume_legacy_column_name_still_accepted(client):
    """Legacy-алиас (`_COLUMN_TO_STEP.setdefault("resume", "resume")`) — делегат со старой
    открытой спекой шага (колонка "resume" вместо "resume_text") не должен ловить 400
    bad_field; ответ всё равно приезжает в НОВУЮ колонку `resume_text`."""
    _set("reg_q_resume", "on")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume": {"text": "Резюме по старой спеке"}}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(DELEGATE_ID)
    assert row["answers"]["resume_text"] == "Резюме по старой спеке"
    assert "resume" not in row["answers"]


# ── D5: «Источник» из метки не попадает в профиль ────────────────────────────────────────────

def test_source_from_tag_hides_source_field_and_total(client):
    _fill(DELEGATE_ID, source="src_vk_ads", source_from_tag=1)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    field_keys = {f["key"] for f in body["fields"]}
    assert "reg_q_source" not in field_keys
    total_hidden = body["form_total"]

    _fill(DELEGATE_ID, source_from_tag=0)
    body2 = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    field_keys2 = {f["key"] for f in body2["fields"]}
    assert "reg_q_source" in field_keys2
    assert field_keys2 - field_keys == {"reg_q_source"}
    assert body2["form_total"] == total_hidden + 1


# ── D5: «Амбассадор» — явное сравнение, не строковая истинность «0» ──────────────────────────

@pytest.mark.parametrize("raw", [0, "0", "", None, "-"])
def test_ambassador_falsy_values_hide_field(client, raw):
    _set("reg_q_ambassador", "on")
    _fill(DELEGATE_ID, is_ambassador_candidate=raw)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    field_keys = {f["key"] for f in body["fields"]}
    assert "reg_q_ambassador" not in field_keys, raw


def test_ambassador_truthy_value_shows_yes(client):
    _set("reg_q_ambassador", "on")
    _fill(DELEGATE_ID, is_ambassador_candidate=1)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["reg_q_ambassador"]["value"] == "Да"
