"""Форум-ночь п.7 (D-XX, «🔕 Не присылать сегодня»): делегат отключает НЕважные рассылки до
конца сегодняшнего дня (МСК).

Переделка (ревью 470ce5e..3703ba4): кнопка «🔕» теперь ВНУТРИ клавиатуры самой рассылки
(text/фото/видео/документ — `services.scheduler.recipient_markup`, `reply_markup` принимают и
`send_message`, и `copy_message`), не отдельным сообщением — раньше предложение шло после
КАЖДОЙ неважной рассылки (спам), теперь оно естественным образом не повторяется чаще самой
рассылки. Отдельным сообщением предложение осталось ТОЛЬКО у альбома (`send_media_group` не
принимает `reply_markup`) — и только раз в сутки на получателя (`users.mute_offer_shown_date`).
D-30 (24.09): кнопка доступна ВЕСЬ СЕЗОН, не только в день форума — старый гейт
`offer_mute_today_if_forum_day` убран целиком (единственное условие теперь — рассылка
неважная). Заглушка — колонка `users.mute_broadcasts_until` (MSK-дата), проверяется при
КАЖДОЙ доставке (`database.db.get_muted_today_ids`), а не единожды на постановке. Важные
рассылки игнорируют заглушку полностью (важное приходит всегда).

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
    """Переделка (ревью 470ce5e..3703ba4): кнопка «🔕» чаще всего сидит ВНУТРИ клавиатуры самой
    рассылки — тап НЕ должен стирать текст (`edit_text` затёр бы содержимое, которое делегат
    только что получил), только клавиатуру (`edit_reply_markup`). `text`/`.markup` — то, что
    реально несёт сообщение ДО тапа (симулирует уже доставленную рассылку)."""

    def __init__(self, text=None, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup
        self.markup = reply_markup  # алиас для читаемости старых ассертов на "markup"
        self.chat = FakeChat(0)
        self.edit_reply_markup_calls: list = []

    async def edit_text(self, text, reply_markup=None):
        raise AssertionError("edit_text не должен вызываться — содержимое рассылки нельзя стирать")

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup
        self.markup = reply_markup
        self.edit_reply_markup_calls.append(reply_markup)


class FakeCallback:
    def __init__(self, uid, message=None):
        self.from_user = FakeUser(uid)
        self.message = message if message is not None else FakeSentMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_mute_broadcasts_today_sets_column_and_swaps_button_not_text(tmp_path, monkeypatch):
    """Тап «🔕» на ВСТРОЕННОЙ кнопке: содержимое рассылки (`.text`) остаётся нетронутым, кнопка
    свапается на «🔔 Присылать всё» на месте, подтверждение уходит алертом (`callback.answer`),
    не правкой сообщения."""
    _ready(tmp_path)

    async def go():
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(ua, "msk_now", lambda: datetime(2026, 9, 24, 15, 0))
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        content_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔕 Не присылать сегодня", callback_data="bc_mute_today"),
        ]])
        msg = FakeSentMessage(text="Важная новость дня", reply_markup=content_markup)
        cb = FakeCallback(DELEGATE_ID, message=msg)
        await ua.mute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] == "2026-09-24"
        assert msg.text == "Важная новость дня"  # содержимое НЕ тронуто
        assert msg.reply_markup.inline_keyboard[0][0].callback_data == "bc_unmute_today"
        text, show_alert = cb.answers[0]
        assert "только важное" in text
        assert show_alert is True

    asyncio.run(go())


def test_unmute_broadcasts_today_clears_column_and_swaps_button_back(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        await _add_delegate(DELEGATE_ID)
        await db.set_broadcast_mute(DELEGATE_ID, "2026-09-24")
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        content_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔔 Присылать всё", callback_data="bc_unmute_today"),
        ]])
        msg = FakeSentMessage(text="Обычная новость", reply_markup=content_markup)
        cb = FakeCallback(DELEGATE_ID, message=msg)
        await ua.unmute_broadcasts_today(cb)

        user = await db.get_user(DELEGATE_ID)
        assert user["mute_broadcasts_until"] is None
        assert msg.text == "Обычная новость"
        assert msg.reply_markup.inline_keyboard[0][0].callback_data == "bc_mute_today"
        text, show_alert = cb.answers[0]
        assert "все рассылки" in text
        assert show_alert is True

    asyncio.run(go())


def test_mute_tap_preserves_managers_own_keyboard_rows(tmp_path, monkeypatch):
    """Собственная клавиатура менеджера (кнопки-ссылки) рядом с «🔕» — свап трогает ТОЛЬКО
    строку «🔕», остальные ряды остаются нетронутыми."""
    _ready(tmp_path)

    async def go():
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(ua, "msk_now", lambda: datetime(2026, 9, 24, 15, 0))
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        content_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подробнее", url="https://example.com")],
            [InlineKeyboardButton(text="🔕 Не присылать сегодня", callback_data="bc_mute_today")],
        ])
        msg = FakeSentMessage(text="Рассылка со своей кнопкой", reply_markup=content_markup)
        cb = FakeCallback(DELEGATE_ID, message=msg)
        await ua.mute_broadcasts_today(cb)

        rows = msg.reply_markup.inline_keyboard
        assert len(rows) == 2
        assert rows[0][0].text == "🔗 Подробнее"
        assert rows[0][0].url == "https://example.com"
        assert rows[1][0].callback_data == "bc_unmute_today"

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


# ── D-30 (24.09): кнопка доступна ВЕСЬ СЕЗОН — старый гейт «только в день форума» убран ────

def test_send_mute_offer_if_eligible_important_never_offers(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        class _B:
            async def send_message(self, *a, **k):
                raise AssertionError("important broadcast must never offer mute")

        assert await sched.send_mute_offer_if_eligible(_B(), DELEGATE_ID, True) is None

    asyncio.run(go())


# ── recipient_markup: кнопка «🔕» ВСТРОЕНА в клавиатуру рассылки (не отдельным сообщением) ─

def test_recipient_markup_adds_mute_row_any_day(tmp_path, monkeypatch):
    """D-30: кнопка добавляется для неважной рассылки в ЛЮБОЙ день сезона, не только в день
    форума — `forum_date` намеренно НЕ выставляется в этом тесте."""
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        markup = await sched.recipient_markup(DELEGATE_ID, important=False)
        assert markup is not None
        assert markup.inline_keyboard[-1][0].callback_data == sched.MUTE_TODAY_CALLBACK

    asyncio.run(go())


def test_recipient_markup_none_only_when_important(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        # Важная -- «🔕» не предлагается никогда.
        assert await sched.recipient_markup(DELEGATE_ID, important=True) is None
        # Неважная -- предлагается, даже без forum_date вовсе (D-30).
        assert await sched.recipient_markup(DELEGATE_ID, important=False) is not None

    asyncio.run(go())


def test_recipient_markup_keeps_managers_own_rows_and_appends_mute_last(tmp_path, monkeypatch):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        own = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Подробнее", url="https://example.com"),
        ]])
        markup = await sched.recipient_markup(DELEGATE_ID, important=False, base_markup=own)
        assert len(markup.inline_keyboard) == 2
        assert markup.inline_keyboard[0][0].text == "🔗 Подробнее"
        assert markup.inline_keyboard[-1][0].callback_data == sched.MUTE_TODAY_CALLBACK

    asyncio.run(go())


# ── send_mute_offer_if_eligible (альбом): не чаще раза в сутки на получателя ────────────────

def test_send_mute_offer_if_eligible_shows_once_then_skips_same_day(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)
        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))

        class _B:
            def __init__(self):
                self.calls = 0

            async def send_message(self, *a, **k):
                self.calls += 1
                return SimpleNamespace(message_id=42)

        bot = _B()
        first = await sched.send_mute_offer_if_eligible(bot, DELEGATE_ID, False)
        assert first == 42
        assert bot.calls == 1

        second = await sched.send_mute_offer_if_eligible(bot, DELEGATE_ID, False)
        assert second is None
        assert bot.calls == 1  # второй раз в тот же день — предложение НЕ отправлено повторно

    asyncio.run(go())


def test_send_mute_offer_if_eligible_shows_again_next_day(tmp_path, monkeypatch):
    async def go():
        fast_init_db()
        await _add_delegate(DELEGATE_ID)

        class _B:
            async def send_message(self, *a, **k):
                return SimpleNamespace(message_id=1)

        monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 9, 24, 10, 0))
        await sched.send_mute_offer_if_eligible(_B(), DELEGATE_ID, False)
        assert await db.get_mute_offer_shown_ids("2026-09-24") == {DELEGATE_ID}
        assert await db.get_mute_offer_shown_ids("2026-09-25") == set()

    asyncio.run(go())


# ── Интеграция: важная рассылка доходит муженным, неважная — нет, отчёт считает N ──────────

class FakeBroadcastBot:
    def __init__(self):
        self.copy_calls = []
        self.send_calls = []
        self._next_id = 8000

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, reply_markup=None):
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
