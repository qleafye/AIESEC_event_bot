"""Квик 260914-rgr (RGR-01..07), задача 2: периодическая сверка состава + экран «💬 Чат».

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


class _FakeMsg:
    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text


class FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _FakeMsg()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


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


# ── экран «💬 Чат» ────────────────────────────────────────────────────────────────────────

def test_render_chat_screen_without_bindings_gives_instruction(tmp_path):
    _ready(tmp_path)
    from handlers import admin_chat

    text, _kb = asyncio.run(admin_chat.render_chat_screen(ADMIN_ID))

    assert "Добавьте бота в группу делегатов" in text


def test_render_chat_screen_with_binding_shows_four_matching_numbers(tmp_path):
    _ready(tmp_path)
    from handlers import admin_chat

    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))
    ids = asyncio.run(_seed_approved(3))
    asyncio.run(db.upsert_chat_member(CHAT_ID, ids[0], "member", source="test"))
    asyncio.run(db.upsert_chat_member(CHAT_ID, 999999, "member", source="test"))  # не зарегистрирован

    text, _kb = asyncio.run(admin_chat.render_chat_screen(ADMIN_ID))
    counts = asyncio.run(db.chat_counts(CHAT_ID, None))
    line = (
        f"одобрено {counts['approved']} · в чате {counts['in_chat']} · "
        f"не в чате {counts['not_in_chat']} · в чате, но не зарегистрированы "
        f"{counts['unknown_members']}"
    )
    assert line in text
    assert counts == {"approved": 3, "in_chat": 1, "not_in_chat": 2, "unknown_members": 1}


def test_broadcast_button_hidden_without_broadcast_capability(tmp_path):
    _ready(tmp_path)
    from handlers import admin_chat

    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))

    _text, kb_stranger = asyncio.run(admin_chat.render_chat_screen(STRANGER_ID))
    flat_stranger = [btn.callback_data for row in kb_stranger.inline_keyboard for btn in row]
    assert not any(cd.startswith("chat_broadcast_out") for cd in flat_stranger)

    _text2, kb_admin = asyncio.run(admin_chat.render_chat_screen(ADMIN_ID))
    flat_admin = [btn.callback_data for row in kb_admin.inline_keyboard for btn in row]
    assert any(cd.startswith("chat_broadcast_out") for cd in flat_admin)


def test_chat_unbind_go_deletes_keys_and_rows_of_three_tables(tmp_path):
    _ready(tmp_path)
    from handlers import admin_chat

    asyncio.run(chat_tracking.bind_chat(ADMIN_ID, CHAT_ID, "Делегаты", None))
    asyncio.run(db.upsert_chat_member(CHAT_ID, 111, "member", source="test"))
    asyncio.run(db.log_chat_event(CHAT_ID, 111, "join"))
    asyncio.run(db.bump_chat_activity(CHAT_ID, 111, reply=False, media=False))

    cb = FakeCallback("chat_unbind_go:global", ADMIN_ID)
    asyncio.run(admin_chat.chat_unbind_go(cb))

    assert asyncio.run(chat_tracking.bound_chats()) == []

    async def _counts():
        out = {}
        async with db._connect() as conn:
            for table in ("chat_members", "chat_activity", "chat_events"):
                async with conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE chat_id = ?", (CHAT_ID,),
                ) as cursor:
                    out[table] = (await cursor.fetchone())[0]
        return out

    assert asyncio.run(_counts()) == {"chat_members": 0, "chat_activity": 0, "chat_events": 0}


def test_new_chat_screen_callbacks_are_registered_in_admin_caps():
    from handlers.admin_caps import ADMIN_CAPS

    for callback_data in (
        "admin_chat", "chat_chat_tracking_toggle", "chat_refresh_now",
        "chat_unbind:*", "chat_unbind_go:*",
    ):
        assert callback_data in ADMIN_CAPS
