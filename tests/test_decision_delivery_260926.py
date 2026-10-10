"""Координатор 25.09 — учёт доставки решения по заявке (память auto-approve-incident-260906:
38 заявок одобрены молча без письма делегату — ровно то, что эта задача должна ловить).

Покрывает: `database.db.record_decision_delivery`/миграцию колонок, запись успеха/каждой
причины сбоя в `services.application_effects.apply_decision_effects`/`mass_approve_effects`,
до-миграционные записи = «неизвестно» (не «не доставлено»), последнее решение (возврат на
модерацию сбрасывает учёт), классификацию причины (`_classify_decision_delivery_error`),
раскладку `services.decision_delivery.summarize_deliveries` и «📨 Переотправить решения»
(`resend_undelivered_decisions` — пересчёт, пропуск заблокировавших/уже доставленных,
RetryAfter, двойной тап, город админа), fail-soft самого учёта.

pytest-asyncio недоступна — `asyncio.run()` на каждый async-сценарий, БД — шаблонная копия
(`tests/_dbtpl.py::fast_init_db`), тот же приём, что у соседних тестов Phase 33."""
from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import services.application_effects as application_effects
import services.decision_delivery as decision_delivery
from config import config
from database import db
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260926701


def _run(coro):
    return asyncio.run(coro)


