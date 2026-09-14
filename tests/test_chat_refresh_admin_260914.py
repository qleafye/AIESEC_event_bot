"""Квик 260914-rgr (RGR-01..07), задача 2: периодическая сверка состава.

Правка 15.09 (владелец, «привязка через личку админа»): экран «💬 Чат» снесён целиком
(`handlers/admin_chat.py` удалён) — тесты того экрана ниже удалены вместе с ним. Сверка
состава (`chat_tracking.refresh_chat`/`refresh_all_chats`) и джоба планировщика — не
затронуты: манула «🔄 Сверить сейчас» больше нет, но плановая джоба и её тумблер остаются.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
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
    def __init__(self, fail_ids=(), status_for=None):
        self.calls: list[tuple] = []
        self.sent: list[tuple] = []
        self.fail_ids = set(fail_ids)
        self.status_for = status_for or {}

    async def get_chat_member(self, chat_id, telegram_id):
        self.calls.append((chat_id, telegram_id))
        if telegram_id in self.fail_ids:
            raise RuntimeError("boom")
        status = self.status_for.get(telegram_id, "member")
        return SimpleNamespace(status=status)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append((chat_id, text))


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
