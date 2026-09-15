"""Тесты инструмента рассылки восстановления резюме (инцидент 05-10.09.2026, `ef315f9`).

Стиль — по образцу `tests/test_requeue_auto_approved_260913.py`: временная БД через
`config.DB_PATH`, прямые `INSERT`/`UPDATE` через `aiosqlite` (не `db.add_user` — 66 колонок в
этом файле не нужны), async-логика внутри `asyncio.run(go())` (pytest-asyncio недоступен).

`build_bot`/`run_test_to` тестируются через фейковый `bot_factory` — реальный
`aiogram.Bot`/сетевой прокси в тестах не создаётся никогда.
"""
import asyncio

import aiosqlite

from config import config
from database import db
import tools.send_resume_recovery as tool

SEASON = "YL 26/2"
WINDOW_FROM = "2026-09-05 14:30:00"
WINDOW_TO = "2026-09-11 00:30:00"


def _use_tmp_db(tmp_path, name="test_resume_recovery.db"):
    config.DB_PATH = str(tmp_path / name)


async def _insert_user(**kwargs) -> None:
    defaults = {
        "telegram_id": None,
        "username": "u",
        "full_name": "Тестовый Тестов",
        "status": "approved",
        "season": SEASON,
        "registration_date": "2026-09-06 09:00:00",
        "resume_file_id": None,
        "resume_text": None,
        "resume_url": None,
        "resume_link": None,
    }
    defaults.update(kwargs)
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username, full_name, status, season, "
            "registration_date, resume_file_id, resume_text, resume_url, resume_link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                defaults["telegram_id"],
                defaults["username"],
                defaults["full_name"],
                defaults["status"],
                defaults["season"],
                defaults["registration_date"],
                defaults["resume_file_id"],
                defaults["resume_text"],
                defaults["resume_url"],
                defaults["resume_link"],
            ),
        )
        await conn.commit()


async def _seed_base() -> None:
    """Базовый набор: A-кандидат (rejected, без резюме), B-кандидат (approved, без резюме),
    pending-кандидат, кандидат с резюме (исключается), кандидат вне окна, кандидат другого
    сезона."""
    await db.init_db()

    await _insert_user(
        telegram_id=1001, username="reject1", status="rejected",
        registration_date="2026-09-06 10:00:00",
    )
    await _insert_user(
        telegram_id=1002, username="approve1", status="approved",
        registration_date="2026-09-06 11:00:00",
    )
    await _insert_user(
        telegram_id=1003, username="pending1", status="pending",
        registration_date="2026-09-06 12:00:00",
    )
    # Резюме уже есть (resume_url) -> не должен попасть ни в один список.
    await _insert_user(
        telegram_id=1004, username="hasresume", status="rejected",
        registration_date="2026-09-06 13:00:00", resume_url="https://cloud.example/1004",
    )
    # Вне окна инцидента.
    await _insert_user(
        telegram_id=1005, username="outside_window", status="rejected",
        registration_date="2026-09-12 09:00:00",
    )
    # Другой сезон.
    await _insert_user(
        telegram_id=1006, username="other_season", status="rejected", season="YL 26/1",
        registration_date="2026-09-06 09:30:00",
    )
    # Прочерк в resume_text -> тоже считается "нет резюме" (конвенция RESUME_COLUMNS).
    await _insert_user(
        telegram_id=1007, username="dash_resume", status="approved",
        registration_date="2026-09-06 14:00:00", resume_text="-",
    )
    await db.set_setting("event_season", SEASON)


# ── Задача 1: выборка кандидатов ────────────────────────────────────────────────────────────

