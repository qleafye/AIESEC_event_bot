"""Квик 260915-4mv — очистка резюме в Mini App обязана чистить не только `reg_drafts`
(квик 260912-l53), но и `users.resume_url` + ячейку «Резюме (ссылка)» в Google Sheets:
`draft_patch` (`miniapp/routers/form.py`) при `clear: ["resume"]` зовёт
`services.reg_finalize._apply_resume_url(telegram_id, full, None)` тем же путём, что запись
ссылки при загрузке (`handle_resume_upload`/`retry_pending_resume_uploads`).

Харнесс — `tests/test_miniapp_resume_clear_260912.py` (та же временная БД, `_standard_seed`,
`reg_q_resume=on`). HTTP-уровень мокает сам `_apply_resume_url` (лениво импортированный в
`draft_patch` — монкипатч исходного модуля срабатывает, тот же приём, что описан в докстринге
`tests/test_reg_finalize.py`). Один тест зовёт настоящий `_apply_resume_url` напрямую и мокает
только `services.sheets.update_row_by_id` (сетевой хвост) — идиома `tests/
test_resume_retry_260907.py` — чтобы проверить реальный эффект на БД и на ячейку листа.
"""
from __future__ import annotations

import asyncio
import logging

from database import db as bot_db
from handlers.reg_schema import active_sheet_headers
from services import reg_finalize as rf
from services import sheets as sheets_service

from tests.test_miniapp_form import _fill
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

import pytest

RESUME_URL = "https://cloud.example.org/s/TOK/download?path=%2F&files=cv.pdf"


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "resume_url_clear_260915.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _patch_clear_resume(client, telegram_id: int):
    before = client.get("/app/api/reg/draft", headers=_hdr(telegram_id)).json()
    return client.patch(
        "/app/api/reg/draft", headers=_hdr(telegram_id),
        json={"version": before["version"], "answers": {}, "clear": ["resume"]},
    )


# ══════════════════════════════════════════════════════════════════════════════════════════
# HTTP-уровень (draft_patch): вызов _apply_resume_url замокан — проверяем ТОЛЬКО проводку
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_clear_calls_apply_resume_url_with_none_when_link_present(client, monkeypatch):
    _set("reg_q_resume", "on")
    _fill(DELEGATE_ID, resume_url=RESUME_URL)

    calls = []

    async def _fake_apply(telegram_id, full, url):
        calls.append((telegram_id, url))

    monkeypatch.setattr(rf, "_apply_resume_url", _fake_apply)

    resp = _patch_clear_resume(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    assert calls == [(DELEGATE_ID, None)]


def test_clear_skips_apply_when_no_resume_url(client, monkeypatch):
    _set("reg_q_resume", "on")
    # DELEGATE_ID из _standard_seed без явной ссылки -- resume_url в users пуст.

    calls = []

    async def _fake_apply(telegram_id, full, url):
        calls.append((telegram_id, url))

    monkeypatch.setattr(rf, "_apply_resume_url", _fake_apply)

    resp = _patch_clear_resume(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    assert calls == [], "_apply_resume_url не должен звать Sheets впустую без ссылки"


def test_clear_apply_failure_is_fail_soft(client, monkeypatch, caplog):
    _set("reg_q_resume", "on")
    _fill(DELEGATE_ID, resume_url=RESUME_URL)

    async def _boom(telegram_id, full, url):
        raise RuntimeError("sheets недоступен")

    monkeypatch.setattr(rf, "_apply_resume_url", _boom)

    with caplog.at_level(logging.ERROR):
        resp = _patch_clear_resume(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    assert "Failed to clear resume_url" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Прямой вызов _apply_resume_url(tid, full, None) — реальный эффект на БД и лист
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_apply_resume_url_none_clears_db_and_sheet_cell(client, monkeypatch):
    _set("reg_q_resume", "on")
    _fill(DELEGATE_ID, resume_url=RESUME_URL, event_city=None, participant_type="full")

    captured: dict = {}

    async def _fake_update_row_by_id(tab, telegram_id, row):
        captured["tab"] = tab
        captured["telegram_id"] = telegram_id
        captured["row"] = row
        return True

    monkeypatch.setattr(sheets_service, "update_row_by_id", _fake_update_row_by_id)

    full = asyncio.run(bot_db.get_user(DELEGATE_ID))
    asyncio.run(rf._apply_resume_url(DELEGATE_ID, full, None))

    updated = asyncio.run(bot_db.get_user(DELEGATE_ID))
    assert updated["resume_url"] is None

    assert captured, "update_row_by_id должен быть вызван"
    headers = asyncio.run(active_sheet_headers(None))
    idx = headers.index("Резюме (ссылка)")
    # Квик 260919 (08-sheets-dashboard): _sheet_safe (identity) заменил _csv_safe для строк
    # листа -- пустая ячейка резюме остаётся обычным «-», без ведущего апострофа (services/
    # sheets.py пишет явным RAW, Google Sheets формулу не считает).
    assert captured["row"][idx] == "-"
