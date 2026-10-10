"""Phase 33 (delegate-card admin actions, часть 2): четыре новых пункта карточки `/find` —
пометка «кем возвращена» в each-режиме (задача 0), «🧹 Сбросить зависшую анкету» (задача 1),
«👤 Роль для «только /start»» (задача 2), «📎 Заменить резюме» (задача 3). Каждая — свой раздел
файла, тем же приёмом, что tests/test_card_actions_260925.py: fake callback/message на слое
хендлера + прямой вызов сервиса на слое БД.

pytest-asyncio недоступна в этом окружении — `asyncio.run()` на каждый async-хелпер, БД —
шаблонная копия (`tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

import pytest

import domain.cities as cities
from config import config
from database import db
from handlers.access.admin_caps import role_caps_key
from services.applications.revert_pending import revert_to_pending
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260926201
BOUND_MSK_ID = 260926202
BOUND_SPB_ID = 260926203
DELEGATE_ID = 260926301
STARTED_ONLY_ID = 260926302

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path, name="test_card_actions_part2_260926.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    config.GOOGLE_SHEET_ID = ""
    config.GOOGLE_CREDENTIALS_FILE = ""


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


async def _seed_user(telegram_id, *, city="msk", status="approved", full_name="Тест Тестов",
                      username="testdel"):
    await db.add_user({
        "telegram_id": telegram_id, "full_name": full_name, "username": username,
        "registration_date": "2026-01-01", "event_city": city,
    })
    if status != "approved":
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
            await conn.commit()


class _FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return object()


class _FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class _FakeMessage:
    def __init__(self):
        self.edits = []  # (text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id, bot=None, username=None, full_name=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username=username, full_name=full_name)
        self.message = _FakeMessage()
        self.bot = bot or _FakeBot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Задача 0: each-режим — карточка менеджерам называет админа, вернувшего заявку
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_revert_each_mode_admin_text_names_the_admin(tmp_path, monkeypatch):
    """each-режим (reg_submit_notify_mode=each, дефолт): admin_text, ушедший в
    notify_by_capability, обязан нести «...админом <имя>» — тот же посыл, что digest-блок
    «Возвращены на модерацию» уже несёт с 9d15517 (там причина видна из заголовка блока)."""
    _db_ready(tmp_path)
    calls = []

    async def _fake_notify_by_capability(bot, cap, text, **kwargs):
        calls.append(text)
        return 1

    monkeypatch.setattr("handlers.access.admin_caps.notify_by_capability", _fake_notify_by_capability)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(
            DELEGATE_ID, by_admin=SUPERADMIN_ID, admin_name="Мария Менеджерова",
            notify=False, bot=_FakeBot(),
        )

    _run(scenario())
    assert len(calls) == 1
    assert "↩️ <b>Возвращена на модерацию админом Мария Менеджерова</b>" in calls[0]


def test_revert_each_mode_without_admin_name_keeps_old_text(tmp_path, monkeypatch):
    """`admin_name` не передан (обратная совместимость) — текст байт-в-байт прежний."""
    _db_ready(tmp_path)
    calls = []

    async def _fake_notify_by_capability(bot, cap, text, **kwargs):
        calls.append(text)
        return 1

    monkeypatch.setattr("handlers.access.admin_caps.notify_by_capability", _fake_notify_by_capability)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False, bot=_FakeBot())

    _run(scenario())
    assert "↩️ <b>Возвращена на модерацию</b>" in calls[0]
    assert "админом" not in calls[0]


def test_revertp_apply_passes_admin_name_from_callback(tmp_path, monkeypatch):
    """UI-слой: `revertp_apply` вычисляет имя из `callback.from_user` тем же приёмом, что
    `admin_sos.py::sos_claim`/`sos_resolve` (full_name -> username -> код-фолбэк «Админ»)."""
    from handlers.applications import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.applications.admin_revert_pending.revert_to_pending", _fake_revert)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(
            f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID, full_name="Мария Менеджерова",
        )
        await admin_revert_pending.revertp_apply(cb)
        return cb

    _run(scenario())
    assert calls[0]["admin_name"] == "Мария Менеджерова"


def test_revertp_apply_admin_name_falls_back_to_username_then_code(tmp_path, monkeypatch):
    from handlers.applications import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.applications.admin_revert_pending.revert_to_pending", _fake_revert)

    async def scenario_username():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID, username="mgr_no_name")
        await admin_revert_pending.revertp_apply(cb)

    _run(scenario_username())
    assert calls[0]["admin_name"] == "mgr_no_name"

    calls.clear()
    _db_ready(tmp_path, name="test_card_actions_part2_260926_b.db")

    async def scenario_bare():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID)
        await admin_revert_pending.revertp_apply(cb)

    _run(scenario_bare())
    assert calls[0]["admin_name"] == "Админ"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: «🧹 Сбросить зависшую анкету»
# ═══════════════════════════════════════════════════════════════════════════════════════════

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from handlers.states import Registration, SosReport  # noqa: E402
from services.reg_stuck_reset import preview_stuck_reset, reset_stuck_registration  # noqa: E402


class _FakeBotWithId(_FakeBot):
    def __init__(self, bot_id=1):
        super().__init__()
        self.id = bot_id


def _fsm_ctx(storage, tid, bot_id=1):
    return FSMContext(storage=storage, key=StorageKey(bot_id=bot_id, chat_id=tid, user_id=tid))


async def _seed_draft(tid, *, kind="new", step="phone", event_city="msk", minutes_ago=0):
    """Черновик со свежей `updated_at` (upsert_reg_draft штампует msk_now()); `minutes_ago`
    отматывает `updated_at` назад ПОСЛЕ вставки — прямой UPDATE, тот же приём, что тесты
    city_move используют для «давних» черновиков."""
    await db.upsert_reg_draft(
        tid, kind=kind, event_city=event_city, step=step, patch={"phone": "+79990000000"},
        source="bot",
    )
    if minutes_ago:
        from datetime import timedelta

        from services.infra.timeutil import msk_now

        stamp = (msk_now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET updated_at = ? WHERE telegram_id = ?", (stamp, tid),
            )
            await conn.commit()


# ── services/reg_stuck_reset.py — БД-слой ────────────────────────────────────────────────────

def test_preview_stuck_reset_none_without_draft(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        return await preview_stuck_reset(STARTED_ONLY_ID)

    assert _run(scenario()) is None


def test_preview_stuck_reset_reports_step_and_recent_minutes(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        await _seed_draft(STARTED_ONLY_ID, kind="new", step="phone", minutes_ago=5)
        return await preview_stuck_reset(STARTED_ONLY_ID)

    preview = _run(scenario())
    assert preview["ok"] is True
    assert preview["kind"] == "new"
    assert preview["step"] == "phone"
    assert preview["minutes_since_activity"] is not None
    assert 4 <= preview["minutes_since_activity"] <= 6
    assert preview["is_recent"] is True
    assert preview["has_submitted_application"] is False


def test_preview_stuck_reset_not_recent_past_15_minutes(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new", step="university", minutes_ago=40)
        return await preview_stuck_reset(STARTED_ONLY_ID)

    preview = _run(scenario())
    assert preview["is_recent"] is False
    assert 39 <= preview["minutes_since_activity"] <= 41


def test_preview_stuck_reset_edit_kind_has_submitted_application(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        await _seed_draft(DELEGATE_ID, kind="edit", step="phone", minutes_ago=2)
        return await preview_stuck_reset(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["kind"] == "edit"
    assert preview["has_submitted_application"] is True


def test_reset_new_kind_deletes_draft_and_reg_started(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        report = await reset_stuck_registration(
            _FakeBotWithId(), None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )
        draft = await db.get_reg_draft(STARTED_ONLY_ID)
        started = await db.get_reg_started_by_id(STARTED_ONLY_ID)
        return report, draft, started

    report, draft, started = _run(scenario())
    assert report["ok"] is True
    assert report["kind"] == "new"
    assert report["reg_started_cleared"] is True
    assert draft is None
    assert started is None


def test_reset_edit_kind_keeps_reg_started_and_users_untouched(tmp_path):
    """Координатор/33-SEED: правка уже поданной анкеты — сбрасывается ТОЛЬКО черновик,
    users не трогается (тут же и косвенно: reg_started для такого делегата обычно и так
    пуст, clear_reg_started зовётся один раз, на первой подаче)."""
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", full_name="Одобренный Делегат")
        await _seed_draft(DELEGATE_ID, kind="edit")
        report = await reset_stuck_registration(
            _FakeBotWithId(), None, DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False,
        )
        draft = await db.get_reg_draft(DELEGATE_ID)
        user = await db.get_user(DELEGATE_ID)
        return report, draft, user

    report, draft, user = _run(scenario())
    assert report["ok"] is True
    assert report["kind"] == "edit"
    assert report["reg_started_cleared"] is False
    assert draft is None
    assert user is not None
    assert user["full_name"] == "Одобренный Делегат"
    assert user["status"] == "approved"


def test_reset_race_no_draft_reports_error(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        return await reset_stuck_registration(
            _FakeBotWithId(), None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )

    report = _run(scenario())
    assert report["ok"] is False
    assert "уже нет" in report["error"]


def test_reset_clears_fsm_when_registration_state(tmp_path):
    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        ctx = _fsm_ctx(storage, STARTED_ONLY_ID)
        await ctx.set_state(Registration.phone)
        await ctx.update_data(full_name="Тест")
        report = await reset_stuck_registration(
            _FakeBotWithId(), storage, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )
        after_state = await ctx.get_state()
        after_data = await ctx.get_data()
        return report, after_state, after_data

    report, after_state, after_data = _run(scenario())
    assert report["fsm_cleared"] is True
    assert after_state is None
    assert after_data == {}


def test_reset_does_not_touch_fsm_of_other_scenario(tmp_path):
    """Координатор 25.09: состояние вне групп анкеты регистрации (SOS, опрос, ...) НЕ
    трогаем — делегат мог зайти в анкету раньше и уйти в другой сценарий, не закрыв
    черновик, второй случай не наш(его сброса) удел."""
    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        ctx = _fsm_ctx(storage, STARTED_ONLY_ID)
        await ctx.set_state(SosReport.collecting)
        await ctx.update_data(sos_collecting_report_id=42)
        report = await reset_stuck_registration(
            _FakeBotWithId(), storage, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )
        after_state = await ctx.get_state()
        after_data = await ctx.get_data()
        return report, after_state, after_data

    report, after_state, after_data = _run(scenario())
    assert report["fsm_cleared"] is False
    assert after_state == SosReport.collecting.state
    assert after_data == {"sos_collecting_report_id": 42}


def test_reset_no_storage_is_harmless(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        return await reset_stuck_registration(
            _FakeBotWithId(), None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )

    report = _run(scenario())
    assert report["ok"] is True
    assert report["fsm_cleared"] is False


def test_reset_notify_true_sends_delegate_message(tmp_path):
    _db_ready(tmp_path)
    bot = _FakeBotWithId()

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        return await reset_stuck_registration(
            bot, None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=True,
        )

    report = _run(scenario())
    assert report["notified"] is True
    delegate_sends = [s for s in bot.sent if s[0] == STARTED_ONLY_ID]
    assert len(delegate_sends) == 1
    assert "/start" in delegate_sends[0][1]


def test_reset_notify_false_sends_nothing(tmp_path):
    _db_ready(tmp_path)
    bot = _FakeBotWithId()

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        return await reset_stuck_registration(
            bot, None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )

    _run(scenario())
    assert bot.sent == []


def test_reset_records_answer_history(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_draft(STARTED_ONLY_ID, kind="new")
        await reset_stuck_registration(
            _FakeBotWithId(), None, STARTED_ONLY_ID, by_admin=SUPERADMIN_ID, notify=False,
        )
        return await db.get_answer_history(STARTED_ONLY_ID)

    history = _run(scenario())
    assert history and history[0]["source"] == f"admin:{SUPERADMIN_ID}"


# ── handlers/applications/admin_reg_reset.py — UI-слой ───────────────────────────────────────────────────

def test_regreset_start_shows_activity_line(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        await _seed_draft(DELEGATE_ID, kind="edit", step="phone", minutes_ago=20)
        cb = _FakeCallback(f"regreset_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_reg_reset.regreset_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Сбросить зависшую анкету" in text
    assert "20 мин назад" in text
    assert "шаг phone" in text
    assert "⚠️" not in text
    buttons = _cbs(kb)
    assert f"regreset_apply:{DELEGATE_ID}:1" in buttons
    assert f"regreset_cancel:{DELEGATE_ID}" in buttons


def test_regreset_start_warns_about_recent_activity(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        await _seed_draft(STARTED_ONLY_ID, kind="new", step="full_name", minutes_ago=3)
        cb = _FakeCallback(f"regreset_start:{STARTED_ONLY_ID}", SUPERADMIN_ID)
        await admin_reg_reset.regreset_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "⚠️" in text
    assert "прямо сейчас заполняет анкету" in text
    # Кнопка сброса остаётся доступна, даже при недавней активности.
    buttons = _cbs(kb)
    assert f"regreset_apply:{STARTED_ONLY_ID}:1" in buttons


def test_regreset_start_no_draft_alert(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"regreset_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_reg_reset.regreset_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_regreset_toggle_flips_notify_button(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        await _seed_draft(DELEGATE_ID, kind="edit")
        cb = _FakeCallback(f"regreset_toggle:{DELEGATE_ID}:0", SUPERADMIN_ID)
        await admin_reg_reset.regreset_toggle(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    button_labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("ВЫКЛ" in t for t in button_labels)
    buttons = _cbs(kb)
    assert f"regreset_apply:{DELEGATE_ID}:0" in buttons


def test_regreset_apply_uses_displayed_notify_state(tmp_path, monkeypatch):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)
    calls = []

    async def _fake_reset(bot, storage, tid, *, by_admin, notify):
        calls.append({"tid": tid, "by_admin": by_admin, "notify": notify})
        return {"ok": True, "kind": "new", "fsm_cleared": True, "notified": True}

    monkeypatch.setattr("handlers.applications.admin_reg_reset.reset_stuck_registration", _fake_reset)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"regreset_apply:{DELEGATE_ID}:1", SUPERADMIN_ID)
        await admin_reg_reset.regreset_apply(cb)
        return cb

    cb = _run(scenario())
    assert calls[0]["notify"] is True
    assert calls[0]["tid"] == DELEGATE_ID
    text, _ = cb.message.edits[0]
    assert "сброшена" in text


def test_regreset_apply_denies_forged_city_out_of_scope(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="approved")
        await _seed_draft(DELEGATE_ID, kind="edit", event_city="spb")
        cb = _FakeCallback(f"regreset_apply:{DELEGATE_ID}:0", BOUND_MSK_ID)
        await admin_reg_reset.regreset_apply(cb)
        draft_after = await db.get_reg_draft(DELEGATE_ID)
        return cb, draft_after

    cb, draft_after = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True
    assert draft_after is not None  # не тронуто


def test_regreset_cancel_changes_nothing(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        await _seed_draft(DELEGATE_ID, kind="edit")
        cb = _FakeCallback(f"regreset_cancel:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_reg_reset.regreset_cancel(cb)
        draft_after = await db.get_reg_draft(DELEGATE_ID)
        return cb, draft_after

    cb, draft_after = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "отменён" in text
    assert draft_after is not None


def test_regreset_capability_registered_for_every_callback():
    from handlers.access.admin_caps import ADMIN_CAPS

    for prefix in ("regreset_start:*", "regreset_toggle:*", "regreset_apply:*", "regreset_cancel:*"):
        assert ADMIN_CAPS.get(prefix) == "moderate_reg", prefix


def test_find_card_shows_reset_button_only_with_draft(tmp_path):
    """Кнопка на карточке /find видна, только когда есть черновик (33-SEED «Кнопка видна,
    когда есть черновик»)."""
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario_with_draft():
        await _seed_user(DELEGATE_ID, status="approved", username="withdraft")
        await _seed_draft(DELEGATE_ID, kind="edit")

        import handlers.admin as admin_mod

        async def _fake_get_user_by_username(username):
            return await db.get_user(DELEGATE_ID)

        orig = admin_mod.get_user_by_username
        admin_mod.get_user_by_username = _fake_get_user_by_username
        try:
            captured = {}

            class _M:
                def __init__(self):
                    self.text = f"/find @withdraft"

                async def answer(self, text, parse_mode=None, reply_markup=None):
                    captured["text"] = text
                    captured["kb"] = reply_markup

            await admin.cmd_find_user(_M())
        finally:
            admin_mod.get_user_by_username = orig
        return captured

    captured = _run(scenario_with_draft())
    buttons = _cbs(captured["kb"])
    assert f"regreset_start:{DELEGATE_ID}" in buttons


def test_find_card_hides_reset_button_without_draft(tmp_path):
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", username="nodraft")

        import handlers.admin as admin_mod

        async def _fake_get_user_by_username(username):
            return await db.get_user(DELEGATE_ID)

        orig = admin_mod.get_user_by_username
        admin_mod.get_user_by_username = _fake_get_user_by_username
        try:
            captured = {}

            class _M:
                def __init__(self):
                    self.text = f"/find @nodraft"

                async def answer(self, text, parse_mode=None, reply_markup=None):
                    captured["text"] = text
                    captured["kb"] = reply_markup

            await admin.cmd_find_user(_M())
        finally:
            admin_mod.get_user_by_username = orig
        return captured

    captured = _run(scenario())
    buttons = _cbs(captured["kb"])
    assert f"regreset_start:{DELEGATE_ID}" not in buttons


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: «👤 Роль для «только /start»»
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_find_falls_back_to_reg_started_when_not_in_users(tmp_path):
    """/find сам ищет только @username (память проекта) — фоллбэк на reg_started тем же
    приёмом, что services/access/person_search.py уже даёт мастеру выдачи ролей."""
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")

        captured = {}

        class _M:
            def __init__(self):
                self.text = "/find @started_only"
                self.from_user = _FakeUser(SUPERADMIN_ID)

            async def answer(self, text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

        await admin.cmd_find_user(_M())
        return captured

    captured = _run(scenario())
    assert "анкету не подавал" in captured["text"]
    assert "started_only" in captured["text"]
    assert "не найден в базе данных" not in captured["text"]
    buttons = _cbs(captured["kb"])
    assert f"roles_addfor:{STARTED_ONLY_ID}" in buttons


def test_find_hides_role_button_without_settings_capability(tmp_path):
    """Ревью part2: «roles_addfor:*» требует `settings` (ADMIN_CAPS) — модератор без этого
    права не должен видеть кнопку, которая в ответ на тап отказала бы."""
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await _setup_bound_staff()  # BOUND_MSK_ID держит только moderate_reg, не settings
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")

        captured = {}

        class _M:
            def __init__(self):
                self.text = "/find @started_only"
                self.from_user = _FakeUser(BOUND_MSK_ID)

            async def answer(self, text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

        await admin.cmd_find_user(_M())
        return captured

    captured = _run(scenario())
    buttons = _cbs(captured["kb"])
    assert f"roles_addfor:{STARTED_ONLY_ID}" not in buttons


def test_find_still_reports_truly_unknown_username(tmp_path):
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        captured = {}

        class _M:
            def __init__(self):
                self.text = "/find @nobody_at_all"

            async def answer(self, text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

        await admin.cmd_find_user(_M())
        return captured

    captured = _run(scenario())
    assert "не найден в базе данных" in captured["text"]


def test_find_users_row_takes_priority_over_reg_started(tmp_path):
    """Тёзка (тот же telegram_id есть и в users, и в reg_started, обычный путь после подачи
    анкеты) — карточка полная, не «не подавал(а)»."""
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", username="samepersn")
        await db.mark_reg_started(DELEGATE_ID, "samepersn", event_city="msk")

        captured = {}

        class _M:
            def __init__(self):
                self.text = "/find @samepersn"

            async def answer(self, text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

        await admin.cmd_find_user(_M())
        return captured

    captured = _run(scenario())
    assert "Пользователь найден:" in captured["text"]
    assert "анкету не подавал" not in captured["text"]


def test_find_reg_started_card_shows_reset_button_when_draft_exists(tmp_path):
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        await _seed_draft(STARTED_ONLY_ID, kind="new")

        captured = {}

        class _M:
            def __init__(self):
                self.text = "/find @started_only"
                self.from_user = _FakeUser(SUPERADMIN_ID)

            async def answer(self, text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

        await admin.cmd_find_user(_M())
        return captured

    captured = _run(scenario())
    buttons = _cbs(captured["kb"])
    assert f"regreset_start:{STARTED_ONLY_ID}" in buttons


# ── handlers/access/admin_roles.py — прямой вход в мастер выдачи роли ─────────────────────────────

def test_roles_add_for_shows_assign_screen_for_reg_started_person(tmp_path):
    from handlers.access import admin_roles

    _db_ready(tmp_path)

    async def scenario():
        await db.mark_reg_started(STARTED_ONLY_ID, "started_only", event_city="msk")
        cb = _FakeCallback(f"roles_addfor:{STARTED_ONLY_ID}", SUPERADMIN_ID)
        await admin_roles.roles_add_for(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Кого назначить" in text
    assert "анкету пока не подавал" in text
    buttons = _cbs(kb)
    assert f"roles_addrole:{STARTED_ONLY_ID}:reg_manager" in buttons


def test_roles_add_for_shows_assign_screen_for_users_row(tmp_path):
    from handlers.access import admin_roles

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", full_name="Полная Анкета")
        cb = _FakeCallback(f"roles_addfor:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_roles.roles_add_for(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "Полная Анкета" in text
    assert "анкету пока не подавал" not in text


def test_roles_add_for_rejects_malformed_callback(tmp_path):
    from handlers.access import admin_roles

    _db_ready(tmp_path)

    async def scenario():
        cb = _FakeCallback("roles_addfor:not_a_number", SUPERADMIN_ID)
        await admin_roles.roles_add_for(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_roles_addfor_capability_registered():
    from handlers.access.admin_caps import ADMIN_CAPS

    assert ADMIN_CAPS.get("roles_addfor:*") == "settings"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: «📎 Заменить резюме»
# ═══════════════════════════════════════════════════════════════════════════════════════════

from services.resume_replace import (  # noqa: E402
    preview_resume_replace, replace_resume, validate_resume_document,
)


def _configure_nextcloud(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "https://cloud.example.org/remote.php/dav/files/bot")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "TOK")


def _unconfigure_nextcloud(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_WEBDAV_URL", "")
    monkeypatch.setattr(config, "NEXTCLOUD_PUBLIC_URL", "")
    monkeypatch.setattr(config, "NEXTCLOUD_FOLDER_SHARE_TOKEN", "")


class _FakeDocument:
    def __init__(self, file_id, file_name, file_size=1000):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class _FakeStateMessage:
    def __init__(self, user_id, document=None, text=None):
        self.from_user = _FakeUser(user_id)
        self.document = document
        self.text = text
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


# ── services/resume_replace.py — валидация + БД-слой ────────────────────────────────────────

def test_validate_resume_document_rejects_wrong_extension():
    assert validate_resume_document("resume.exe", 1000) is not None


def test_validate_resume_document_rejects_too_large():
    from domain.regform.engine import RESUME_MAX_BYTES

    assert validate_resume_document("resume.pdf", RESUME_MAX_BYTES + 1) is not None


def test_validate_resume_document_accepts_pdf_and_docx():
    assert validate_resume_document("resume.pdf", 1000) is None
    assert validate_resume_document("resume.DOCX", 1000) is None


def test_preview_resume_replace_none_for_unknown_user(tmp_path):
    _db_ready(tmp_path)
    assert _run(preview_resume_replace(999999999)) is None


def test_preview_resume_replace_reports_old_state(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET resume_file_id = ?, resume_url = ? WHERE telegram_id = ?",
                ("OLDFILE", "https://cloud.example.org/s/TOK/download?files=old.pdf", DELEGATE_ID),
            )
            await conn.commit()
        return await preview_resume_replace(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["ok"] is True
    assert preview["had_old_file"] is True
    assert preview["old_resume_url"] == "https://cloud.example.org/s/TOK/download?files=old.pdf"


def test_preview_resume_replace_no_old_file(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await preview_resume_replace(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["had_old_file"] is False
    assert preview["old_resume_url"] is None


def test_replace_resume_unknown_user_errors(tmp_path):
    _db_ready(tmp_path)
    report = _run(replace_resume(
        _FakeBotWithId(), 999999999, "NEWFILE", "resume.pdf", by_admin=SUPERADMIN_ID,
    ))
    assert report["ok"] is False


def test_replace_resume_not_configured_still_updates_file_id(tmp_path, monkeypatch):
    """Сбой/отсутствие Nextcloud НЕ рвёт саму замену file_id (fail-soft, 33-SEED)."""
    _db_ready(tmp_path)
    _unconfigure_nextcloud(monkeypatch)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        report = await replace_resume(
            _FakeBotWithId(), DELEGATE_ID, "NEWFILE", "resume.pdf", by_admin=SUPERADMIN_ID,
        )
        user = await db.get_user(DELEGATE_ID)
        return report, user

    report, user = _run(scenario())
    assert report["ok"] is True
    assert report["sheet_updated"] is False
    assert report["cloud_error"] is not None
    assert user["resume_file_id"] == "NEWFILE"


def test_replace_resume_uploads_and_updates_sheet(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _configure_nextcloud(monkeypatch)

    from services import nextcloud as nextcloud_mod
    from services.sheets import sheets as sheets_mod

    upload_calls = []

    async def _fake_upload_resume(bot, file_id, filename):
        upload_calls.append((file_id, filename))
        return "https://cloud.example.org/s/TOK/download?files=new.pdf"

    sheet_calls = []

    async def _fake_update_row_by_id(tab, tid, row):
        sheet_calls.append((tab, tid))
        return True

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _fake_update_row_by_id)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET resume_file_id = ? WHERE telegram_id = ?", ("OLDFILE", DELEGATE_ID),
            )
            await conn.commit()
        report = await replace_resume(
            _FakeBotWithId(), DELEGATE_ID, "NEWFILE", "resume.pdf", by_admin=SUPERADMIN_ID,
        )
        user = await db.get_user(DELEGATE_ID)
        history = await db.get_answer_history(DELEGATE_ID)
        return report, user, history

    report, user, history = _run(scenario())
    assert report["ok"] is True
    assert report["sheet_updated"] is True
    assert report["new_resume_url"] == "https://cloud.example.org/s/TOK/download?files=new.pdf"
    assert user["resume_file_id"] == "NEWFILE"
    assert user["resume_url"] == "https://cloud.example.org/s/TOK/download?files=new.pdf"
    assert upload_calls and upload_calls[0][0] == "NEWFILE"
    assert sheet_calls
    assert history and history[0]["source"] == f"admin:{SUPERADMIN_ID}"
    assert history[0]["changes"] == [{"column": "resume_file_id", "old": True, "new": True}]


def test_replace_resume_upload_failure_reports_cloud_error(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _configure_nextcloud(monkeypatch)

    from services import nextcloud as nextcloud_mod

    async def _fake_upload_resume_fail(bot, file_id, filename):
        return None

    monkeypatch.setattr(nextcloud_mod, "upload_resume", _fake_upload_resume_fail)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await replace_resume(
            _FakeBotWithId(), DELEGATE_ID, "NEWFILE", "resume.pdf", by_admin=SUPERADMIN_ID,
        )

    report = _run(scenario())
    assert report["ok"] is True
    assert report["sheet_updated"] is False
    assert report["cloud_error"] is not None


# ── handlers/applications/admin_resume_replace.py — UI-слой ──────────────────────────────────────────────

def test_resumerep_start_sets_state_and_shows_old_resume(tmp_path):
    from handlers.applications import admin_resume_replace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", full_name="Тест Тестов")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET resume_file_id = ?, resume_url = ? WHERE telegram_id = ?",
                ("OLDFILE", "https://cloud.example.org/s/TOK/download?files=old.pdf", DELEGATE_ID),
            )
            await conn.commit()
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        cb = _FakeCallback(f"resumerep_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resume_replace.resumerep_start(cb, state)
        after_state = await state.get_state()
        after_data = await state.get_data()
        return cb, after_state, after_data

    cb, after_state, after_data = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Заменить резюме" in text
    assert "old.pdf" in text
    from handlers.states import ResumeReplace
    assert after_state == ResumeReplace.waiting_for_file.state
    assert after_data["resumerep_tid"] == DELEGATE_ID
    buttons = _cbs(kb)
    assert f"resumerep_cancel:{DELEGATE_ID}" in buttons


def test_resumerep_start_denied_when_city_out_of_scope(tmp_path):
    from handlers.applications import admin_resume_replace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="approved")
        state = _fsm_ctx(storage, BOUND_MSK_ID)
        cb = _FakeCallback(f"resumerep_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await admin_resume_replace.resumerep_start(cb, state)
        after_state = await state.get_state()
        return cb, after_state

    cb, after_state = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True
    assert after_state is None


def test_resumerep_cancel_clears_state(tmp_path):
    from handlers.applications import admin_resume_replace
    from handlers.states import ResumeReplace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        await state.set_state(ResumeReplace.waiting_for_file)
        await state.update_data(resumerep_tid=DELEGATE_ID)
        cb = _FakeCallback(f"resumerep_cancel:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resume_replace.resumerep_cancel(cb, state)
        after_state = await state.get_state()
        return cb, after_state

    cb, after_state = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "отменена" in text
    assert after_state is None


def test_resumerep_cancel_text_clears_state(tmp_path):
    from handlers.applications import admin_resume_replace
    from handlers.states import ResumeReplace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        await state.set_state(ResumeReplace.waiting_for_file)
        await state.update_data(resumerep_tid=DELEGATE_ID)
        msg = _FakeStateMessage(SUPERADMIN_ID, text="/cancel")
        await admin_resume_replace.resumerep_cancel_text(msg, state)
        after_state = await state.get_state()
        return msg, after_state

    msg, after_state = _run(scenario())
    assert "отменена" in msg.answers[0][0]
    assert after_state is None


def test_resumerep_receive_file_rejects_wrong_type_keeps_state(tmp_path):
    from handlers.applications import admin_resume_replace
    from handlers.states import ResumeReplace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        await state.set_state(ResumeReplace.waiting_for_file)
        await state.update_data(resumerep_tid=DELEGATE_ID)
        msg = _FakeStateMessage(SUPERADMIN_ID, document=_FakeDocument("BAD", "resume.exe"))
        await admin_resume_replace.resumerep_receive_file(msg, state, _FakeBotWithId())
        after_state = await state.get_state()
        user = await db.get_user(DELEGATE_ID)
        return msg, after_state, user

    msg, after_state, user = _run(scenario())
    assert "PDF" in msg.answers[0][0] or "DOCX" in msg.answers[0][0]
    assert after_state == ResumeReplace.waiting_for_file.state  # состояние осталось
    assert user["resume_file_id"] is None  # не тронуто


def test_resumerep_receive_file_success(tmp_path, monkeypatch):
    from handlers.applications import admin_resume_replace
    from handlers.states import ResumeReplace

    _db_ready(tmp_path)
    _unconfigure_nextcloud(monkeypatch)
    storage = MemoryStorage()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", full_name="Тест Тестов")
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        await state.set_state(ResumeReplace.waiting_for_file)
        await state.update_data(resumerep_tid=DELEGATE_ID)
        msg = _FakeStateMessage(SUPERADMIN_ID, document=_FakeDocument("NEWFILE", "resume.pdf"))
        await admin_resume_replace.resumerep_receive_file(msg, state, _FakeBotWithId())
        after_state = await state.get_state()
        user = await db.get_user(DELEGATE_ID)
        return msg, after_state, user

    msg, after_state, user = _run(scenario())
    text = msg.answers[0][0]
    assert "заменено" in text
    assert after_state is None
    assert user["resume_file_id"] == "NEWFILE"


def test_resumerep_receive_file_denies_forged_city_out_of_scope(tmp_path):
    from handlers.applications import admin_resume_replace
    from handlers.states import ResumeReplace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="approved")
        state = _fsm_ctx(storage, BOUND_MSK_ID)
        await state.set_state(ResumeReplace.waiting_for_file)
        await state.update_data(resumerep_tid=DELEGATE_ID)
        msg = _FakeStateMessage(BOUND_MSK_ID, document=_FakeDocument("NEWFILE", "resume.pdf"))
        await admin_resume_replace.resumerep_receive_file(msg, state, _FakeBotWithId())
        after_state = await state.get_state()
        user = await db.get_user(DELEGATE_ID)
        return msg, after_state, user

    msg, after_state, user = _run(scenario())
    assert after_state is None  # состояние гасится и при отказе — начинать заново с карточки
    assert user["resume_file_id"] is None


def test_resumerep_receive_other_reminds(tmp_path):
    from handlers.applications import admin_resume_replace

    _db_ready(tmp_path)

    async def scenario():
        msg = _FakeStateMessage(SUPERADMIN_ID, text="привет")
        await admin_resume_replace.resumerep_receive_other(msg)
        return msg

    msg = _run(scenario())
    assert "PDF" in msg.answers[0][0] or "DOCX" in msg.answers[0][0]


def test_resumerep_capability_registered_for_every_callback():
    from handlers.access.admin_caps import ADMIN_CAPS

    for prefix in ("resumerep_start:*", "resumerep_cancel:*", "state:ResumeReplace:*"):
        assert ADMIN_CAPS.get(prefix) == "moderate_reg", prefix


def test_find_card_shows_resume_replace_button_for_submitted_delegate(tmp_path):
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", username="hasresume")

        import handlers.admin as admin_mod

        async def _fake_get_user_by_username(username):
            return await db.get_user(DELEGATE_ID)

        orig = admin_mod.get_user_by_username
        admin_mod.get_user_by_username = _fake_get_user_by_username
        try:
            captured = {}

            class _M:
                def __init__(self):
                    self.text = "/find @hasresume"

                async def answer(self, text, parse_mode=None, reply_markup=None):
                    captured["text"] = text
                    captured["kb"] = reply_markup

            await admin.cmd_find_user(_M())
        finally:
            admin_mod.get_user_by_username = orig
        return captured

    captured = _run(scenario())
    buttons = _cbs(captured["kb"])
    assert f"resumerep_start:{DELEGATE_ID}" in buttons


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Ревью part2: _parse_tid отклоняет отрицательные id (делегатский telegram_id никогда не
# отрицателен — это чаты/каналы, не люди), тот же гейт, что admin_roles._parse_staff_role_callback.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_regreset_parse_tid_rejects_negative_id(tmp_path):
    from handlers.applications import admin_reg_reset

    _db_ready(tmp_path)

    async def scenario():
        cb = _FakeCallback("regreset_start:-1", SUPERADMIN_ID)
        await admin_reg_reset.regreset_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_resumerep_parse_tid_rejects_negative_id(tmp_path):
    from handlers.applications import admin_resume_replace

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        state = _fsm_ctx(storage, SUPERADMIN_ID)
        cb = _FakeCallback("resumerep_start:-1", SUPERADMIN_ID)
        await admin_resume_replace.resumerep_start(cb, state)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True
