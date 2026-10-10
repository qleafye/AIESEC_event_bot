"""Форум-ночь п.7 (D-XX, «❗ Важное»): пометка важной рассылки.

Переделка (ревью 470ce5e..3703ba4): пометка важности БОЛЬШЕ НЕ уходит отдельным
сообщением-маркером — она встраивается ПЕРВОЙ строкой в само содержимое (текст через
`bot.send_message`, подпись медиа через `caption=` у `bot.copy_message`; альбом — подпись
ПЕРВОГО элемента, `bot.send_media_group` не принимает `reply_markup` вовсе). Старая схема
(отдельный маркер ПЕРЕД содержимым) дублировалась на 429-ретрае (маркер — отдельный вызов ДО
содержимого, повтор всего `send_one` слал маркер ВТОРОЙ раз) и на crash-resume отложенной
рассылки (маркер не чекпойнтился, чекпоинт знал только про основной `send`) — обе дыры
закрыты тем, что отдельного маркера больше не существует.

Тумблер «❗ Отметить как важное» на экране подтверждения (по умолчанию выключен) — и для
мгновенной рассылки (`handlers/comms/admin_broadcasts.py::bc_important_toggle`/`bc_go`), и для
отложенной (`sched_important_toggle`/`sched_go`, отдельный экран `Broadcast.schedule_confirm`).

Стиль и фейки — `tests/test_broadcast_confirm_stop_260910.py` (FakeBot/FakeState/FakeMessage/
FakeCallback, `asyncio.run`, `config.DB_PATH` в tmp_path) для мгновенной ветки;
`tests/test_broadcast_checkpointing_260819.py` (`_Bot`/`_run_send`) для отложенной.
"""
import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter

from config import config
from database import db
from handlers.comms import admin_broadcasts
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
        self.reply_markup = None
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
        self.copy_kwargs: list[dict] = []  # (caption, reply_markup) на каждый copy_message
        self._next_id = 5000

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        sent = FakeSentMessage(self._next_id, text, reply_markup)
        self.sent_messages.append(sent)
        self.send_calls.append((chat_id, text))
        return sent

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, reply_markup=None):
        self.copy_calls.append(chat_id)
        self.copy_kwargs.append({"caption": caption, "reply_markup": reply_markup})
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


# ── process_broadcast: определение kind (text/media) для последующего bc_go ────────────────

def test_process_broadcast_text_message_sets_text_kind(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def fake_all():
        return [1]
    monkeypatch.setattr(admin_broadcasts, "get_all_users_ids", fake_all)

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, text="Чисто текстовая новость")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        data = await state.get_data()
        assert data["bc_kind"] == "text"
        assert data["bc_content_html"] == "Чисто текстовая новость"

    asyncio.run(go())


# ── Мгновенная рассылка: пометка встроена ВНУТРЬ содержимого, отдельного маркера нет ───────

def test_bc_go_important_text_embeds_prefix_single_call(tmp_path, monkeypatch):
    """Текстовая важная рассылка -- ОДИН вызов bot.send_message на получателя, пометка внутри
    текста (никакого отдельного маркера, `bot.copy_calls` пуст)."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)
    asyncio.run(db.set_setting("important_broadcast_label", "❗ Срочно"))

    async def go():
        state = FakeState(
            bc_users=[1, 2], bc_preview="Важная новость", bc_important=True,
            bc_kind="text", bc_content_html="Важная новость",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 2 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        assert bot.copy_calls == []  # текстовая рассылка НЕ идёт через copy_message
        assert sorted(bot.send_calls) == [(1, "❗ Срочно\n\nВажная новость"), (2, "❗ Срочно\n\nВажная новость")]

        rows = await db.list_recent_broadcasts(10)
        assert rows[0]["important"] == 1
        assert rows[0]["full_text"] == "Важная новость"

    asyncio.run(go())


def test_bc_go_important_media_embeds_prefix_via_caption(tmp_path, monkeypatch):
    """Фото/видео/документ с подписью -- пометка внутри CAPTION `copy_message`, ни одного
    отдельного send_message-маркера."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)
    asyncio.run(db.set_setting("important_broadcast_label", "❗ Срочно"))

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2],
            bc_preview="Подпись фото", bc_important=True,
            bc_kind="media", bc_content_html="Подпись фото",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 2 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        assert bot.send_calls == []  # ни одного отдельного маркера получателям
        assert sorted(bot.copy_calls) == [1, 2]
        assert all(k["caption"] == "❗ Срочно\n\nПодпись фото" for k in bot.copy_kwargs)

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


