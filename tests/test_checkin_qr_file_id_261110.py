"""Карточка QR не рисуется заново, пока отпечаток тот же.

«Мой QR» и утренняя рассылка берут сохранённый file_id. Смена имени рисует снова.
Битый file_id заменяется свежим png внутри отправки. Запрет чата png не запускает.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile

from config import config
from database import db
from services import scheduler as sched
import services.checkin_broadcast as cb
from services.forum import checkin as checkin_mod
from services.forum.qr_card_theme import CardTheme
from handlers import user_actions as ua_mod
from tests.test_checkin_qr_260923 import UID, _seed_user, _set_setting, _use_tmp_db
from tests.test_forum_bcast_fixes_261003 import _ready, _seed


def _run(coro):
    return asyncio.run(coro)


class _PhotoMessage:
    def __init__(self, uid=UID, file_id="fid-1"):
        self.text = "🎟 Мой QR"
        self.chat = SimpleNamespace(id=uid)
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []
        self.photos = []
        self.file_id = file_id

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        return "sent"

    async def answer_photo(self, photo, caption=None, **kwargs):
        self.photos.append((photo, caption))
        return SimpleNamespace(photo=[SimpleNamespace(file_id=self.file_id)])


def _rename(uid, name):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET full_name = ? WHERE telegram_id = ?", (name, uid))
    conn.commit()
    conn.close()


def test_my_qr_reuses_file_id_until_name_changes(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, "qr_file_id.db")
    _seed_user(UID)
    _run(_set_setting("checkin_qr_enabled", "on"))
    rendered = []

    async def _build(user):
        rendered.append(user["full_name"])
        return b"png", "caption-ru", True

    monkeypatch.setattr(checkin_mod, "_build_checkin_qr", _build)

    first = _PhotoMessage()
    _run(ua_mod.show_my_checkin_qr(first))
    assert len(rendered) == 1
    assert isinstance(first.photos[0][0], BufferedInputFile)
    stored = _run(db.checkin_qr_card_get(UID))
    assert stored is not None and stored[1] == "fid-1"

    second = _PhotoMessage(file_id="fid-ignored")
    _run(ua_mod.show_my_checkin_qr(second))
    assert rendered == ["Иванов Иван"]
    assert second.photos[0][0] == "fid-1"

    _rename(UID, "Новое Имя")
    third = _PhotoMessage(file_id="fid-2")
    _run(ua_mod.show_my_checkin_qr(third))
    assert rendered == ["Иванов Иван", "Новое Имя"]
    assert isinstance(third.photos[0][0], BufferedInputFile)
    assert _run(db.checkin_qr_card_get(UID))[1] == "fid-2"


def test_bare_qr_is_not_cached(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, "qr_bare.db")
    _seed_user(UID)
    _run(_set_setting("checkin_qr_enabled", "on"))
    rendered = []

    async def _build(user):
        rendered.append(1)
        return b"png", "caption-ru", False

    monkeypatch.setattr(checkin_mod, "_build_checkin_qr", _build)
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    assert rendered == [1, 1]
    assert _run(db.checkin_qr_card_get(UID)) is None


class _BroadcastBot:
    def __init__(self, *, on_file_id=None):
        self.photos = []
        self.on_file_id = on_file_id

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append(photo)
        if isinstance(photo, str):
            if self.on_file_id is not None:
                raise self.on_file_id
            return SimpleNamespace(photo=[SimpleNamespace(file_id="fid-qr")])
        return SimpleNamespace(photo=[SimpleNamespace(file_id="fid-fresh")])


def _evening_then_morning(monkeypatch, bot, builds, *, styled=True):
    async def _qr(user):
        builds.append(user["telegram_id"])
        return b"PNG", "cap", styled

    monkeypatch.setattr(checkin_mod, "_build_checkin_qr", _qr)
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    assert _run(cb.send_broadcast(None))["sent"] == 1
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    return _run(cb.send_morning_repeat(None))


def test_morning_repeat_reuses_evening_file_id(tmp_path, monkeypatch):
    _ready(tmp_path, "qr_morning.db")
    _seed(7, name="Иванов Иван")
    builds = []
    bot = _BroadcastBot()
    result = _evening_then_morning(monkeypatch, bot, builds)
    assert result["sent"] == 1
    assert builds == [7]
    assert isinstance(bot.photos[0], BufferedInputFile)
    assert bot.photos[1] == "fid-fresh"


def test_stale_file_id_falls_back_to_png(tmp_path, monkeypatch):
    _ready(tmp_path, "qr_stale.db")
    _seed(7, name="Иванов Иван")
    builds = []
    bot = _BroadcastBot(on_file_id=TelegramBadRequest(method=None, message="wrong file identifier"))
    result = _evening_then_morning(monkeypatch, bot, builds)
    assert result["sent"] == 1
    assert builds == [7, 7]
    assert isinstance(bot.photos[1], str)
    assert isinstance(bot.photos[2], BufferedInputFile)
    assert _run(db.checkin_qr_card_get(7))[1] == "fid-fresh"


def test_forbidden_file_id_is_not_redrawn(tmp_path, monkeypatch):
    _ready(tmp_path, "qr_forbid.db")
    _seed(7, name="Иванов Иван")
    builds = []
    bot = _BroadcastBot(on_file_id=TelegramForbiddenError(method=None, message="blocked"))
    result = _evening_then_morning(monkeypatch, bot, builds)
    assert result["failed"] == 1
    assert builds == [7]
    assert len(bot.photos) == 2


def test_broadcast_bare_qr_is_not_cached(tmp_path, monkeypatch):
    _ready(tmp_path, "qr_bcast_bare.db")
    _seed(7, name="Иванов Иван")
    builds = []
    bot = _BroadcastBot()
    result = _evening_then_morning(monkeypatch, bot, builds, styled=False)
    assert result["sent"] == 1
    assert builds == [7, 7]
    assert _run(db.checkin_qr_card_get(7)) is None
    assert isinstance(bot.photos[0], BufferedInputFile)
    assert isinstance(bot.photos[1], BufferedInputFile)


def test_stale_bare_redraw_keeps_stored_card(tmp_path, monkeypatch):
    _ready(tmp_path, "qr_stale_bare.db")
    _seed(7, name="Иванов Иван")
    builds = []

    async def _qr(user):
        builds.append(user["telegram_id"])
        return b"PNG", "cap", len(builds) == 1

    class _Bot:
        def __init__(self):
            self.bytes_sent = 0

        async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
            if isinstance(photo, str):
                raise TelegramBadRequest(method=None, message="wrong file identifier")
            self.bytes_sent += 1
            return SimpleNamespace(photo=[SimpleNamespace(file_id=f"fid-{self.bytes_sent}")])

    monkeypatch.setattr(checkin_mod, "_build_checkin_qr", _qr)
    monkeypatch.setattr(sched, "_bot", _Bot())
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    assert _run(cb.send_broadcast(None))["sent"] == 1
    assert _run(db.checkin_qr_card_get(7))[1] == "fid-1"
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    result = _run(cb.send_morning_repeat(None))
    assert result["sent"] == 1
    assert builds == [7, 7]
    assert _run(db.checkin_qr_card_get(7))[1] == "fid-1"


def _patch_theme_render(monkeypatch, draws, values, theme):
    async def inputs(user, payload):
        return {
            "payload": payload,
            "name": user.get("full_name") or "—",
            "city": "",
            "title": "Пропуск",
            "footer": "вход",
            "watermark": "АЙСЕК",
            "vk": "",
            "theme": values,
        }

    async def load(overrides=None, bot=None):
        return theme

    def render(*args, **kwargs):
        draws.append(1)
        return b"png"

    monkeypatch.setattr(checkin_mod, "card_render_inputs", inputs)
    monkeypatch.setattr("services.forum.qr_card_theme.load_card_theme", load)
    monkeypatch.setattr("services.forum.qr_card.render_qr_card", render)


def test_card_without_downloaded_image_is_not_cached(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, "qr_image_miss.db")
    _seed_user(UID)
    _run(_set_setting("checkin_qr_enabled", "on"))
    draws = []
    _patch_theme_render(
        monkeypatch, draws,
        {"qr_card_logo": "logo-file", "qr_card_show_logo": "on"},
        CardTheme(),
    )
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage(file_id="fid-2")))
    assert draws == [1, 1]
    assert _run(db.checkin_qr_card_get(UID)) is None


def test_card_without_downloaded_background_is_not_cached(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, "qr_bg_miss.db")
    _seed_user(UID)
    _run(_set_setting("checkin_qr_enabled", "on"))
    draws = []
    _patch_theme_render(monkeypatch, draws, {"qr_card_bg_image": "bg-file"}, CardTheme())
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    assert draws == [1, 1]
    assert _run(db.checkin_qr_card_get(UID)) is None


def test_hidden_logo_miss_still_caches(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, "qr_logo_off.db")
    _seed_user(UID)
    _run(_set_setting("checkin_qr_enabled", "on"))
    draws = []
    _patch_theme_render(
        monkeypatch, draws,
        {"qr_card_logo": "logo-file", "qr_card_show_logo": "off"},
        CardTheme(),
    )
    _run(ua_mod.show_my_checkin_qr(_PhotoMessage()))
    assert _run(db.checkin_qr_card_get(UID))[1] == "fid-1"
    second = _PhotoMessage(file_id="fid-2")
    _run(ua_mod.show_my_checkin_qr(second))
    assert draws == [1]
    assert second.photos[0][0] == "fid-1"
    assert _run(db.checkin_qr_card_get(UID))[1] == "fid-2"
