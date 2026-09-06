"""Phase 27 (27-04, LANG-01) — экран выбора языка на /start, переключатель в меню,
`users.lang`. Хендлеры зовутся напрямую с Fake message/callback (как в
tests/test_delegate_texts_registry_260819.py / tests/test_returning_delegate_073.py) —
pytest-asyncio в этом окружении нет, только asyncio.run().
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_lang
from keyboards.builders import get_main_menu_kb

UID = 810001
OTHER_UID = 810002


def _use_tmp_db(tmp_path, name="test_i18n_lang_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _enable_module(ask_on_start="on"):
    """Прямая запись в bot_settings (не через set_setting) — та же идиома, что
    tests/test_i18n_enqueue_27.py::_enable_module, не запускает bulk_seed побочным эффектом."""
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on"),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_ask_on_start", ask_on_start),
        )
        await conn.commit()


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, language_code=None, username=None):
        self.id = uid
        self.language_code = language_code
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Records (text, reply_markup, parse_mode) triples from answer/answer_photo — same shape
    as tests/test_returning_delegate_073.py::_KBCapturingMessage."""

    def __init__(self, uid, language_code=None, username=None):
        self.from_user = _FakeUser(uid, language_code, username)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup, parse_mode)]

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def answer_photo(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption if caption is not None else "<photo>", reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.language_code, self.from_user.username)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, language_code=None):
        self.data = data
        self.from_user = _FakeUser(user_id, language_code)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _texts(msg: _KBCapturingMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kb_msgs(msg: _KBCapturingMessage):
    return [(t, rm, p) for (t, rm, p) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


# ── модуль выключен: cmd_start не показывает экран языка вовсе ──────────────────────────────

def test_module_off_no_lang_screen(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        msg = _KBCapturingMessage(UID, language_code="de")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        assert not any("Choose the form language" in (t or "") for t in _texts(msg))
        # поток прежний -- delegate-новичок получает обычное приветствие/CTA, не завис на языке
        assert msg.sent
        user = await db.get_user(UID)
        assert user is None  # ничего лишнего не записалось до самой анкеты

    asyncio.run(go())


# ── модуль включён, клиент уже русский -- вопроса нет ────────────────────────────────────────

def test_module_on_russian_client_no_screen(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        msg = _KBCapturingMessage(UID, language_code="ru")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        assert not any("Choose the form language" in (t or "") for t in _texts(msg))
        user = await db.get_user(UID)
        assert (user or {}).get("lang") in (None, "")

    asyncio.run(go())


# ── модуль включён, клиент не русский -- ровно два инлайн-кнопки ────────────────────────────

def test_module_on_non_russian_client_shows_two_buttons(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        msg = _KBCapturingMessage(UID, language_code="de")
        state = _new_state(UID)
        shown = await reg_lang.offer_language(msg, state)
        assert shown is True

        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        datas = _callback_datas(inline[0][1])
        assert datas == ["lang_pick:ru", "lang_pick:en"]

    asyncio.run(go())


# ── тап lang_pick:en -- users.lang записан, переживает «перезапуск» ─────────────────────────

def test_lang_pick_en_persists(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        cb = _FakeCallback("lang_pick:en", UID)
        state = _new_state(UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert cb.answers and cb.answers[0][0] == "✅ English selected."
        user = await db.get_user(UID)
        assert user["lang"] == "en"

        # "Перезапуск" -- новый независимый вызов get_user (не тот же Python-объект),
        # язык не в FSM, переживает без state.
        user_again = await db.get_user(UID)
        assert user_again["lang"] == "en"

    asyncio.run(go())


# ── незнакомый код -- ничего не записывается ────────────────────────────────────────────────

def test_lang_pick_unknown_code_writes_nothing(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        cb = _FakeCallback("lang_pick:xx", UID)
        state = _new_state(UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        user = await db.get_user(UID)
        assert (user or {}).get("lang") in (None, "")
        assert cb.answers == [(None, False)]

    asyncio.run(go())


# ── menu_lang: отсутствует при выключенном модуле, есть при включённом + сама кнопка "on" ───
# (двойной гейт -- см. keyboards/builders.py::get_main_menu_kb -- предотвращает мёртвую кнопку,
# если менеджер включил только модуль или только саму кнопку)

def test_menu_lang_absent_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("menu_lang", "on")  # кнопка включена, но модуль -- нет
        kb = await get_main_menu_kb(UID)
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert reg_lang.LANG_MENU_BUTTON_TEXT not in texts

    asyncio.run(go())


def test_menu_lang_present_when_module_and_button_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.set_setting("menu_lang", "on")
        kb = await get_main_menu_kb(UID)
        texts = [btn.text for row in kb.keyboard for btn in row]
        assert reg_lang.LANG_MENU_BUTTON_TEXT in texts

    asyncio.run(go())


# ── нажатие кнопки меню -- один литерал, один фильтр, срабатывает независимо от текущего lang ─

def test_menu_lang_open_shows_screen_regardless_of_current_lang(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        for lang_hint in ("ru", "en"):
            msg = _KBCapturingMessage(UID, language_code=lang_hint)
            await reg_lang.menu_lang_open(msg)
            inline = _inline_kb_msgs(msg)
            assert len(inline) == 1
            assert _callback_datas(inline[0][1]) == ["lang_pick:ru", "lang_pick:en"]

    asyncio.run(go())


def test_menu_lang_open_silent_when_module_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        msg = _KBCapturingMessage(UID, language_code="ru")
        await reg_lang.menu_lang_open(msg)
        assert msg.sent == []

    asyncio.run(go())


# ── язык никогда не хранится в FSM ────────────────────────────────────────────────────────

def test_language_never_stored_in_fsm(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})
        cb = _FakeCallback("lang_pick:en", UID)
        state = _new_state(UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())
        data = await state.get_data()
        assert "lang" not in data and all("lang" != k for k in data)

    asyncio.run(go())
