"""Quick 260910-okb (BC-05/06): отзыв рассылки у получателей + экран «Последние рассылки».

Раньше отправленное было ничем не отозвать: ни message_id, ни журнала. Эти тесты покрывают
`_broadcast_card` (единый рендер итога и строки списка), гейт 48 ч (Bot API отдаёт
delete_message только это окно) и сам отзыв через `bc_rev`/`bc_revgo`/`bc_revno`.

Стиль: прямой вызов хендлеров с фейковым state/callback (tests/test_city_broadcast_phase72.py,
tests/test_broadcast_confirm_stop_260910.py), `_spawn` перехватывается монкипатчем и довыполняется
вручную. pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_broadcasts
from services import broadcast_run as br

ADMIN_ID = 900920


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "revoke.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


def _fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


async def _seed_broadcast(admin_id, text_preview, pairs, *, started_ago_hours=1, status="done"):
    """Создаёт строку broadcasts со `started_at` в прошлом и её доставленные пары
    (chat_id, message_id), как их писал бы реальный run_broadcast."""
    bid = await db.create_broadcast(admin_id, text_preview, len(pairs))
    started_at = (datetime.now() - timedelta(hours=started_ago_hours)).strftime("%Y-%m-%d %H:%M:%S")
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE broadcasts SET started_at = ?, status = ?, delivered = ?, blocked = 0 "
            "WHERE id = ?",
            (started_at, status, len(pairs), bid),
        )
        await conn.commit()
    for chat_id, message_id in pairs:
        await db.record_broadcast_delivery(bid, chat_id, message_id)
    return bid


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeSentMessage:
    def __init__(self, message_id=1, text=None, reply_markup=None):
        self.message_id = message_id
        self.text = text
        self.markup = reply_markup
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data, *, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message or FakeSentMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeLogTarget:
    """Стенд для _render_broadcast_log: и callback.message, и «сырое» сообщение команды
    /broadcasts используют .answer() как единственный интерфейс — тот же, что у реального
    aiogram Message."""

    def __init__(self):
        self.answers_sent = []

    async def answer(self, text, reply_markup=None):
        self.answers_sent.append((text, reply_markup))


class FakeBot:
    def __init__(self, fail_pairs=()):
        self.deleted = []
        self.fail_pairs = set(fail_pairs)

    async def delete_message(self, chat_id, message_id):
        if (chat_id, message_id) in self.fail_pairs:
            raise Exception("message to delete not found")
        self.deleted.append((chat_id, message_id))


def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _cb_datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_has_revoke_button_when_younger_than_48h(tmp_path):
    """В итоге рассылки моложе 48 ч есть кнопка «🗑 Удалить у получателей»."""
    _ready(tmp_path)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100), (2, 200)], started_ago_hours=1)
        row = await db.get_broadcast(bid)
        text, kb = admin_broadcasts._broadcast_card(row)
        assert kb is not None
        assert f"bc_rev:{bid}" in _cb_datas(kb)
        assert "Удалить нельзя" not in text

    asyncio.run(go())


def test_card_has_no_button_when_older_than_48h(tmp_path):
    """Рассылка старше 48 ч: кнопки нет, в карточке строка «Удалить нельзя: прошло больше
    48 часов»."""
    _ready(tmp_path)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100)], started_ago_hours=49)
        row = await db.get_broadcast(bid)
        text, kb = admin_broadcasts._broadcast_card(row)
        assert kb is None
        assert "Удалить нельзя: прошло больше 48 часов" in text

    asyncio.run(go())


def test_bc_rev_shows_confirmation_without_deleting(tmp_path):
    """Нажатие кнопки показывает подтверждение с числом получателей и словами «Вернуть
    нельзя»; без нажатия «Да» ни одного delete_message не происходит."""
    _ready(tmp_path)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100), (2, 200), (3, 300)])
        cb = FakeCallback(f"bc_rev:{bid}")

        await admin_broadcasts.bc_rev(cb)

        assert "3" in cb.message.text
        assert "Вернуть нельзя" in cb.message.text
        assert f"bc_revgo:{bid}" in _cb_datas(cb.message.markup)
        assert "bc_revno" in _cb_datas(cb.message.markup)

    asyncio.run(go())


def test_bc_rev_refuses_when_gate_expired_even_if_button_still_shown(tmp_path):
    """Гейт 48 ч перепроверяется В ХЕНДЛЕРЕ, а не только в отрисовке — карточка могла быть
    нарисована вчера, кнопка не истекает."""
    _ready(tmp_path)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100)], started_ago_hours=49)
        cb = FakeCallback(f"bc_rev:{bid}")

        await admin_broadcasts.bc_rev(cb)

        assert cb.answers == [("Удалить нельзя: прошло больше 48 часов.", True)]
        assert cb.message.edits == []  # экран подтверждения не нарисован

    asyncio.run(go())


def test_bc_revgo_deletes_every_saved_pair_marks_revoked_and_reports_counts(tmp_path, monkeypatch):
    """После подтверждения bot.delete_message вызван по каждой сохранённой паре, статус
    рассылки — 'revoked', итог «Удалено X, не удалось Y»."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100), (2, 200), (3, 300)])
        bot = FakeBot(fail_pairs={(2, 200)})
        cb = FakeCallback(f"bc_revgo:{bid}")

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_revgo(cb, bot)
        assert len(spawned) == 1
        await spawned[0]

        assert sorted(bot.deleted) == [(1, 100), (3, 300)]
        row = await db.get_broadcast(bid)
        assert row["status"] == "revoked"
        assert "Удалено 2" in cb.message.text
        assert "не удалось 1" in cb.message.text

    asyncio.run(go())