def _db_ready(tmp_path, name="test_decision_delivery_260926.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _seed_user(tid, *, status="pending", city=None, full_name="Тест Тестов", username="test"):
    await db.add_user({
        "telegram_id": tid, "full_name": full_name, "username": username,
        "registration_date": "2026-01-01 00:00:00", "event_city": city,
    })
    await db.set_user_status(tid, status)


class _FakeBot:
    """`send_message` — по чат-id настраиваемое поведение: успех (по умолчанию), заданное
    исключение, или счётчик попыток (для RetryAfter — второй вызов уже без сбоя)."""

    def __init__(self, *, raise_for: dict[int, Exception] | None = None,
                 retry_after_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self._raise_for = dict(raise_for or {})
        self._retry_after_for = set(retry_after_for or set())
        self._retry_attempts: dict[int, int] = {}

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self._retry_after_for:
            attempt = self._retry_attempts.get(chat_id, 0) + 1
            self._retry_attempts[chat_id] = attempt
            if attempt == 1:
                raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        exc = self._raise_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Миграция + запись напрямую
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_migration_adds_decision_delivery_columns(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        async with db._connect() as conn:
            cur = await conn.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in await cur.fetchall()}
        return cols

    cols = _run(scenario())
    for col in ("decision_delivery_status", "decision_delivery_decision",
                "decision_delivery_at", "decision_delivery_error"):
        assert col in cols


def test_record_decision_delivery_writes_all_columns(tmp_path):
    _db_ready(tmp_path)
    _run(_seed_user(1, status="approved"))
    _run(db.record_decision_delivery(1, "approved", "delivered", None))

    user = _run(db.get_user(1))
    assert user["decision_delivery_status"] == "delivered"
    assert user["decision_delivery_decision"] == "approved"
    assert user["decision_delivery_error"] is None
    assert user["decision_delivery_at"]


def test_pre_migration_decision_is_unknown_not_undelivered(tmp_path):
    """Решение принято (status='rejected'), но `decision_delivery_*` НИКОГДА не писались —
    ровно то, что видит база сразу после миграции на проде. Категория — «неизвестно», НЕ
    «не доставлено»."""
    _db_ready(tmp_path)
    _run(_seed_user(2, status="rejected"))

    user = _run(db.get_user(2))
    summary = decision_delivery.summarize_deliveries([user])
    assert summary["unknown"] and summary["unknown"][0]["tid"] == 2
    assert summary["failed"] == []


def test_revert_to_pending_resets_delivery_record(tmp_path):
    """Возврат на модерацию — решение, к которому относилась запись доставки, отменено:
    следующее решение (не это) должно её описывать, а не унаследованный статус."""
    _db_ready(tmp_path)
    _run(_seed_user(3, status="approved"))
    _run(db.record_decision_delivery(3, "approved", "delivered"))

    reverted = _run(db.revert_user_to_pending(3, "approved"))
    assert reverted is True

    user = _run(db.get_user(3))
    assert user["status"] == "pending"
    assert user["decision_delivery_status"] is None
    assert user["decision_delivery_decision"] is None
    assert user["decision_delivery_error"] is None


def test_last_decision_overwrites_previous_delivery_record(tmp_path):
    """Одобрение -> возврат в ожидание -> отказ: учёт доставки должен описывать ПОСЛЕДНЕЕ
    решение (отказ), не подмешивать старое одобрение."""
    _db_ready(tmp_path)
    _run(_seed_user(4, status="pending"))
    _run(db.set_user_status(4, "approved"))
    _run(db.record_decision_delivery(4, "approved", "delivered"))
    _run(db.revert_user_to_pending(4, "approved"))
    _run(db.set_user_status(4, "rejected"))
    _run(db.record_decision_delivery(4, "rejected", "failed", "чат не найден"))

    user = _run(db.get_user(4))
    assert user["decision_delivery_decision"] == "rejected"
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_error"] == "чат не найден"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# apply_decision_effects — запись успеха/каждой причины сбоя
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_apply_decision_effects_approved_success_records_delivered(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(101, status="approved"))

    async def fake_approve_user(bot, tid):
        return None  # успех

    import handlers.reg.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    _run(application_effects.apply_decision_effects(_FakeBot(), 101, "approved"))
    user = _run(db.get_user(101))
    assert user["decision_delivery_status"] == "delivered"
    assert user["decision_delivery_decision"] == "approved"
    assert user["decision_delivery_error"] is None


def test_apply_decision_effects_approved_blocked_records_bot_blocked_label(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(102, status="approved"))
    exc = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")

    async def fake_approve_user(bot, tid):
        return exc

    import handlers.reg.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    _run(application_effects.apply_decision_effects(_FakeBot(), 102, "approved"))
    user = _run(db.get_user(102))
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_error"] == "бот заблокирован делегатом"


def test_apply_decision_effects_approved_deactivated_records_deactivated_label(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(103, status="approved"))
    exc = TelegramForbiddenError(method=None, message="Forbidden: user is deactivated")

    async def fake_approve_user(bot, tid):
        return exc

    import handlers.reg.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    _run(application_effects.apply_decision_effects(_FakeBot(), 103, "approved"))
    user = _run(db.get_user(103))
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_error"] == "пользователь удалён"


def test_apply_decision_effects_rejected_chat_not_found_records_label(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(104, status="rejected"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    exc = TelegramBadRequest(method=None, message="Bad Request: chat not found")
    bot = _FakeBot(raise_for={104: exc})
    _run(application_effects.apply_decision_effects(bot, 104, "rejected", "не подошёл трек"))

    user = _run(db.get_user(104))
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_decision"] == "rejected"
    assert user["decision_delivery_error"] == "чат не найден"


def test_apply_decision_effects_rejected_other_error_records_generic_label(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(105, status="rejected"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    exc = RuntimeError("сеть моргнула")
    bot = _FakeBot(raise_for={105: exc})
    _run(application_effects.apply_decision_effects(bot, 105, "rejected", None))

    user = _run(db.get_user(105))
    assert user["decision_delivery_status"] == "failed"
    assert user["decision_delivery_error"] == "ошибка отправки: сеть моргнула"


def test_apply_decision_effects_rejected_success_records_delivered(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(106, status="rejected"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    bot = _FakeBot()
    _run(application_effects.apply_decision_effects(bot, 106, "rejected", "причина"))

    user = _run(db.get_user(106))
    assert user["decision_delivery_status"] == "delivered"
    assert bot.sent and bot.sent[0][0] == 106


def test_apply_decision_effects_notify_false_does_not_touch_delivery(tmp_path, monkeypatch):
    """`notify=False` — эффект без попытки отправки (например, узкий пересчёт листа) — учёт
    НЕ трогается вовсе: писать «не доставлено» о письме, которое не пытались слать, ложь."""
    _db_ready(tmp_path)
    _run(_seed_user(107, status="approved"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    _run(application_effects.apply_decision_effects(_FakeBot(), 107, "approved", notify=False))
    user = _run(db.get_user(107))
    assert user["decision_delivery_status"] is None


def test_apply_decision_effects_quiet_hours_records_queued(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(108, status="approved"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    from services import quiet_hours

    async def fake_defer_until(now, uid):
        from datetime import timedelta
        return now + timedelta(hours=1)

    enqueued = []

    async def fake_enqueue(uid, kind, payload, due, now):
        enqueued.append((uid, kind, payload))
        return 1

    monkeypatch.setattr(quiet_hours, "defer_until", fake_defer_until)
    monkeypatch.setattr(quiet_hours, "enqueue", fake_enqueue)

    _run(application_effects.apply_decision_effects(_FakeBot(), 108, "approved"))
    user = _run(db.get_user(108))
    assert user["decision_delivery_status"] == "queued"
    assert enqueued  # решение реально ушло в очередь, не потеряно


def test_apply_decision_effects_retry_after_then_success_records_delivered(tmp_path, monkeypatch):
    """429 на первой попытке, успех на повторе (services.infra.telegram_send.send_with_retry) —
    итог всё равно «доставлено», не «не доставлено»."""
    _db_ready(tmp_path)
    _run(_seed_user(109, status="rejected"))

    async def fake_update_status_in_sheet(tid, label):
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    bot = _FakeBot(retry_after_for={109})
    _run(application_effects.apply_decision_effects(bot, 109, "rejected", None))

    user = _run(db.get_user(109))
    assert user["decision_delivery_status"] == "delivered"
    assert len(bot.sent) == 1  # ретрай не задвоил письмо


def test_record_delivery_fail_soft_does_not_break_decision(tmp_path, monkeypatch):
    """Сбой самой ЗАПИСИ учёта (`record_decision_delivery` роняется) не должен ломать решение —
    `update_status_in_sheet` всё равно вызывается, функция не поднимает исключение наружу."""
    _db_ready(tmp_path)
    _run(_seed_user(110, status="approved"))

    async def fake_approve_user(bot, tid):
        return None

    import handlers.reg.reg_schema as reg_schema
    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)

    sheet_calls = []

    async def fake_update_status_in_sheet(tid, label):
        sheet_calls.append(tid)
        return True

    monkeypatch.setattr(application_effects, "update_status_in_sheet", fake_update_status_in_sheet)

    async def raising_record(*args, **kwargs):
        raise RuntimeError("БД недоступна (тест)")

    monkeypatch.setattr(db, "record_decision_delivery", raising_record)

    # Не должно поднять исключение наружу.
    _run(application_effects.apply_decision_effects(_FakeBot(), 110, "approved"))
    assert sheet_calls == [110]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# mass_approve_effects
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_mass_approve_effects_records_delivered_and_failed(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(201, status="approved"))
    _run(_seed_user(202, status="approved"))

    async def fake_bulk_update(payload):
        return None

    monkeypatch.setattr(application_effects, "bulk_update_status_in_sheet", fake_bulk_update)

    exc = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
    bot = _FakeBot()

    import handlers.reg.reg_schema as reg_schema
    real_approve_user = reg_schema.approve_user

    async def fake_approve_user(b, tid):
        if tid == 202:
            return exc
        return None

    monkeypatch.setattr(reg_schema, "approve_user", fake_approve_user)

    _run(application_effects.mass_approve_effects(bot, [201, 202]))

    u201 = _run(db.get_user(201))
    u202 = _run(db.get_user(202))
    assert u201["decision_delivery_status"] == "delivered"
    assert u202["decision_delivery_status"] == "failed"
    assert u202["decision_delivery_error"] == "бот заблокирован делегатом"
    assert real_approve_user  # sanity: сохранили ссылку, не потеряли оригинал по ошибке


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Классификация причины — чистый unit-тест
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_classify_decision_delivery_error_labels():
    classify = application_effects._classify_decision_delivery_error
    assert classify(
        TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
    ) == "бот заблокирован делегатом"
    assert classify(
        TelegramForbiddenError(method=None, message="Forbidden: user is deactivated")
    ) == "пользователь удалён"
    assert classify(
        TelegramBadRequest(method=None, message="Bad Request: chat not found")
    ) == "чат не найден"
    assert classify(RuntimeError("что-то своё")) == "ошибка отправки: что-то своё"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# summarize_deliveries — чистая раскладка
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_summarize_deliveries_categories():
    users = [
        {"telegram_id": 1, "status": "approved", "decision_delivery_status": "failed",
         "decision_delivery_error": "бот заблокирован делегатом", "full_name": "A", "username": "a",
         "event_city": "msk"},
        {"telegram_id": 2, "status": "rejected", "decision_delivery_status": "failed",
         "decision_delivery_error": "чат не найден", "full_name": "B", "username": "b",
         "event_city": "msk"},
        {"telegram_id": 3, "status": "approved", "decision_delivery_status": "delivered",
         "decision_delivery_error": None, "full_name": "C", "username": "c", "event_city": "msk"},
        {"telegram_id": 4, "status": "rejected", "decision_delivery_status": "queued",
         "decision_delivery_error": None, "full_name": "D", "username": "d", "event_city": "msk"},
        {"telegram_id": 5, "status": "approved", "decision_delivery_status": None,
         "decision_delivery_error": None, "full_name": "E", "username": "e", "event_city": "msk"},
        {"telegram_id": 6, "status": "pending", "decision_delivery_status": None,
         "decision_delivery_error": None, "full_name": "F", "username": "f", "event_city": "msk"},
    ]
    summary = decision_delivery.summarize_deliveries(users)
    assert {it["tid"] for it in summary["failed"]} == {1, 2}
    assert {it["tid"] for it in summary["blocked"]} == {1}
    assert {it["tid"] for it in summary["resendable"]} == {2}
    assert {it["tid"] for it in summary["queued"]} == {4}
    assert {it["tid"] for it in summary["unknown"]} == {5}
    # pending (tid=6) — решения не было вовсе, ни в одну категорию не попадает.
    all_tids = {it["tid"] for cat in summary.values() for it in cat}
    assert 6 not in all_tids


# ═══════════════════════════════════════════════════════════════════════════════════════════
# resend_undelivered_decisions — переотправка
# ═══════════════════════════════════════════════════════════════════════════════════════════

async def _prep_resend_fixture(*, city=None):
    await _seed_user(301, status="approved", city=city, full_name="Заблокировал", username="blocked")
    await db.record_decision_delivery(301, "approved", "failed", "бот заблокирован делегатом")
    await _seed_user(302, status="rejected", city=city, full_name="НеДошло", username="notfound")
    await db.record_decision_delivery(302, "rejected", "failed", "чат не найден")


async def _fake_update_status_in_sheet(tid, label):
    return True


def test_resend_skips_blocked_and_sends_only_resendable(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_prep_resend_fixture())

    async def fake_last_rejection_reason(tid):
        return "не подошёл трек"

    import services.applications as applications_mod
    monkeypatch.setattr(applications_mod, "last_rejection_reason", fake_last_rejection_reason)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", _fake_update_status_in_sheet)

    bot = _FakeBot()
    result = _run(decision_delivery.resend_undelivered_decisions(bot))

    assert result["ok"] is True
    assert result["done"] == 1
    assert result["total"] == 1  # 301 (заблокирован) не входит в targets вовсе
    assert len(result["blocked"]) == 1 and result["blocked"][0]["tid"] == 301
    sent_ids = {cid for cid, _text in bot.sent}
    assert sent_ids == {302}

    u301 = _run(db.get_user(301))
    u302 = _run(db.get_user(302))
    assert u301["decision_delivery_status"] == "failed"  # не тронут — не шлём заблокированным
    assert u302["decision_delivery_status"] == "delivered"


def test_resend_skips_already_delivered(tmp_path, monkeypatch):
    """Пересчёт СВЕЖИЙ: если статус доставки успел смениться на 'delivered' между открытием
    отчёта и тапом «Да» — второе письмо НЕ уходит (уже не входит в targets)."""
    _db_ready(tmp_path)
    _run(_seed_user(303, status="rejected", full_name="Уже доставлено", username="ok"))
    _run(db.record_decision_delivery(303, "rejected", "delivered"))

    bot = _FakeBot()
    result = _run(decision_delivery.resend_undelivered_decisions(bot))
    assert result["ok"] is True
    assert result["total"] == 0
    assert bot.sent == []


def test_resend_recomputes_before_sending(tmp_path, monkeypatch):
    """Между показом экрана подтверждения и тапом «Да» статус мог смениться (тест эмулирует
    ситуацию: недоставленный делегат УСПЕЛ получить delivered от параллельного пути ДО вызова
    resend — resend должен увидеть это и не отправить второе письмо)."""
    _db_ready(tmp_path)
    _run(_seed_user(304, status="rejected", full_name="Гонка", username="race"))
    _run(db.record_decision_delivery(304, "rejected", "failed", "чат не найден"))

    # Между открытием отчёта (снаружи теста) и вызовом resend кто-то параллельно доставил.
    _run(db.record_decision_delivery(304, "rejected", "delivered"))

    bot = _FakeBot()
    result = _run(decision_delivery.resend_undelivered_decisions(bot))
    assert result["total"] == 0
    assert bot.sent == []


def test_resend_double_tap_returns_busy(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _run(_seed_user(305, status="rejected", full_name="Т", username="t"))
    _run(db.record_decision_delivery(305, "rejected", "failed", "чат не найден"))

    bot = _FakeBot()

    async def scenario():
        first, second = await asyncio.gather(
            decision_delivery.resend_undelivered_decisions(bot),
            decision_delivery.resend_undelivered_decisions(bot),
        )
        return first, second

    first, second = _run(scenario())
    oks = [r for r in (first, second) if r["ok"]]
    refused = [r for r in (first, second) if not r["ok"]]
    assert len(refused) == 1
    assert "уже выполняется" in refused[0]["error"]
    assert len(oks) == 1


def test_resend_approved_skips_payment_step_when_payment_enabled(tmp_path, monkeypatch):
    """Координатор 25.09: переотправка одобрения при `payment_enabled=on` шлёт ТОЛЬКО текст
    решения — `handlers.payment.start_payment_step` (единственное место, которое пишет FSM
    делегата в пути одобрения) не вызывается вовсе, а не «вызывается и откатывается»."""
    _db_ready(tmp_path)
    _run(db.set_setting("payment_enabled", "on"))
    _run(_seed_user(308, status="approved", full_name="Одобрен", username="apprvd"))
    _run(db.record_decision_delivery(308, "approved", "failed", "чат не найден"))

    import handlers.payment as payment_mod
    payment_calls = []

    async def fake_start_payment_step(bot, telegram_id, participant_type="full"):
        payment_calls.append(telegram_id)
        return None

    monkeypatch.setattr(payment_mod, "start_payment_step", fake_start_payment_step)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", _fake_update_status_in_sheet)

    bot = _FakeBot()
    result = _run(decision_delivery.resend_undelivered_decisions(bot))

    assert result["ok"] is True
    assert result["done"] == 1
    assert payment_calls == []  # шаг оплаты не открывался
    sent_ids = {cid for cid, _text in bot.sent}
    assert sent_ids == {308}  # текст решения ушёл напрямую

    user = _run(db.get_user(308))
    assert user["decision_delivery_status"] == "delivered"
    assert user["decision_delivery_decision"] == "approved"


def test_normal_decision_still_opens_payment_step_when_payment_enabled(tmp_path, monkeypatch):
    """Контроль: обычное решение модератора (не переотправка) при `payment_enabled=on`
    по-прежнему уходит в шаг оплаты — эта задача меняет только путь переотправки."""
    _db_ready(tmp_path)
    _run(db.set_setting("payment_enabled", "on"))
    _run(_seed_user(309, status="approved", full_name="Одобрен2", username="apprvd2"))

    import handlers.payment as payment_mod
    payment_calls = []

    async def fake_start_payment_step(bot, telegram_id, participant_type="full"):
        payment_calls.append(telegram_id)
        return None

    monkeypatch.setattr(payment_mod, "start_payment_step", fake_start_payment_step)
    monkeypatch.setattr(application_effects, "update_status_in_sheet", _fake_update_status_in_sheet)

    bot = _FakeBot()
    _run(application_effects.apply_decision_effects(bot, 309, "approved"))

    assert payment_calls == [309]  # обычное решение по-прежнему открывает шаг оплаты
    user = _run(db.get_user(309))
    assert user["decision_delivery_status"] == "delivered"


def test_resend_city_scope_limits_to_admin_city(tmp_path, monkeypatch):
    """Город админа учитывается — переотправка в скоупе города не трогает недоставленные
    решения ДРУГОГО города."""
    _db_ready(tmp_path)
    _run(_seed_user(306, status="rejected", city="spb", full_name="СПб", username="spb1"))
    _run(db.record_decision_delivery(306, "rejected", "failed", "чат не найден"))
    _run(_seed_user(307, status="rejected", city="msk", full_name="Мск", username="msk1"))
    _run(db.record_decision_delivery(307, "rejected", "failed", "чат не найден"))

    bot = _FakeBot()
    result = _run(decision_delivery.resend_undelivered_decisions(bot, city_scope=("spb", ())))

    assert result["total"] == 1
    sent_ids = {cid for cid, _t in bot.sent}
    assert sent_ids == {306}
