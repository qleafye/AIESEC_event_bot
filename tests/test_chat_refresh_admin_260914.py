"""Квик 260914-rgr (RGR-01..07), задача 2: периодическая сверка состава.

Правка 15.09 (владелец, «привязка через личку админа»): экран «💬 Чат» снесён целиком
(`handlers/admin_chat.py` удалён) — тесты того экрана ниже удалены вместе с ним. Сверка
состава (`chat_tracking.refresh_chat`/`refresh_all_chats`) и джоба планировщика — не
затронуты: манула «🔄 Сверить сейчас» больше нет, но плановая джоба и её тумблер остаются.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
import logging
import pickle
from datetime import datetime, timedelta
from types import SimpleNamespace

from config import config
from database import db
from services import chat_tracking
from services import scheduler as sched

ADMIN_ID = 900801
STRANGER_ID = 900802
CHAT_ID = -1009988877766


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "chat_refresh.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


async def _seed_approved(n, start=800000, city=None):
    ids = []
    async with db._connect() as conn:
        for i in range(n):
            tid = start + i
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, event_city) "
                "VALUES (?, ?, 'approved', ?)",
                (tid, f"Делегат {i}", city),
            )
            ids.append(tid)
        await conn.commit()
    return ids


class FakeBot:
    def __init__(self, fail_ids=(), status_for=None, error_text_for=None):
        self.calls: list[tuple] = []
        self.sent: list[tuple] = []
        self.fail_ids = set(fail_ids)
        self.status_for = status_for or {}
        # Квик 260915-twr (D1): per-id текст исключения — чтобы сымитировать реальный ответ
        # Telegram (PARTICIPANT_ID_INVALID/USER_ID_INVALID) отдельно от generic-сбоя ("boom").
        self.error_text_for = error_text_for or {}

    async def get_chat_member(self, chat_id, telegram_id):
        self.calls.append((chat_id, telegram_id))
        if telegram_id in self.error_text_for:
            raise RuntimeError(self.error_text_for[telegram_id])
        if telegram_id in self.fail_ids:
            raise RuntimeError("boom")
        status = self.status_for.get(telegram_id, "member")
        return SimpleNamespace(status=status)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append((chat_id, text))


class _FakeAPScheduler:
    """Голая замена APScheduler для юнит-теста `_add_interval_job`/`schedule_bind_reconcile` —
    ничего не пиклит и не хранит на диске, только фиксирует, с чем был позван `add_job`."""

    def __init__(self):
        self.added: list[tuple] = []

    def get_job(self, job_id):
        return None

    def add_job(self, func, trigger, **kwargs):
        self.added.append((func, trigger, kwargs))


# ── refresh_chat: батчи/пауза/ошибки/потолок ─────────────────────────────────────────────

def test_refresh_chat_pauses_between_batches(tmp_path, monkeypatch):
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(chat_tracking.REFRESH_BATCH + 5))
    bot = FakeBot()
    sleep_calls: list = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(chat_tracking.asyncio, "sleep", _fake_sleep)

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    assert report["checked"] == len(ids)
    assert len(sleep_calls) == 1  # два батча -> ровно одна пауза между ними
    assert sleep_calls[0] == chat_tracking.REFRESH_PAUSE_SECONDS


def test_refresh_chat_error_on_one_delegate_does_not_break_run(tmp_path):
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(3))
    bot = FakeBot(fail_ids={ids[1]})

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    assert report["checked"] == 3
    assert report["errors"] == 1
    assert report["present"] == 2


def test_refresh_chat_truncates_at_max_calls(tmp_path):
    _ready(tmp_path)
    asyncio.run(_seed_approved(10))
    bot = FakeBot()

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None, max_calls=4))

    assert report["truncated"] is True
    assert report["checked"] == 4


def test_refresh_all_chats_noop_when_tracking_off(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))
    asyncio.run(_seed_approved(5))
    bot = FakeBot()

    reports = asyncio.run(chat_tracking.refresh_all_chats(bot))

    assert reports == []
    assert bot.calls == []


def test_chat_membership_refresh_job_silent_when_disabled(tmp_path):
    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))
    asyncio.run(_seed_approved(3))
    bot = FakeBot()
    sched._bot = bot

    asyncio.run(sched.chat_membership_refresh_job())

    assert bot.calls == []


# ── Правка 15.09: тумблер учёта переехал в общий раздел «🔧 Управление» ──────────────────

def test_chat_tracking_toggle_is_registered_in_admin_caps():
    """Экран «💬 Чат» снесён целиком (`handlers/admin_chat.py` удалён) — единственный
    оставшийся вход в тумблер учёта регистрируется на `admin.router` под общей капой
    `settings`, как и любой другой тумблер раздела «🔧 Управление»."""
    from handlers.admin_caps import ADMIN_CAPS

    assert ADMIN_CAPS["toggle_chat_tracking_enabled"] == "settings"
    for stale in ("admin_chat", "chat_chat_tracking_toggle", "chat_refresh_now",
                  "chat_unbind:*", "chat_unbind_go:*", "chat_broadcast_out:*"):
        assert stale not in ADMIN_CAPS


# ── Квик 260915-twr (D1): PARTICIPANT_ID_INVALID/USER_ID_INVALID — ответ, а не сбой ──────

def test_refresh_chat_participant_id_invalid_marks_absent_not_error(tmp_path):
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(1))
    tid = ids[0]
    bot = FakeBot(error_text_for={tid: "Telegram server says - Bad Request: PARTICIPANT_ID_INVALID"})

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    assert report["errors"] == 0
    assert report["not_found"] == 1
    row = asyncio.run(db.chat_member_row(CHAT_ID, tid))
    assert row is not None
    assert row["status"] not in db.CHAT_PRESENT_STATUSES


def test_refresh_chat_user_id_invalid_marks_absent_not_error(tmp_path):
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(1))
    tid = ids[0]
    bot = FakeBot(error_text_for={tid: "USER_ID_INVALID"})

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    assert report["errors"] == 0
    assert report["not_found"] == 1


def test_refresh_chat_absent_delegate_drops_out_of_stale_candidates_next_run(tmp_path):
    """441 таких делегатов на проде перепроверялись каждые 6 часов впустую именно потому,
    что строка в `chat_members` не писалась вовсе — свежий `updated_at` после фикса убирает
    делегата из кандидатов на следующем прогоне до истечения `chat_refresh_minutes`."""
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(1))
    tid = ids[0]
    bot = FakeBot(error_text_for={tid: "PARTICIPANT_ID_INVALID"})
    asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    older_than = (chat_tracking.msk_now() - timedelta(minutes=360)).strftime("%Y-%m-%d %H:%M:%S")
    candidates = asyncio.run(db.stale_chat_member_candidates(CHAT_ID, [tid], older_than))

    assert candidates == []


def test_refresh_chat_other_error_still_counts_as_error_and_writes_nothing(tmp_path):
    """Регресс на существующее поведение: ошибка БЕЗ PARTICIPANT_ID_INVALID/USER_ID_INVALID
    в тексте остаётся `errors += 1`, строка в `chat_members` не пишется."""
    _ready(tmp_path)
    ids = asyncio.run(_seed_approved(1))
    tid = ids[0]
    bot = FakeBot(fail_ids={tid})  # текст "boom" — не матчится ни под один absent-паттерн

    report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, None))

    assert report["errors"] == 1
    assert report["not_found"] == 0
    row = asyncio.run(db.chat_member_row(CHAT_ID, tid))
    assert row is None


def test_refresh_chat_logs_info_summary_at_the_end(tmp_path, caplog):
    """Сейчас успешный прогон не оставляет в логе НИ ОДНОЙ строки — «сверка вообще идёт?»
    приходится выяснять по БД."""
    _ready(tmp_path)
    asyncio.run(_seed_approved(2))
    bot = FakeBot()

    with caplog.at_level(logging.INFO, logger="services.chat_tracking"):
        report = asyncio.run(chat_tracking.refresh_chat(bot, CHAT_ID, "msk"))

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        f"checked={report['checked']}" in m and f"chat_id={CHAT_ID}" in m for m in infos
    ), infos


# ── Квик 260915-twr (D2): разовая сверка после привязки ──────────────────────────────────

def test_schedule_bind_reconcile_is_fail_soft_without_scheduler(tmp_path, monkeypatch):
    """Привязка чата обязана состояться и без планировщика — критерий приёмки этого
    подшага, тот же довод, что держит `test_chat_binding_260914.py` зелёным без единой
    правки."""
    _ready(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", None)

    asyncio.run(chat_tracking.schedule_bind_reconcile(CHAT_ID, None, ADMIN_ID))  # не бросает


def test_schedule_bind_reconcile_adds_date_job_with_picklable_args(tmp_path, monkeypatch):
    _ready(tmp_path)
    fake = _FakeAPScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)

    asyncio.run(chat_tracking.schedule_bind_reconcile(CHAT_ID, "msk", ADMIN_ID))

    assert len(fake.added) == 1
    func, trigger, kwargs = fake.added[0]
    assert func is chat_tracking.bind_reconcile_job
    assert trigger == "date"
    assert kwargs["id"] == f"chatbind_reconcile_{CHAT_ID}"
    assert kwargs["replace_existing"] is True
    assert kwargs["args"] == [CHAT_ID, "msk", ADMIN_ID]
    pickle.dumps(kwargs["args"])  # picklable-скаляры, не объекты/замыкания


def test_bind_reconcile_job_sends_summary_to_admin(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(_seed_approved(2))
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)

    asyncio.run(chat_tracking.bind_reconcile_job(CHAT_ID, None, ADMIN_ID))

    assert bot.sent, "админ должен лично получить итог сверки после привязки"
    recipient, text = bot.sent[0]
    assert recipient == ADMIN_ID
    assert "2" in text  # present=2 из отформатированного chat_bind_reconcile_done_text


# ── Квик 260915-twr (D3): первый прогон интервальной джобы — через first_run_delay ───────

def test_add_interval_job_with_first_run_delay_pins_near_term_run(monkeypatch):
    fake = _FakeAPScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)

    sched._add_interval_job(
        lambda: None, "twr_probe_job_with_delay", timedelta(hours=6),
        first_run_delay=timedelta(minutes=2),
    )

    assert len(fake.added) == 1
    _, trigger, kwargs = fake.added[0]
    assert trigger == "interval"
    next_run = kwargs["next_run_time"]
    now = datetime.now(sched.MOSCOW_TZ)
    assert abs((next_run - now).total_seconds()) <= 150


def test_add_interval_job_without_first_run_delay_is_byte_for_byte_parity(monkeypatch):
    """Паритет с остальными джобами: без `first_run_delay` `_add_interval_job` не ставит
    `next_run_time` вовсе, когда джобы ещё нет в jobstore — байт-в-байт прежнее поведение
    (`id`/`replace_existing`/`seconds` — обычные позиционные аргументы каждого вызова, не
    часть докстрингового "kwargs", это в тесте выше проверяет именно `next_run_time`)."""
    fake = _FakeAPScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)

    sched._add_interval_job(lambda: None, "twr_probe_job_no_delay", timedelta(hours=6))

    assert len(fake.added) == 1
    _, trigger, kwargs = fake.added[0]
    assert "next_run_time" not in kwargs
