"""Phase 09.1 Plan 04 (D, GAME-07) — debounced background resync of the gamification sheet
tabs («Гейма» + «История сдач»).

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run(),
same convention as every other 09.1 test file. Debounce tests always pass a short `delay=`
override (never the real 30s DEBOUNCE_SECONDS) so the suite stays fast and deterministic --
CONTEXT.md D's own instruction ("делай задержку подменяемой, тесты не должны спать 30 с").

Task 1: services/game_sync.py -- request_resync's cancel-and-restart debounce, set_rebuild
registration, the one-shot admin failure warning, and handlers/admin.py::rebuild_game_sheets
(the shared body sync_game_sheets/request_resync both eventually call).
Task 2: the 5 handlers/admin.py hook points (game_task_confirm/grev_approve/
grev_approve_amount_step/grev_reject_reason/cmd_coins) + sync_game_sheets_confirm's
"обновлено N мин назад" phrase (_last_sync_phrase).
"""
import asyncio
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import services.game_sync as game_sync
from handlers import admin as admin_mod
from handlers import admin_gamification
from handlers.states import GameReview


ADMIN_ID = 930951
DELEGATE_ID = 930952


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_autosync_091.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def setup_function(_):
    # Module-level debounce state must not leak between tests (no fixture resets it
    # automatically -- reset_for_tests() exists exactly for this).
    game_sync.reset_for_tests()
    game_sync.set_rebuild(None)


def teardown_function(_):
    game_sync.reset_for_tests()
    game_sync.set_rebuild(None)


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _seed_task(text="Задание", category="Light", coins=30, proof_type="text",
               deadline_at="2026-08-25 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _seed_submission(task_id, user_id=DELEGATE_ID, content_type="text", content="готово",
                      submitted_at="2026-08-14 10:00:00"):
    return asyncio.run(db.create_submission(task_id, user_id, content_type, content, submitted_at))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID, bot=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.deleted = False
        self.answers_sent = []
        self.answer_markups = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.answer_markups.append(reply_markup)

    async def answer_photo(self, photo):
        pass

    async def answer_document(self, document):
        pass

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, bot=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = bot if bot is not None else FakeBot()
        self.message = FakeMessage(user_id=user_id, bot=self.bot)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


# ── Task 1: request_resync -- "last call wins" cancel-and-restart debounce ─────────────────

def test_two_requests_in_window_collapse_to_one_rebuild():
    calls = []

    async def fake_rebuild():
        calls.append(1)
        return (0, 0)

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        game_sync.request_resync(delay=0.02)
        await asyncio.sleep(0.005)
        game_sync.request_resync(delay=0.02)  # cancels the first pending timer, restarts it
        await asyncio.sleep(0.06)

    asyncio.run(go())
    assert calls == [1]


def test_twenty_requests_in_a_row_collapse_to_one_rebuild():
    calls = []

    async def fake_rebuild():
        calls.append(1)
        return (0, 0)

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        for _ in range(20):
            game_sync.request_resync(delay=0.02)
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.06)

    asyncio.run(go())
    assert calls == [1]  # 20 moderator decisions in a row -> exactly one Sheets write


def test_request_after_completed_rebuild_starts_a_new_one():
    calls = []

    async def fake_rebuild():
        calls.append(1)
        return (0, 0)

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        game_sync.request_resync(delay=0.01)
        await asyncio.sleep(0.04)  # first timer fully ran
        game_sync.request_resync(delay=0.01)
        await asyncio.sleep(0.04)  # second timer fully ran, independently

    asyncio.run(go())
    assert len(calls) == 2


def test_request_resync_without_rebuild_registered_does_not_raise():
    game_sync.set_rebuild(None)  # early-boot edge case: no admin.py import yet

    async def go():
        game_sync.request_resync(delay=0.01)
        await asyncio.sleep(0.03)

    asyncio.run(go())  # the assertion IS that this returns without raising


def test_cancelled_timer_never_rebuilds():
    calls = []

    async def fake_rebuild():
        calls.append(1)
        return (0, 0)

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        game_sync.request_resync(delay=0.05)
        await asyncio.sleep(0.005)
        game_sync.request_resync(delay=0.05)  # cancels the first timer before it ever fires
        await asyncio.sleep(0.08)

    asyncio.run(go())
    assert calls == [1]  # only the surviving (second) timer ever ran the rebuild


# ── Task 1: one-shot admin failure warning ──────────────────────────────────────────────────

