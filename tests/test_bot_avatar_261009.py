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
