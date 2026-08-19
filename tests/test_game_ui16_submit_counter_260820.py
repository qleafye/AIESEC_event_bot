"""Phase 16 Plan 02 (GAME-UI-02) — Экран 3: сдача задания с ОДНИМ редактируемым сообщением-
счётчиком вместо нового «Принял, частей: N» на каждую часть, плюс «🗑 Убрать последнее» и
инлайн «❌ Отмена».

Fake-дублёры скопированы из tests/test_gamification_delegate_phase9.py и расширены (конвенция
«копируй и расширяй», не импорт между тест-файлами): FakeMessage.answer() возвращает объект с
message_id, FakeBot умеет edit_message_text и пишет вызовы в .edits. Хендлеры вызываются
напрямую, asyncio.run + tmp_path-БД, как во всех тестах геймы.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import user_actions as ua_mod
from handlers import game_submit_counter as counter_mod
from handlers.states import GameSubmit


ADMIN_ID = 160201
DELEGATE_ID = 160202


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_ui16_submit_counter.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_delegate(uid=DELEGATE_ID):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-01",
    }))


def _seed_task(text="Пост со скрином", category="Light", coins=30, proof_type="photo,text",
               deadline_at="2026-12-31 23:59:00", created_by=ADMIN_ID):
    return asyncio.run(db.create_task(text, category, coins, proof_type, deadline_at, created_by))


def _new_state(uid=DELEGATE_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid, full_name=None):
        self.id = uid
        self.full_name = full_name


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


_MSG_ID_COUNTER = [1000]


class FakeMessage:
    def __init__(self, text=None, user_id=DELEGATE_ID, photo=None, document=None,
                 caption=None, media_group_id=None, voice=None):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.photo = photo
        self.document = document
        self.caption = caption
        self.media_group_id = media_group_id
        self.voice = voice
        self.answers = []
        self.edits = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))
        _MSG_ID_COUNTER[0] += 1
        return FakeSent(_MSG_ID_COUNTER[0])

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakePhotoSize:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeCallback:
    def __init__(self, data, user_id=DELEGATE_ID, full_name=None):
        self.data = data
        self.from_user = FakeUser(user_id, full_name=full_name)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))


def _flat_kb_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _flat_kb_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _start(task_id, uid=DELEGATE_ID):
    state = _new_state(uid)
    callback = FakeCallback(f"mytask_submit:{task_id}", user_id=uid)
    asyncio.run(ua_mod.mytask_submit_start(callback, state))
    return state, callback


# ── Task 1: рендер счётчика + клавиатура + ключи реестра ───────────────────────────────────

def test_counter_text_empty_has_zero_and_no_dangling_separator(tmp_path):
    _db_ready(tmp_path)
    text = asyncio.run(ua_mod._game_counter_text([]))
    assert "0" in text
    assert not text.endswith("·") and not text.endswith("· ")
    assert text == "Частей: 0"


def test_counter_text_breakdown_ordered_photo_before_text_zero_kinds_omitted(tmp_path):
    _db_ready(tmp_path)
    parts = [{"kind": "text", "content": "x", "caption": None},
             {"kind": "photo", "content": "p1", "caption": None}]  # текст пришёл ПЕРВЫМ
    text = asyncio.run(ua_mod._game_counter_text(parts))
    assert "📸1" in text and "✍️1" in text
    assert text.index("📸1") < text.index("✍️1")  # порядок фиксированный, не по приходу
    assert "📄" not in text and "🔗" not in text  # нулевые виды не показываются
    assert text == "Частей: 2 · 📸1 ✍️1"


def test_counter_text_survives_broken_template_in_registry(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_collected_template", "Собрано {oops}"))
    text = asyncio.run(ua_mod._game_counter_text([{"kind": "photo", "content": "p"}]))
    assert "1" in text and "📸1" in text  # фолбэк, а не исключение посреди сдачи


def test_counter_kb_empty_has_done_and_cancel_only(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(ua_mod._game_counter_kb([]))
    assert _flat_kb_data(kb) == ["gs_done", "gs_cancel"]
    assert "🗑 Убрать последнее" not in _flat_kb_texts(kb)


def test_counter_kb_with_parts_has_all_three_buttons(tmp_path):
    _db_ready(tmp_path)
    kb = asyncio.run(ua_mod._game_counter_kb([{"kind": "text", "content": "x"}]))
    assert _flat_kb_data(kb) == ["gs_done", "gs_remove_last", "gs_cancel"]
    assert _flat_kb_texts(kb) == ["✅ Готово", "🗑 Убрать последнее", "❌ Отмена"]


def test_counter_button_label_comes_from_registry(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("game_proof_remove_last_button", "Убрать"))
    kb = asyncio.run(ua_mod._game_counter_kb([{"kind": "text", "content": "x"}]))
    assert "Убрать" in _flat_kb_texts(kb)


def test_registry_has_two_new_game_keys_and_admin_order_lists_them():
    import settings_schema as s
    from handlers import admin as _admin_mod  # noqa: F401 -- ядро первым, иначе circular import
    from handlers import admin_settings
    for key in ("game_proof_collected_template", "game_proof_remove_last_button"):
        assert s.SETTINGS_SCHEMA[key]["group"] == "game"
        assert s.SETTINGS_SCHEMA[key]["default"]
        assert key in admin_settings._GAME_FIELD_ORDER
    assert "{count}" in s.SETTINGS_SCHEMA["game_proof_collected_template"]["default"]
    assert "{breakdown}" in s.SETTINGS_SCHEMA["game_proof_collected_template"]["default"]


def test_counter_helpers_live_in_seam_module_and_are_the_same_objects():
    """Вынесено в handlers/game_submit_counter.py из-за потолка размера user_actions.py --
    ua_mod._game_counter_* обязаны быть теми же объектами, не копиями."""
    assert ua_mod._game_counter_text is counter_mod.game_counter_text
    assert ua_mod._game_counter_kb is counter_mod.game_counter_kb
    assert ua_mod._edit_counter is counter_mod.edit_counter
