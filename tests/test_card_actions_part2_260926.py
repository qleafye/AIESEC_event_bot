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

import cities
from config import config
from database import db
from handlers.admin_caps import role_caps_key
from services.revert_pending import revert_to_pending
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

    monkeypatch.setattr("handlers.admin_caps.notify_by_capability", _fake_notify_by_capability)

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

    monkeypatch.setattr("handlers.admin_caps.notify_by_capability", _fake_notify_by_capability)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False, bot=_FakeBot())

    _run(scenario())
    assert "↩️ <b>Возвращена на модерацию</b>" in calls[0]
    assert "админом" not in calls[0]


def test_revertp_apply_passes_admin_name_from_callback(tmp_path, monkeypatch):
    """UI-слой: `revertp_apply` вычисляет имя из `callback.from_user` тем же приёмом, что
    `admin_sos.py::sos_claim`/`sos_resolve` (full_name -> username -> код-фолбэк «Админ»)."""
    from handlers import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.admin_revert_pending.revert_to_pending", _fake_revert)

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
    from handlers import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": False}

    monkeypatch.setattr("handlers.admin_revert_pending.revert_to_pending", _fake_revert)

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

        from services.timeutil import msk_now

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


# ── handlers/admin_reg_reset.py — UI-слой ───────────────────────────────────────────────────

def test_regreset_start_shows_activity_line(tmp_path):
    from handlers import admin_reg_reset

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
    from handlers import admin_reg_reset

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
    from handlers import admin_reg_reset

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
    from handlers import admin_reg_reset

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
    from handlers import admin_reg_reset

    _db_ready(tmp_path)
    calls = []

    async def _fake_reset(bot, storage, tid, *, by_admin, notify):
        calls.append({"tid": tid, "by_admin": by_admin, "notify": notify})
        return {"ok": True, "kind": "new", "fsm_cleared": True, "notified": True}

    monkeypatch.setattr("handlers.admin_reg_reset.reset_stuck_registration", _fake_reset)

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
    from handlers import admin_reg_reset

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
    from handlers import admin_reg_reset

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
    from handlers.admin_caps import ADMIN_CAPS

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
