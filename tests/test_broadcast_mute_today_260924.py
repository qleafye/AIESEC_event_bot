"""Форум-ночь п.7 (D-XX, «🔕 Не присылать сегодня»): делегат отключает НЕважные рассылки до
конца сегодняшнего дня (МСК).

Кнопка появляется отдельным сообщением ПОСЛЕ доставленной неважной рассылки, и только в день
форума города получателя (`services.scheduler.offer_mute_today_if_forum_day`) — иначе кнопка
лишняя (D-XX). Заглушка — колонка `users.mute_broadcasts_until` (MSK-дата), проверяется при
КАЖДОЙ доставке (`database.db.get_muted_today_ids`), а не единожды на постановке. Важные
рассылки игнорируют заглушку полностью (D-XX: важное приходит всегда).

pytest-asyncio недоступен — каждый async вызов через `asyncio.run()`, БД — `tmp_path`
(конвенция `tests/_dbtpl.py::fast_init_db`).
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_broadcasts, user_actions as ua
from handlers.states import Broadcast
from services import scheduler as sched
from tests._dbtpl import fast_init_db

ADMIN_ID = 900940
DELEGATE_ID = 900941
DELEGATE2_ID = 900942


def _ready(tmp_path, name="mute_today.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)
    from services import broadcast_run as br
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


async def _add_delegate(uid, event_city=None):
    await db.add_user({
        "telegram_id": uid, "full_name": f"Delegate {uid}",
        "registration_date": "2026-09-24 00:00:00", "event_city": event_city,
    })


# ── Литерал callback_data совпадает с константами services.scheduler ───────────────────────

def test_mute_callback_literals_match_scheduler():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ua))
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
            "mute_broadcasts_today", "unmute_broadcasts_today",
        ):
            for deco in node.decorator_list:
                seen[node.name] = ast.unparse(deco)
    assert sched.MUTE_TODAY_CALLBACK in seen["mute_broadcasts_today"]
    assert sched.UNMUTE_TODAY_CALLBACK in seen["unmute_broadcasts_today"]


# ── Тап делегата: заглушка ставится/снимается, ответ подтверждает ──────────────────────────

class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeSentMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.chat = FakeChat(0)

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.message = FakeSentMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_mute_broadcasts_today_sets_column_and_confirms(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(ua, "msk_now", lambda: datetime(2026, 9, 24, 15, 0))
        cb = FakeCallback(DELEGATE_ID)
        await ua.mute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] == "2026-09-24"
        assert "только важное" in cb.message.text
        assert cb.message.markup.inline_keyboard[0][0].callback_data == "bc_unmute_today"

    asyncio.run(go())


def test_unmute_broadcasts_today_clears_column(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        await _add_delegate(DELEGATE_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        cb = FakeCallback(DELEGATE_ID)
        await ua.unmute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] is None
        assert "все рассылки" in cb.message.text

    asyncio.run(go())


# ── database.db: get_muted_today_ids / set_broadcast_mute ──────────────────────────────────

def test_get_muted_today_ids_only_matches_exact_date(tmp_path):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        await _add_delegate(DELEGATE2_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        await db.set_broadcast_mute(DELEGATE2_ID, "2026-09-23")

        assert await db.get_muted_today_ids("2026-09-24") == {DELEGATE_ID}
        assert await db.get_muted_today_ids("2026-09-23") == {DELEGATE2_ID}
        assert await db.get_muted_today_ids("2026-09-25") == set()

    asyncio.run(go())


# ── Гейт кнопки: только в день форума города получателя ────────────────────────────────────

def test_offer_mute_today_gate_true_on_forum_day(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        await db.set_setting("forum_date", "24.09.2026")
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        assert await sched.offer_mute_today_if_forum_day(DELEGATE_ID) is True

    asyncio.run(go())


def test_offer_mute_today_gate_false_other_day(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        await db.set_setting("forum_date", "03.10.2026")
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        assert await sched.offer_mute_today_if_forum_day(DELEGATE_ID) is False

    asyncio.run(go())


def test_offer_mute_today_gate_false_no_forum_date(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        assert await sched.offer_mute_today_if_forum_day(DELEGATE_ID) is False

    asyncio.run(go())


def test_send_mute_offer_if_eligible_important_never_offers(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        await db.set_setting("forum_date", "24.09.2026")
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        class _B:
            async def send_message(self, *a, **k):
                raise AssertionError("important broadcast must never offer mute")

        assert await sched.send_mute_offer_if_eligible(_B(), DELEGATE_ID, True) is None

    asyncio.run(go())


# ── Интеграция: важная рассылка доходит муженным, неважная — нет, отчёт считает N ──────────

class FakeBroadcastBot:
    def __init__(self):
        self.copy_calls = []
        self.send_calls = []
        self._next_id = 8000

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copy_calls.append(chat_id)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.send_calls.append((chat_id, text))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


class FakeState:
    def __init__(self, **data):
        self._data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self._data = {}
        self.state = None


class FakeSentMessage2:
    def __init__(self):
        self.text = None
        self.markup = None
        self.chat = FakeChat(ADMIN_ID)

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback2:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeSentMessage2()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_bc_go_non_important_skips_muted_recipient_and_reports(tmp_path, monkeypatch):
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await _add_delegate(DELEGATE_ID)
        await _add_delegate(DELEGATE2_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        monkeypatch.setattr(admin_broadcasts, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 15, 0))

        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=1, bc_users=[DELEGATE_ID, DELEGATE2_ID],
            bc_preview="Обычная новость",
        )
        await state.set_state(Broadcast.confirm)
        cb = FakeCallback2("bc_go", ADMIN_ID)
        bot = FakeBroadcastBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        assert bot.copy_calls == [DELEGATE2_ID]  # DELEGATE_ID (замьючен) пропущен
        rows = await db.list_recent_broadcasts(10)
        row = rows[0]
        assert row["total"] == 1
        assert row["mute_skipped"] == 1
        text, _kb = admin_broadcasts._broadcast_card(row)
        assert "🔕 Не отправлено (выключили уведомления на сегодня): 1" in text

    asyncio.run(go())


def test_bc_go_important_reaches_muted_recipient(tmp_path, monkeypatch):
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        await _add_delegate(DELEGATE_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        monkeypatch.setattr(admin_broadcasts, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 15, 0))

        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=1, bc_users=[DELEGATE_ID],
            bc_preview="Важная новость", bc_important=True,
        )
        await state.set_state(Broadcast.confirm)
        cb = FakeCallback2("bc_go", ADMIN_ID)
        bot = FakeBroadcastBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        assert bot.copy_calls == [DELEGATE_ID]  # важная рассылка игнорирует заглушку
        rows = await db.list_recent_broadcasts(10)
        assert rows[0]["mute_skipped"] == 0

    asyncio.run(go())


def test_scheduled_broadcast_non_important_skips_muted(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "sched_mute.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)

    async def fake_all():
        return [DELEGATE_ID, DELEGATE2_ID]

    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        await _add_delegate(DELEGATE2_ID)
        monkeypatch.setattr(db, "get_all_users_ids", fake_all)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-01-01")
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 1, 1, 9, 0))

        bid = await db.create_scheduled_broadcast(
            "Обычная отложенная", None, None, "2026-01-01 10:00:00", created_by=1,
        )

        class _B:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append(chat_id)
                return SimpleNamespace(message_id=999)

        bot = _B()
        prev = sched._bot
        sched._bot = bot
        try:
            await sched.send_scheduled_broadcast(bid)
        finally:
            sched._bot = prev

        assert bot.sent == [DELEGATE2_ID]
        row = await db.get_scheduled_broadcast(bid)
        log_row = await db.get_broadcast(row["log_broadcast_id"])
        assert log_row["total"] == 1
        assert log_row["mute_skipped"] == 1

    asyncio.run(go())
