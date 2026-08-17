"""A-9: последняя, четвёртая точка TZ-кластера (`.planning/REVIEW-TRIAGE-260816.md` §6),
оставшаяся вне рамок `TZFIX-260816` (`quick/260816-1mo-tzfix`), потому что живёт в третьем
файле — `handlers/payment.py`, а не `services/scheduler.py` / `handlers/admin.py`.

Баг: `_schedule_deadline_reminders` (handlers/payment.py:178) брала `datetime.now()` — часы
контейнера (python:3.11-slim без ENV TZ -> UTC) — и сравнивала их с `payment_deadline`,
который менеджер вводит в МОСКОВСКИХ часах. В 3-часовом окне между UTC и МСК уже прошедшие
по Москве `minus3d`/`minus1d` проходили проверку `> now` как «будущие», джоба ставилась в
прошлое, `misfire_grace_time=86400` её не отбрасывал -> делегату немедленно прилетало
«до оплаты 3 дня».

Стиль и конвенции скопированы из tests/test_timezone_fix_260816.py:
- pytest-asyncio в окружении нет -> каждый async-вызов гоняется через asyncio.run().
- config.DB_PATH указывает на tmp_path-файл, БД поднимается через asyncio.run(db.init_db()).
- Импорт в правимой функции ЛОКАЛЬНЫЙ (внутри _schedule_deadline_reminders), поэтому
  патчатся атрибуты МОДУЛЯ services.scheduler (sched_mod._now_moscow_naive /
  sched_mod.schedule_payment_reminder) — не атрибуты handlers.payment, там их просто нет.
"""
import asyncio
import contextlib
import datetime as datetime_module
import inspect
import logging
from datetime import datetime

from config import config
from database import db
from database.db import add_user, set_setting

import services.scheduler as sched_mod
import handlers.payment as pay

TELEGRAM_ID = 260817001


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_payment_tz_260817.db")
    asyncio.run(db.init_db())


def _seed_user(telegram_id: int = TELEGRAM_ID):
    asyncio.run(add_user({
        "telegram_id": telegram_id,
        "full_name": "TZ A-9",
        "registration_date": "x",
    }))
    asyncio.run(set_setting("payment_deadline", "01.07.2026 15:00"))


@contextlib.contextmanager
def _patch_container_clock(fixed_now: datetime):
    """Deterministically freeze the value the UNFIXED code reads.

    `_schedule_deadline_reminders` does `from datetime import datetime, timedelta` as a
    LOCAL import inside the function body, then calls bare `datetime.now()` — the real
    container/system clock. Patching `sched_mod._now_moscow_naive` (as the three points
    fixed in TZFIX-260816 required) has ZERO effect on this call, because the buggy code
    never references that helper at all. The only way to deterministically reproduce the
    UTC/MSK window bug — independent of the host machine's real system clock/timezone and
    of "today"'s date drifting past the scenario's fixed dates — is to swap the `datetime`
    class the local import resolves, exactly like `freezegun` does (not used here to avoid
    a new dependency for a two-test file).
    """
    real_datetime = datetime_module.datetime

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    datetime_module.datetime = _FixedDatetime
    try:
        yield
    finally:
        datetime_module.datetime = real_datetime


# Оракул: контейнер (UTC) «видит» 30.06.2026 13:00, что по Москве — 30.06.2026 16:00 (3-часовое
# смещение). Дедлайн 01.07.2026 15:00 -> minus1d = 30.06.2026 15:00: по МСК уже прошло (15:00 <
# 16:00), но по часам контейнера ещё не наступило (15:00 > 13:00) — ровно то окно, в котором
# жил баг. `_now_moscow_naive` патчится синхронно на корректное МСК-время: до фикса код его не
# читает вовсе (эффекта нет, это и есть баг), после фикса — читает и получает правильный ответ.

def test_deadline_reminders_not_scheduled_in_utc_msk_window(tmp_path, caplog):
    _db_ready(tmp_path)
    _seed_user()

    calls = []
    orig_now_moscow = sched_mod._now_moscow_naive
    orig_schedule = sched_mod.schedule_payment_reminder
    sched_mod._now_moscow_naive = lambda: datetime(2026, 6, 30, 16, 0)
    sched_mod.schedule_payment_reminder = lambda uid, run_at, label: calls.append((run_at, label))
    try:
        with _patch_container_clock(datetime(2026, 6, 30, 13, 0)):
            with caplog.at_level(logging.ERROR, logger="handlers.payment"):
                asyncio.run(pay._schedule_deadline_reminders(TELEGRAM_ID))
        assert calls == [], (
            "ни minus3d (28.06, прошло в обеих системах отсчёта), ни minus1d "
            "(30.06 15:00, прошло только по МСК) не должны планироваться"
        )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], (
            f"пустой calls должен объясняться временем, а не проглоченным исключением: {errors}"
        )
    finally:
        sched_mod._now_moscow_naive = orig_now_moscow
        sched_mod.schedule_payment_reminder = orig_schedule


def test_deadline_reminders_scheduled_when_both_in_future(tmp_path):
    """Позитивный контроль: доказывает, что тест выше не проходит вхолостую — с тем же
    дедлайном оба напоминания в будущем ставятся и по часам контейнера, и по МСК."""
    _db_ready(tmp_path)
    _seed_user()

    calls = []
    orig_now_moscow = sched_mod._now_moscow_naive
    orig_schedule = sched_mod.schedule_payment_reminder
    sched_mod._now_moscow_naive = lambda: datetime(2026, 6, 1, 10, 0)
    sched_mod.schedule_payment_reminder = lambda uid, run_at, label: calls.append((run_at, label))
    try:
        with _patch_container_clock(datetime(2026, 6, 1, 7, 0)):
            asyncio.run(pay._schedule_deadline_reminders(TELEGRAM_ID))
        assert calls == [
            (datetime(2026, 6, 28, 15, 0), "minus3d"),
            (datetime(2026, 6, 30, 15, 0), "minus1d"),
        ]
    finally:
        sched_mod._now_moscow_naive = orig_now_moscow
        sched_mod.schedule_payment_reminder = orig_schedule


def test_schedule_deadline_reminders_uses_moscow_helper_not_bare_now():
    """Структурный якорь: тело функции не содержит `datetime.now()` (за вычетом комментариев)
    и содержит `_now_moscow_naive` — комментарий про «часы контейнера» не должен сам себя
    валидировать, отсюда фильтрация комментариев."""
    source = inspect.getsource(pay._schedule_deadline_reminders)
    code_lines = [
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert "datetime.now()" not in code_only
    assert "_now_moscow_naive" in code_only