def test_failed_rebuild_warns_admins_once_per_streak(monkeypatch):
    warns = []

    async def fake_alert(text):
        warns.append(text)

    monkeypatch.setattr(game_sync, "_send_admin_alert", fake_alert)

    async def fake_rebuild():
        return (-1, 0)  # simulated Google Sheets failure -- same shape rebuild_game_sheets returns

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        for _ in range(3):
            game_sync.request_resync(delay=0.01)
            await asyncio.sleep(0.03)

    asyncio.run(go())
    assert len(warns) == 1  # three consecutive failures -> exactly one admin warning, no spam


def test_failure_flag_resets_after_a_successful_rebuild(monkeypatch):
    warns = []

    async def fake_alert(text):
        warns.append(text)

    monkeypatch.setattr(game_sync, "_send_admin_alert", fake_alert)

    results = iter([(-1, 0), (0, 0), (-1, 0)])

    async def fake_rebuild():
        return next(results)

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        for _ in range(3):
            game_sync.request_resync(delay=0.01)
            await asyncio.sleep(0.03)

    asyncio.run(go())
    assert len(warns) == 2  # fail(warn) -> success(reset) -> fail(warn again)


def test_exception_during_rebuild_warns_once_and_does_not_crash(monkeypatch):
    warns = []

    async def fake_alert(text):
        warns.append(text)

    monkeypatch.setattr(game_sync, "_send_admin_alert", fake_alert)

    async def fake_rebuild():
        raise RuntimeError("boom")

    game_sync.set_rebuild(fake_rebuild)

    async def go():
        game_sync.request_resync(delay=0.01)
        await asyncio.sleep(0.03)  # must not propagate out of the background task

    asyncio.run(go())
    assert len(warns) == 1


# ── Task 1: handlers/admin.py::rebuild_game_sheets — shared body ───────────────────────────

