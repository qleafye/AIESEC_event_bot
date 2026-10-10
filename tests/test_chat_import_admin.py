"""«📥 Загрузить историю чата» на экране «🏆 Рейтинг чата»: кнопка -> файл result.json документом ->
предпросмотр (ничего не пишет) -> подтверждение (пишет ровно показанное, повтор безопасен);
неверный/большой файл — человеческая ошибка; права settings."""
from __future__ import annotations

import asyncio
import json
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from handlers import admin_chat_import as h
from handlers import admin_chat_rating as scr
from handlers.admin_caps import ADMIN_CAPS, required_capability
from handlers.states import ChatExportImport
from tests.test_chat_export_import_260927 import (  # noqa: F401 — фикстуры db_path, _frozen_now
    CHAT_ID,
    _count,
    _fixture_export,
    _frozen_now,
    db_path,
)

ADMIN = 900927301


def _run(coro):
    return asyncio.run(coro)


class _User:
    id = ADMIN


class _Doc:
    def __init__(self, name="result.json", size=1000):
        self.file_id = "F1"
        self.file_name = name
        self.file_size = size


class _Message:
    def __init__(self, document=None, text=None):
        self.document = document
        self.text = text
        self.from_user = _User()
        self.answers = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        pass


class _Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = _User()
        self.message = _Message()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _Bot:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def download(self, file_id, destination=None):
        destination.write(self.payload)


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


def _setup(db_path):  # noqa: F811
    config.ADMIN_IDS = [ADMIN]
    _run(db.set_setting("event_city_enabled", "on"))
    assert _run(cities.set_admin_city(ADMIN, "spb"))


def _payload(data=None) -> bytes:
    return json.dumps(data or _fixture_export(), ensure_ascii=False).encode("utf-8")


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _to_confirm(state, payload=None):
    cb = _Callback("chimp:open")
    _run(h.chat_import_open(cb, state))
    assert _run(state.get_state()) == ChatExportImport.waiting_file.state
    msg = _Message(document=_Doc())
    _run(h.chat_import_file(msg, state, _Bot(payload or _payload())))
    return cb, msg


def test_button_on_rating_screen_and_caps(db_path):  # noqa: F811
    _setup(db_path)
    _, kb = _run(scr.render_chat_rating_screen(ADMIN))
    assert ("📥 Загрузить историю чата", "chimp:open") in [
        (b.text, b.callback_data) for r in kb.inline_keyboard for b in r
    ]
    assert ADMIN_CAPS["chimp:*"] == "settings"
    assert required_capability(callback_data="chimp:go") == "settings"
    assert ADMIN_CAPS["state:ChatExportImport:*"] == "settings"


def test_open_explains_how_to_export_and_preview_writes_nothing(db_path):  # noqa: F811
    _setup(db_path)
    state = _state()
    cb, msg = _to_confirm(state)
    ask = cb.message.answers[0][0]
    assert "Экспорт истории чата" in ask and "result.json" in ask and "20 МБ" in ask
    text, kb = msg.answers[-1]
    assert "Добавится новых сообщений:" in text and "авторов:" in text
    assert "Уже есть в базе: 0" in text
    assert _cbs(kb)[0] == "chimp:go"
    assert _run(state.get_state()) == ChatExportImport.confirm.state
    assert _count(db_path, "chat_messages") == 0 and _count(db_path, "chat_reactions") == 0


def test_go_writes_exactly_previewed_and_repeat_adds_nothing(db_path):  # noqa: F811
    _setup(db_path)
    state = _state()
    _, msg = _to_confirm(state)
    plan = _run(state.get_data())["plan"]
    go = _Callback("chimp:go")
    _run(h.chat_import_go(go, state))
    assert _count(db_path, "chat_messages") == plan["to_add"] > 0
    assert f"Добавлено сообщений: {plan['to_add']}" in go.message.answers[0][0]
    assert _run(state.get_state()) is None

    again = _Callback("chimp:go")  # двойное нажатие
    _run(h.chat_import_go(again, state))
    assert again.answers[0][1] is True
    assert _count(db_path, "chat_messages") == plan["to_add"]

    state2 = _state()
    _, msg2 = _to_confirm(state2)
    text, kb = msg2.answers[-1]
    assert f"Уже есть в базе: {plan['to_add']}" in text
    assert "Добавлять нечего" in text and "chimp:go" not in _cbs(kb)


def test_wrong_files_get_human_errors(db_path):  # noqa: F811
    _setup(db_path)
    state = _state()
    _run(h.chat_import_open(_Callback("chimp:open"), state))

    big = _Message(document=_Doc(size=25 * 1024 * 1024))
    _run(h.chat_import_file(big, state, _Bot(b"")))
    assert "больше 20 МБ" in big.answers[0][0] and "частями" in big.answers[0][0]

    zipped = _Message(document=_Doc(name="chat.zip"))
    _run(h.chat_import_file(zipped, state, _Bot(b"")))
    assert "не result.json" in zipped.answers[0][0]

    junk = _Message(document=_Doc())
    _run(h.chat_import_file(junk, state, _Bot(b"not json")))
    assert "не JSON" in junk.answers[0][0] and "Отмена" in junk.answers[0][0]

    other = _Message(document=_Doc())
    _run(h.chat_import_file(other, state, _Bot(json.dumps({"a": 1}).encode())))
    assert "не экспорт чата" in other.answers[0][0]

    assert _run(state.get_state()) == ChatExportImport.waiting_file.state
    assert _count(db_path, "chat_messages") == 0


def test_export_of_another_chat_is_flagged(db_path):  # noqa: F811
    _setup(db_path)
    data = _fixture_export()
    data["id"] = 1234
    state = _state()
    _, msg = _to_confirm(state, _payload(data))
    text, kb = msg.answers[-1]
    assert "⚠️ В файле история чата" in text
    assert "⚠️ Это тот же чат — загрузить" in [b.text for r in kb.inline_keyboard for b in r][0]


def test_no_bound_chat_is_explained(tmp_path):
    config.DB_PATH = str(tmp_path / "nochat.db")
    from tests._dbtpl import fast_init_db
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]
    cb = _Callback("chimp:open")
    _run(h.chat_import_open(cb, _state()))
    assert "не привязан" in cb.message.edits[0][0]


def test_cancel_clears_state(db_path):  # noqa: F811
    _setup(db_path)
    state = _state()
    _run(h.chat_import_open(_Callback("chimp:open"), state))
    msg = _Message(text="Отмена")
    _run(h.chat_import_cancel(msg, state))
    assert _run(state.get_state()) is None
    assert "Ничего не загружено" in msg.answers[0][0]
