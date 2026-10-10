"""Форум-ночь B1 (идея №10): перевыпуск QR — старый токен перестаёт открывать вход, новый
уходит делегату. Покрывает три слоя:

- `database.db.reissue_checkin_token`/`get_checkin_token_replacement` — новый токен отличается
  от старого, старый попадает в `checkin_token_replacements` с верным `telegram_id`, пользователя
  нет вовсе -- `None`, у делегата ещё не было токена -- реиссью всё равно выдаёт новый и ничего
  не кладёт в таблицу замен (нечего класть).
- `services.forum.checkin.resolve_scanned_user` -- единая точка «токен -> (делегат, код отказа)»:
  обычный активный токен ведёт себя как раньше (denial по `checkin_denial`), ЗАМЕНЁННЫЙ токен
  даёт `(None, "token_replaced")`, вообще незнакомый токен -- `(None, "no_user")`.
- `handlers/admin.py::cmd_find_user` (кнопка на карточке) + `handlers/forum/admin_checkin.py`
  (подтверждение/само действие) -- та же фейковая обвязка, что `tests/test_admin_checkin_
  260924.py` (`_FakeMessage`/`_FakeCallback`, `asyncio.run`, без pytest-asyncio, БД — шаблонная
  копия `tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from database.db import _connect
from handlers import admin as admin_mod
from handlers.forum import admin_checkin
from services.forum.checkin import build_payload, current_event_tag, resolve_scanned_user
from tests._dbtpl import fast_init_db

ADMIN_ID = 910201
DELEGATE_ID = 910202
STRANGER_ID = 910203


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_checkin_reissue_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _insert_user(telegram_id, *, status="approved", username=None, full_name="Тест Тестов"):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, username) VALUES (?, ?, ?, ?)",
            (telegram_id, full_name, status, username),
        )
        await conn.commit()


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeEditableMessage:
    def __init__(self):
        self.sent = []  # message.answer() calls: (text, reply_markup)
        self.edits = []  # message.edit_text() calls: text

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.sent.append((text, reply_markup))
        return None

    async def edit_text(self, text, parse_mode=None, reply_markup=None, *a, **k):
        self.edits.append(text)
        return None


class _FakeBot:
    def __init__(self):
        self.sent_photos = []  # (chat_id, photo, caption)
        self.fail_send_photo = False

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if self.fail_send_photo:
            raise RuntimeError("delegate blocked the bot")
        self.sent_photos.append((chat_id, photo, caption))
        return None


class _FakeCallback:
    def __init__(self, data, user_id, bot=None):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeEditableMessage()
        self.bot = bot or _FakeBot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _FakeMessage:
    def __init__(self, text, user_id):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.answers = []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, *a, **k):
        self.answers.append((text, parse_mode, reply_markup))
        return None


# ── database.db: reissue_checkin_token / get_checkin_token_replacement ─────────────────────

def test_reissue_generates_a_different_token(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    new_token = asyncio.run(db.reissue_checkin_token(DELEGATE_ID))
    assert new_token
    assert new_token != old_token
    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"] == new_token


def test_reissue_moves_old_token_into_replacements_table(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    asyncio.run(db.reissue_checkin_token(DELEGATE_ID))
    replacement = asyncio.run(db.get_checkin_token_replacement(old_token))
    assert replacement is not None
    assert replacement["telegram_id"] == DELEGATE_ID
    assert replacement["replaced_at"]


def test_reissue_without_prior_token_still_issues_a_fresh_one(tmp_path):
    """Делегат ни разу не открывал «🎟 Мой QR» -- checkin_token ещё NULL. Реиссью всё равно
    обязана выдать рабочий токен (не падать на отсутствии старого), и класть в таблицу замен
    нечего -- никакой строки для None не появляется."""
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID))
    new_token = asyncio.run(db.reissue_checkin_token(DELEGATE_ID))
    assert new_token
    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"] == new_token


def test_reissue_missing_user_returns_none(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(db.reissue_checkin_token(999999999)) is None


def test_reissue_twice_keeps_both_old_tokens_in_replacements(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID))
    token_a = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    token_b = asyncio.run(db.reissue_checkin_token(DELEGATE_ID))
    token_c = asyncio.run(db.reissue_checkin_token(DELEGATE_ID))
    assert len({token_a, token_b, token_c}) == 3
    assert asyncio.run(db.get_checkin_token_replacement(token_a)) is not None
    assert asyncio.run(db.get_checkin_token_replacement(token_b)) is not None
    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"] == token_c


def test_get_checkin_token_replacement_unknown_token_is_none(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(db.get_checkin_token_replacement("never-existed")) is None
    assert asyncio.run(db.get_checkin_token_replacement(None)) is None


# ── services.forum.checkin.resolve_scanned_user ───────────────────────────────────────────────────

def test_resolve_scanned_user_active_token_behaves_like_checkin_denial(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    user, denial_code = asyncio.run(resolve_scanned_user(token))
    assert user is not None
    assert user["telegram_id"] == DELEGATE_ID
    assert denial_code is None


def test_resolve_scanned_user_replaced_token_gives_token_replaced(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    asyncio.run(db.reissue_checkin_token(DELEGATE_ID))

    user, denial_code = asyncio.run(resolve_scanned_user(old_token))
    assert user is None
    assert denial_code == "token_replaced"


def test_resolve_scanned_user_unknown_token_gives_no_user(tmp_path):
    _db_ready(tmp_path)
    user, denial_code = asyncio.run(resolve_scanned_user("garbage-never-issued"))
    assert user is None
    assert denial_code == "no_user"


def test_resolve_scanned_user_empty_token_gives_no_user(tmp_path):
    _db_ready(tmp_path)
    user, denial_code = asyncio.run(resolve_scanned_user(None))
    assert user is None
    assert denial_code == "no_user"


def test_resolve_scanned_user_new_token_still_works_after_sibling_reissue(tmp_path):
    """Реиссью не портит НОВЫЙ токен -- скан свежего QR продолжает находить делегата."""
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    new_token = asyncio.run(db.reissue_checkin_token(DELEGATE_ID))

    user, denial_code = asyncio.run(resolve_scanned_user(new_token))
    assert user is not None
    assert user["telegram_id"] == DELEGATE_ID
    assert denial_code is None


# ── handlers/admin.py::cmd_find_user — кнопка на карточке ──────────────────────────────────

def test_find_user_card_has_reissue_button(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, username="seeded"))
    message = _FakeMessage("/find @seeded", ADMIN_ID)
    asyncio.run(admin_mod.cmd_find_user(message))

    text, parse_mode, kb = message.answers[0]
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"checkin_reissue:{DELEGATE_ID}" in buttons


# ── handlers/forum/admin_checkin.py: подтверждение / выполнение / отмена ─────────────────────────

def test_reissue_confirm_shows_yes_no_keyboard(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback(f"checkin_reissue:{DELEGATE_ID}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_confirm(cb))

    text, kb = cb.message.sent[0]
    assert "перестанет работать" in text
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"checkin_reissue_yes:{DELEGATE_ID}" in buttons
    assert "checkin_reissue_no" in buttons


def test_reissue_cancel_changes_nothing(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))

    cb = _FakeCallback("checkin_reissue_no", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_cancel(cb))

    assert cb.message.edits == ["Отменено. QR не менялся."]
    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"] == old_token


def test_reissue_go_missing_user_reports_not_found(tmp_path):
    _db_ready(tmp_path)
    cb = _FakeCallback("checkin_reissue_yes:999999999", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))
    assert cb.message.edits == ["Не нашёл делегата — возможно, уже удалён."]


def test_reissue_go_approved_delegate_gets_new_qr_sent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))

    cb = _FakeCallback(f"checkin_reissue_yes:{DELEGATE_ID}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert cb.message.edits and cb.message.edits[0].startswith("✅ QR перевыпущен.")
    assert "отправлен делегату" in cb.message.edits[0]
    assert len(cb.bot.sent_photos) == 1
    chat_id, photo, caption = cb.bot.sent_photos[0]
    assert chat_id == DELEGATE_ID
    assert caption

    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"] != old_token
    replacement = asyncio.run(db.get_checkin_token_replacement(old_token))
    assert replacement is not None


def test_reissue_go_not_approved_delegate_skips_send_and_explains_why(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="pending"))

    cb = _FakeCallback(f"checkin_reissue_yes:{DELEGATE_ID}", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert not cb.bot.sent_photos
    assert "нет пропуска" in cb.message.edits[0]
    assert "Заявка ещё на рассмотрении" in cb.message.edits[0]


def test_reissue_go_send_photo_failure_still_reports_success_with_a_note(tmp_path):
    """Делегат заблокировал бота -- реиссью УЖЕ произошёл (новый токен в БД), доставка QR
    подвела: менеджер должен узнать оба факта, а не молча потерять результат за исключением."""
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    bot = _FakeBot()
    bot.fail_send_photo = True

    cb = _FakeCallback(f"checkin_reissue_yes:{DELEGATE_ID}", ADMIN_ID, bot=bot)
    asyncio.run(admin_checkin.checkin_reissue_go(cb))

    assert cb.message.edits[0].startswith("✅ QR перевыпущен.")
    assert "не получилось" in cb.message.edits[0]
    current = asyncio.run(db.get_user(DELEGATE_ID))
    assert current["checkin_token"]  # реиссью всё равно состоялся


# ── интеграция: старый QR после перевыпуска отвечает «QR заменён» на пути CSV-загрузки ──────

def test_csv_upload_flags_replaced_token_distinctly_from_not_found(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_insert_user(DELEGATE_ID, status="approved"))
    old_token = asyncio.run(db.get_or_create_checkin_token(DELEGATE_ID))
    asyncio.run(db.reissue_checkin_token(DELEGATE_ID))

    tag = asyncio.run(current_event_tag())
    old_payload = build_payload(tag, "Тест Тестов", "—", old_token)

    from services.forum.checkin import find_checkin_records
    records = find_checkin_records(f"{old_payload},2026-10-03 09:00:00", tag)
    assert len(records) == 1

    state = None
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))
    asyncio.run(state.update_data(checkin_records=records))

    cb = _FakeCallback("checkin_point:entry", ADMIN_ID)
    asyncio.run(admin_checkin.checkin_point_pick(cb, state))

    texts = [t for t, _rm in cb.message.sent]
    assert any("QR заменён: 1" in t for t in texts)
    assert any("не найдено: 0" in t for t in texts)
