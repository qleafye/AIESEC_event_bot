"""Квик 260912 (W5, Задача 1) — находка живой приёмки стенда YL26 (ночь 10-11.09): смена языка
кнопкой главного меню перезапускала воронку `/start` у уже зарегистрированного делегата (тап
попадал на "У тебя есть незаконченная анкета" из-за висящего `kind="edit"` черновика).

Третий сегмент `callback_data` (`origin`) различает экран из `/start` (пауза внутри воронки —
реинвоук `cmd_start` обязателен, deep-link атрибуция восстанавливается) от экрана из меню
(самостоятельное действие уже находящегося где-то делегата — `cmd_start` не зовётся никогда).

Хендлеры зовутся напрямую с Fake message/callback — тот же приём, что
`tests/test_i18n_lang_27.py` (pytest-asyncio в этом окружении нет, только `asyncio.run()`).
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_lang

UID = 812001


def _use_tmp_db(tmp_path, name="test_lang_switch_260912.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _enable_module():
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on"),
        )
        await conn.commit()


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, language_code=None):
        self.id = uid
        self.language_code = language_code


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    def __init__(self, uid, language_code=None):
        self.from_user = _FakeUser(uid, language_code)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup)]
        self.markup_cleared = 0

    async def answer(self, text=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_cleared += 1
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.language_code)
        new.sent = self.sent
        new.markup_cleared = self.markup_cleared
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
    return [t for (t, _rm) in msg.sent]


# ── origin=start: реинвоук cmd_start, поведение байт-в-байт сегодняшнее ─────────────────────

def test_origin_start_reinvokes_cmd_start(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []

    async def _fake_cmd_start(message, state, bot, command=None):
        calls.append((message.from_user.id, getattr(command, "args", None)))

    monkeypatch.setattr(reg, "cmd_start", _fake_cmd_start)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        await state.update_data(**{reg_lang._DEEPLINK_RESUME_KEY: "ref_abc"})
        cb = _FakeCallback("lang_pick:en:start", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert calls == [(UID, "ref_abc")]
        assert cb.answers and cb.answers[0][0] == "✅ English selected."
        assert cb.message.markup_cleared == 1
        # deep-link ключ снят из FSM после употребления.
        data = await state.get_data()
        assert not data.get(reg_lang._DEEPLINK_RESUME_KEY)

    asyncio.run(go())


# ── origin=menu: cmd_start НИКОГДА не зовётся ───────────────────────────────────────────────

def test_origin_menu_never_calls_cmd_start(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []

    async def _fake_cmd_start(message, state, bot, command=None):
        calls.append(message.from_user.id)

    monkeypatch.setattr(reg, "cmd_start", _fake_cmd_start)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:en:menu", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert calls == []

    asyncio.run(go())


# ── origin=menu, зарегистрированный делегат: «незаконченная анкета» не всплывает ────────────
# (регресс-доказательство: реинвоук cmd_start с висящим reg_draft уводил на offer_resume)

def test_origin_menu_registered_delegate_no_resume_screen(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({
            "telegram_id": UID, "full_name": "Тест Тестов", "registration_date": "2026-01-01",
            "status": "approved",
        })
        # Черновик правки, зависший в БД -- та самая находка живой приёмки (kind="edit").
        await db.upsert_reg_draft(UID, kind="edit", patch={"age": "20"}, source="bot")

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:en:menu", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        texts = _texts(cb.message)
        assert not any("незакон" in (t or "").lower() for t in texts)
        assert texts == ["✅ English selected."]

    asyncio.run(go())


# ── origin=menu + активное FSM-состояние: только алерт, ни одного нового сообщения ──────────

def test_origin_menu_with_active_fsm_state_only_alert(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        await state.set_state("Registration:age")
        cb = _FakeCallback("lang_pick:en:menu", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert cb.answers == [("✅ English selected.", False)]
        assert cb.message.sent == []  # ни одного нового сообщения
        # состояние осталось прежним -- анкета не потеряна
        assert await state.get_state() == "Registration:age"

    asyncio.run(go())


# ── origin=menu без FSM-состояния: пустой answer() + одно сообщение с обновлённым меню ──────

def test_origin_menu_without_fsm_state_sends_confirm_with_menu(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:en:menu", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert cb.answers == [(None, False)]
        assert len(cb.message.sent) == 1
        text, markup = cb.message.sent[0]
        assert text == "✅ English selected."
        assert isinstance(markup, ReplyKeyboardMarkup)

    asyncio.run(go())


# ── легаси-payload без origin ("lang_pick:ru") -- трактуется как "menu", анкету не рвёт ──────

def test_legacy_payload_without_origin_behaves_as_menu(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []
    monkeypatch.setattr(reg, "cmd_start", lambda *a, **k: calls.append(1))

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:ru", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert calls == []
        user = await db.get_user(UID)
        assert user["lang"] == "ru"

    asyncio.run(go())


# ── незнакомый origin при валидном коде -- деградирует в "menu", не в исключение ────────────

def test_unknown_origin_degrades_to_menu(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []
    monkeypatch.setattr(reg, "cmd_start", lambda *a, **k: calls.append(1))

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:ru:whatever", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        assert calls == []
        user = await db.get_user(UID)
        assert user["lang"] == "ru"

    asyncio.run(go())


# ── незнакомый код языка: алерт-заглушка, БД не трогается (origin не имеет значения) ────────

def test_unknown_code_does_not_touch_db(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        await db.add_user({"telegram_id": UID, "full_name": "Тест Тестов", "registration_date": None})

        state = _new_state(UID)
        cb = _FakeCallback("lang_pick:xx:start", UID)
        await reg_lang.lang_pick_choose(cb, state, bot=object())

        user = await db.get_user(UID)
        assert (user or {}).get("lang") in (None, "")
        assert cb.answers == [(None, False)]

    asyncio.run(go())


# ── callback_data кнопок несёт правильный origin у обеих точек входа ────────────────────────

def test_offer_language_uses_start_origin(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        msg = _KBCapturingMessage(UID, language_code="de")
        state = _new_state(UID)
        shown = await reg_lang.offer_language(msg, state)
        assert shown is True
        _text, markup = msg.sent[0]
        assert isinstance(markup, InlineKeyboardMarkup)
        datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert datas == ["lang_pick:ru:start", "lang_pick:en:start"]

    asyncio.run(go())


def test_menu_lang_open_uses_menu_origin(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_module()
        msg = _KBCapturingMessage(UID, language_code="ru")
        await reg_lang.menu_lang_open(msg)
        _text, markup = msg.sent[0]
        datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert datas == ["lang_pick:ru:menu", "lang_pick:en:menu"]

    asyncio.run(go())
