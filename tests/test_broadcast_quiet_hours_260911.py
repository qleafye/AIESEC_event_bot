"""Quick 260911-805 (W4-01): мгновенная рассылка честно предупреждает о тихих часах.

УАТ-находка ночи 10-11.09: гейт тихих часов стоял только на отложенной ветке
(`broadcast_schedule_when`), а мгновенная рассылка (`_send_confirm_prompt` — общий хвост и
для одного сообщения, и для альбома) уходила получателям ночью без единого предупреждения.

Перенос на конец окна НЕ делаем (D-01 плана): мгновенная ветка уже разрешила аудиторию в
список id (`bc_users`) и не умеет сохранить альбом обратно в `filter_spec`. Фикс — только
честное предупреждение с явным «всё равно сейчас», callback `bc_go` не меняется.

Стиль и фейки — `tests/test_broadcast_confirm_stop_260910.py` (FakeBot/FakeState/FakeMessage,
`asyncio.run`, `config.DB_PATH` в tmp_path). Значения тихих часов — через `db.set_setting`,
«сейчас» — монкипатчем `_now_moscow_naive` в модуле `handlers.admin_broadcasts` (та же манера,
что `tests/test_quiet_hours_260904.py::test_broadcast_schedule_*`).
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_broadcasts
from handlers.states import Broadcast

ADMIN_ID = 900920


def _ready(tmp_path, name="quiet_confirm.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


def _patch_audience(monkeypatch, ids):
    async def fake_all():
        return list(ids)
    monkeypatch.setattr(admin_broadcasts, "get_all_users_ids", fake_all)


class FakeSentMessage:
    def __init__(self, message_id, text=None, reply_markup=None):
        self.message_id = message_id
        self.text = text
        self.markup = reply_markup

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


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


class FakeBot:
    def __init__(self):
        self.sent_messages: list[FakeSentMessage] = []
        self._next_id = 6000

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        sent = FakeSentMessage(self._next_id, text, reply_markup)
        self.sent_messages.append(sent)
        return sent


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


def _set_now(monkeypatch, dt: datetime):
    monkeypatch.setattr(admin_broadcasts, "_now_moscow_naive", lambda: dt)


# ── Паритет: тумблер выключен -> сегодняшний экран байт-в-байт ─────────────────────────────

def test_toggle_off_confirm_screen_is_byte_for_byte_today(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")

        await admin_broadcasts.process_broadcast(msg, state, bot)

        assert len(bot.sent_messages) == 1
        prompt = bot.sent_messages[0]
        assert prompt.text == "Отправить это 3 пользователям?"
        assert _btn_texts(prompt.markup) == ["✅ Отправить 3 пользователям", "❌ Отмена"]
        assert _cb_datas(prompt.markup) == ["bc_go", "bc_no"]
        assert state.state == Broadcast.confirm

    asyncio.run(go())


# ── Тумблер включён, «сейчас» ВНЕ окна -> тот же байт-в-байт экран (паритет) ───────────────

def test_toggle_on_but_outside_window_is_byte_for_byte_today(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2, 3])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 12, 0))

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")

        await admin_broadcasts.process_broadcast(msg, state, bot)

        prompt = bot.sent_messages[0]
        assert prompt.text == "Отправить это 3 пользователям?"
        assert _btn_texts(prompt.markup) == ["✅ Отправить 3 пользователям", "❌ Отмена"]
        assert _cb_datas(prompt.markup) == ["bc_go", "bc_no"]
        assert state.state == Broadcast.confirm

    asyncio.run(go())


# ── Тумблер включён, «сейчас» В окне -> честное предупреждение ────────────────────────────

def test_toggle_on_inside_window_warns_and_keeps_bc_go(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2, 3])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 23, 30))

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")

        await admin_broadcasts.process_broadcast(msg, state, bot)

        prompt = bot.sent_messages[0]
        assert "22:00" in prompt.text and "09:00" in prompt.text
        assert "09:00" in prompt.text  # конец тишины назван
        assert "тишину не ждёт" in prompt.text
        assert "🕓 Запланировать" in prompt.text
        texts = _btn_texts(prompt.markup)
        assert any("Всё равно отправить сейчас" in t for t in texts)
        assert "❌ Отмена" in texts
        assert _cb_datas(prompt.markup) == ["bc_go", "bc_no"]
        assert state.state == Broadcast.confirm

    asyncio.run(go())


def test_window_through_midnight_0200_still_warns(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 2, 0))

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        prompt = bot.sent_messages[0]
        assert any("Всё равно отправить сейчас" in t for t in _btn_texts(prompt.markup))

    asyncio.run(go())


def test_window_edge_exactly_end_time_no_warning(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 9, 0))

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        prompt = bot.sent_messages[0]
        assert prompt.text == "Отправить это 1 пользователям?"
        assert _btn_texts(prompt.markup) == ["✅ Отправить 1 пользователям", "❌ Отмена"]

    asyncio.run(go())


def test_start_equals_end_no_window_no_warning(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "10:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "10:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 10, 0))

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Всем привет")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        prompt = bot.sent_messages[0]
        assert prompt.text == "Отправить это 1 пользователям?"

    asyncio.run(go())


# ── Альбомная ветка получает предупреждение той же точкой ─────────────────────────────────

def test_album_branch_gets_same_warning(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2])
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))
    _set_now(monkeypatch, datetime(2026, 9, 11, 23, 0))

    async def _noop(_seconds):
        return None
    monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()

        async def fake_send_media_group(chat_id, media):
            return [SimpleNamespace(message_id=1)]
        bot.send_media_group = fake_send_media_group

        mgid = "mg-260911"
        msg = FakeMessage(chat_id=ADMIN_ID, message_id=100, user_id=ADMIN_ID)
        msg.media_group_id = mgid
        msg.photo = [SimpleNamespace(file_id="file0")]

        admin_broadcasts.pending_albums[mgid] = {"messages": [msg]}
        await admin_broadcasts._collect_album_and_preview(mgid, [1, 2], bot, state, ADMIN_ID)

        prompt = bot.sent_messages[0]
        assert any("Всё равно отправить сейчас" in t for t in _btn_texts(prompt.markup))
        assert _cb_datas(prompt.markup) == ["bc_go", "bc_no"]

    asyncio.run(go())


# ── bc_go после предупреждения работает как обычно (гейт не меняет доставку) ──────────────

def test_bc_go_after_warning_still_delivers(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(db.set_setting("quiet_hours_enabled", "on"))
    asyncio.run(db.set_setting("quiet_hours_start", "22:00"))
    asyncio.run(db.set_setting("quiet_hours_end", "09:00"))

    async def _noop(_seconds):
        return None
    monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)
    from services import broadcast_run as br
    monkeypatch.setattr(br.asyncio, "sleep", _noop)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2, 3], bc_preview="hi всем",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "🌙 Сейчас тихие часы...")
        bot = FakeBot()

        class FakeCallback:
            def __init__(self, data, user_id, message):
                self.data = data
                self.from_user = FakeUser(user_id)
                self.message = message

            async def answer(self, text=None, show_alert=False):
                pass

        cb = FakeCallback("bc_go", ADMIN_ID, confirm_msg)

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        assert len(spawned) == 1
        await spawned[0]

        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 1
        assert rows[0]["total"] == 3

    asyncio.run(go())
