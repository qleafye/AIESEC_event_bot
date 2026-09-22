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
    """Строка стоит в разделе «apps» ПЕРЕД toggle_reg_edit_remoderation — читается тройкой:
    сначала «можно ли править», потом «можно ли подать заново» (Квик 260922-wrg, встал
    СРАЗУ ПОСЛЕ этой строки), потом «что делать с правкой»."""
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    callbacks = [row[1] for row in apps_rows if row[0] == "toggle"]
    i_policy = callbacks.index("toggle_reg_edit_policy")
    i_remod = callbacks.index("toggle_reg_edit_remoderation")
    assert i_policy < i_remod


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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3 — вход в правку из чата: `cmd_start`, `reg_resume`, `reg_handoff`
# ══════════════════════════════════════════════════════════════════════════════════════════

from handlers import registration as reg
from handlers import reg_flow
from handlers import reg_handoff
from handlers import reg_resume

from tests.test_returning_delegate_073 import (
    FakeCommand,
    _callback_datas,
    _inline_kb_msgs,
    _new_state,
    _register,
    _texts,
)
from tests.test_returning_delegate_073 import _FakeCallback
from tests.test_returning_delegate_073 import _KBCapturingMessage as _KBMsg

CHAT_UID = 800300


def _chat_ready(tmp_path, name="reg_edit_policy_chat.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    _run(db.set_setting("event_season", "YL'26"))


def test_start_edit_deeplink_blocked_when_never(tmp_path):
    """`/start edit` (T-w2m-03): у одобренного делегата при «нельзя» — текст реестра + главное
    меню, мастер правки не стартует, FSM пуст."""
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        await db.set_setting("reg_edit_policy", "never")
        await db.set_setting("reg_edit_closed_text", "Нельзя, пишите менеджеру.")
        msg = _KBMsg(CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("edit"))
        data = await state.get_data()
        return msg, data

    msg, data = _run(go())
    assert any(t == "Нельзя, пишите менеджеру." for t in _texts(msg))
    assert "_prior_answers" not in data
    assert not any("consent" in (t or "").lower() for t in _texts(msg))


def test_start_edit_deeplink_passes_when_always(tmp_path):
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        msg = _KBMsg(CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("edit"))
        data = await state.get_data()
        return msg, data

    msg, data = _run(go())
    assert not any(t == "Изменить анкету сейчас нельзя. Если нужно что-то поправить — напишите "
                       "менеджеру мероприятия." for t in _texts(msg))
    assert "_prior_answers" in data  # byte-в-byte прежний путь ветки (b) resume_arg == "edit"


def test_start_with_edit_draft_blocked_when_never(tmp_path):
    """`/start` у делегата с черновиком kind='edit' при «нельзя» — тот же ответ, экрана
    «Продолжить/Заново» нет."""
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        await db.upsert_reg_draft(
            CHAT_UID, kind="edit", participant_type="full", step="phone",
            patch={"phone": "+7999"}, source="bot",
        )
        await db.set_setting("reg_edit_policy", "never")
        msg = _KBMsg(CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert not any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


def test_start_with_new_kind_draft_still_offers_resume_when_never(tmp_path):
    """Регресс: черновик kind='new' при «нельзя» -> экран «Продолжить/Заново» как сегодня —
    гейт вообще не касается первичной подачи."""
    _chat_ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            CHAT_UID, kind="new", participant_type="full", step="phone",
            patch={"age": "20"}, source="bot",
        )
        await db.set_setting("reg_edit_policy", "never")
        msg = _KBMsg(CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


def test_rejected_delegate_passes_through_at_any_policy(tmp_path):
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="rejected", season=None)
        await db.set_setting("reg_edit_policy", "never")
        msg = _KBMsg(CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert len(inline) == 1
    assert _callback_datas(inline[0][1]) == ["rereg_start"]


def test_reg_resume_continue_blocked_when_kind_edit_and_never(tmp_path):
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        await db.upsert_reg_draft(
            CHAT_UID, kind="edit", participant_type="full", step="phone",
            patch={"phone": "+7999"}, source="bot",
        )
        await db.set_setting("reg_edit_policy", "never")
        cb = _FakeCallback("reg_resume:continue", CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg_resume.reg_resume_continue(cb, state, bot=object())
        data = await state.get_data()
        return cb, data

    cb, data = _run(go())
    assert _texts(cb.message)  # что-то отправлено делегату
    assert "_draft_kind" not in data  # FSM не восстановлен


def test_reg_handoff_to_bot_blocked_when_kind_edit_and_never(tmp_path):
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        await db.upsert_reg_draft(
            CHAT_UID, kind="edit", participant_type="full", step="phone",
            patch={"phone": "+7999"}, source="bot",
        )
        await db.set_setting("reg_edit_policy", "never")
        cb = _FakeCallback("reg_handoff:to_bot", CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg_handoff.reg_handoff_to_bot(cb, state, bot=object())
        data = await state.get_data()
        return cb, data

    cb, data = _run(go())
    assert "_draft_kind" not in data


def test_reg_resume_restart_yes_still_works_when_never(tmp_path):
    """Р-4 #4: отмена правки (не открывает мастер) остаётся открытой при любом положении."""
    _chat_ready(tmp_path)

    async def go():
        await _register(CHAT_UID, "delegate", status="approved", season="YL'26")
        await db.upsert_reg_draft(
            CHAT_UID, kind="edit", participant_type="full", step="phone",
            patch={"phone": "+7999"}, source="bot",
        )
        await db.set_setting("reg_edit_policy", "never")
        cb = _FakeCallback("reg_resume:restart_yes", CHAT_UID, "delegate")
        state = _new_state(CHAT_UID)
        await reg_resume.reg_resume_restart_yes(cb, state, bot=object())
        draft = await db.get_reg_draft(CHAT_UID)
        return cb, draft

    cb, draft = _run(go())
    assert draft is None  # черновик всё равно удалён
    assert any("Изменения отменены" in (t or "") for t in _texts(cb.message))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Квик 260922-wrg (задача 1) — «🔁 Повторная подача после отказа»: resubmit_allowed_for
# (чистая), resubmit_gate (fail-soft + дефолт текста), open_gate, reg_engine.is_past_season_row,
# кнопка-цикл раздела «📋 Заявки», врезки в /start, rereg_start, finalize_registration.
# ══════════════════════════════════════════════════════════════════════════════════════════

import reg_engine


# ── resubmit_allowed_for — чистое правило ────────────────────────────────────────────────────

def test_resubmit_allow_permits_rejected_current_season():
    assert reg_edit_policy.resubmit_allowed_for("allow", status="rejected", current_season=True) is True


def test_resubmit_deny_forbids_rejected_current_season():
    assert reg_edit_policy.resubmit_allowed_for("deny", status="rejected", current_season=True) is False


def test_resubmit_deny_permits_rejected_past_season():
    """Возвращенец прошлого сезона — тумблер его не касается ни при каком положении."""
    assert reg_edit_policy.resubmit_allowed_for("deny", status="rejected", current_season=False) is True


@pytest.mark.parametrize("status", ["approved", "pending", None, ""])
def test_resubmit_deny_does_not_gate_non_rejected_status(status):
    assert reg_edit_policy.resubmit_allowed_for("deny", status=status, current_season=True) is True


def test_resubmit_unknown_policy_fails_soft_to_allowed():
    assert reg_edit_policy.resubmit_allowed_for("bogus", status="rejected", current_season=True) is True


# ── resubmit_gate — реестр + fail-soft ───────────────────────────────────────────────────────

def test_resubmit_gate_default_allow_passes(tmp_path):
    _ready(tmp_path, "resubmit_gate_default.db")
    row = {"status": "rejected", "season": None}
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(row))
    assert can_resubmit is True
    assert text is None


def test_resubmit_gate_deny_forbids_with_default_text(tmp_path):
    _ready(tmp_path, "resubmit_gate_deny.db")
    _run(db.set_setting("reg_resubmit_after_reject", "deny"))
    row = {"status": "rejected", "season": None}
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(row))
    assert can_resubmit is False
    assert text == SETTINGS_SCHEMA["reg_resubmit_closed_text"]["default"]


def test_resubmit_gate_deny_uses_custom_text(tmp_path):
    _ready(tmp_path, "resubmit_gate_custom.db")
    _run(db.set_setting("reg_resubmit_after_reject", "deny"))
    _run(db.set_setting("reg_resubmit_closed_text", "Напишите менеджеру @manager."))
    row = {"status": "rejected", "season": None}
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(row))
    assert can_resubmit is False
    assert text == "Напишите менеджеру @manager."


def test_resubmit_gate_past_season_rejected_passes_even_when_deny(tmp_path):
    _ready(tmp_path, "resubmit_gate_past.db")
    _run(db.set_setting("event_season", "YL'26"))
    _run(db.set_setting("reg_resubmit_after_reject", "deny"))
    row = {"status": "rejected", "season": "YL'25"}
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(row))
    assert can_resubmit is True
    assert text is None


