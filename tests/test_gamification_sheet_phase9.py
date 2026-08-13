"""Phase 9 Plan 05 (GAME-01..03, D-05) — «🔄 Таблица геймы»: two named Google Sheets tabs
rebuilt from `list_all_tasks()`/`list_all_submissions()` by one manual button tap.

Two layers of tests, same convention as tests/test_gamification_review_phase9.py:
- `_build_game_matrix`/`_build_game_history` are pure functions -- tested directly against
  hand-built dicts shaped like the real `list_all_tasks()`/`list_all_submissions()` rows, no DB.
- `sync_game_sheets` (the callback handler) is tested against a real tmp_path SQLite DB with
  `sync_named_worksheet` monkeypatched (same monkeypatch convention as
  tests/test_city_admin_phase71.py::test_export_incomplete_and_scheduler_sync_produce_same_batches),
  since the whole point of T-09-16 is that TWO independent calls happen and each failure is
  reported without losing the other tab.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run() and
config.DB_PATH points at a tmp_path file, same convention as every other phase-9 test file.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_caps


ADMIN_ID = 930901
DELEGATE_ID = 930902


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_sheet_phase9.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _task(id, text="Задание", created_at="2026-08-01 10:00:00"):
    return {"id": id, "text": text, "created_at": created_at}


def _submission(id, task_id, user_id, status, coins_awarded=None, reject_reason=None,
                 content_type="text", submitted_at="2026-08-10 10:00:00",
                 task_text="Задание", task_category="Light", reviewed_by=None,
                 reviewed_at=None, user_full_name="Дельгат", user_username="delegate"):
    return {
        "id": id, "task_id": task_id, "user_id": user_id, "content_type": content_type,
        "content": "содержимое", "submitted_at": submitted_at, "status": status,
        "reviewed_by": reviewed_by, "reviewed_at": reviewed_at,
        "coins_awarded": coins_awarded, "reject_reason": reject_reason,
        "task_text": task_text, "task_category": task_category,
        "user_full_name": user_full_name, "user_username": user_username,
    }


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, user_id=ADMIN_ID):
        self.from_user = FakeUser(user_id)
        self.answers_sent = []
        self.text = None
        self.markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


# ── Task 1: _build_game_matrix / _build_game_history (pure) ────────────────────────────────

def test_build_game_matrix_headers_are_participants_plus_one_column_per_task():
    long_text = "Э" * 60  # longer than the 40-char truncation
    tasks = [
        _task(1, text="Задание А", created_at="2026-08-01 10:00:00"),
        _task(2, text=long_text, created_at="2026-08-05 10:00:00"),
    ]
    headers, rows = admin_mod._build_game_matrix(tasks, [])
    assert headers == ["telegram_id", "ФИО", "Юзернейм", "Задание А", long_text[:40]]
    assert rows == []


def test_build_game_matrix_cell_reflects_latest_active_submission_status():
    tasks = [_task(1, "Задание 1"), _task(2, "Задание 2")]
    submissions = [
        # user 101: approved on task 1, nothing on task 2 -> "-"
        _submission(10, task_id=1, user_id=101, status="approved", coins_awarded=30),
        # user 102: pending on task 1
        _submission(11, task_id=1, user_id=102, status="pending"),
        # user 103: ONLY a rejected submission on task 1 (no later active attempt) -> "❌"
        _submission(12, task_id=1, user_id=103, status="rejected", reject_reason="дубль"),
        # user 104: only submitted task 2 (not task 1) -> task-1 cell is "-"
        _submission(13, task_id=2, user_id=104, status="approved", coins_awarded=15),
        # user 105: rejected THEN resubmitted+approved on task 1 (D-05 resubmission-after-reject)
        # -- latest-by-id must win, so the cell shows the approval, not "❌".
        _submission(20, task_id=1, user_id=105, status="rejected", reject_reason="не то"),
        _submission(21, task_id=1, user_id=105, status="approved", coins_awarded=25),
    ]
    headers, rows = admin_mod._build_game_matrix(tasks, submissions)
    by_uid = {r[0]: r for r in rows}

    assert by_uid[101][3:] == ["✅ 30", "-"]
    assert by_uid[102][3:] == ["⏳", "-"]
    assert by_uid[103][3:] == ["❌", "-"]
    assert by_uid[104][3:] == ["-", "✅ 15"]
    assert by_uid[105][3:] == ["✅ 25", "-"]  # resubmission after rejection wins, not "❌"


def test_build_game_matrix_rows_only_participants_with_at_least_one_submission():
    tasks = [_task(1, "Задание 1")]
    submissions = [
        _submission(10, task_id=1, user_id=201, status="approved", coins_awarded=20),
        # user 201 was rejected once, then resubmitted -- still ONE row, not two.
        _submission(9, task_id=1, user_id=201, status="rejected"),
        _submission(11, task_id=1, user_id=202, status="pending"),
    ]
    headers, rows = admin_mod._build_game_matrix(tasks, submissions)
    uids = [r[0] for r in rows]
    assert sorted(uids) == [201, 202]
    assert 999 not in uids  # a hypothetical 1000-user base never leaks empty rows in here


def test_build_game_history_rows_include_every_submission_including_rejected():
    submissions = [
        _submission(1, task_id=1, user_id=101, status="pending", task_text="Знакомство"),
        _submission(2, task_id=1, user_id=102, status="approved", coins_awarded=30,
                    reviewed_by=ADMIN_ID, reviewed_at="2026-08-11 09:00:00"),
        # a rejected attempt AND its resubmission -- D-05 requires BOTH rows visible, not just
        # the currently-active one.
        _submission(3, task_id=1, user_id=103, status="rejected", reject_reason="не тот скрин",
                    reviewed_by=ADMIN_ID, reviewed_at="2026-08-11 09:05:00"),
        _submission(4, task_id=1, user_id=103, status="approved", coins_awarded=30,
                    reviewed_by=ADMIN_ID, reviewed_at="2026-08-12 09:05:00"),
    ]
    headers, rows = admin_mod._build_game_history(submissions)
    assert headers == ["ID сдачи", "Задание", "Категория", "Участник", "Юзернейм", "Тип",
                        "Отправлено", "Статус", "Проверил", "Когда", "Начислено",
                        "Причина отказа"]
    assert len(rows) == 4  # every submission is its own row, including the rejected one
    ids = [r[0] for r in rows]
    assert ids == [1, 2, 3, 4]
    rejected_row = rows[2]
    assert rejected_row[7] == "Отклонено"
    assert rejected_row[11] == "не тот скрин"
    approved_row = rows[1]
    assert approved_row[7] == "Одобрено"
    assert approved_row[10] == 30


# ── Task 1: admin_game_sync_sheet handler — two independent writes ─────────────────────────

def test_admin_game_sync_sheet_reports_row_counts_on_success(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def go():
        t1 = await db.create_task("Раннее задание", "Light", 20, "text",
                                   "2026-08-20 23:59:00", ADMIN_ID)
        t2 = await db.create_task("Позднее задание", "Medium", 40, "text",
                                   "2026-08-25 23:59:00", ADMIN_ID)
        await db.create_submission(t1, DELEGATE_ID, "text", "готово", "2026-08-14 10:00:00")
        await db.create_submission(t2, DELEGATE_ID, "text", "тоже готово", "2026-08-14 11:00:00")

        calls = []

        async def _fake_sync(title, headers, rows):
            calls.append((title, headers, len(rows)))
            return len(rows)

        monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_sync)

        callback = FakeCallback("admin_game_sync_sheet")
        await admin_mod.sync_game_sheets(callback)

        titles = [c[0] for c in calls]
        assert titles == ["Гейма", "История сдач"]
        # column order follows created_at ASC (t1 earlier than t2) even though list_all_tasks()
        # itself returns DESC -- the handler must explicitly reverse it.
        matrix_headers = calls[0][1]
        assert matrix_headers.index("Раннее задание") < matrix_headers.index("Позднее задание")

        text = callback.message.answers_sent[-1]
        assert "Гейма: 1 строк" in text
        assert "История сдач: 2 строк" in text

    asyncio.run(go())


def test_admin_game_sync_sheet_reports_failure_without_crashing(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def go():
        t1 = await db.create_task("Задание", "Light", 20, "text", "2026-08-20 23:59:00", ADMIN_ID)
        await db.create_submission(t1, DELEGATE_ID, "text", "готово", "2026-08-14 10:00:00")

        calls = []

        async def _fake_sync(title, headers, rows):
            calls.append(title)
            if title == "Гейма":
                return -1  # simulate one tab failing
            return len(rows)

        monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_sync)

        callback = FakeCallback("admin_game_sync_sheet")
        await admin_mod.sync_game_sheets(callback)  # must not raise

        # both tabs were still attempted -- one failure does not cancel the other call.
        assert calls == ["Гейма", "История сдач"]

        text = callback.message.answers_sent[-1]
        assert "Гейма" in text and "ошибка синхронизации" in text
        assert "История сдач: 1 строк" in text  # second tab unaffected by the first's failure

    asyncio.run(go())


# ── Quick 260814-gsg: confirm gate before the destructive tab rewrite ──────────────────────
#
# sync_named_worksheet clears both tabs and rewrites them from the DB. The two neighbouring
# destructive admin rows («♻️ Пересобрать таблицу», «🧹 Убрать дубли») got confirmation screens
# after the 13.08 sheet incident; this one shipped without one. Found by phase-9 verification.

def test_game_sync_confirm_does_not_touch_the_sheets(monkeypatch):
    """The tap that used to rewrite both tabs must now only draw a screen."""
    called = []

    async def _fake_sync(title, headers, rows):
        called.append(title)
        return len(rows)

    monkeypatch.setattr(admin_mod, "sync_named_worksheet", _fake_sync)

    callback = FakeCallback("admin_game_sync_sheet")

    async def go():
        await admin_mod.sync_game_sheets_confirm(callback)

    asyncio.run(go())

    assert called == []  # nothing cleared, nothing written
    cbs = [b.callback_data for row in callback.message.markup.inline_keyboard for b in row]
    assert "admin_game_sync_sheet_go" in cbs
    assert "admin_menu" in cbs  # escape hatch


def test_game_sync_confirm_names_what_is_lost():
    """A confirm screen that doesn't state the actual damage is decoration, not a gate: the only
    thing a rewrite can destroy is what a manager typed straight into the sheet."""
    callback = FakeCallback("admin_game_sync_sheet")

    async def go():
        await admin_mod.sync_game_sheets_confirm(callback)

    asyncio.run(go())

    text = callback.message.text
    assert "⚠️" in text
    assert "руками" in text.lower()
    assert "пропадут" in text.lower()
    # both tab names must be visible — the manager decides knowing which tabs are wiped
    assert "Гейма" in text and "История сдач" in text


def test_game_sync_go_is_capability_mapped():
    """An unmapped callback is rejected by the capability gate — the confirm screen would render
    a dead button. Same capability as the menu row that opens it."""
    caps = admin_caps.ADMIN_CAPS
    assert caps["admin_game_sync_sheet_go"] == caps["admin_game_sync_sheet"]
