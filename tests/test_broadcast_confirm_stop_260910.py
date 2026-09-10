"""Quick 260910-okb (BC-01/02/03): превью+подтверждение, прогресс и кнопка «Остановить» для
немедленной рассылки.

09.09 сообщение в состоянии рассылки уходило получателям СРАЗУ, без единого подтверждения —
менеджер, отправивший «Привет» на 951 человека, не мог остановить цикл, крутившийся прямо в
хендлере. Эти тесты покрывают новый путь `handlers/admin_broadcasts.py`: превью не шлёт
получателям, подтверждение стартует фоновый прогон (services/broadcast_run.run_broadcast), и
кнопка «⛔ Остановить» реально прерывает его — адресно, по своей рассылке.

Стиль: прямой вызов хендлеров с фейковым state (tests/test_city_broadcast_phase72.py),
`_spawn` перехватывается монкипатчем (tests/test_polls_260822.py), затем корутина довыполняется
вручную — так фоновый прогон детерминирован внутри теста. pytest-asyncio в проекте нет — async
гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_broadcasts
from handlers.states import Broadcast
from services import broadcast_run as br

ADMIN_ID = 900910
OTHER_ADMIN_ID = 900911


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "confirm_stop.db")
    config.ADMIN_IDS = [ADMIN_ID, OTHER_ADMIN_ID]
    asyncio.run(db.init_db())


def _patch_audience(monkeypatch, ids):
    async def fake_all():
        return list(ids)
    monkeypatch.setattr(admin_broadcasts, "get_all_users_ids", fake_all)


def _fast_sleep(monkeypatch):
    """Гасим оба источника пауз: 0.8с сборки альбома в admin_broadcasts и 0.05с/ретрай-паузы
    внутри services/broadcast_run — иначе тесты растягиваются на реальные секунды."""
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(admin_broadcasts.asyncio, "sleep", _noop)
    monkeypatch.setattr(br.asyncio, "sleep", _noop)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeSentMessage:
    """То, что возвращает bot.send_message — экран подтверждения/прогресса; carries .edit_text
    так же, как реальный aiogram Message, и попутно ведёт журнал правок для проверки прогресса."""

    def __init__(self, message_id, text=None, reply_markup=None):
        self.message_id = message_id
        self.text = text
        self.markup = reply_markup
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edits.append((text, reply_markup))


class FakeMessage:
    """Входящее сообщение менеджера в состоянии Broadcast.message."""

    def __init__(self, *, chat_id, message_id=1, text=None, caption=None,
                 media_group_id=None, photo=None, video=None, document=None, audio=None,
                 user_id=None):
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id if user_id is not None else chat_id)
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.html_text = text or caption
        self.media_group_id = media_group_id
        self.photo = photo
        self.video = video
        self.document = document
        self.audio = audio
        self.copy_calls = []
        self.answers = []

    async def send_copy(self, chat_id):
        self.copy_calls.append(chat_id)
        return SimpleNamespace(message_id=999)

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data, *, user_id, message):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self, fail_chat_ids=()):
        self.sent_messages: list[FakeSentMessage] = []
        self.copy_calls: list[int] = []
        self.media_group_calls: list[tuple] = []
        self.fail_chat_ids = set(fail_chat_ids)
        self._next_id = 5000

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        sent = FakeSentMessage(self._next_id, text, reply_markup)
        self.sent_messages.append(sent)
        return sent

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copy_calls.append(chat_id)
        if chat_id in self.fail_chat_ids:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_media_group(self, chat_id, media):
        self.media_group_calls.append((chat_id, media))
        if chat_id in self.fail_chat_ids:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        out = []
        for _ in media:
            self._next_id += 1
            out.append(SimpleNamespace(message_id=self._next_id))
        return out


class FakeState:
    """Минимальный FSMContext-заменитель — путь рассылки использует только эти три метода."""

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


def test_message_in_broadcast_state_does_not_send_anything(tmp_path, monkeypatch):
    """Сообщение в Broadcast.message не отправляет НИЧЕГО получателям: бот не сделал ни одной
    копии, менеджер получил превью и клавиатуру с двумя кнопками."""
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2, 3])

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, message_id=10, text="Всем привет")

        await admin_broadcasts.process_broadcast(msg, state, bot)

        assert bot.copy_calls == []
        assert bot.media_group_calls == []
        assert msg.copy_calls == [ADMIN_ID]  # только превью-копия себе, не получателям
        assert len(bot.sent_messages) == 1
        prompt = bot.sent_messages[0]
        texts = _btn_texts(prompt.markup)
        assert any("Отправить" in t and "3" in t for t in texts)
        assert "❌ Отмена" in texts
        assert state.state == Broadcast.confirm

    asyncio.run(go())


def test_confirm_button_caption_has_recipient_count(tmp_path, monkeypatch):
    """Подпись кнопки подтверждения содержит число получателей."""
    _ready(tmp_path)
    _patch_audience(monkeypatch, [1, 2, 3, 4, 5, 6, 7])

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        msg = FakeMessage(chat_id=ADMIN_ID, message_id=11, text="Привет всем")
        await admin_broadcasts.process_broadcast(msg, state, bot)
        prompt = bot.sent_messages[0]
        go_btn = [b for row in prompt.markup.inline_keyboard for b in row
                  if b.callback_data == "bc_go"][0]
        assert "7" in go_btn.text

    asyncio.run(go())


def test_bc_no_cancels_without_sending_and_clears_fsm(tmp_path, monkeypatch):
    """Нажатие «Отмена» на экране подтверждения — ни одной отправки, FSM очищен."""
    _ready(tmp_path)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=1, bc_users=[1, 2, 3], bc_preview="hi",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 3 пользователям?")
        cb = FakeCallback("bc_no", user_id=ADMIN_ID, message=confirm_msg)

        await admin_broadcasts.bc_no(cb, state)

        assert confirm_msg.edits == [("Рассылка отменена.", None)]
        assert state.state is None
        assert (await db.list_recent_broadcasts(10)) == []

    asyncio.run(go())


def test_bc_go_creates_row_and_delivers_then_shows_final_summary(tmp_path, monkeypatch):
    """Нажатие подтверждения создаёт строку в broadcasts и запускает прогон; после его конца
    получателям ушли копии, а менеджеру — итог «Рассылка завершена»."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2, 3], bc_preview="hi всем",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 3 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))

        await admin_broadcasts.bc_go(cb, state, bot)

        # Строка журнала создана СРАЗУ, до завершения прогона.
        rows = await db.list_recent_broadcasts(10)
        assert len(rows) == 1
        assert rows[0]["admin_id"] == ADMIN_ID
        assert rows[0]["total"] == 3
        assert state.state is None  # FSM очищен сразу после старта
        assert len(spawned) == 1

        await spawned[0]  # довыполняем фоновый прогон синхронно

        assert sorted(bot.copy_calls) == [1, 2, 3]
        assert "Рассылка завершена" in confirm_msg.text
        assert "✅ Отправлено 3" in confirm_msg.text
        row = await db.get_broadcast(rows[0]["id"])
        assert row["status"] == "done"
        assert row["delivered"] == 3

    asyncio.run(go())