def test_resubmit_gate_approved_status_always_passes(tmp_path):
    _ready(tmp_path, "resubmit_gate_approved.db")
    _run(db.set_setting("reg_resubmit_after_reject", "deny"))
    row = {"status": "approved", "season": None}
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(row))
    assert can_resubmit is True
    assert text is None


def test_resubmit_gate_fails_soft_on_registry_error(tmp_path, monkeypatch):
    _ready(tmp_path, "resubmit_gate_error.db")

    async def boom(key):
        raise RuntimeError("сбой чтения реестра")

    monkeypatch.setattr(reg_edit_policy, "get_setting_typed", boom)
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate({"status": "rejected"}))
    assert can_resubmit is True
    assert text is None


def test_resubmit_gate_none_row_passes():
    can_resubmit, text = _run(reg_edit_policy.resubmit_gate(None))
    assert can_resubmit is True
    assert text is None


# ── open_gate — edit_gate + resubmit_gate ────────────────────────────────────────────────────

def test_open_gate_edit_closed_wins_over_resubmit(tmp_path):
    _ready(tmp_path, "open_gate_edit_closed.db")
    _run(db.set_setting("reg_edit_policy", "never"))
    row = {"status": "approved", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_open, text = _run(reg_edit_policy.open_gate(row))
    assert can_open is False
    assert text == SETTINGS_SCHEMA["reg_edit_closed_text"]["default"]


def test_open_gate_resubmit_closed_when_edit_open(tmp_path):
    _ready(tmp_path, "open_gate_resubmit_closed.db")
    _run(db.set_setting("reg_resubmit_after_reject", "deny"))
    row = {"status": "rejected", "season": None}
    can_open, text = _run(reg_edit_policy.open_gate(row))
    assert can_open is False
    assert text == SETTINGS_SCHEMA["reg_resubmit_closed_text"]["default"]


def test_open_gate_passes_when_both_open(tmp_path):
    _ready(tmp_path, "open_gate_both_open.db")
    row = {"status": "approved", "season": None, "registration_date": "2026-01-01 00:00:00"}
    can_open, text = _run(reg_edit_policy.open_gate(row))
    assert can_open is True
    assert text is None


# ── reg_engine.is_past_season_row ────────────────────────────────────────────────────────────

def test_is_past_season_row_true_when_differs():
    assert reg_engine.is_past_season_row({"season": "YL'25"}, "YL'26") is True


def test_is_past_season_row_false_when_same():
    assert reg_engine.is_past_season_row({"season": "YL'26"}, "YL'26") is False


def test_is_past_season_row_false_when_row_season_empty():
    assert reg_engine.is_past_season_row({"season": None}, "YL'26") is False


def test_is_past_season_row_false_when_event_season_empty():
    assert reg_engine.is_past_season_row({"season": "YL'25"}, None) is False


def test_is_past_season_row_false_when_row_none():
    assert reg_engine.is_past_season_row(None, "YL'26") is False


# ── Кнопка-цикл раздела «📋 Заявки» ───────────────────────────────────────────────────────────

def test_resubmit_toggle_row_present_right_after_edit_policy_row():
    apps_rows = next(rows for token, _label, rows in sec.SECTIONS if token == "apps")
    callbacks = [row[1] for row in apps_rows if row[0] == "toggle"]
    i_policy = callbacks.index("toggle_reg_edit_policy")
    i_resubmit = callbacks.index("toggle_reg_resubmit_after_reject")
    i_remod = callbacks.index("toggle_reg_edit_remoderation")
    assert i_resubmit == i_policy + 1
    assert i_remod == i_resubmit + 1


def test_settings_toggle_rows_contains_reg_resubmit_row(tmp_path):
    _ready(tmp_path, "resubmit_toggle_rows.db")
    rows = _run(st.settings_toggle_rows())
    assert "toggle_reg_resubmit_after_reject" in rows
    button = rows["toggle_reg_resubmit_after_reject"][0][0]
    assert button.callback_data == "toggle_reg_resubmit_after_reject"
    label = SETTINGS_SCHEMA["reg_resubmit_after_reject"]["label"]
    assert label in button.text
    assert "можно" in button.text
    assert "нельзя" in button.text


def test_resubmit_toggle_cycles_two_positions_and_back(tmp_path):
    _ready(tmp_path, "resubmit_toggle_cycle.db")
    cb = FakeCallback("toggle_reg_resubmit_after_reject")
    seen = []
    for _ in range(3):
        _run(st.toggle_reg_resubmit_after_reject(cb))
        seen.append(_run(db.get_setting("reg_resubmit_after_reject")))
    assert seen == ["deny", "allow", "deny"]


def test_resubmit_toggle_alert_is_human_never_shows_raw_codes(tmp_path):
    _ready(tmp_path, "resubmit_toggle_alert.db")
    cb = FakeCallback("toggle_reg_resubmit_after_reject")
    _run(st.toggle_reg_resubmit_after_reject(cb))
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert "allow" not in text
    assert "deny" not in text
    assert "нельзя" in text or "заново" in text


def test_admin_caps_maps_resubmit_toggle_to_settings_capability():
    from handlers.admin_caps import ADMIN_CAPS
    assert ADMIN_CAPS["toggle_reg_resubmit_after_reject"] == "settings"


# ── /start возвращенца: deny/allow, прошлый сезон при deny сохраняет кнопку ────────────────

RESUBMIT_UID = 800400


def _resubmit_ready(tmp_path, name="reg_resubmit_chat.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(db.init_db())
    _run(db.set_setting("event_season", "YL'26"))


def test_start_returning_rejected_current_season_denied_no_button(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await db.set_setting("reg_resubmit_closed_text", "Заявки в этом сезоне закрыты.")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert all("rereg_start" not in _callback_datas(rm) for (_, rm, _) in inline)
    assert any(t == "Заявки в этом сезоне закрыты." for t in _texts(msg))


def test_start_returning_rejected_current_season_allowed_has_button(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert any("rereg_start" in _callback_datas(rm) for (_, rm, _) in inline)


def test_start_returning_rejected_past_season_keeps_button_even_when_deny(tmp_path):
    """Возвращенец прошлого сезона — тумблер его не касается вовсе."""
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'25")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert any("rereg_start" in _callback_datas(rm) for (_, rm, _) in inline)


def test_start_returning_approved_past_season_keeps_button_when_deny(tmp_path):
    """Approved прошлого сезона — тоже не rejected текущего, тумблер не касается."""
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="approved", season="YL'25")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert any("rereg_start" in _callback_datas(rm) for (_, rm, _) in inline)


# ── rereg_start: deny блокирует без сброса FSM, allow проходит как раньше ──────────────────

def test_rereg_start_denied_when_deny_rejected_current_season(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await db.set_setting("reg_resubmit_closed_text", "Нельзя, сезон закрыт.")
        state = _new_state(RESUBMIT_UID)
        cb = _FakeCallback("rereg_start", RESUBMIT_UID, "delegate")
        await reg_flow.rereg_start(cb, state)
        data = await state.get_data()
        fsm_state = await state.get_state()
        return cb, data, fsm_state

    cb, data, fsm_state = _run(go())
    text, show_alert = cb.answers[-1]
    assert text == "Нельзя, сезон закрыт."
    assert show_alert is True
    assert "_prior_answers" not in data
    assert fsm_state is None


def test_rereg_start_allowed_when_allow_rejected_current_season(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        state = _new_state(RESUBMIT_UID)
        cb = _FakeCallback("rereg_start", RESUBMIT_UID, "delegate")
        await reg_flow.rereg_start(cb, state)
        data = await state.get_data()
        return data

    data = _run(go())
    assert "_prior_answers" in data


# ── черновик (offer_resume): rejected при deny не предлагается ─────────────────────────────

def test_start_rejected_new_draft_not_offered_when_deny(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        await db.upsert_reg_draft(
            RESUBMIT_UID, kind="new", participant_type="full", step="phone",
            patch={"age": "20"}, source="bot",
        )
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await db.set_setting("reg_resubmit_closed_text", "Нельзя подать заново.")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert not any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)
    assert any(t == "Нельзя подать заново." for t in _texts(msg))


def test_start_rejected_new_draft_still_offered_when_allow(tmp_path):
    _resubmit_ready(tmp_path)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        await db.upsert_reg_draft(
            RESUBMIT_UID, kind="new", participant_type="full", step="phone",
            patch={"age": "20"}, source="bot",
        )
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)
        return msg

    msg = _run(go())
    inline = _inline_kb_msgs(msg)
    assert any("reg_resume:continue" in _callback_datas(rm) for (_, rm, _) in inline)


# ── finalize_registration: страховка на случай обхода /start ───────────────────────────────
#
# Ниже врезки claim_reg_draft заглушён сентинелом: если он вызван — страховка НЕ заблокировала
# (дошли до тела функции дальше неё), если нет — заблокировала раньше него. Тот же приём, что
# `monkeypatch.setattr(reg, "upsert_reg_draft", boom)` в tests/test_reg_resume_draft.py.

def test_finalize_registration_blocked_for_rejected_current_season_when_deny(tmp_path, monkeypatch):
    _resubmit_ready(tmp_path)

    async def boom(*a, **k):
        raise AssertionError("страховка обязана заблокировать ДО claim_reg_draft")

    monkeypatch.setattr(reg, "claim_reg_draft", boom)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await db.set_setting("reg_resubmit_closed_text", "Нельзя завершить.")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await state.update_data(full_name="Новое Имя")
        await reg.finalize_registration(msg, state, bot=object())
        return msg

    msg = _run(go())
    assert any(t == "Нельзя завершить." for t in _texts(msg))


def test_finalize_registration_passes_through_for_rejected_current_season_when_allow(tmp_path, monkeypatch):
    _resubmit_ready(tmp_path)
    reached = []

    async def sentinel(*a, **k):
        reached.append(1)
        raise RuntimeError("sentinel stop -- доказывает, что страховка НЕ заблокировала")

    monkeypatch.setattr(reg, "claim_reg_draft", sentinel)

    async def go():
        await _register(RESUBMIT_UID, "delegate", status="rejected", season="YL'26")
        msg = _KBMsg(RESUBMIT_UID, "delegate")
        state = _new_state(RESUBMIT_UID)
        await state.update_data(full_name="Новое Имя")
        try:
            await reg.finalize_registration(msg, state, bot=object())
        except RuntimeError:
            pass
        return reached

    reached = _run(go())
    assert reached == [1]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Правка 260922-wrg (владелец): «настройки должны работать по городам» — reg_edit_policy,
# reg_resubmit_after_reject, reg_resubmit_closed_text помечены "per_city": True и резолвятся
# через cities.get_setting_typed_for_city(key, user_row.get("event_city")). Город A «нельзя»,
# город B «можно» -> разные решения одним и тем же гейтом на разных строках.
# ══════════════════════════════════════════════════════════════════════════════════════════

import cities as cities_mod


def _two_cities():
    return [
        {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
    ]


def test_resubmit_gate_per_city_msk_deny_spb_allow(tmp_path):
    """Ядро правки владельца: один и тот же тумблер `reg_resubmit_after_reject` может быть
    «нельзя» у одного города и «можно» (общее значение) у другого — resubmit_gate отдаёт
    РАЗНЫЕ решения по `event_city` строки, не глобальный ответ на всех."""
    _ready(tmp_path, "resubmit_percity.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting("event_city_enabled", "on")
            await db.set_setting("event_season", "YL'26")
            composed = cities_mod.per_city_key("reg_resubmit_after_reject", "msk")
            await db.set_setting(composed, "deny")
            row_msk = {"status": "rejected", "season": "YL'26", "event_city": "msk"}
            row_spb = {"status": "rejected", "season": "YL'26", "event_city": "spb"}
            return (
                await reg_edit_policy.resubmit_gate(row_msk),
                await reg_edit_policy.resubmit_gate(row_spb),
            )

        (msk_ok, msk_text), (spb_ok, spb_text) = _run(go())
        assert msk_ok is False  # Москва: своё значение "deny"
        assert msk_text == SETTINGS_SCHEMA["reg_resubmit_closed_text"]["default"]
        assert spb_ok is True  # СПб: своего значения нет -> общий дефолт "allow"
        assert spb_text is None
    finally:
        cities_mod.set_cities_for_test(saved)


def test_resubmit_gate_per_city_uses_city_own_closed_text(tmp_path):
    _ready(tmp_path, "resubmit_percity_text.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting("event_city_enabled", "on")
            await db.set_setting("event_season", "YL'26")
            await db.set_setting(cities_mod.per_city_key("reg_resubmit_after_reject", "msk"), "deny")
            await db.set_setting(
                cities_mod.per_city_key("reg_resubmit_closed_text", "msk"),
                "Москва: заявки закрыты.",
            )
            row_msk = {"status": "rejected", "season": "YL'26", "event_city": "msk"}
            return await reg_edit_policy.resubmit_gate(row_msk)

        ok, text = _run(go())
        assert ok is False
        assert text == "Москва: заявки закрыты."
    finally:
        cities_mod.set_cities_for_test(saved)


def test_resubmit_gate_module_off_collapses_to_global_despite_city_override(tmp_path):
    """Модуль городов выключен -> общий ответ байт-в-байт, даже если у Москвы где-то
    завалялось собственное значение (cities.get_setting_typed_for_city's own contract)."""
    _ready(tmp_path, "resubmit_percity_off.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting(cities_mod.per_city_key("reg_resubmit_after_reject", "msk"), "deny")
            row_msk = {"status": "rejected", "season": None, "event_city": "msk"}
            return await reg_edit_policy.resubmit_gate(row_msk)

        ok, text = _run(go())
        assert ok is True  # event_city_enabled никогда не был "on" в этом тесте
        assert text is None
    finally:
        cities_mod.set_cities_for_test(saved)


def test_edit_gate_per_city_never_for_one_city_always_for_other(tmp_path):
    """reg_edit_policy тоже per_city (правка владельца) — тот же приём проверен на edit_gate."""
    _ready(tmp_path, "edit_gate_percity.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting("event_city_enabled", "on")
            await db.set_setting(cities_mod.per_city_key("reg_edit_policy", "msk"), "never")
            row_msk = {
                "status": "approved", "season": None, "event_city": "msk",
                "registration_date": "2026-01-01 00:00:00",
            }
            row_spb = {
                "status": "approved", "season": None, "event_city": "spb",
                "registration_date": "2026-01-01 00:00:00",
            }
            return (
                await reg_edit_policy.edit_gate(row_msk),
                await reg_edit_policy.edit_gate(row_spb),
            )

        (msk_ok, _), (spb_ok, _) = _run(go())
        assert msk_ok is False  # Москва: своё "never"
        assert spb_ok is True  # СПб: общий дефолт "always"
    finally:
        cities_mod.set_cities_for_test(saved)


# ── Админ-тумблер: правка идёт для города из шапки (тот же паттерн, что reg_resume_mode) ──

RESUBMIT_ADMIN_UID = ADMIN_ID


def test_toggle_reg_resubmit_writes_composed_key_for_header_city(tmp_path):
    _ready(tmp_path, "resubmit_toggle_percity.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting("event_city_enabled", "on")
            await cities_mod.set_admin_city(RESUBMIT_ADMIN_UID, "msk")
            cb = FakeCallback("toggle_reg_resubmit_after_reject")
            await st.toggle_reg_resubmit_after_reject(cb)
            composed_msk = cities_mod.per_city_key("reg_resubmit_after_reject", "msk")
            composed_spb = cities_mod.per_city_key("reg_resubmit_after_reject", "spb")
            msk_val = await db.get_setting(composed_msk)
            spb_val = await db.get_setting(composed_spb)
            global_val = await db.get_setting("reg_resubmit_after_reject")
            return cb, msk_val, spb_val, global_val

        cb, msk_val, spb_val, global_val = _run(go())
        assert msk_val == "deny"  # своё значение только у Москвы
        assert not spb_val  # СПб не тронут
        assert not global_val  # общий ключ не тронут -- писали составной
        text, show_alert = cb.answers[-1]
        assert show_alert is True
        assert "Москва" in text
    finally:
        cities_mod.set_cities_for_test(saved)


def test_toggle_reg_resubmit_all_cities_header_uses_global_key(tmp_path):
    """Шапка = «все города» (ALL_CITIES) -> сегодняшняя глобальная ветка, ключ без города."""
    _ready(tmp_path, "resubmit_toggle_allcities.db")
    saved = list(cities_mod.CITIES)
    try:
        cities_mod.set_cities_for_test(_two_cities())

        async def go():
            await db.set_setting("event_city_enabled", "on")
            await cities_mod.set_admin_city(RESUBMIT_ADMIN_UID, cities_mod.ALL_CITIES)
            cb = FakeCallback("toggle_reg_resubmit_after_reject")
            await st.toggle_reg_resubmit_after_reject(cb)
            return await db.get_setting("reg_resubmit_after_reject")

        global_val = _run(go())
        assert global_val == "deny"
    finally:
        cities_mod.set_cities_for_test(saved)