def test_bc_revno_cancels_without_deleting(tmp_path):
    _ready(tmp_path)

    async def go():
        bid = await _seed_broadcast(ADMIN_ID, "hi", [(1, 100)])
        cb = FakeCallback("bc_revno")
        await admin_broadcasts.bc_revno(cb)
        assert cb.message.text == "Удаление отменено."
        row = await db.get_broadcast(bid)
        assert row["status"] == "done"  # отмена не трогает статус

    asyncio.run(go())


def test_broadcast_log_screen_caps_at_ten(tmp_path):
    """Экран «Последние рассылки» показывает не больше 10 записей."""
    _ready(tmp_path)

    async def go():
        for i in range(12):
            await _seed_broadcast(ADMIN_ID, f"msg {i}", [(i, 1000 + i)], started_ago_hours=1)

        target = FakeLogTarget()
        await admin_broadcasts._render_broadcast_log(target)

        assert len(target.answers_sent) == 10

    asyncio.run(go())


def test_broadcast_log_screen_fresh_have_button_old_do_not(tmp_path):
    """У свежих записей списка — кнопка удаления, у старых (>48ч) — нет."""
    _ready(tmp_path)

    async def go():
        for i in range(9):
            await _seed_broadcast(ADMIN_ID, f"msg {i}", [(i, 1000 + i)], started_ago_hours=1)
        # Создана ПОСЛЕДНЕЙ (id самый большой) — окажется первой в ORDER BY id DESC.
        await _seed_broadcast(ADMIN_ID, "old one", [(99, 9999)], started_ago_hours=49)

        target = FakeLogTarget()
        await admin_broadcasts._render_broadcast_log(target)

        assert len(target.answers_sent) == 10
        old_text, old_kb = target.answers_sent[0]
        assert "old one" in old_text
        assert old_kb is None
        assert "Удалить нельзя: прошло больше 48 часов" in old_text
        assert all(kb is not None for _, kb in target.answers_sent[1:])

    asyncio.run(go())


def test_broadcast_log_screen_empty_state(tmp_path):
    _ready(tmp_path)

    async def go():
        target = FakeLogTarget()
        await admin_broadcasts._render_broadcast_log(target)
        assert target.answers_sent == [("Рассылок пока не было.", None)]

    asyncio.run(go())