def test_stop_button_halts_progress_before_total(tmp_path, monkeypatch):
    """Кнопка стопа: request_stop вызван, в итоге текст «Остановлено», отправок меньше, чем
    total."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)

    async def go():
        state = FakeState(
            bc_chat_id=ADMIN_ID, bc_message_id=42, bc_users=[1, 2, 3, 4, 5], bc_preview="hi",
        )
        await state.set_state(Broadcast.confirm)
        confirm_msg = FakeSentMessage(1, "Отправить это 5 пользователям?")
        cb = FakeCallback("bc_go", user_id=ADMIN_ID, message=confirm_msg)
        bot = FakeBot()

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        await admin_broadcasts.bc_go(cb, state, bot)
        bid = (await db.list_recent_broadcasts(1))[0]["id"]

        # После второй копии менеджер жмёт «⛔ Остановить» на своей же рассылке.
        calls = {"n": 0}
        real_copy = bot.copy_message

        async def copy_then_maybe_stop(chat_id, from_chat_id, message_id):
            calls["n"] += 1
            result = await real_copy(chat_id, from_chat_id, message_id)
            if calls["n"] == 2:
                stop_cb = FakeCallback(
                    f"bc_stop:{bid}", user_id=ADMIN_ID, message=FakeSentMessage(2),
                )
                await admin_broadcasts.bc_stop(stop_cb)
                assert stop_cb.answers == [("Останавливаю…", False)]
            return result

        bot.copy_message = copy_then_maybe_stop

        await spawned[0]

        assert calls["n"] == 2
        row = await db.get_broadcast(bid)
        assert row["status"] == "stopped"
        assert row["delivered"] == 2 < row["total"]
        assert "⛔ Остановлено" in confirm_msg.text

    asyncio.run(go())


def test_stop_on_someone_elses_broadcast_is_refused(tmp_path, monkeypatch):
    """Стоп чужой рассылки (admin_id в строке broadcasts не совпадает с нажавшим) — флаг не
    выставляется, менеджеру ответ «Это не ваша рассылка»."""
    _ready(tmp_path)

    async def go():
        bid = await db.create_broadcast(ADMIN_ID, "hi", 3)
        stop_cb = FakeCallback(
            f"bc_stop:{bid}", user_id=OTHER_ADMIN_ID, message=FakeSentMessage(1),
        )

        await admin_broadcasts.bc_stop(stop_cb)

        assert stop_cb.answers == [("Это не ваша рассылка.", True)]
        assert br.is_stopped(bid) is False

    asyncio.run(go())


def test_album_three_messages_produce_one_preview_and_one_keyboard(tmp_path, monkeypatch):
    """Альбом: три сообщения одной медиагруппы дают ОДНО превью-альбом и одну клавиатуру."""
    _ready(tmp_path)
    _fast_sleep(monkeypatch)
    _patch_audience(monkeypatch, [1, 2])

    async def go():
        state = FakeState(target_type="all")
        bot = FakeBot()
        mgid = "mg-260910"
        msgs = [
            FakeMessage(
                chat_id=ADMIN_ID, message_id=100 + i, media_group_id=mgid,
                photo=[SimpleNamespace(file_id=f"file{i}")],
            )
            for i in range(3)
        ]

        spawned = []
        monkeypatch.setattr(admin_broadcasts, "_spawn", lambda coro: spawned.append(coro))
        for m in msgs:
            await admin_broadcasts.process_broadcast(m, state, bot)

        assert len(spawned) == 1  # спавнится один раз — на первом сообщении альбома
        await spawned[0]

        assert len(bot.media_group_calls) == 1  # ровно одно превью-отправление альбома
        assert len(bot.sent_messages) == 1  # и ровно одна клавиатура подтверждения
        data = await state.get_data()
        assert len(data.get("bc_album", [])) == 3
        assert data.get("bc_preview") == "[альбом x 3]"
        assert state.state == Broadcast.confirm

    asyncio.run(go())


def test_cancel_on_message_step_still_works(tmp_path):
    """«Отмена» на шаге ввода сообщения работает как раньше (текст «Рассылка отменена.»)."""
    _ready(tmp_path)

    async def go():
        state = FakeState()
        await state.set_state(Broadcast.message)
        msg = FakeMessage(chat_id=ADMIN_ID, message_id=1, text="Отмена")

        await admin_broadcasts.cancel_broadcast(msg, state)

        assert msg.answers[0][0] == "Рассылка отменена."
        assert state.state is None

    asyncio.run(go())
