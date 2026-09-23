"""Phase 2 (APP-08) pure-helper tests for the pending reminder config."""
import asyncio

from config import config
from database import db
from services.reminders import _reminder_enabled, _reminder_interval, DEFAULT_INTERVAL
import services.reminders as reminders_mod


def test_enabled_default_on():
    assert _reminder_enabled(None) is True
    assert _reminder_enabled("on") is True


def test_enabled_off():
    assert _reminder_enabled("off") is False


def test_interval_valid():
    assert _reminder_interval("60") == 60


def test_interval_default_on_none():
    assert _reminder_interval(None) == DEFAULT_INTERVAL


def test_interval_default_on_garbage():
    assert _reminder_interval("abc") == DEFAULT_INTERVAL


def test_interval_default_on_nonpositive():
    assert _reminder_interval("0") == DEFAULT_INTERVAL
    assert _reminder_interval("-5") == DEFAULT_INTERVAL


# ── Квик 260923 (D-B): «🤖 Автоотказ с прошлой сводки» ─────────────────────────

ADMIN_ID = 926501
MSK_MANAGER_ID = 926502
DELEGATE_MSK = 926510


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reminders_autoreject.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    reminders_mod._last_summary_at = None  # изоляция от соседнего теста в том же воркере


def _seed_reject_rule(**overrides):
    fields = {
        "name": None, "city": None, "tracks": "[]", "conditions": "{}",
        "action": "reject", "reject_text": None, "enabled": 1, "created_by": None,
    }
    fields.update(overrides)
    return asyncio.run(db.create_reject_rule(**fields))


def _add_delegate(tid, city, *, status="rejected"):
    asyncio.run(db.add_user({
        "telegram_id": tid, "event_city": city, "full_name": f"Делегат {tid}",
        "registration_date": "2026-09-23 00:00:00",
    }))
    asyncio.run(db.set_user_status(tid, status))


def _log_auto_reject(tid, rule_id):
    from services.reject_journal import record_auto_reject
    return asyncio.run(record_auto_reject(tid, [rule_id], ["текст"]))


def test_text_for_recipient_appends_auto_reject_line_when_enabled(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_rules_enabled", "on"))
    rid = _seed_reject_rule(name="Младше 16")
    _add_delegate(DELEGATE_MSK, "msk")
    _log_auto_reject(DELEGATE_MSK, rid)

    text = asyncio.run(reminders_mod._text_for_recipient(ADMIN_ID, since="2000-01-01 00:00:00"))

    assert text is not None
    assert "🤖 Автоотказ с прошлой сводки: 1" in text


def test_text_for_recipient_zero_pending_and_one_auto_reject_sends_only_auto_line(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_rules_enabled", "on"))
    rid = _seed_reject_rule(name="Младше 16")
    _add_delegate(DELEGATE_MSK, "msk")
    _log_auto_reject(DELEGATE_MSK, rid)

    text = asyncio.run(reminders_mod._text_for_recipient(ADMIN_ID, since="2000-01-01 00:00:00"))

    assert text == "🤖 Автоотказ с прошлой сводки: 1"


def test_text_for_recipient_both_zero_returns_none(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_rules_enabled", "on"))
    assert asyncio.run(reminders_mod._text_for_recipient(ADMIN_ID)) is None


def test_text_for_recipient_module_off_is_byte_identical(tmp_path):
    """reject_rules_enabled=off -> текст прежний, автоотказ не считается вовсе."""
    _db_ready(tmp_path)
    rid = _seed_reject_rule(name="Младше 16")
    _add_delegate(DELEGATE_MSK, "msk")
    _log_auto_reject(DELEGATE_MSK, rid)
    _add_delegate(926511, "spb", status="pending")

    text = asyncio.run(reminders_mod._text_for_recipient(ADMIN_ID, since="2000-01-01 00:00:00"))

    assert "🤖 Автоотказ" not in text


def test_text_for_recipient_bound_manager_sees_own_city_auto_reject_count(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reject_rules_enabled", "on"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    rid = _seed_reject_rule(name="Младше 16")
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    _add_delegate(DELEGATE_MSK, "msk")
    _log_auto_reject(DELEGATE_MSK, rid)
    _add_delegate(926512, "spb")
    _log_auto_reject(926512, rid)

    text = asyncio.run(
        reminders_mod._text_for_recipient(MSK_MANAGER_ID, since="2000-01-01 00:00:00")
    )

    assert "🤖 Автоотказ с прошлой сводки: 1" in text  # только свой город, не оба


def test_pending_reminder_loop_initializes_watermark_on_first_call(tmp_path, monkeypatch):
    """Первый вход инициализирует `_last_summary_at` «сейчас минус интервал», не None."""
    _db_ready(tmp_path)

    class _StopLoop(Exception):
        pass

    async def fake_sleep(_seconds):
        raise _StopLoop()

    orig_sleep = reminders_mod.asyncio.sleep
    reminders_mod.asyncio.sleep = fake_sleep

    class _Bot:
        async def send_message(self, *a, **kw):
            pass

    assert reminders_mod._last_summary_at is None
    try:
        try:
            asyncio.run(reminders_mod.pending_reminder_loop(_Bot()))
        except _StopLoop:
            pass
    finally:
        reminders_mod.asyncio.sleep = orig_sleep

    assert reminders_mod._last_summary_at is not None