def test_bc_go_not_important_media_caption_passthrough_none(tmp_path, monkeypatch):
    """Неважная медиа-рассылка -- `caption=None` в copy_message: Telegram сохраняет исходную
    подпись байт-в-байт, бот её не трогает вовсе (никакого риска расхождения форматирования на
    самой частой ветке)."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1],
            bc_preview="Подпись фото", bc_kind="media", bc_content_html="Подпись фото",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 1 пользователю?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        # D-30: «🔕 Не присылать сегодня» доступна весь сезон — кнопка висит на любой
        # неважной рассылке; подпись при этом по-прежнему не трогаем (caption=None).
        assert len(bot.copy_kwargs) == 1
        assert bot.copy_kwargs[0]["caption"] is None
        markup = bot.copy_kwargs[0]["reply_markup"]
        assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["bc_mute_today"]

    asyncio.run(go())


def test_bc_go_important_retry_after_does_not_duplicate_marker(tmp_path, monkeypatch):
    """Находка ревью (🟡): 429-ретрай раньше слал ВТОРОЙ маркер (отдельный вызов ДО содержимого,
    ретрай повторял весь `send_one`). Теперь пометка встроена в содержимое -- ретрай просто
    повторяет ОДИН И ТОТ ЖЕ combined-вызов, итог -- ровно одно доставленное сообщение."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)
    asyncio.run(db.set_setting("important_broadcast_label", "❗ Срочно"))

    async def go():
        state = FakeState(
            bc_users=[1], bc_preview="Важная новость", bc_important=True,
            bc_kind="text", bc_content_html="Важная новость",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 1 пользователю?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)

        class RetryOnceBot(FakeBot):
            def __init__(self):
                super().__init__()
                self._raised = False

            async def send_message(self, chat_id, text, reply_markup=None):
                if not self._raised:
                    self._raised = True
                    raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
                return await super().send_message(chat_id, text, reply_markup=reply_markup)

        bot = RetryOnceBot()
        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        await spawned[0]

        # Ровно ОДНО итоговое сообщение — не маркер+контент по отдельности, не задвоенный маркер.
        assert bot.send_calls == [(1, "❗ Срочно\n\nВажная новость")]
        rows = await db.list_recent_broadcasts(10)
        assert rows[0]["delivered"] == 1

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


# ── Отложенная рассылка: флаг переживает создание -> отправку, пометка встроена ────────────

class _SchedBot:
    def __init__(self):
        self.sent: list[tuple] = []
        self._next_id = 7000

    async def send_message(self, chat_id, text, reply_markup=None):
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


def test_scheduled_broadcast_important_embeds_prefix_single_call_per_recipient(tmp_path, monkeypatch):
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

        # РОВНО один вызов send_message на получателя (пометка внутри текста, не отдельный
        # маркер) — прежняя схема слала маркер+содержимое = 2 вызова на получателя.
        assert sorted(bot.sent) == [
            (11, "❗ Важно\n\nОтложенная важная новость"),
            (12, "❗ Важно\n\nОтложенная важная новость"),
        ]

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


def test_scheduled_broadcast_resume_does_not_duplicate_marker(tmp_path, monkeypatch):
    """Находка ревью (🟡): crash-resume раньше мог повторно слать маркер, если основной `send`
    успел уйти, а маркер — нет (или наоборот), потому что чекпоинт (`mark_delivery`) знал
    только про основной send. Пометка теперь ВНУТРИ основного текста — чекпоинт по chat_id
    покрывает её автоматически: получатель, уже отмеченный доставленным, не получает НИ
    содержимого, НИ пометки повторно на resume."""
    _isolate_scheduler(tmp_path, monkeypatch)

    async def fake_all():
        return [31, 32]

    async def go():
        fast_init_db()
        monkeypatch.setattr(db, "get_all_users_ids", fake_all)
        await db.set_setting("important_broadcast_label", "❗ Важно")
        bid = await db.create_scheduled_broadcast(
            "Крашнутая важная новость", None, None, "2026-01-01 10:00:00", created_by=1,
            important=True,
        )

        # Первый прогон "падает" после доставки первому получателю.
        real_mark = db.mark_delivery
        calls = {"n": 0}

        async def mark_then_crash(b, c, ok):
            await real_mark(b, c, ok)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("process died")
        monkeypatch.setattr(db, "mark_delivery", mark_then_crash)

        bot1 = _SchedBot()
        await _run_send(bid, bot1)
        assert bot1.sent == [(31, "❗ Важно\n\nКрашнутая важная новость")]
        assert await db.list_delivered_chat_ids(bid) == {31}

        # Резюм: 31 не получает ничего повторно (ни маркера, ни содержимого), 32 -- ровно раз.
        monkeypatch.setattr(db, "mark_delivery", real_mark)
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE scheduled_broadcasts SET status = 'pending' WHERE id = ?", (bid,)
            )
            await conn.commit()
        bot2 = _SchedBot()
        await _run_send(bid, bot2)
        assert bot2.sent == [(32, "❗ Важно\n\nКрашнутая важная новость")]

    asyncio.run(go())
