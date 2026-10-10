"""Форум-ночь B4 (идея №8): «🧪 Проверить приложение-сканер» — волонтёр проверяет своё
приложение-сканер ЗАРАНЕЕ без риска что-либо испортить: бот парсит выгрузку тем же
`find_checkin_records`, что настоящая загрузка, но НИКОГДА не зовёт `record_checkin` — только
отвечает, читается ли формат/время скана.

Стиль — тот же приём, что tests/test_admin_checkin_260924.py (`_FakeBot.download`,
`_FakeMessage`/`_FakeCallback`, `asyncio.run`, без pytest-asyncio); БД — шаблонная копия
через tests/_dbtpl.py::fast_init_db."""
from __future__ import annotations

import asyncio
import io

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from handlers.forum import admin_checkin
from handlers.states import CheckinTestUpload
from services import checkin as checkin_mod
from services.checkin import build_payload, current_event_tag
from tests._dbtpl import fast_init_db

ADMIN_ID = 910401


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_checkin_test_upload_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, user_id, text=None, document=None):
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.document = document
        self.sent = []  # list[(text, reply_markup)]

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeCallbackMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None


class _FakeBotSendPhoto:
    def __init__(self):
        self.sent_photos = []

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.sent_photos.append((chat_id, photo, caption))
        return None


class _FakeCallback:
    def __init__(self, data, user_id, bot=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeCallbackMessage()
        self.bot = bot or _FakeBotSendPhoto()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeDocument:
    def __init__(self, file_id="doc1", file_size=1024):
        self.file_id = file_id
        self.file_size = file_size


class _FakeBotDownload:
    def __init__(self, content: bytes):
        self.content = content

    async def download(self, file_id):
        return io.BytesIO(self.content)


def _flat_text(fake) -> list[str]:
    return [t for t, _rm in fake.sent]


# ── entry points ─────────────────────────────────────────────────────────────────────────────

def test_test_start_offers_test_qr_button_and_sets_state(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    cb = _FakeCallback("checkin_test_start", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_test_start(cb, state))

    assert asyncio.run(state.get_state()) == CheckinTestUpload.waiting_file.state
    kb = cb.message.sent[0][1]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "checkin_test_qr" in cbs


def test_test_qr_sends_photo_with_test_marker_caption(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("checkin_test_qr", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_test_qr(cb))

    assert len(cb.bot.sent_photos) == 1
    chat_id, _photo, caption = cb.bot.sent_photos[0]
    assert chat_id == ADMIN_ID
    assert "НЕ пропуск" in caption


# ── upload: no marking, only readability report ─────────────────────────────────────────────

def test_upload_reports_count_and_readable_time(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinTestUpload.waiting_file))

    tag = asyncio.run(current_event_tag())
    qr = build_payload(tag, "Тестовый QR", "—", "TESTabc123")
    content = f"timestamp,content\n2026-10-03T09:15:00,{qr}\n".encode("utf-8")

    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBotDownload(content)
    asyncio.run(admin_checkin.checkin_test_file_step(message, state, bot))

    texts = _flat_text(message)
    assert any("Приложение подходит" in t and "нашёл 1 QR форума" in t and "время скана читается" in t for t in texts)
    assert asyncio.run(state.get_state()) is None  # состояние снято после одного файла


def test_upload_reports_unreadable_time(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinTestUpload.waiting_file))

    tag = asyncio.run(current_event_tag())
    qr = build_payload(tag, "Тестовый QR", "—", "TESTxyz999")
    # без соседней колонки-времени -- сканер не пишет метку скана вовсе
    content = f"content\n{qr}\n".encode("utf-8")

    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBotDownload(content)
    asyncio.run(admin_checkin.checkin_test_file_step(message, state, bot))

    texts = _flat_text(message)
    assert any("НЕ читается" in t for t in texts)


def test_upload_no_matching_codes_gives_friendly_error(tmp_path):
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinTestUpload.waiting_file))

    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBotDownload("some,unrelated,csv\n1,2,3\n".encode("utf-8"))
    asyncio.run(admin_checkin.checkin_test_file_step(message, state, bot))

    texts = _flat_text(message)
    assert any("не нашёл ни одного qr" in t.lower() for t in texts)


def test_upload_never_calls_record_checkin(tmp_path, monkeypatch):
    """Гвоздь задачи: пробная выгрузка НИЧЕГО не отмечает, даже если код в файле совпадает
    с настоящим делегатским токеном. Форум-ночь п.5: `record_checkin`/`record_arrival` теперь
    единая точка отметки для входа И сессий — сторожим обе."""
    _db_ready(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.set_state(CheckinTestUpload.waiting_file))

    called = []
    async def _fake_record_checkin(*a, **k):
        called.append((a, k))
        return "new", "2026-10-03 09:15:00"

    async def _fake_record_arrival(*a, **k):
        called.append((a, k))
        return {"status": "new", "scanned_at": "2026-10-03 09:15:00"}
    monkeypatch.setattr(checkin_mod, "record_checkin", _fake_record_checkin)
    monkeypatch.setattr(checkin_mod, "record_arrival", _fake_record_arrival)

    tag = asyncio.run(current_event_tag())
    qr = build_payload(tag, "Тестовый QR", "—", "TESTreal000")
    content = f"timestamp,content\n2026-10-03T09:15:00,{qr}\n".encode("utf-8")

    message = _FakeMessage(ADMIN_ID, document=_FakeDocument())
    bot = _FakeBotDownload(content)
    asyncio.run(admin_checkin.checkin_test_file_step(message, state, bot))

    assert called == []


def test_invalid_upload_asks_for_document(tmp_path):
    _db_ready(tmp_path)
    message = _FakeMessage(ADMIN_ID, text="случайный текст")
    asyncio.run(admin_checkin.checkin_test_file_invalid(message))
    texts = _flat_text(message)
    assert any("документом" in t for t in texts)
