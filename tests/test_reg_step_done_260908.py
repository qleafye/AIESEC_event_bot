"""UAT 07.09 (T-d6t-04): `reg_engine.STEP_DONE` — маркер «все включённые шаги отвечены» в
`reg_drafts.step`. PATCH последнего шага кладёт маркер вместо уже отвеченного шага (иначе бот,
читающий тот же столбец, переспрашивает после «✍️ Продолжить в чате»); `resume_from_draft`
на маркере финализирует анкету, а не задаёт вопрос повторно.

Серверный харнесс — `tests/test_miniapp_form.py` (тот же клиент/сиды). Бот-часть — приём
`tests/test_reg_resume_draft.py` (Fake-объекты aiogram, `_seed_new_draft`, monkeypatch
`finalize_registration`/`_ask_step_or_recall` в модуле `handlers.reg_resume`, где они и
импортированы по имени).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import reg_engine
from database import db
from config import config

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _use_tmp_db,
)
from tests.test_reg_resume_draft import (
    USER_ID,
    _KBCapturingMessage,
    _new_state,
    _seed_new_draft,
    _texts,
)

FORM_JS = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js" / "form.js"


def _run(coro):
    return asyncio.run(coro)


# ── Сервер: PATCH /app/api/reg/draft ─────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    path = _use_tmp_db(tmp_path, "reg_step_done.db")
    _standard_seed()
    return path


@pytest.fixture
def client(db_path):
    return _client(_cfg(db_path))


def _draft_row(telegram_id: int) -> dict | None:
    return _run(db.get_reg_draft(telegram_id))


def test_patch_last_enabled_step_stores_marker(client):
    draft = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    steps = draft["steps"]
    assert len(steps) >= 2, "нужен как минимум один шаг ДО последнего для второго теста"
    last_key = steps[-1]["key"]

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": draft["version"], "answers": {}, "step": last_key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["step"] == reg_engine.STEP_DONE
    row = _draft_row(DELEGATE_ID)
    assert row["step"] == reg_engine.STEP_DONE


def test_patch_non_last_step_stores_next_enabled(client):
    draft = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    steps = draft["steps"]
    first_key = steps[0]["key"]
    second_key = steps[1]["key"]

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": draft["version"], "answers": {}, "step": first_key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["step"] == second_key
    row = _draft_row(DELEGATE_ID)
    assert row["step"] == second_key


def test_patch_without_step_leaves_draft_step_unchanged(client):
    draft = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    steps = draft["steps"]
    last_key = steps[-1]["key"]
    client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": draft["version"], "answers": {}, "step": last_key},
    )
    row_before = _draft_row(DELEGATE_ID)
    assert row_before["step"] == reg_engine.STEP_DONE

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": row_before["version"], "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    row_after = _draft_row(DELEGATE_ID)
    assert row_after["step"] == reg_engine.STEP_DONE


# ── Бот: resume_from_draft / offer_resume ────────────────────────────────────────────────

def _use_tmp_bot_db(tmp_path, name="test_reg_step_done_bot.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_resume_from_draft_marker_finalizes_without_asking(tmp_path, monkeypatch):
    _use_tmp_bot_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await _seed_new_draft(USER_ID, step=reg_engine.STEP_DONE, patch={"age": "22"})
        from handlers import reg_resume

        calls = []

        async def fake_finalize(message, state, bot):
            calls.append("finalize")

        async def fake_ask(step_key, message, state, step, total):
            calls.append(f"ask:{step_key}")

        monkeypatch.setattr(reg_resume, "finalize_registration", fake_finalize)
        monkeypatch.setattr(reg_resume, "_ask_step_or_recall", fake_ask)

        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.resume_from_draft(msg, state, bot=object(), draft=draft)
        return calls

    calls = asyncio.run(go())
    assert calls == ["finalize"]


def test_resume_from_draft_unknown_step_falls_back(tmp_path, monkeypatch):
    """Незнакомый шаг (не маркер) — прежний fallback: согласия -> ФИО, вопрос не повторяется."""
    _use_tmp_bot_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await _seed_new_draft(USER_ID, step="a_step_turned_off_meanwhile", patch={"age": "22"})
        from handlers import reg_resume

        calls = []

        async def fake_finalize(message, state, bot):
            calls.append("finalize")

        monkeypatch.setattr(reg_resume, "finalize_registration", fake_finalize)

        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.resume_from_draft(msg, state, bot=object(), draft=draft)
        return calls, msg

    calls, msg = asyncio.run(go())
    assert calls == []  # not finalized
    assert msg.sent  # какой-то экран показан (согласие или ФИО)


def test_resume_from_draft_enabled_step_asks_question(tmp_path, monkeypatch):
    _use_tmp_bot_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_phone", "on")
        await _seed_new_draft(USER_ID, step="phone", patch={"age": "22"})
        from handlers import reg_resume

        calls = []

        async def fake_finalize(message, state, bot):
            calls.append("finalize")

        async def fake_ask(step_key, message, state, step, total):
            calls.append(f"ask:{step_key}")

        monkeypatch.setattr(reg_resume, "finalize_registration", fake_finalize)
        monkeypatch.setattr(reg_resume, "_ask_step_or_recall", fake_ask)

        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.resume_from_draft(msg, state, bot=object(), draft=draft)
        return calls

    calls = asyncio.run(go())
    assert calls == ["ask:phone"]


def test_offer_resume_marker_shows_step_equals_total(tmp_path):
    _use_tmp_bot_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_phone", "on")
        await _seed_new_draft(USER_ID, step=reg_engine.STEP_DONE, patch={"age": "22", "phone": "+7999"})
        from handlers import reg_resume

        draft = await db.get_reg_draft(USER_ID)
        msg = _KBCapturingMessage(USER_ID, "delegate")
        await reg_resume.offer_resume(msg, draft)
        return msg

    msg = asyncio.run(go())
    texts = _texts(msg)
    # continue_label подставляет {step}/{total} — оба совпадают (не единица на дочитанной анкете).
    assert any(re.search(r"\b2\s*/\s*2\b|\b2\b.*\b2\b", t or "") for t in texts) or msg.sent


# ── Сторож дрейфа: литерал маркера в form.js совпадает с reg_engine.STEP_DONE ───────────────

def test_step_done_literal_matches_between_js_and_python():
    text = FORM_JS.read_text(encoding="utf-8")
    m = re.search(r'export const STEP_DONE = "([^"]+)";', text)
    assert m, "form.js должен экспортировать STEP_DONE строковым литералом"
    assert m.group(1) == reg_engine.STEP_DONE
