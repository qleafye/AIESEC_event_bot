"""«🖼 Аватар бота» (handlers/admin_bot_avatar.py): фото от менеджера -> `setMyProfilePhoto`,
без BotFather; отказ Telegram — человеческим текстом."""
from __future__ import annotations

import asyncio
import io

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SetMyProfilePhoto
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic

from handlers import admin_bot_avatar as ava
from handlers.admin_caps import required_capability
from handlers.admin_sections import SECTIONS, section_of


class _Bot:
    def __init__(self, fail=None):
        self.fail = fail
        self.photos = []

    async def download(self, file_id, destination):
        destination.write(b"\xff\xd8jpeg-" + file_id.encode())
        destination.seek(0)
        return destination

    async def set_my_profile_photo(self, photo):
        if self.fail is not None:
            raise self.fail
        self.photos.append(photo)

    async def remove_my_profile_photo(self):
        return True


def test_photo_is_downloaded_and_uploaded_as_static_profile_photo():
    bot = _Bot()
    assert asyncio.run(ava.set_avatar_from_photo(bot, "FILE1")) is None
    (photo,) = bot.photos
    assert isinstance(photo, InputProfilePhotoStatic)
    assert isinstance(photo.photo, BufferedInputFile)
    assert photo.photo.data == b"\xff\xd8jpeg-FILE1"


def test_telegram_refusals_are_human():
    method = SetMyProfilePhoto(photo=InputProfilePhotoStatic(photo="attach://x"))
    limited = _Bot(fail=TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=3600))
    text = asyncio.run(ava.set_avatar_from_photo(limited, "F"))
    assert "1 ч" in text and "Retry" not in text

    bad = _Bot(fail=TelegramBadRequest(method=method, message="Bad Request: PHOTO_INVALID"))
    text = asyncio.run(ava.set_avatar_from_photo(bad, "F"))
    assert "не принял" in text and "PHOTO_INVALID" not in text

    down = _Bot(fail=OSError("proxy"))
    assert "через минуту" in asyncio.run(ava.set_avatar_from_photo(down, "F"))


def test_old_aiogram_without_method_points_to_botfather():
    class _Old:
        async def download(self, file_id, destination):
            return io.BytesIO(b"x")

    assert asyncio.run(ava.set_avatar_from_photo(_Old(), "F")) == ava.UNSUPPORTED_TEXT


def test_screen_lives_in_event_section_with_settings_capability():
    rows = dict((tok, items) for tok, _label, items in SECTIONS)["event"]
    assert ("screen", "admin_bot_avatar", "🖼 Аватар бота") in rows
    assert section_of("admin_bot_avatar") == "event"
    for cb in ("admin_bot_avatar", "botava_cancel", "botava_rm", "botava_rm_yes"):
        assert required_capability(callback_data=cb) == "settings"
    assert required_capability(raw_state="BotAvatar:photo") == "settings"


# ── ревью: подтверждение перед установкой, ожидание не переживает уход с экрана ───────────

from datetime import datetime
from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message

from config import config
from handlers.admin import router as admin_router
from handlers.states import BotAvatar
from tests._dbtpl import fast_init_db


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=5, user_id=5))


class _Msg:
    def __init__(self, bot=None, file_id="PHOTO1"):
        self.bot = bot
        self.from_user = SimpleNamespace(id=5)
        self.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=file_id)]
        self.replies = []
        self.answers = []

    async def reply(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, **kw):
        return None

    async def edit_text(self, text, **kw):
        self.answers.append((text, kw.get("reply_markup")))


class _Cb:
    def __init__(self, bot, data):
        self.bot = bot
        self.data = data
        self.from_user = SimpleNamespace(id=5)
        self.message = _Msg(bot)
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append((text, show_alert))


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_photo_asks_confirmation_and_sets_nothing_yet():
    async def go():
        bot, state, msg = _Bot(), _state(), None
        await state.set_state(BotAvatar.photo)
        msg = _Msg(bot)
        await ava.bot_avatar_photo(msg, state)
        assert bot.photos == []
        (text, kb), = msg.replies
        assert text == ava.CONFIRM_TEXT
        assert _buttons(kb) == ["botava_set_yes", "botava_cancel"]
        assert await state.get_state() == BotAvatar.confirm.state

        cb = _Cb(bot, "botava_set_yes")
        await ava.bot_avatar_set_go(cb, state)
        assert len(bot.photos) == 1 and bot.photos[0].photo.data.endswith(b"PHOTO1")
        assert await state.get_state() is None
        assert cb.message.answers[-1][0] == ava.DONE_TEXT

    asyncio.run(go())


def test_confirm_without_pending_photo_sets_nothing():
    async def go():
        bot, state = _Bot(), _state()
        cb = _Cb(bot, "botava_set_yes")
        await ava.bot_avatar_set_go(cb, state)
        assert bot.photos == []
        assert cb.alerts == [(ava.EXPIRED_TEXT, True)]

    asyncio.run(go())


def test_refusal_on_confirm_keeps_waiting_for_another_photo():
    async def go():
        method = SetMyProfilePhoto(photo=InputProfilePhotoStatic(photo="attach://x"))
        bot, state = _Bot(fail=TelegramRetryAfter(method=method, message="x", retry_after=60)), _state()
        await state.set_state(BotAvatar.confirm)
        await state.update_data(avatar_file_id="F")
        cb = _Cb(bot, "botava_set_yes")
        await ava.bot_avatar_set_go(cb, state)
        assert "1 мин" in cb.message.answers[-1][0]
        assert await state.get_state() == BotAvatar.photo.state

    asyncio.run(go())


def _handler(callback):
    return next(h for h in admin_router.message.handlers if h.callback is callback)


def _tg_message(text):
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=5, type="private"), text=text)


def test_commands_are_not_swallowed_by_not_photo_handler():
    handler = _handler(ava.bot_avatar_not_photo)

    async def go(text):
        ok, _ = await handler.check(_tg_message(text), raw_state=BotAvatar.photo.state)
        return ok

    assert asyncio.run(go("/broadcast")) is False
    assert asyncio.run(go("/start")) is False
    assert asyncio.run(go("привет")) is True


def test_admin_command_and_panel_drop_avatar_wait(tmp_path):
    from handlers.admin import cmd_admin_help
    from handlers.admin_cities import admin_menu_root

    config.DB_PATH = str(tmp_path / "ava.db")
    fast_init_db()

    async def go():
        state = _state()
        await state.set_state(BotAvatar.photo)
        await cmd_admin_help(_Msg(), state)
        assert await state.get_state() is None

        await state.set_state(BotAvatar.confirm)
        await admin_menu_root(_Cb(None, "admin_menu"), state)
        assert await state.get_state() is None

        # чужое состояние эти точки входа не трогают
        from handlers.states import CoinsTransfer
        await state.set_state(CoinsTransfer.link)
        await admin_menu_root(_Cb(None, "admin_menu"), state)
        assert await state.get_state() == CoinsTransfer.link.state

    asyncio.run(go())


def test_start_drops_avatar_wait():
    import inspect
    from handlers.registration import cmd_start

    src = inspect.getsource(cmd_start)
    head = src[: src.index("offer_language")]
    assert 'startswith("BotAvatar:")' in head and "state.clear()" in head
