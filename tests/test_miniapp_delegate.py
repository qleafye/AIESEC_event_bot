"""Phase 19 Plan 03 (WEBAPP-01, D-07/D-08): читающие делегатские маршруты Mini App —
профиль, задания и карточка, баланс/история/рейтинг. Харнесс — `tests/test_miniapp_routes.py`
(`TestClient` + `make_init_data`). Все данные сидируются прямо в БД через `database.db`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from database import db as bot_db

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    REJECTED_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

OTHER_ID = 900110  # второй одобренный делегат (для рейтинга)


def _run(coro):
    return asyncio.run(coro)


async def _sql(query: str, params=()):
    async with bot_db._connect() as conn:
        await conn.execute(query, params)
        await conn.commit()


def _fill_profile(telegram_id: int, **columns):
    sets = ", ".join(f"{c} = ?" for c in columns)
    _run(_sql(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*columns.values(), telegram_id)))


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_delegate.db")
    _standard_seed()
    _seed(users=[(OTHER_ID, "approved")])
    return _client(_cfg(db_path))


# ── профиль (D-08) ──────────────────────────────────────────────────────────────────────

def test_profile_returns_labeled_nonempty_fields_and_rereg_deeplink(client):
    _fill_profile(DELEGATE_ID, phone="+7 999", city="Москва", email="", work_status=1,
                  resume_file_id="AgACfile", receipt_file_id="AgACrcpt", payment_status="paid")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["reg_q_phone"] == {"key": "reg_q_phone", "label": "\U0001f4f1 Телефон", "value": "+7 999"}
    assert by_key["reg_q_city"]["value"] == "Москва"
    assert by_key["reg_q_work"]["value"] == "Да"
    assert "reg_q_email" not in by_key  # пустое — не показываем
    assert "reg_q_resume" not in by_key  # только file_id — не текст/ссылка
    # порядок — как в REG_LABELS: телефон раньше города
    keys = [f["key"] for f in body["fields"]]
    assert keys.index("reg_q_phone") < keys.index("reg_q_city")
    # служебных колонок в ответе нет нигде
    assert "AgACfile" not in resp.text and "AgACrcpt" not in resp.text
    assert body["status"] == "approved" and body["status_label"] == "Одобрена"
    assert body["payment_status"] == "paid" and body["payment_status_label"] == "Оплатил"
    assert body["edit_deeplink"] == "https://t.me/YouLead_test_bot?start=rereg"
    assert body["edit_hint"]  # дефолт реестра miniapp_profile_edit_hint


@pytest.mark.parametrize("user_id,kind", [(PENDING_ID, "pending"), (REJECTED_ID, "rejected"),
                                          (UNREGISTERED_ID, "unregistered")])
def test_profile_gated_for_non_approved(client, user_id, kind):
    resp = client.get("/app/api/profile", headers=_hdr(user_id))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "delegate_gate", "kind": kind}


def test_profile_section_off(client):
    _set("miniapp_section_profile", "off")
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"
