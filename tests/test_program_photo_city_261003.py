"""Программа у делегата: чат и Mini App по одному правилу (тумблер «Таблица/Фото» города), фото
программы — своё у города; общее — только городу без своего фото и без сессий; загрузка фото
из админки — для города экрана/шапки.

Конвенция соседей (`tests/test_program_view_260924.py`): Fake-объекты, БД — tmp_path
(`fast_init_db`), `asyncio.run()`."""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from cities import per_city_key
from config import config
from database import db
from handlers.states import ProgramPhotoUpload
from services import program
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 961003001
MANAGER_ID = 961003002
SPB_DELEGATE = 961003010
TMN_DELEGATE = 961003011


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, cities_on=True):
    config.DB_PATH = str(tmp_path / "program_photo_city.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    if cities_on:
        _run(db.set_setting("event_city_enabled", "on"))


def _delegate(tid, city):
    _run(db.add_user({
        "telegram_id": tid, "full_name": "Делегат", "registration_date": "2026-09-01",
        "event_city": city, "status": "approved",
    }))


class _User:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = "Тест"


class _Chat:
    def __init__(self, cid):
        self.id = cid
        self.type = "private"


class _Msg:
    def __init__(self, user_id, text=None, photo=None, caption=None):
        self.from_user = _User(user_id)
        self.chat = _Chat(user_id)
        self.text = text
        self.caption = caption
        self.html_text = caption or text
        self.photo = photo
        self.photos_sent: list = []
        self.texts: list = []
        self.edited = None
        self.bot = None

    async def answer(self, text, parse_mode=None, reply_markup=None, **kw):
        self.texts.append(text)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.photos_sent.append(photo)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text


class _Photo:
    def __init__(self, file_id):
        self.file_id = file_id


class _Callback:
    def __init__(self, data, user_id=SUPERADMIN_ID):
        self.data = data
        self.from_user = _User(user_id)
        self.message = _Msg(user_id)
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _show(tid):
    from handlers import user_actions

    msg = _Msg(tid, text="📅 Программа форума")
    _run(user_actions.show_program(msg))
    return msg


# ── Показ: чат по тумблеру, как Mini App ────────────────────────────────────────────────────

def test_chat_follows_table_toggle_even_when_city_has_own_photo(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting(per_city_key("program_miniapp_view", "spb"), "table"))
    msg = _show(SPB_DELEGATE)
    assert msg.photos_sent == []  # раньше любое фото перекрывало сессии в чате
    assert any("Открытие" in t for t in msg.texts)
    assert _run(program.resolve_program_content("spb"))[0] == "table"  # Mini App — то же


def test_chat_shows_own_photo_when_toggle_is_photo(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting(per_city_key("program_miniapp_view", "spb"), "photo"))
    assert _show(SPB_DELEGATE).photos_sent == ["SPB_PHOTO"]


# ── Фото своё у города; общее — только городу без своего фото и без сессий ──────────────────

def test_shared_photo_does_not_cover_city_with_sessions(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _delegate(TMN_DELEGATE, "tyumen")
    _run(db.set_setting("program_photo_file_id", "SHARED_PHOTO"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting("program_miniapp_view", "photo"))  # даже при общем выборе «Фото»
    spb = _show(SPB_DELEGATE)
    assert spb.photos_sent == [] and any("Открытие" in t for t in spb.texts)
    assert _show(TMN_DELEGATE).photos_sent == ["SHARED_PHOTO"]  # у Тюмени ни своего, ни сессий


def test_city_photo_is_not_seen_by_other_city(tmp_path):
    _ready(tmp_path)
    _delegate(SPB_DELEGATE, "spb")
    _delegate(TMN_DELEGATE, "tyumen")
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.create_program_session("tyumen", "2026-10-03", "10:00", "11:00", "Сессия Тюмени"))
    assert _show(SPB_DELEGATE).photos_sent == ["SPB_PHOTO"]
    tmn = _show(TMN_DELEGATE)
    assert tmn.photos_sent == [] and any("Сессия Тюмени" in t for t in tmn.texts)


def test_own_caption_goes_with_own_photo(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("program_caption", "Общая подпись"))
    _run(db.set_setting(per_city_key("program_photo_file_id", "spb"), "SPB_PHOTO"))
    _run(db.set_setting(per_city_key("program_caption", "spb"), "Подпись СПб"))
    assert _run(program.program_photo_caption("spb")) == "Подпись СПб"
    assert _run(program.program_photo_caption("tyumen")) == "Общая подпись"


def test_cities_off_shared_photo_is_the_photo(tmp_path):
    _ready(tmp_path, cities_on=False)
    _delegate(SPB_DELEGATE, None)
    _run(db.set_setting("program_photo_file_id", "ONLY_PHOTO"))
    _run(db.set_setting("program_miniapp_view", "photo"))
    assert _show(SPB_DELEGATE).photos_sent == ["ONLY_PHOTO"]


# ── Загрузка — для города экрана ────────────────────────────────────────────────────────────

def _upload(uid, data, file_id, caption=None):
    from handlers import admin_program_view

    state = _state(uid)
    cb = _Callback(data, user_id=uid)
    _run(admin_program_view.prog_photo_start(cb, state))
    if _run(state.get_state()) != ProgramPhotoUpload.waiting.state:
        return cb, None
    msg = _Msg(uid, photo=[_Photo(file_id)], caption=caption)
    _run(admin_program_view.prog_photo_receive(msg, state))
    return cb, msg


def test_upload_writes_city_photo_not_shared(tmp_path):
    _ready(tmp_path)
    _cb, msg = _upload(SUPERADMIN_ID, "prog_photo:spb:program", "NEW_SPB", caption="Программа СПб")
    assert _run(db.get_setting(per_city_key("program_photo_file_id", "spb"))) == "NEW_SPB"
    assert _run(db.get_setting(per_city_key("program_caption", "spb"))) == "Программа СПб"
    assert not _run(db.get_setting("program_photo_file_id"))
    assert any("сохранено" in t for t in msg.texts)


def test_upload_prompt_names_the_city(tmp_path):
    _ready(tmp_path)
    label = _run(cities.city_label("tyumen"))
    cb, _msg = _upload(SUPERADMIN_ID, "prog_photo:tyumen:hub", "NEW_TMN")
    assert label in cb.message.edited


def test_upload_warns_when_city_shows_table(tmp_path):
    _ready(tmp_path)
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _cb, msg = _upload(SUPERADMIN_ID, "prog_photo:spb:program", "NEW_SPB")
    assert any("Переключить вид" in t for t in msg.texts)  # объясняет, почему фото не видно


def test_bound_manager_cannot_upload_for_other_city(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", SUPERADMIN_ID))
    _run(db.set_staff_city(MANAGER_ID, "msk"))
    cb, msg = _upload(MANAGER_ID, "prog_photo:spb:program", "EVIL")
    assert msg is None and cb.answers and cb.answers[0][1] is True
    assert not _run(db.get_setting(per_city_key("program_photo_file_id", "spb")))


def test_settings_photo_button_uses_header_city(tmp_path):
    from handlers import admin_settings

    _ready(tmp_path)
    _run(cities.set_admin_city(SUPERADMIN_ID, "spb"))
    state = _state(SUPERADMIN_ID)
    cb = _Callback("settings_photo:program")
    _run(admin_settings.settings_photo_start(cb, state))
    assert _run(state.get_state()) == ProgramPhotoUpload.waiting.state
    assert _run(state.get_data())["code"] == "spb"


def test_city_screens_show_photo_button(tmp_path):
    from handlers import admin_forum_functions, admin_program

    _ready(tmp_path)
    _text, kb = _run(admin_program.render_city_program_screen(SUPERADMIN_ID, "spb"))
    assert "prog_photo:spb:program" in [b.callback_data for row in kb.inline_keyboard for b in row]
    text, kb = _run(admin_forum_functions._render_hub(SUPERADMIN_ID, "spb"))
    assert "prog_photo:spb:hub" in [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "🖼 Фото программы" in text
