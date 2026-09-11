"""Квик 260911-w2m («Правка анкеты — не безусловна»): один реестровый ключ с тремя
положениями («всегда можно» / «только до решения» / «нельзя») и один текст отказа гейтят
правку уже ПОДАННОЙ анкеты в трёх точках — профиль/PATCH/submit приложения (задача 2) и вход
в правку из чата (задача 3). Этот файл собирает тесты всех трёх задач по мере их исполнения:

  Задача 1 (ниже) — реестр, чистое правило `edit_allowed_for`, `edit_gate` fail-soft/на
  отклонённом, кнопка-цикл раздела «📋 Заявки».
  Задача 2 — HTTP-контракт Mini App (профиль, PATCH/submit черновика).
  Задача 3 — вход в правку из чата (`cmd_start`, `reg_resume`, `reg_handoff`).

pytest-asyncio недоступен в этом окружении — каждый async-вызов идёт через `asyncio.run()`,
БД — временный файл через `config.DB_PATH` (конвенция проекта, `tests/test_reg_edit_history.py`
и соседи).
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from services import reg_edit_policy
from handlers import admin_sections as sec
from handlers import admin_settings as st

from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_roles_phase8 import ADMIN_ID, _roles_ready


def _ready(tmp_path, name="reg_edit_policy_260911.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1a — чистое правило `edit_allowed_for` (без единого чтения БД)
# ══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("policy", ["always", "until_decision", "never"])
def test_primary_submission_never_gated(policy):
    """Главный сторож задачи: первичная подача (`submitted=False`) разрешена при ЛЮБОМ
    положении — тумблер про правку технически не дотягивается до неё."""
    assert reg_edit_policy.edit_allowed_for(policy, submitted=False, status=None) is True


def test_always_allows_regardless_of_status():
    assert reg_edit_policy.edit_allowed_for("always", submitted=True, status="approved") is True
    assert reg_edit_policy.edit_allowed_for("always", submitted=True, status="pending") is True
    assert reg_edit_policy.edit_allowed_for("always", submitted=True, status="") is True


def test_never_forbids_any_submitted_status():
    assert reg_edit_policy.edit_allowed_for("never", submitted=True, status="pending") is False
    assert reg_edit_policy.edit_allowed_for("never", submitted=True, status="approved") is False


def test_until_decision_pending_allowed_approved_forbidden():
    assert reg_edit_policy.edit_allowed_for("until_decision", submitted=True, status="pending") is True
    assert reg_edit_policy.edit_allowed_for("until_decision", submitted=True, status="approved") is False


def test_until_decision_empty_status_reads_as_approved():
    """Р-2: пустой статус = «одобрена» — не второе прочтение, а тот же `(status or
    "approved")`, что использует остальной проект."""
    assert reg_edit_policy.edit_allowed_for("until_decision", submitted=True, status="") is False
    assert reg_edit_policy.edit_allowed_for("until_decision", submitted=True, status=None) is False


def test_unknown_policy_value_fails_soft_to_allowed():
    assert reg_edit_policy.edit_allowed_for("bogus-future-value", submitted=True, status="approved") is True


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1b — `edit_gate` (реестр + reg_engine.has_submitted_anketa)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_edit_gate_default_always_allows_approved_delegate(tmp_path):
    _ready(tmp_path)
    row = {"status": "approved", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_edit, text = _run(reg_edit_policy.edit_gate(row))
    assert can_edit is True
    assert text is None


def test_edit_gate_never_forbids_approved_with_default_text(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reg_edit_policy", "never"))
    row = {"status": "approved", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_edit, text = _run(reg_edit_policy.edit_gate(row))
    assert can_edit is False
    assert text  # непустой всегда — дефолт реестра, не None
    assert text == SETTINGS_SCHEMA["reg_edit_closed_text"]["default"]


def test_edit_gate_never_forbids_uses_custom_text(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reg_edit_policy", "never"))
    _run(db.set_setting("reg_edit_closed_text", "Напишите менеджеру @manager."))
    row = {"status": "approved", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_edit, text = _run(reg_edit_policy.edit_gate(row))
    assert can_edit is False
    assert text == "Напишите менеджеру @manager."


@pytest.mark.parametrize("policy", ["always", "until_decision", "never"])
def test_edit_gate_rejected_delegate_always_passes(tmp_path, policy):
    """Р-1: отклонённый делегат («status == rejected») вне гейта ПРИ ЛЮБОМ положении —
    has_submitted_anketa ложна для rejected, повторная подача (D-10) не может быть убита
    тумблером про правку."""
    _ready(tmp_path)
    _run(db.set_setting("reg_edit_policy", policy))
    row = {"status": "rejected", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_edit, text = _run(reg_edit_policy.edit_gate(row))
    assert can_edit is True
    assert text is None


def test_edit_gate_no_submission_always_passes_even_when_never(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("reg_edit_policy", "never"))
    can_edit, text = _run(reg_edit_policy.edit_gate({}))
    assert can_edit is True
    assert text is None
    can_edit2, text2 = _run(reg_edit_policy.edit_gate(None))
    assert can_edit2 is True
    assert text2 is None


def test_edit_gate_fails_soft_on_registry_error(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def boom(key):
        raise RuntimeError("сбой чтения реестра")

    monkeypatch.setattr(reg_edit_policy, "get_setting_typed", boom)
    can_edit, text = _run(reg_edit_policy.edit_gate({"status": "approved", "registration_date": "x"}))
    assert can_edit is True
    assert text is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1c — кнопка-цикл раздела «📋 Заявки»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_toggle_row_present_before_remoderation_row():
    """Строка стоит в разделе «apps» СРАЗУ ПЕРЕД toggle_reg_edit_remoderation — читается
    парой: сначала «можно ли», потом «что делать с правкой»."""
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    callbacks = [row[1] for row in apps_rows if row[0] == "toggle"]
    i_policy = callbacks.index("toggle_reg_edit_policy")
    i_remod = callbacks.index("toggle_reg_edit_remoderation")
    assert i_policy == i_remod - 1


def test_settings_toggle_rows_contains_reg_edit_policy_row(tmp_path):
    _ready(tmp_path)
    rows = _run(st.settings_toggle_rows())
    assert "toggle_reg_edit_policy" in rows
    button = rows["toggle_reg_edit_policy"][0][0]
    assert button.callback_data == "toggle_reg_edit_policy"
    label = SETTINGS_SCHEMA["reg_edit_policy"]["label"]
    assert label in button.text
    assert "всегда можно" in button.text
    assert "только до решения" in button.text


def test_toggle_cycles_through_three_positions_and_back(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("toggle_reg_edit_policy")
    seen = []
    for _ in range(4):
        _run(st.toggle_reg_edit_policy(cb))
        seen.append(_run(db.get_setting("reg_edit_policy")))
    assert seen == ["until_decision", "never", "always", "until_decision"]


def test_alert_after_tap_is_human_and_never_shows_raw_codes(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("toggle_reg_edit_policy")
    _run(st.toggle_reg_edit_policy(cb))
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    for code in ("always", "until_decision", "never"):
        assert code not in text
    assert "только до решения" in text or "До одобрения" in text


def test_toggle_redraws_apps_section_screen(tmp_path):
    _ready(tmp_path)
    cb = FakeCallback("toggle_reg_edit_policy")
    _run(st.toggle_reg_edit_policy(cb))
    assert cb.message.edit_calls == 1
    assert cb.message.text  # раздел перерисован не пустым текстом


def test_admin_caps_maps_toggle_to_settings_capability():
    from handlers.admin_caps import ADMIN_CAPS
    assert ADMIN_CAPS["toggle_reg_edit_policy"] == "settings"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2 — HTTP-контракт Mini App: профиль, PATCH/submit черновика
# ══════════════════════════════════════════════════════════════════════════════════════════

import httpx

from database import db as bot_db

from tests.test_miniapp_form import FakeBotApi
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    PENDING_ID,
    UNREGISTERED_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _use_tmp_db,
)
from tests.test_miniapp_form import _draft_row, _seed_draft
from miniapp import telegram_api


def _miniapp_ready(tmp_path, name="reg_edit_policy_miniapp.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return db_path


def _bot_api(monkeypatch):
    fake = FakeBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


# ── GET /app/api/profile ─────────────────────────────────────────────────────────────────

def test_profile_can_edit_true_by_default(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_edit"] is True
    assert body["edit_closed_text"] is None


def test_profile_can_edit_false_with_text_when_never(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["can_edit"] is False
    assert body["edit_closed_text"]


async def _seed_unsubmitted_approved_row(telegram_id: int) -> None:
    """Строка `users` без поданной анкеты (пустой `registration_date`, D15) — тот же
    артефакт, что предзаведённая строка амбассадора/менеджера, на который опирается
    докстринг `reg_engine.has_submitted_anketa`. Проходит `delegate_gate` (status="approved"
    читается как разрешённый), но `has_submitted_anketa` для неё ложна."""
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, registration_date) "
            "VALUES (?, ?, 'approved', NULL)",
            (telegram_id, f"User {telegram_id}"),
        )
        await conn.commit()


NO_SUBMISSION_ID = 900199


def test_profile_can_edit_true_without_submission_even_when_never(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    _run(_seed_unsubmitted_approved_row(NO_SUBMISSION_ID))
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/profile", headers=_hdr(NO_SUBMISSION_ID))
    body = resp.json()
    assert body["can_edit"] is True
    assert body["edit_closed_text"] is None


# ── GET /app/api/reg/draft ────────────────────────────────────────────────────────────────

def test_draft_get_edit_closed_true_when_kind_edit_and_never(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    _seed_draft(DELEGATE_ID, kind="edit", patch={"age": 25})
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    body = resp.json()
    assert body["edit_closed"] is True
    assert body["edit_closed_text"]


def test_draft_get_edit_closed_false_when_kind_new_regardless_of_policy(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID))
    body = resp.json()
    assert body["kind"] == "new"
    assert body["edit_closed"] is False
    assert body["edit_closed_text"] is None


# ── PATCH /app/api/reg/draft ──────────────────────────────────────────────────────────────

def test_patch_submitted_anketa_409_edit_closed_no_write(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"phone": "+79997776655"}},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "edit_closed"
    assert body["text"]
    assert _draft_row(DELEGATE_ID) is None  # ничего не записано
    user = _run(bot_db.get_user(DELEGATE_ID))
    assert user.get("phone") != "+79997776655"


def test_patch_kind_new_passes_even_when_never(tmp_path):
    """Главный регресс-сторож: первичная подача не гейтится ни при каком положении."""
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"age": "22"}},
    )
    assert resp.status_code == 200, resp.text


def test_patch_until_decision_pending_passes_approved_blocked(tmp_path):
    db_path = _miniapp_ready(tmp_path)
    _run(bot_db.set_setting("reg_edit_policy", "until_decision"))
    client = _client(_cfg(db_path))

    resp_pending = client.patch(
        "/app/api/reg/draft", headers=_hdr(PENDING_ID),
        json={"version": 0, "answers": {"phone": "+79997776655"}},
    )
    assert resp_pending.status_code == 200, resp_pending.text

    resp_approved = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"phone": "+79997776655"}},
    )
    assert resp_approved.status_code == 409
    assert resp_approved.json()["reason"] == "edit_closed"


# ── POST /app/api/reg/draft/submit ────────────────────────────────────────────────────────

def test_submit_submitted_anketa_409_edit_closed_no_claim(tmp_path, monkeypatch):
    db_path = _miniapp_ready(tmp_path)
    _bot_api(monkeypatch)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    _seed_draft(DELEGATE_ID, kind="edit", patch={"age": 25})
    client = _client(_cfg(db_path))
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "edit_closed"
    assert _draft_row(DELEGATE_ID) is not None  # черновик НЕ захвачен claim_reg_draft


def test_submit_kind_new_passes_even_when_never(tmp_path, monkeypatch):
    db_path = _miniapp_ready(tmp_path)
    _bot_api(monkeypatch)
    _run(bot_db.set_setting("reg_edit_policy", "never"))
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов"})
    client = _client(_cfg(db_path))
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "new"
