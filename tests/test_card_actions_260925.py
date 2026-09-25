"""Phase 33 (delegate-card admin actions): три админ-действия на карточке `/find`, вне объёма
которую уже покрывает `tests/test_city_move_260925.py` (перевод в город) — «↩️ Вернуть в
ожидание» (Task 1), «🔁 Разрешить повторную подачу» (Task 2), «✏️ Открыть правку после решения»
(Task 3). Каждое действие — свой раздел файла (Part A/B/C ниже), тем же приёмом, что
test_city_move_260925.py: fake callback/message на слое хендлера + прямой вызов сервиса на
слое БД, права/подделанный callback_data на обоих слоях.

pytest-asyncio недоступна в этом окружении — `asyncio.run()` на каждый async-хелпер, БД —
шаблонная копия (`tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

import pytest

import cities
from config import config
from database import db
from handlers.admin_caps import role_caps_key
from services.checkin import checkin_denial
from services.revert_pending import preview_revert_pending, revert_to_pending
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260925201
BOUND_MSK_ID = 260925202
BOUND_SPB_ID = 260925203
DELEGATE_ID = 260925301
REFERRER_ID = 260925302

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


def _db_ready(tmp_path, name="test_card_actions_260925.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    # GOOGLE_SHEET_ID/CREDENTIALS_FILE остаются пустыми (тестовое окружение) — update_status_
    # in_sheet/schedule_sheet_logs_sync fail-soft выходят до сети (см. их докстринги).
    config.GOOGLE_SHEET_ID = ""
    config.GOOGLE_CREDENTIALS_FILE = ""


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


async def _seed_user(telegram_id, *, city="msk", status="approved", full_name="Тест Тестов",
                      referrer_id=None):
    await db.add_user({
        "telegram_id": telegram_id, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city, "referrer_id": referrer_id,
    })
    if status != "approved":  # add_user never touches status; schema default is 'approved'
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
            await conn.commit()


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


class _FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return object()


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.edits = []  # (text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id, bot=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.bot = bot or _FakeBot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part A: services/revert_pending.py — БД-слой Task 1
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_revert_approved_reverts_status_and_records_history(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        report = await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)
        return report

    report = _run(scenario())
    assert report["ok"] is True
    assert report["before_status"] == "approved"
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "pending"
    history = _run(db.get_answer_history(DELEGATE_ID))
    assert history and history[0]["source"] == f"admin:{SUPERADMIN_ID}"
    assert history[0]["changes"] == [{"column": "status", "old": "approved", "new": "pending"}]


def test_revert_rejected_reverts_status_too(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)

    report = _run(scenario())
    assert report["ok"] is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "pending"


def test_revert_refuses_when_already_pending(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="pending")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)

    report = _run(scenario())
    assert report["ok"] is False
    assert "не одобрена" in report["error"] or "не отклонена" in report["error"]
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "pending"  # не тронуто


def test_revert_refuses_unknown_user(tmp_path):
    _db_ready(tmp_path)
    report = _run(revert_to_pending(999999999, by_admin=SUPERADMIN_ID, notify=False))
    assert report["ok"] is False


def test_revert_status_race_reports_error_without_crashing(tmp_path, monkeypatch):
    """Гонка T-23-02: `revert_user_to_pending` находит статус уже изменённым — сервис
    сообщает отказ, не поднимает исключение и не пишет историю."""
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")

        async def _lost_race(telegram_id, from_status):
            return False

        monkeypatch.setattr("services.revert_pending.revert_user_to_pending", _lost_race)
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)

    report = _run(scenario())
    assert report["ok"] is False
    assert "параллельно" in report["error"]
    history = _run(db.get_answer_history(DELEGATE_ID))
    assert history == []


def test_revert_cancels_payment_reminders_only_when_was_approved(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.revert_pending.cancel_payment_reminders", lambda uid: calls.append(uid),
    )
    _db_ready(tmp_path)

    async def scenario_approved():
        await _seed_user(DELEGATE_ID, status="approved")
        await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)

    _run(scenario_approved())
    assert calls == [DELEGATE_ID]

    calls.clear()
    _db_ready(tmp_path, name="test_card_actions_260925_b.db")

    async def scenario_rejected():
        await _seed_user(DELEGATE_ID, status="rejected")
        await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)

    _run(scenario_rejected())
    assert calls == []  # отклонённого не трогаем — напоминаний об оплате у него и так не было


def test_revert_notify_false_sends_nothing_to_delegate(tmp_path):
    """`notify=False` управляет ТОЛЬКО сообщением делегату — уведомление менеджерам через
    `notify_application` (очередь/дайджест «как при обычной подаче») всё равно уходит, это
    отдельный канал (см. test_revert_routes_through_reg_digest_notify_application)."""
    _db_ready(tmp_path)
    bot = _FakeBot()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False, bot=bot)

    report = _run(scenario())
    assert report["notified"] is False
    assert DELEGATE_ID not in [chat_id for chat_id, _text, _kw in bot.sent]


def test_revert_notify_true_sends_delegate_message(tmp_path):
    _db_ready(tmp_path)
    bot = _FakeBot()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=True, bot=bot)

    report = _run(scenario())
    assert report["notified"] is True
    delegate_sends = [s for s in bot.sent if s[0] == DELEGATE_ID]
    assert len(delegate_sends) == 1


def test_revert_notify_send_failure_does_not_fail_the_action(tmp_path):
    """Сбой отправки делегату — не повод откатывать сам возврат в ожидание (fail-soft, тот же
    приём, что у остальных уведомителей проекта)."""
    _db_ready(tmp_path)

    class _BrokenBot(_FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("delegate blocked the bot")

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=True, bot=_BrokenBot())

    report = _run(scenario())
    assert report["ok"] is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "pending"


def test_revert_routes_through_reg_digest_notify_application(tmp_path, monkeypatch):
    """«Очередь модерации/дайджест — как при обычной подаче» (33-SEED): та же точка входа
    `notify_application`, `is_new=True`."""
    _db_ready(tmp_path)
    calls = []

    async def _fake_notify_application(bot, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("services.reg_digest.notify_application", _fake_notify_application)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", city="msk")
        return await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False, bot=_FakeBot())

    _run(scenario())
    assert len(calls) == 1
    assert calls[0]["is_new"] is True
    assert calls[0]["telegram_id"] == DELEGATE_ID
    assert calls[0]["auto_rejected"] is False


def test_preview_revert_shows_coin_balance_and_referrer(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(REFERRER_ID, status="approved", full_name="Пригласивший")
        await _seed_user(DELEGATE_ID, status="approved", referrer_id=REFERRER_ID)
        await db.add_coins(DELEGATE_ID, 30, reason="test", source="manual")
        await db.add_coins(REFERRER_ID, 50, reason="test", source="manual")
        return await preview_revert_pending(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["ok"] is True
    assert preview["coins_balance"] == 30
    assert preview["referrer"]["telegram_id"] == REFERRER_ID
    assert preview["referrer"]["coins_balance"] == 50
    assert preview["referrer"]["full_name"] == "Пригласивший"


def test_preview_revert_no_referrer_is_none(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved", referrer_id=None)
        return await preview_revert_pending(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["referrer"] is None


def test_preview_revert_refuses_when_pending(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="pending")
        return await preview_revert_pending(DELEGATE_ID)

    preview = _run(scenario())
    assert preview["ok"] is False


def test_revert_source_admin_prefix_does_not_break_history_screen(tmp_path):
    """Формат `source=f"admin:{admin_id}"` не сравнивается с `"admin"` нигде в проекте
    (grep-проверка была сделана при разработке) — экран «🕓 История» печатает незнакомый
    префикс как есть через `.get(source, source)`, не падает."""
    from services.applications import EDITED_SOURCE_LABELS

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)
        return await db.get_answer_history(DELEGATE_ID)

    history = _run(scenario())
    source = history[0]["source"]
    assert source == f"admin:{SUPERADMIN_ID}"
    # Тот же приём, что handlers/admin_moderation.py::appr_history — не бросает исключение.
    label = EDITED_SOURCE_LABELS.get(source, source)
    assert label == source


# ── QR-пропуск: регрессия «сканер проверяет статус вживую» ───────────────────────────────────

def test_checkin_denial_blocks_after_revert(tmp_path):
    """Task 1: «QR перестаёт пускать» — без отдельного кода, checkin_denial читает статус
    вживую (services/checkin.py::checkin_denial)."""
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        before = await checkin_denial(await db.get_user(DELEGATE_ID))
        await revert_to_pending(DELEGATE_ID, by_admin=SUPERADMIN_ID, notify=False)
        after = await checkin_denial(await db.get_user(DELEGATE_ID))
        return before, after

    before, after = _run(scenario())
    assert before is None  # одобрен — пропуск есть
    assert after == "not_approved"  # после возврата — пропуска больше нет


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part B: handlers/admin_revert_pending.py — UI-слой Task 1
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_revertp_start_shows_confirm_screen_for_approved(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_revert_pending.revertp_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Вернуть в ожидание" in text
    buttons = _cbs(kb)
    assert f"revertp_apply:{DELEGATE_ID}:0" in buttons
    assert f"revertp_toggle:{DELEGATE_ID}:1" in buttons
    assert f"revertp_cancel:{DELEGATE_ID}" in buttons


def test_revertp_start_refuses_when_pending(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="pending")
        cb = _FakeCallback(f"revertp_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_revert_pending.revertp_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_revertp_start_denied_when_city_out_of_scope(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="approved")
        cb = _FakeCallback(f"revertp_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await admin_revert_pending.revertp_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_revertp_toggle_flips_notify_button(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_toggle:{DELEGATE_ID}:1", SUPERADMIN_ID)
        await admin_revert_pending.revertp_toggle(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    button_labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("ВКЛ" in t for t in button_labels)
    buttons = _cbs(kb)
    assert f"revertp_apply:{DELEGATE_ID}:1" in buttons
    assert f"revertp_toggle:{DELEGATE_ID}:0" in buttons


def test_revertp_apply_uses_displayed_notify_state(tmp_path, monkeypatch):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)
    calls = []

    async def _fake_revert(*a, **kw):
        calls.append(kw)
        return {"ok": True, "sheet_updated": True, "notified": True}

    monkeypatch.setattr("handlers.admin_revert_pending.revert_to_pending", _fake_revert)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:1", SUPERADMIN_ID)
        await admin_revert_pending.revertp_apply(cb)
        return cb

    cb = _run(scenario())
    assert calls[0]["notify"] is True
    text, _ = cb.message.edits[0]
    assert "возвращён" in text


def test_revertp_apply_denies_forged_city_out_of_scope(tmp_path):
    """TOCTOU: право перепроверяется ЗАНОВО на шаге применения — тот же приём, что у
    admin_city_move.py."""
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="approved")
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", BOUND_MSK_ID)
        await admin_revert_pending.revertp_apply(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "approved"  # не тронуто


def test_revertp_apply_reports_error_without_crashing_on_race(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="pending")  # уже не approved/rejected
        cb = _FakeCallback(f"revertp_apply:{DELEGATE_ID}:0", SUPERADMIN_ID)
        await admin_revert_pending.revertp_apply(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "Не вернул" in text


def test_revertp_cancel_changes_nothing(tmp_path):
    from handlers import admin_revert_pending

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"revertp_cancel:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_revert_pending.revertp_cancel(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "отменён" in text
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "approved"


def test_revertp_capability_registered_for_every_callback():
    """T-08-12 (deny-by-default): каждый callback-префикс хендлера обязан быть в ADMIN_CAPS,
    иначе CapabilityMiddleware молча блокирует кнопку всем, включая суперадмина."""
    from handlers.admin_caps import ADMIN_CAPS

    for prefix in ("revertp_start:*", "revertp_toggle:*", "revertp_apply:*", "revertp_cancel:*"):
        assert ADMIN_CAPS.get(prefix) == "moderate_reg", prefix


def test_card_button_visible_only_for_approved_and_rejected():
    """Кнопка «↩️ Вернуть в ожидание» видна только когда есть что возвращать (services/
    revert_pending.py::REVERTIBLE_STATUSES) — та же проверка, что определяет видимость на
    карточке /find (handlers/admin.py::cmd_find_user)."""
    from services.revert_pending import REVERTIBLE_STATUSES

    assert REVERTIBLE_STATUSES == ("approved", "rejected")
    assert "pending" not in REVERTIBLE_STATUSES


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part D: services/delegate_overrides.py — общий примитив персональных исключений (задачи 2/3)
# ═══════════════════════════════════════════════════════════════════════════════════════════

from services import delegate_overrides  # noqa: E402


def test_grant_override_creates_active_row_and_records_history(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        result = await delegate_overrides.grant_override(
            DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID,
        )
        active = await delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)
        history = await db.get_answer_history(DELEGATE_ID)
        return result, active, history

    result, active, history = _run(scenario())
    assert result["ok"] is True
    assert active is not None
    assert active["granted_by"] == SUPERADMIN_ID
    assert history and history[0]["source"] == f"admin:{SUPERADMIN_ID}"
    assert history[0]["changes"] == [{"column": "resubmit_override", "old": None, "new": "granted"}]


def test_grant_override_refuses_duplicate_active(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        return await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)

    result = _run(scenario())
    assert result["ok"] is False


def test_grant_override_unknown_kind_refused(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        return await delegate_overrides.grant_override(DELEGATE_ID, "made_up_kind", SUPERADMIN_ID)

    result = _run(scenario())
    assert result["ok"] is False


def test_revoke_override_closes_active_row(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        result = await delegate_overrides.revoke_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        active = await delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)
        return result, active

    result, active = _run(scenario())
    assert result["ok"] is True
    assert active is None


def test_revoke_override_refuses_when_nothing_active(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        return await delegate_overrides.revoke_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)

    result = _run(scenario())
    assert result["ok"] is False


def test_consume_override_closes_active_row(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        consumed = await delegate_overrides.consume_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)
        active = await delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)
        return consumed, active

    consumed, active = _run(scenario())
    assert consumed is True
    assert active is None


def test_consume_override_without_active_is_harmless_noop(tmp_path):
    """Обычный делегат без исключения — consume_override гасить нечего, вызывающий код
    (services/reg_finalize.py) зовёт эту функцию БЕЗУСЛОВНО на каждой подаче/правке."""
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        return await delegate_overrides.consume_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)

    consumed = _run(scenario())
    assert consumed is False


def test_regrant_after_revoke_creates_new_row_history_keeps_both(tmp_path):
    """Append-only: повторная выдача после отзыва — новая строка, не перезапись старой; обе
    записи истории («выдал»/«отозвал»/«выдал снова») видны."""
    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        await delegate_overrides.revoke_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        result = await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        history = await db.get_answer_history(DELEGATE_ID, limit=10)
        return result, history

    result, history = _run(scenario())
    assert result["ok"] is True
    assert len(history) == 3


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part E: services/reg_edit_policy.py — гейт resubmit_gate уважает персональное исключение
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_resubmit_gate_denied_globally_but_allowed_via_override(tmp_path):
    from services import reg_edit_policy

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("reg_resubmit_after_reject", "deny")
        user = {"telegram_id": DELEGATE_ID, "status": "rejected", "event_city": None, "season": None}
        before = await reg_edit_policy.resubmit_gate(user)
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        after = await reg_edit_policy.resubmit_gate(user)
        return before, after

    before, after = _run(scenario())
    assert before[0] is False
    assert after == (True, None)


def test_resubmit_gate_peek_does_not_consume_override(tmp_path):
    """Гейт — peek, не consume: несколько проверок за один поход делегата не гасят
    исключение раньше времени (гашение — только в точке фактического использования,
    services/reg_finalize.py)."""
    from services import reg_edit_policy

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("reg_resubmit_after_reject", "deny")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        user = {"telegram_id": DELEGATE_ID, "status": "rejected", "event_city": None, "season": None}
        for _ in range(3):
            await reg_edit_policy.resubmit_gate(user)
        return await delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)

    active = _run(scenario())
    assert active is not None


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part F: services/reg_finalize.py — фактический резабмит гасит исключение
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_finalize_resubmit_consumes_override(tmp_path):
    from services import reg_finalize as rf

    _db_ready(tmp_path)

    async def scenario():
        await db.add_user({
            "telegram_id": DELEGATE_ID, "full_name": "Тест Тестов", "username": "@test",
            "registration_date": "2026-01-01", "event_city": None, "participant_type": "full",
        })
        await db.set_user_status(DELEGATE_ID, "rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        draft = {"telegram_id": DELEGATE_ID, "kind": "edit", "answers": {"phone": "+79997778899"}, "updated_by": "bot"}
        result = await rf.finalize_data(DELEGATE_ID, "@test", draft)
        active = await delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT)
        return result, active

    result, active = _run(scenario())
    assert result["resubmitted"] is True
    assert active is None  # погашено


def test_finalize_normal_edit_without_override_is_harmless(tmp_path):
    """Обычная правка без активного исключения — consume_override вызывается безусловно и
    не мешает обычному флоу (no-op)."""
    from services import reg_finalize as rf

    _db_ready(tmp_path)

    async def scenario():
        await db.add_user({
            "telegram_id": DELEGATE_ID, "full_name": "Тест Тестов", "username": "@test",
            "registration_date": "2026-01-01", "event_city": None, "participant_type": "full",
        })
        await db.set_user_status(DELEGATE_ID, "rejected")
        draft = {"telegram_id": DELEGATE_ID, "kind": "edit", "answers": {"phone": "+79997778899"}, "updated_by": "bot"}
        return await rf.finalize_data(DELEGATE_ID, "@test", draft)

    result = _run(scenario())
    assert result["resubmitted"] is True


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part G: handlers/admin_resubmit_grant.py — UI-слой Task 2
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_resubg_start_shows_confirm_screen_for_rejected(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        cb = _FakeCallback(f"resubg_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resubmit_grant.resubg_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Разрешить повторную подачу" in text
    buttons = _cbs(kb)
    assert f"resubg_apply:{DELEGATE_ID}:1" in buttons  # дефолт — сообщить (notify=True)
    assert f"resubg_cancel:{DELEGATE_ID}" in buttons


def test_resubg_start_refuses_when_not_rejected(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"resubg_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resubmit_grant.resubg_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_resubg_start_denied_when_city_out_of_scope(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="rejected")
        cb = _FakeCallback(f"resubg_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await admin_resubmit_grant.resubg_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_resubg_apply_grants_and_sends_default_notify(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)
    bot = _FakeBot()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        cb = _FakeCallback(f"resubg_apply:{DELEGATE_ID}:1", SUPERADMIN_ID, bot=bot)
        await admin_resubmit_grant.resubg_apply(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "выдано" in text
    active = _run(delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT))
    assert active is not None
    delegate_sends = [s for s in bot.sent if s[0] == DELEGATE_ID]
    assert len(delegate_sends) == 1


def test_resubg_apply_notify_off_sends_nothing_to_delegate(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)
    bot = _FakeBot()

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        cb = _FakeCallback(f"resubg_apply:{DELEGATE_ID}:0", SUPERADMIN_ID, bot=bot)
        await admin_resubmit_grant.resubg_apply(cb)
        return cb

    _run(scenario())
    assert bot.sent == []


def test_resubg_apply_refuses_forged_status(tmp_path):
    """Подделанный callback (uid другого статуса): резолв делегата по /find шёл, когда он
    был rejected, но к моменту тапа менеджер/делегат успели его сменить."""
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="approved")
        cb = _FakeCallback(f"resubg_apply:{DELEGATE_ID}:1", SUPERADMIN_ID)
        await admin_resubmit_grant.resubg_apply(cb)
        return cb

    cb = _run(scenario())
    assert cb.answers and cb.answers[0][1] is True
    active = _run(delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT))
    assert active is None


def test_resubg_apply_denies_forged_city_out_of_scope(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", status="rejected")
        cb = _FakeCallback(f"resubg_apply:{DELEGATE_ID}:0", BOUND_MSK_ID)
        await admin_resubmit_grant.resubg_apply(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_resubg_revoke_closes_active_override(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)
        cb = _FakeCallback(f"resubg_revoke:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resubmit_grant.resubg_revoke(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "отозвано" in text
    active = _run(delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT))
    assert active is None


def test_resubg_cancel_changes_nothing(tmp_path):
    from handlers import admin_resubmit_grant

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected")
        cb = _FakeCallback(f"resubg_cancel:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_resubmit_grant.resubg_cancel(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "не выдано" in text
    active = _run(delegate_overrides.active_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT))
    assert active is None


def test_resubg_capability_registered_for_every_callback():
    from handlers.admin_caps import ADMIN_CAPS

    for prefix in (
        "resubg_start:*", "resubg_toggle:*", "resubg_apply:*", "resubg_cancel:*", "resubg_revoke:*",
    ):
        assert ADMIN_CAPS.get(prefix) == "moderate_reg", prefix


def test_card_shows_resubmit_override_line_and_revoke_button(tmp_path):
    """Карточка /find показывает строку исключения + кнопку «отозвать» вместо кнопки выдачи,
    когда исключение уже активно (33-SEED: «видны в карточке /find строкой ... с кнопкой
    «отозвать»»)."""
    from handlers import admin

    _db_ready(tmp_path)

    async def scenario():
        await _seed_user(DELEGATE_ID, status="rejected", full_name="Отклонённый Делегат")
        await db.add_user({
            "telegram_id": SUPERADMIN_ID, "full_name": "Менеджер Менеджеров",
            "registration_date": "2026-01-01",
        })
        await delegate_overrides.grant_override(DELEGATE_ID, delegate_overrides.KIND_RESUBMIT, SUPERADMIN_ID)

        class _Msg:
            def __init__(self):
                self.answers = []

            async def answer(self, text, parse_mode=None, reply_markup=None):
                self.answers.append((text, reply_markup))

        class _M:
            def __init__(self):
                self.text = f"/find @{DELEGATE_ID}"

        msg = _M()
        msg.answer = _Msg().answer

        from database.db import get_user_by_username
        import handlers.admin as admin_mod

        async def _fake_get_user_by_username(username):
            return await db.get_user(DELEGATE_ID)

        orig = admin_mod.get_user_by_username
        admin_mod.get_user_by_username = _fake_get_user_by_username
        try:
            captured = {}

            async def _answer(text, parse_mode=None, reply_markup=None):
                captured["text"] = text
                captured["kb"] = reply_markup

            msg.answer = _answer
            await admin.cmd_find_user(msg)
        finally:
            admin_mod.get_user_by_username = orig
        return captured

    captured = _run(scenario())
    assert "Разрешена повторная подача" in captured["text"]
    assert "Менеджер Менеджеров" in captured["text"]
    buttons = _cbs(captured["kb"])
    assert f"resubg_revoke:{DELEGATE_ID}" in buttons
    assert f"resubg_start:{DELEGATE_ID}" not in buttons