def test_select_candidates_picks_in_window_season_no_resume_rows(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()
        rows = await tool.select_candidates(SEASON, WINDOW_FROM, WINDOW_TO)
        ids = {r["telegram_id"] for r in rows}
        assert ids == {1001, 1002, 1003, 1007}

    asyncio.run(go())


def test_bucket_by_status_splits_rejected_approved_pending():
    rows = [
        {"telegram_id": 1, "status": "rejected"},
        {"telegram_id": 2, "status": "approved"},
        {"telegram_id": 3, "status": "pending"},
        {"telegram_id": 4, "status": "waitlist"},
    ]
    buckets = tool.bucket_by_status(rows)
    assert [r["telegram_id"] for r in buckets["A"]] == [1]
    assert [r["telegram_id"] for r in buckets["B"]] == [2]
    assert [r["telegram_id"] for r in buckets["pending"]] == [3]
    assert [r["telegram_id"] for r in buckets["other"]] == [4]


def test_delegate_with_resume_now_is_excluded(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()
        rows = await tool.select_candidates(SEASON, WINDOW_FROM, WINDOW_TO)
        ids = {r["telegram_id"] for r in rows}
        assert 1004 not in ids

    asyncio.run(go())


def test_pending_is_listed_but_not_in_a_or_b(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()
        rows = await tool.select_candidates(SEASON, WINDOW_FROM, WINDOW_TO)
        buckets = tool.bucket_by_status(rows)
        assert [r["telegram_id"] for r in buckets["pending"]] == [1003]
        a_and_b_ids = {r["telegram_id"] for r in buckets["A"] + buckets["B"]}
        assert 1003 not in a_and_b_ids

    asyncio.run(go())


def test_main_dry_run_does_not_send_pending_and_reports_expect_mismatch(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()

        async def fake_bot_factory():
            raise AssertionError("dry-run must never build a Bot")

        rc = await tool.main(
            date_from=WINDOW_FROM, date_to=WINDOW_TO, apply=False,
            bot_factory=fake_bot_factory,
        )
        assert rc == 0
        assert await db.list_recent_broadcasts(10) == []

        rc_mismatch = await tool.main(
            date_from=WINDOW_FROM, date_to=WINDOW_TO, apply=False,
            expect_a=99, bot_factory=fake_bot_factory,
        )
        assert rc_mismatch == 1

    asyncio.run(go())


# ── Задача 2: идемпотентность ────────────────────────────────────────────────────────────────

def test_idempotence_excludes_previously_delivered_ids(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()
        # Симулируем предыдущий прогон: 1001 (когорта A) уже получил письмо.
        bid = await db.create_broadcast(1, tool.PREVIEW_A, 1)
        await db.record_broadcast_delivery(bid, 1001, 555)

        sent_ids = await tool.already_sent_ids()
        assert sent_ids == {1001}

        candidates = await tool.select_candidates(SEASON, WINDOW_FROM, WINDOW_TO)
        fresh = [r for r in candidates if r["telegram_id"] not in sent_ids]
        fresh_ids = {r["telegram_id"] for r in fresh}
        assert 1001 not in fresh_ids
        assert {1002, 1003, 1007} <= fresh_ids

    asyncio.run(go())


def test_already_sent_ids_ignores_unrelated_broadcasts(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        bid = await db.create_broadcast(1, "обычная рассылка про парковку", 1)
        await db.record_broadcast_delivery(bid, 42, 1)
        assert await tool.already_sent_ids() == set()

    asyncio.run(go())


# ── Задача 3: --test-to ─────────────────────────────────────────────────────────────────────

class _FakeMe:
    username = "YouLead2026_bot"


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.session = _FakeSession()

    async def get_me(self):
        return _FakeMe()

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        from types import SimpleNamespace
        return SimpleNamespace(message_id=len(self.sent))


def test_test_to_sends_exactly_two_messages_and_creates_no_broadcast_rows(tmp_path):
    _use_tmp_db(tmp_path)
    fake_bot = _FakeBot()

    async def fake_factory():
        return fake_bot

    async def go():
        await db.init_db()
        rc = await tool.main(test_to=777, bot_factory=fake_factory)
        assert rc == 0
        assert len(fake_bot.sent) == 2
        assert [chat_id for chat_id, _ in fake_bot.sent] == [777, 777]
        assert fake_bot.session.closed is True
        assert await db.list_recent_broadcasts(10) == []

    asyncio.run(go())


def test_test_to_texts_contain_bot_username_from_get_me(tmp_path):
    _use_tmp_db(tmp_path)
    fake_bot = _FakeBot()

    async def fake_factory():
        return fake_bot

    async def go():
        await db.init_db()
        await tool.main(test_to=777, bot_factory=fake_factory)
        texts = [text for _, text in fake_bot.sent]
        assert texts[0] == tool.TEXT_A
        assert "https://t.me/YouLead2026_bot?start=edit" in texts[1]

    asyncio.run(go())


def test_render_text_b_substitutes_username():
    text = tool.render_text_b("SomeOtherBot")
    assert "https://t.me/SomeOtherBot?start=edit" in text
    assert "Спасибо! 🧡💙" in text


# ── Задача 4: тихие часы ────────────────────────────────────────────────────────────────────

def test_quiet_hours_refusal_blocks_apply_without_force(tmp_path, monkeypatch):
    from datetime import datetime

    _use_tmp_db(tmp_path)

    async def go():
        await _seed_base()
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        monkeypatch.setattr(tool, "msk_now", lambda: datetime(2026, 9, 11, 23, 30))

        async def fake_bot_factory():
            raise AssertionError("must not build a Bot when refused for quiet hours")

        rc = await tool.main(
            date_from=WINDOW_FROM, date_to=WINDOW_TO, apply=True,
            bot_factory=fake_bot_factory,
        )
        assert rc == 1
        assert await db.list_recent_broadcasts(10) == []

    asyncio.run(go())


def test_quiet_hours_force_flag_allows_send(tmp_path, monkeypatch):
    from datetime import datetime

    _use_tmp_db(tmp_path)
    fake_bot = _FakeBot()

    async def fake_factory():
        return fake_bot

    async def go():
        await _seed_base()
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        monkeypatch.setattr(tool, "msk_now", lambda: datetime(2026, 9, 11, 23, 30))

        rc = await tool.main(
            date_from=WINDOW_FROM, date_to=WINDOW_TO, apply=True,
            force_quiet_hours=True, admin_id=1, bot_factory=fake_factory,
        )
        assert rc == 0
        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 2

    asyncio.run(go())


def test_quiet_hours_outside_window_sends_normally(tmp_path, monkeypatch):
    from datetime import datetime

    _use_tmp_db(tmp_path)
    fake_bot = _FakeBot()

    async def fake_factory():
        return fake_bot

    async def go():
        await _seed_base()
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        monkeypatch.setattr(tool, "msk_now", lambda: datetime(2026, 9, 11, 12, 0))

        rc = await tool.main(
            date_from=WINDOW_FROM, date_to=WINDOW_TO, apply=True,
            admin_id=1, bot_factory=fake_factory,
        )
        assert rc == 0
        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 2

    asyncio.run(go())
