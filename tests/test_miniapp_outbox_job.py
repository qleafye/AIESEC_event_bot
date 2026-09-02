"""Phase 19 (08, Task 3, WEBAPP-01) — джоба бота: разбор `miniapp_outbox`.

services/miniapp_outbox.py::drain(bot) — диспетчер по `kind`, ретраи с потолком попыток,
`drain` никогда не вызывает `add_coins`. Плюс регистрация интервальной джобы в
services/scheduler.py::init_scheduler (job_id="miniapp_outbox_drain", 30с).

pytest-asyncio недоступен в этом окружении — весь async гоняется через asyncio.run(),
config.DB_PATH указывает на файл в tmp_path (конвенция всех phase-19 тестов).
"""
import asyncio
import inspect

import aiosqlite
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database import db as bot_db
from services import miniapp_outbox
from services import scheduler as sched


def _init(tmp_path, name="miniapp_outbox_job.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


def _run(coro):
    return asyncio.run(coro)


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _enqueue(kind, payload):
    return _run(bot_db.enqueue_miniapp_outbox(kind, payload, "2026-08-23 12:00:00"))


class FakeBot:
    pass


# ── submission_created -> notify_submission ─────────────────────────────────────────────

def test_submission_created_calls_notify_submission_with_expected_args(tmp_path, monkeypatch):
    _init(tmp_path)
    calls = []

    async def fake_notify(bot, **kwargs):
        calls.append((bot, kwargs))

    monkeypatch.setattr(miniapp_outbox, "notify_submission", fake_notify)
    payload = {
        "submission_id": 1, "user_id": 2, "task_id": 3,
        "task_text": "Пост со скрином", "submitter_name": "Ира",
    }
    row_id = _enqueue("submission_created", payload)
    bot = FakeBot()

    done = _run(miniapp_outbox.drain(bot))

    assert done == 1
    assert len(calls) == 1
    called_bot, kwargs = calls[0]
    assert called_bot is bot
    assert kwargs == payload
    row = _run(_fetchall("SELECT processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)))[0]
    assert row["processed_at"]


# ── submission_reviewed/task_changed/coins_manual -> request_resync ────────────────────

def test_resync_kinds_call_request_resync_and_mark_processed(tmp_path, monkeypatch):
    _init(tmp_path)
    calls = []
    monkeypatch.setattr(miniapp_outbox, "request_resync", lambda *a, **kw: calls.append((a, kw)))

    ids = []
    for kind, payload in [
        ("submission_reviewed", {"submission_id": 1, "user_id": 2, "status": "approved", "coins": 10}),
        ("task_changed", {"task_id": 5}),
        ("coins_manual", {"user_id": 9, "delta": 3}),
    ]:
        ids.append(_enqueue(kind, payload))

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 3
    assert len(calls) == 3
    rows = _run(_fetchall(
        f"SELECT id, processed_at FROM miniapp_outbox WHERE id IN ({','.join('?' * len(ids))})",
        tuple(ids),
    ))
    assert all(r["processed_at"] for r in rows)


# ── reg_finalized/reg_edited/reg_resume_upload -> services.reg_finalize (Phase 21, 21-08) ──

def test_reg_finalized_kind_calls_post_finalize_with_new_mode(tmp_path, monkeypatch):
    _init(tmp_path)
    calls = []

    async def fake_post_finalize(bot, telegram_id, mode, **kwargs):
        calls.append((bot, telegram_id, mode, kwargs))

    monkeypatch.setattr(miniapp_outbox, "post_finalize", fake_post_finalize)
    row_id = _enqueue("reg_finalized", {"telegram_id": 12345})
    bot = FakeBot()

    done = _run(miniapp_outbox.drain(bot))

    assert done == 1
    assert len(calls) == 1
    called_bot, telegram_id, mode, kwargs = calls[0]
    assert called_bot is bot
    assert telegram_id == 12345
    assert mode == "new"
    row = _run(_fetchall("SELECT processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)))[0]
    assert row["processed_at"]


def test_reg_edited_kind_calls_post_finalize_with_edit_mode_and_derived_facts(tmp_path, monkeypatch):
    """T-21-08: payload несёт только telegram_id — недостающие changed_columns/remoderated/
    resubmitted дочитывает services.reg_finalize.derive_edit_facts (payload без ответов
    анкеты — ПД в очередь не попадают)."""
    _init(tmp_path)
    post_finalize_calls = []
    derive_calls = []

    async def fake_post_finalize(bot, telegram_id, mode, **kwargs):
        post_finalize_calls.append((telegram_id, mode, kwargs))

    async def fake_derive(telegram_id, full):
        derive_calls.append((telegram_id, full))
        return (["phone"], False, True)

    monkeypatch.setattr(miniapp_outbox, "post_finalize", fake_post_finalize)
    monkeypatch.setattr(miniapp_outbox, "derive_edit_facts", fake_derive)
    monkeypatch.setattr(miniapp_outbox, "get_user", lambda telegram_id: _fake_user_row(telegram_id))
    _enqueue("reg_edited", {"telegram_id": 999})

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 1
    assert len(post_finalize_calls) == 1
    telegram_id, mode, kwargs = post_finalize_calls[0]
    assert telegram_id == 999
    assert mode == "edit"
    assert kwargs["changed_columns"] == ["phone"]
    assert kwargs["remoderated"] is False
    assert kwargs["resubmitted"] is True
    assert len(derive_calls) == 1


async def _fake_user_row(telegram_id):
    return {"telegram_id": telegram_id, "status": "pending"}


def test_reg_resume_upload_kind_calls_handle_resume_upload(tmp_path, monkeypatch):
    _init(tmp_path)
    calls = []

    async def fake_handle(bot, telegram_id, file_id, filename):
        calls.append((bot, telegram_id, file_id, filename))

    monkeypatch.setattr(miniapp_outbox, "handle_resume_upload", fake_handle)
    _enqueue("reg_resume_upload", {"telegram_id": 555, "file_id": "AgAD1", "filename": "cv.pdf"})
    bot = FakeBot()

    done = _run(miniapp_outbox.drain(bot))

    assert done == 1
    assert len(calls) == 1
    called_bot, telegram_id, file_id, filename = calls[0]
    assert called_bot is bot
    assert telegram_id == 555
    assert file_id == "AgAD1"
    assert filename == "cv.pdf"


def test_reg_finalized_kind_retries_on_failure_same_as_other_kinds(tmp_path, monkeypatch):
    _init(tmp_path)

    async def boom(bot, telegram_id, mode, **kwargs):
        raise RuntimeError("sheets down")

    monkeypatch.setattr(miniapp_outbox, "post_finalize", boom)
    row_id = _enqueue("reg_finalized", {"telegram_id": 1})

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 0
    row = _run(_fetchall(
        "SELECT attempts, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["attempts"] == 1
    assert row["processed_at"] is None


# ── пустая очередь ────────────────────────────────────────────────────────────────────

def test_empty_queue_makes_zero_calls(tmp_path, monkeypatch):
    _init(tmp_path)
    notify_calls = []
    resync_calls = []
    monkeypatch.setattr(miniapp_outbox, "notify_submission", lambda *a, **kw: notify_calls.append(1))
    monkeypatch.setattr(miniapp_outbox, "request_resync", lambda *a, **kw: resync_calls.append(1))

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 0
    assert not notify_calls
    assert not resync_calls


# ── ретраи и потолок попыток ─────────────────────────────────────────────────────────────

def test_handler_exception_increments_attempts_and_stays_in_queue(tmp_path, monkeypatch):
    _init(tmp_path)

    async def boom(bot, **kwargs):
        raise RuntimeError("Telegram unavailable")

    monkeypatch.setattr(miniapp_outbox, "notify_submission", boom)
    row_id = _enqueue("submission_created", {"submission_id": 1, "user_id": 2, "task_id": 3,
                                              "task_text": "x", "submitter_name": "y"})

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 0
    row = _run(_fetchall(
        "SELECT attempts, last_error, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["attempts"] == 1
    assert "Telegram unavailable" in row["last_error"]
    assert row["processed_at"] is None

    # ещё раз — attempts растёт дальше, строка всё ещё в очереди
    _run(miniapp_outbox.drain(FakeBot()))
    row = _run(_fetchall("SELECT attempts, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)))[0]
    assert row["attempts"] == 2
    assert row["processed_at"] is None


def test_row_leaves_queue_after_max_attempts(tmp_path, monkeypatch, caplog):
    _init(tmp_path)

    async def boom(bot, **kwargs):
        raise RuntimeError("permanently broken")

    monkeypatch.setattr(miniapp_outbox, "notify_submission", boom)
    row_id = _enqueue("submission_created", {"submission_id": 1, "user_id": 2, "task_id": 3,
                                              "task_text": "x", "submitter_name": "y"})

    with caplog.at_level("ERROR"):
        for _ in range(miniapp_outbox.MAX_ATTEMPTS):
            done = _run(miniapp_outbox.drain(FakeBot()))

    # the LAST drain() call (5th attempt) removed the row from the queue
    assert done == 1
    row = _run(_fetchall(
        "SELECT attempts, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["attempts"] == miniapp_outbox.MAX_ATTEMPTS
    assert row["processed_at"] is not None
    assert any("giving up" in r.getMessage() for r in caplog.records)

    # queue is unblocked — nothing left to drain
    assert _run(miniapp_outbox.drain(FakeBot())) == 0


# ── неизвестный kind ──────────────────────────────────────────────────────────────────

def test_unknown_kind_does_not_crash_and_is_marked_failed(tmp_path):
    _init(tmp_path)
    row_id = _enqueue("submission_created", {"submission_id": 1, "user_id": 2, "task_id": 3,
                                              "task_text": "x", "submitter_name": "y"})
    # forge an unknown kind directly in the DB — enqueue() itself validates kinds, drain must
    # not trust the stored value either (T-19-55: closed dispatch set).
    _run_direct = asyncio.run

    async def _corrupt():
        async with bot_db._connect() as conn:
            await conn.execute("UPDATE miniapp_outbox SET kind = ? WHERE id = ?", ("coins_award", row_id))
            await conn.commit()

    _run_direct(_corrupt())

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 0  # first attempt: stays in queue with attempts=1
    row = _run(_fetchall(
        "SELECT attempts, last_error, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["attempts"] == 1
    assert row["last_error"]
    assert row["processed_at"] is None


# ── одна битая строка не блокирует остальные ─────────────────────────────────────────────

def test_one_broken_row_does_not_block_the_rest(tmp_path, monkeypatch):
    _init(tmp_path)

    async def boom(bot, **kwargs):
        raise RuntimeError("boom")

    resync_calls = []
    monkeypatch.setattr(miniapp_outbox, "notify_submission", boom)
    monkeypatch.setattr(miniapp_outbox, "request_resync", lambda *a, **kw: resync_calls.append(1))

    broken_id = _enqueue("submission_created", {"submission_id": 1, "user_id": 2, "task_id": 3,
                                                 "task_text": "x", "submitter_name": "y"})
    ok_id = _enqueue("task_changed", {"task_id": 9})

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 1  # the healthy row was processed
    assert resync_calls == [1]
    broken = _run(_fetchall("SELECT processed_at FROM miniapp_outbox WHERE id = ?", (broken_id,)))[0]
    ok = _run(_fetchall("SELECT processed_at FROM miniapp_outbox WHERE id = ?", (ok_id,)))[0]
    assert broken["processed_at"] is None
    assert ok["processed_at"]


# ── drain никогда не начисляет монеты ────────────────────────────────────────────────────

def test_drain_never_imports_or_calls_add_coins():
    import services.miniapp_outbox as mod
    assert not hasattr(mod, "add_coins")
    # No executable reference to add_coins anywhere outside the module's own prose docstring
    # (which explains WHY it must never appear) -- scan only actual statements, not comments.
    source_lines = inspect.getsource(mod).splitlines()
    in_docstring = False
    for line in source_lines:
        stripped = line.strip()
        if stripped.startswith('"""'):
            in_docstring = not in_docstring if stripped.count('"""') == 1 else in_docstring
            continue
        if in_docstring or stripped.startswith("#"):
            continue
        assert "add_coins" not in line, line


# ── регистрация в планировщике ───────────────────────────────────────────────────────────

def _isolate_scheduler(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "miniapp_outbox_sched.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


def test_job_registered_with_expected_id_and_interval(tmp_path, monkeypatch):
    _isolate_scheduler(tmp_path, monkeypatch)

    async def go():
        await bot_db.init_db()
        s = await sched.init_scheduler(bot=object())
        try:
            job = s.get_job("miniapp_outbox_drain")
            assert job is not None
            assert isinstance(job.trigger, IntervalTrigger)
            assert job.trigger.interval.total_seconds() == 30
            assert job.func is sched.miniapp_outbox_drain_job
        finally:
            s.shutdown(wait=False)

    asyncio.run(go())


def test_miniapp_outbox_drain_job_reads_bot_from_module_global(tmp_path, monkeypatch):
    """The job target itself must be a zero-arg picklable coroutine (Pitfall 3) -- it reads
    the bot from services.scheduler's own module global, never a closure/partial."""
    _init(tmp_path)
    calls = []

    async def fake_drain(bot):
        calls.append(bot)
        return 0

    monkeypatch.setattr(miniapp_outbox, "drain", fake_drain)
    sentinel_bot = object()
    monkeypatch.setattr(sched, "_bot", sentinel_bot)

    asyncio.run(sched.miniapp_outbox_drain_job())

    assert calls == [sentinel_bot]
