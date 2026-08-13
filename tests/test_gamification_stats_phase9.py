"""Phase 9 Plan 06 (GAME-01..03) — «📊 Статистика геймы»: the single aggregating query
(`get_game_stats`, 09-01) plus the admin-facing render (`show_game_stats`, this plan).

Two layers, same convention as tests/test_gamification_sheet_phase9.py:
- `get_game_stats()` is tested directly against a real tmp_path SQLite DB (it IS the
  aggregate query -- there is no pure-function layer to test around it).
- `show_game_stats` (the callback handler) is tested against the same DB with a FakeCallback,
  same convention as tests/test_gamification_sheet_phase9.py's handler-level tests.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run() and
config.DB_PATH points at a tmp_path file, same convention as every other phase-9 test file.
"""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin_caps import required_capability


ADMIN_ID = 930601
DELEGATE_A = 930602
DELEGATE_B = 930603


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_gamification_stats_phase9.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, user_id=ADMIN_ID):
        self.from_user = FakeUser(user_id)
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def _make_task(category="Light", coins=20):
    return await db.create_task("Задание", category, coins, "text",
                                 "2026-08-20 23:59:00", ADMIN_ID)


# ── Task 1: get_game_stats (data layer, already shipped by 09-01) ──────────────────────────

def test_get_game_stats_participants_counts_distinct_users(tmp_path):
    _db_ready(tmp_path)

    async def go():
        t1 = await _make_task()
        t2 = await _make_task()
        await db.create_submission(t1, DELEGATE_A, "text", "готово", "2026-08-14 10:00:00")
        await db.create_submission(t2, DELEGATE_A, "text", "готово 2", "2026-08-14 10:05:00")
        await db.create_submission(t1, DELEGATE_B, "text", "тоже готово", "2026-08-14 10:10:00")

        stats = await db.get_game_stats()
        assert stats["participants"] == 2  # 3 sdachi, 2 distinct users

    asyncio.run(go())


def test_get_game_stats_status_counts(tmp_path):
    _db_ready(tmp_path)

    async def go():
        t1 = await _make_task()
        s_approved_1 = await db.create_submission(t1, DELEGATE_A, "text", "a", "2026-08-14 10:00:00")
        await db.claim_submission(s_approved_1, ADMIN_ID, "approved", coins_awarded=20)

        # second task so the second approved submission doesn't collide with idx_game_submissions_active
        t2 = await _make_task()
        s_approved_2 = await db.create_submission(t2, DELEGATE_A, "text", "b", "2026-08-14 10:05:00")
        await db.claim_submission(s_approved_2, ADMIN_ID, "approved", coins_awarded=20)

        t3 = await _make_task()
        s_pending = await db.create_submission(t3, DELEGATE_A, "text", "c", "2026-08-14 10:10:00")

        t4 = await _make_task()
        s_rejected = await db.create_submission(t4, DELEGATE_B, "text", "d", "2026-08-14 10:15:00")
        await db.claim_submission(s_rejected, ADMIN_ID, "rejected", reject_reason="не то")

        stats = await db.get_game_stats()
        assert stats["approved"] == 2
        assert stats["pending"] == 1
        assert stats["rejected"] == 1

    asyncio.run(go())


def test_get_game_stats_by_category_only_counts_approved(tmp_path):
    _db_ready(tmp_path)

    async def go():
        t_hard_pending = await _make_task(category="Hard")
        await db.create_submission(t_hard_pending, DELEGATE_A, "text", "a", "2026-08-14 10:00:00")

        t_hard_approved = await _make_task(category="Hard")
        s2 = await db.create_submission(t_hard_approved, DELEGATE_B, "text", "b", "2026-08-14 10:05:00")
        await db.claim_submission(s2, ADMIN_ID, "approved", coins_awarded=500)

        stats = await db.get_game_stats()
        # the pending Hard submission must NOT count toward by_category
        assert stats["by_category"] == {"Hard": 1}

    asyncio.run(go())


# ── Task 1: capability map ──────────────────────────────────────────────────────────────────

def test_admin_game_stats_key_maps_to_moderate_game():
    assert required_capability(callback_data="admin_game_stats") == "moderate_game"


# ── Task 1: show_game_stats render ──────────────────────────────────────────────────────────

def test_show_game_stats_renders_all_fields(tmp_path):
    _db_ready(tmp_path)

    async def go():
        t1 = await _make_task(category="Light", coins=20)
        s1 = await db.create_submission(t1, DELEGATE_A, "text", "a", "2026-08-14 10:00:00")
        await db.claim_submission(s1, ADMIN_ID, "approved", coins_awarded=20)

        t2 = await _make_task(category="Medium", coins=30)
        await db.create_submission(t2, DELEGATE_B, "text", "b", "2026-08-14 10:05:00")  # pending

        t3 = await _make_task(category="Hard", coins=50)
        s3 = await db.create_submission(t3, DELEGATE_A, "text", "c", "2026-08-14 10:10:00")
        await db.claim_submission(s3, ADMIN_ID, "rejected", reject_reason="дубль")

        callback = FakeCallback("admin_game_stats")
        await admin_mod.show_game_stats(callback)

        text = callback.message.answers_sent[-1]
        assert "Участников: 2" in text
        assert "Сдано на проверке: 1" in text
        assert "Одобрено: 1" in text
        assert "Отклонено: 1" in text
        assert "Light: 1" in text  # approved-only category breakdown
        assert "Medium" not in text  # pending Medium submission excluded from by_category
        assert "Hard" not in text  # rejected Hard submission excluded from by_category
        assert callback.answers  # callback.answer() was called

    asyncio.run(go())


def test_show_game_stats_by_category_empty_when_no_approvals_yet(tmp_path):
    _db_ready(tmp_path)

    async def go():
        t1 = await _make_task(category="Light")
        await db.create_submission(t1, DELEGATE_A, "text", "a", "2026-08-14 10:00:00")  # pending

        callback = FakeCallback("admin_game_stats")
        await admin_mod.show_game_stats(callback)

        text = callback.message.answers_sent[-1]
        assert "Участников: 1" in text
        assert "пока нет одобренных сдач" in text

    asyncio.run(go())


def test_show_game_stats_handles_zero_submissions_without_division_by_zero(tmp_path):
    # CLAUDE.md (13.08, «бот для людей, не для прогеров»): a genuinely empty database must read
    # as a plain sentence for the manager, not a table of zeroes -- CLAUDE.md takes precedence
    # over the plan's literal "shows zero values as text" phrasing (documented as a deviation
    # in the plan's SUMMARY.md). The behavioral guarantee this test actually protects -- the
    # screen never crashes on an empty DB -- is unchanged.
    _db_ready(tmp_path)

    async def go():
        callback = FakeCallback("admin_game_stats")
        await admin_mod.show_game_stats(callback)  # must not raise

        text = callback.message.answers_sent[-1]
        assert "Пока никто ничего не сдавал" in text
        assert callback.answers

    asyncio.run(go())