def test_rebuild_game_sheets_writes_last_synced_at_on_full_success(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_sync(title, headers, rows):
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

    async def go():
        before = await db.get_setting("game_sheet_last_synced_at")
        assert before is None

        result = await admin_gamification.rebuild_game_sheets()
        assert result == (0, 0)

        after = await db.get_setting("game_sheet_last_synced_at")
        assert after is not None

    asyncio.run(go())


def test_rebuild_game_sheets_skips_timestamp_on_partial_failure(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_sync(title, headers, rows):
        return -1 if title == "Гейма" else len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

    async def go():
        result = await admin_gamification.rebuild_game_sheets()
        assert result[0] == -1

        after = await db.get_setting("game_sheet_last_synced_at")
        assert after is None  # a half-failed sync must not claim to be "up to date"

    asyncio.run(go())


def test_rebuild_game_sheets_does_not_raise_on_full_failure(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_sync(title, headers, rows):
        return -1

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

    async def go():
        result = await admin_gamification.rebuild_game_sheets()  # must not raise
        assert result == (-1, -1)
        assert await db.get_setting("game_sheet_last_synced_at") is None

    asyncio.run(go())


def test_button_rebuild_bypasses_the_debounce(tmp_path, monkeypatch):
    """CONTEXT.md D: the button stays a "reconcile now" escape hatch -- it must never wait on
    request_resync's timer, and it must not itself schedule one (that would be redundant, the
    rebuild it just ran already IS the resync)."""
    _db_ready(tmp_path)

    resync_calls = []
    monkeypatch.setattr(admin_gamification, "_request_game_resync", lambda *a, **k: resync_calls.append(1))

    async def _fake_sync(title, headers, rows):
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

    callback = FakeCallback("admin_game_sync_sheet_go")

    async def go():
        await admin_gamification.sync_game_sheets(callback)

    asyncio.run(go())
    assert resync_calls == []
    assert callback.message.answers_sent  # report rendered immediately, no sleep involved


# ── Task 2: five hook points request a resync ───────────────────────────────────────────────

def test_game_task_confirm_requests_a_resync(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_gamification, "_request_game_resync", lambda *a, **k: calls.append(1))

    state = _new_state()
    asyncio.run(state.update_data(
        gt_text="Задание", gt_category="Light", gt_coins=10, gt_proof_type="text",
        gt_deadline="2026-08-25 23:59:00",
    ))
    callback = FakeCallback("gtconfirm")
    asyncio.run(admin_gamification.game_task_confirm(callback, state))

    assert calls == [1]


def test_grev_approve_requests_a_resync_only_when_won(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_gamification, "_request_game_resync", lambda *a, **k: calls.append(1))

    task_id = _seed_task(coins=20)
    sub_id = _seed_submission(task_id)
    state = _new_state()

    first = FakeCallback(f"grev_approve:{sub_id}")
    asyncio.run(admin_gamification.grev_approve(first, state))
    assert calls == [1]

    # a losing race (already-processed submission) must NOT request another resync
    second = FakeCallback(f"grev_approve:{sub_id}", user_id=930953)
    asyncio.run(admin_gamification.grev_approve(second, state))
    assert calls == [1]


def test_grev_approve_amount_step_requests_a_resync_only_when_won(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_gamification, "_request_game_resync", lambda *a, **k: calls.append(1))

    task_id = _seed_task(coins=5)
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_gamification.grev_approve_custom_start(FakeCallback(f"grev_approve_custom:{sub_id}"), state))

    amount_msg = FakeMessage(text="45")
    asyncio.run(admin_gamification.grev_approve_amount_step(amount_msg, state))
    assert calls == [1]


def test_grev_reject_reason_requests_a_resync_only_when_won(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_gamification, "_request_game_resync", lambda *a, **k: calls.append(1))

    task_id = _seed_task()
    sub_id = _seed_submission(task_id)
    state = _new_state()
    asyncio.run(admin_gamification.grev_reject_start(FakeCallback(f"grev_reject:{sub_id}"), state))

    reason_msg = FakeMessage(text="-")
    asyncio.run(admin_gamification.grev_reject_reason(reason_msg, state))
    assert calls == [1]


def test_cmd_coins_requests_a_resync_only_after_add_coins(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    calls = []
    monkeypatch.setattr(admin_mod, "_request_game_resync", lambda *a, **k: calls.append(1))

    asyncio.run(db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Дельгат", "username": "@delegate1",
        "registration_date": "2026-08-01",
    }))

    hint_msg = FakeMessage(text="/coins")
    asyncio.run(admin_mod.cmd_coins(hint_msg, hint_msg.bot))
    assert calls == []  # malformed command -> add_coins never called -> no resync

    coins_msg = FakeMessage(text="/coins @delegate1 +10 бонус")
    asyncio.run(admin_mod.cmd_coins(coins_msg, coins_msg.bot))
    assert calls == [1]


# ── Task 2: sync_game_sheets_confirm's "обновлено N мин назад" phrase ──────────────────────

def test_last_sync_phrase_none_or_empty_or_unparsable_is_never_synced():
    now = datetime(2026, 8, 17, 12, 0, 0)
    assert admin_gamification._last_sync_phrase(None, now) == "ещё не обновлялось"
    assert admin_gamification._last_sync_phrase("", now) == "ещё не обновлялось"
    assert admin_gamification._last_sync_phrase("garbage", now) == "ещё не обновлялось"


def test_last_sync_phrase_just_now():
    now = datetime(2026, 8, 17, 12, 0, 0)
    raw = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    assert admin_gamification._last_sync_phrase(raw, now) == "обновлено только что"


def test_last_sync_phrase_minutes():
    now = datetime(2026, 8, 17, 12, 0, 0)
    raw = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert admin_gamification._last_sync_phrase(raw, now) == "обновлено 5 мин назад"


def test_last_sync_phrase_hours():
    now = datetime(2026, 8, 17, 12, 0, 0)
    raw = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    assert admin_gamification._last_sync_phrase(raw, now) == "обновлено 3 ч назад"


def test_last_sync_phrase_days_falls_back_to_a_date():
    now = datetime(2026, 8, 17, 12, 0, 0)
    raw = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    phrase = admin_gamification._last_sync_phrase(raw, now)
    assert phrase.startswith("обновлено ")
    assert "15.08" in phrase


def test_sync_game_sheets_confirm_shows_never_synced_by_default(tmp_path):
    _db_ready(tmp_path)
    callback = FakeCallback("admin_game_sync_sheet")
    asyncio.run(admin_gamification.sync_game_sheets_confirm(callback))
    assert "ещё не обновлялось" in callback.message.text


def test_sync_game_sheets_confirm_shows_minutes_after_a_rebuild(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_sync(title, headers, rows):
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)

    async def go():
        await admin_gamification.rebuild_game_sheets()
        callback = FakeCallback("admin_game_sync_sheet")
        await admin_gamification.sync_game_sheets_confirm(callback)
        return callback

    callback = asyncio.run(go())
    assert "обновлено" in callback.message.text
    assert "ещё не обновлялось" not in callback.message.text
