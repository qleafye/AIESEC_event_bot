"""Форум-ночь п.7 (D-XX, «❗ Важное»): пометка рассылки как важной.

Тумблер «❗ Отметить как важное» на экране подтверждения (по умолчанию выключен) — и для
мгновенной рассылки (`handlers/admin_broadcasts.py::bc_important_toggle`/`bc_go`), и для
отложенной (`sched_important_toggle`/`sched_go`, отдельный экран `Broadcast.schedule_confirm`,
т.к. до этой правки `broadcast_schedule_message` создавала строку сразу, без подтверждения).
Помеченная важной рассылка получает ОТДЕЛЬНОЕ сообщение-маркер (текст из реестра
`important_broadcast_label`) ПЕРЕД основным содержимым — `bot.copy_message` не умеет подменить
текст чисто текстового сообщения, а `bot.send_media_group` не принимает `reply_markup` вовсе,
поэтому маркер не встроен в основное сообщение (см. докстринг `services/scheduler.py` над
`send_important_marker`).

Стиль и фейки — `tests/test_broadcast_confirm_stop_260910.py` (FakeBot/FakeState/FakeMessage/
FakeCallback, `asyncio.run`, `config.DB_PATH` в tmp_path) для мгновенной ветки;
`tests/test_broadcast_checkpointing_260819.py` (`_Bot`/`_run_send`) для отложенной.
"""
import asyncio
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_broadcasts
from handlers.states import Broadcast
from services import scheduler as sched
from tests._dbtpl import fast_init_db

ADMIN_ID = 900930


def _ready(tmp_path, name="important.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)
    from services import broadcast_run as br
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeSentMessage:
    def __init__(self, message_id, text=None, reply_markup=None, chat_id=ADMIN_ID):
        self.message_id = message_id
        self.text = text
        self.markup = reply_markup
        self.edits = []
        self.chat = FakeChat(chat_id)

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edits.append((text, reply_markup))

    async def delete(self):
        pass


class FakeMessage:
    def __init__(self, *, chat_id, message_id=1, text=None, user_id=None):
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id if user_id is not None else chat_id)
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.html_text = text
        self.media_group_id = None
        self.photo = None
        self.video = None
        self.document = None
        self.audio = None
        self.copy_calls = []

    async def send_copy(self, chat_id):
        self.copy_calls.append(chat_id)
        return SimpleNamespace(message_id=999)


class FakeCallback:
    def __init__(self, data, *, user_id, message):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent_messages: list[FakeSentMessage] = []
        self.send_calls: list[tuple] = []  # (chat_id, text) -- порядок вызовов send_message
        self.copy_calls: list[int] = []
        self._next_id = 5000

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        sent = FakeSentMessage(self._next_id, text, reply_markup)
        self.sent_messages.append(sent)
        self.send_calls.append((chat_id, text))
        return sent

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copy_calls.append(chat_id)
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
        return dict(self._data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self._data = {}
        self.state = None


def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _cb_datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ── Тумблер на экране подтверждения ─────────────────────────────────────────────────────────

def test_important_toggle_off_by_default(tmp_path, monkeypatch):
    """D-01: тумблер по умолчанию выключен -- первая карточка подтверждения несёт
    «❗ Отметить как важное», не «✅ Отмечено»."""
    _ready(tmp_path)

    async def fake_all():
        return [1, 2]
    monkeypatch.setattr(admin_broadcasts, "get_all_users_ids", fake_all)

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Привет")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        prompt = bot.sent_messages[0]
        assert "❗ Отметить как важное" in _btn_texts(prompt.markup)
        assert (await state.get_data()).get("bc_important") is None

    asyncio.run(go())


def test_bc_important_toggle_flips_flag_and_label(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        state = FakeState(bc_users=[1, 2, 3], bc_important=False)
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 3 пользователям?")
        bot = FakeBot()
        cb = FakeCallback("bc_important_toggle", user_id=ADMIN_ID, message=confirm_msg)

        await admin_broadcasts.bc_important_toggle(cb, state, bot)
        assert (await state.get_data())["bc_important"] is True
        assert "✅ Отмечено как важное" in _btn_texts(bot.sent_messages[-1].markup)

        confirm_msg2 = bot.sent_messages[-1]
        cb2 = FakeCallback("bc_important_toggle", user_id=ADMIN_ID, message=confirm_msg2)
        await admin_broadcasts.bc_important_toggle(cb2, state, bot)
        assert (await state.get_data())["bc_important"] is False
        assert "❗ Отметить как важное" in _btn_texts(bot.sent_messages[-1].markup)

    asyncio.run(go())


# ── Мгновенная рассылка: маркер перед содержимым, флаг в журнале ───────────────────────────

def test_bc_go_important_sends_marker_before_content(tmp_path, monkeypatch):
    _ready(tmp_path)
    _fast_sleep(monkeypatch)
    asyncio.run(db.set_setting("important_broadcast_label", "❗ Срочно"))

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2], bc_preview="Важная новость",
            bc_important=True,
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 2 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        # Каждому получателю сначала маркер (send_message), затем содержимое (copy_message).
        marker_calls = [c for c in bot.send_calls if c[1] == "❗ Срочно"]
        assert len(marker_calls) == 2
        assert sorted(cid for cid, _ in marker_calls) == [1, 2]
        assert sorted(bot.copy_calls) == [1, 2]

        rows = await db.list_recent_broadcasts(10)
        assert rows[0]["important"] == 1
        assert rows[0]["full_text"] == "Важная новость"

    asyncio.run(go())


def test_bc_go_not_important_no_marker(tmp_path, monkeypatch):
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2], bc_preview="Обычная новость",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 2 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        # Единственные send_message — экран подтверждения/прогресса самому менеджеру, не
        # получателям (mute-предложение молчит: в тестовой БД forum_date не задан).
        assert bot.send_calls == []
        assert sorted(bot.copy_calls) == [1, 2]

        rows = await db.list_recent_broadcasts(10)
        assert rows[0]["important"] == 0
        assert rows[0]["full_text"] == "Обычная новость"

    asyncio.run(go())


# ── Карточка рассылки: пометка важности + строка отчёта mute_skipped ───────────────────────

def test_broadcast_card_shows_important_prefix_and_mute_skipped():
    row = {
        "id": 7, "admin_id": ADMIN_ID, "text_preview": "hi", "started_at": "2026-09-24 10:00:00",
        "status": "done", "total": 5, "delivered": 3, "blocked": 0, "important": 1,
        "mute_skipped": 2,
    }
    text, _kb = admin_broadcasts._broadcast_card(row)
    assert text.startswith("❗ #7")
    assert "🔕 Не отправлено (выключили уведомления на сегодня): 2" in text


def test_broadcast_card_no_extra_lines_when_not_important_and_nothing_muted():
    row = {
        "id": 8, "admin_id": ADMIN_ID, "text_preview": "hi", "started_at": "2026-09-24 10:00:00",
        "status": "done", "total": 5, "delivered": 5, "blocked": 0, "important": 0,
        "mute_skipped": 0,
    }
    text, _kb = admin_broadcasts._broadcast_card(row)
    assert text.startswith("#8")
    assert "🔕" not in text


# ── Отложенная рассылка: флаг переживает создание -> отправку, маркер уходит ───────────────

class _SchedBot:
    def __init__(self):
        self.sent: list[tuple] = []
        self._next_id = 7000

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


def _isolate_scheduler(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "sched_important.db")
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


async def _run_send(bid, bot):
    prev = sched._bot
    sched._bot = bot
    try:
        await sched.send_scheduled_broadcast(bid)
    finally:
        sched._bot = prev


def test_scheduled_broadcast_important_flag_persists_and_sends_marker(tmp_path, monkeypatch):
    _isolate_scheduler(tmp_path, monkeypatch)

    async def fake_all():
        return [11, 12]

    async def go():
        fast_init_db()
        monkeypatch.setattr(db, "get_all_users_ids", fake_all)
        await db.set_setting("important_broadcast_label", "❗ Важно")

        bid = await db.create_scheduled_broadcast(
            "Отложенная важная новость", None, None, "2026-01-01 10:00:00", created_by=1,
            important=True,
        )
        bot = _SchedBot()
        await _run_send(bid, bot)

        markers = [t for _cid, t in bot.sent if t == "❗ Важно"]
        assert len(markers) == 2
        content = [t for _cid, t in bot.sent if t == "Отложенная важная новость"]
        assert len(content) == 2

        row = await db.get_scheduled_broadcast(bid)
        log_row = await db.get_broadcast(row["log_broadcast_id"])
        assert log_row["important"] == 1
        assert log_row["full_text"] == "Отложенная важная новость"

    asyncio.run(go())


def test_scheduled_broadcast_not_important_defaults_to_zero(tmp_path, monkeypatch):
    _isolate_scheduler(tmp_path, monkeypatch)

    async def fake_all():
        return [21]

    async def go():
        fast_init_db()
        monkeypatch.setattr(db, "get_all_users_ids", fake_all)
        bid = await db.create_scheduled_broadcast(
            "Обычная отложенная новость", None, None, "2026-01-01 10:00:00", created_by=1,
        )
        bot = _SchedBot()
        await _run_send(bid, bot)

        assert bot.sent == [(21, "Обычная отложенная новость")]
        row = await db.get_scheduled_broadcast(bid)
        log_row = await db.get_broadcast(row["log_broadcast_id"])
        assert log_row["important"] == 0

    asyncio.run(go())
