"""Quick 260907-4ai — P0 SkillUp5: raw_button в журнале согласий.

Какой ИМЕННО текст был на кнопке в момент подписи — подпись (`consent_button_text`)
редактируемая настройка, и без снимка нечем доказать, что именно нажал делегат, если
подпись сменили ПОСЛЕ. Покрывает: миграцию колонки, запись record_user_consent,
services.consent.tapped_button_text и обе кнопочные точки принятия (reg_flow/reg_consent) +
Mini App (снимок настройки — там разметки кнопки на сервере нет).

Без pytest-asyncio (как везде в проекте): asyncio.run(...) + config.DB_PATH на tmp_path,
стиль FakeCallback/FakeMessage — как в tests/test_consent_versioning_260822.py.
"""
import asyncio
import sqlite3

from config import config
from database import db
from services import consent as consent_svc

ADMIN_ID = 900907


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "consent_raw_button.db")
    asyncio.run(db.init_db())


def _raw_button_rows(user_id: int) -> list[tuple]:
    con = sqlite3.connect(config.DB_PATH)
    rows = con.execute(
        "SELECT consent_key, raw_button FROM user_consents WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    con.close()
    return rows


class _Btn:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "tester"


class FakeMessage:
    def __init__(self, reply_markup=None):
        self.reply_markup = reply_markup
        self.markup_edits = 0
        self.sent = []
        self.chat = FakeUser(0)  # placeholder, overwritten per-test

    async def answer(self, text=None, reply_markup=None, parse_mode=None, **kw):
        self.sent.append((text, reply_markup))

    async def answer_document(self, file_id, caption=None, reply_markup=None, parse_mode=None):
        self.sent.append((caption, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits += 1
        self.reply_markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, reply_markup=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(reply_markup=reply_markup)
        self.message.chat = FakeUser(user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


# ── init_db: миграция колонки raw_button ─────────────────────────────────────────────────

def test_init_db_on_empty_db_creates_raw_button_column(tmp_path):
    _ready(tmp_path)
    con = sqlite3.connect(config.DB_PATH)
    cols = {row[1] for row in con.execute("PRAGMA table_info(user_consents)")}
    con.close()
    assert "raw_button" in cols


def test_init_db_on_existing_db_adds_column_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE user_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            consent_key TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            consent_version TEXT,
            UNIQUE(user_id, consent_key, consent_version)
        );
        CREATE INDEX idx_consents_user ON user_consents(user_id);
        INSERT INTO user_consents (user_id, consent_key, accepted_at, consent_version)
            VALUES (7, 'data', '2026-07-01T10:00:00', NULL);
        """
    )
    con.commit()
    # Гейт одноразовой МСК-миграции (database.db._migrate_local_timestamps_to_msk, квик
    # 260912-mcj) на UTC-хосте (CI) сдвинул бы accepted_at на +3 часа -- этот тест про
    # миграцию колонки raw_button, не про сдвиг времени, поэтому помечаем гейт уже пройденным.
    con.execute(f"PRAGMA user_version = {db._MSK_MIGRATION_USER_VERSION}")
    con.commit()
    con.close()

    config.DB_PATH = str(path)
    asyncio.run(db.init_db())
    asyncio.run(db.init_db())  # идемпотентно

    con = sqlite3.connect(path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(user_consents)")}
    assert "raw_button" in cols
    rows = con.execute(
        "SELECT id, user_id, consent_key, accepted_at, raw_button FROM user_consents ORDER BY id"
    ).fetchall()
    con.close()
    assert rows == [(1, 7, "data", "2026-07-01T10:00:00", None)]


# ── record_user_consent(raw_button=...) ──────────────────────────────────────────────────

def test_record_user_consent_without_raw_button_writes_null(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.record_user_consent(1, "data"))
    assert _raw_button_rows(1) == [("data", None)]


def test_record_user_consent_with_raw_button_writes_text(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.record_user_consent(2, "data", raw_button="Согласен(-на)"))
    assert _raw_button_rows(2) == [("data", "Согласен(-на)")]


# ── services.consent.tapped_button_text ──────────────────────────────────────────────────

def test_tapped_button_text_returns_matching_button_label():
    cb = FakeCallback("consent_accept:data", reply_markup=_Markup([
        [_Btn("Принимаю", "consent_accept:data")]
    ]))
    assert consent_svc.tapped_button_text(cb) == "Принимаю"


def test_tapped_button_text_none_when_no_markup():
    cb = FakeCallback("consent_accept:data", reply_markup=None)
    assert consent_svc.tapped_button_text(cb) is None


def test_tapped_button_text_none_when_callback_data_does_not_match():
    cb = FakeCallback("consent_accept:data", reply_markup=_Markup([
        [_Btn("Принимаю", "consent_accept:other")]
    ]))
    assert consent_svc.tapped_button_text(cb) is None


def test_tapped_button_text_none_on_broken_structure_never_raises():
    class _Broken:
        pass
    cb = FakeCallback("consent_accept:data", reply_markup=_Broken())
    assert consent_svc.tapped_button_text(cb) is None

    cb2 = FakeCallback("consent_accept:data", reply_markup=_Markup([["not-a-button"]]))
    assert consent_svc.tapped_button_text(cb2) is None


# ── reg_flow.py::process_consent_accept ──────────────────────────────────────────────────

def _fsm_state(user_id):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def test_consent_accept_records_tapped_button_text_even_if_setting_changed_after(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import reg_flow as reg_flow_mod

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(reg_flow_mod, "_ask_full_name", _noop)
    monkeypatch.setattr(reg_flow_mod, "_ask_step_or_recall", _noop)

    asyncio.run(db.set_setting("consent_button_text", "Согласен(-на)"))

    async def go():
        state = _fsm_state(11)
        await state.update_data(_consent_key="data", _consent_queue=[], _consent_i=0)
        cb = FakeCallback("consent_accept:data", user_id=11, reply_markup=_Markup([
            [_Btn("Принимаю", "consent_accept:data")]
        ]))
        # Настройка меняется ПОСЛЕ отрисовки карточки, но ДО обработки тапа — снимок должен
        # остаться "Принимаю", а не текущее значение настройки.
        await db.set_setting("consent_button_text", "Другая подпись")
        await reg_flow_mod.process_consent_accept(cb, state, bot=None)
        return cb

    cb = asyncio.run(go())
    assert _raw_button_rows(11) == [("data", "Принимаю")]
    assert cb.message.markup_edits == 1


def test_consent_accept_falls_back_to_setting_when_markup_lost(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import reg_flow as reg_flow_mod

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(reg_flow_mod, "_ask_full_name", _noop)
    monkeypatch.setattr(reg_flow_mod, "_ask_step_or_recall", _noop)

    asyncio.run(db.set_setting("consent_button_text", "Своя подпись"))

    async def go():
        state = _fsm_state(12)
        await state.update_data(_consent_key="data", _consent_queue=[], _consent_i=0)
        cb = FakeCallback("consent_accept:data", user_id=12, reply_markup=None)  # markup потерян
        await reg_flow_mod.process_consent_accept(cb, state, bot=None)

    asyncio.run(go())
    assert _raw_button_rows(12) == [("data", "Своя подпись")]


def test_consent_accept_falls_back_to_default_literal_when_setting_empty(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import reg_flow as reg_flow_mod

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(reg_flow_mod, "_ask_full_name", _noop)
    monkeypatch.setattr(reg_flow_mod, "_ask_step_or_recall", _noop)

    async def go():
        state = _fsm_state(13)
        await state.update_data(_consent_key="data", _consent_queue=[], _consent_i=0)
        cb = FakeCallback("consent_accept:data", user_id=13, reply_markup=None)
        await reg_flow_mod.process_consent_accept(cb, state, bot=None)

    asyncio.run(go())
    assert _raw_button_rows(13) == [("data", "Согласен(-на)")]


# ── reg_consent.py::consent_renew_accept ─────────────────────────────────────────────────

def test_consent_renew_accept_records_tapped_button_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    from handlers import reg_consent

    asyncio.run(db.set_setting("consent_enabled", "on"))
    asyncio.run(db.set_setting("consent_recollect_enabled", "on"))
    asyncio.run(db.set_setting("consent_list", "Согласие на обработку данных|data"))
    asyncio.run(db.set_setting("consent_button_text", "Согласен(-на)"))
    asyncio.run(db.record_user_consent(21, "data"))
    asyncio.run(db.set_setting("consent_version", "v2"))  # старая редакция -> гейт активен

    cb = FakeCallback("consent_renew:data", user_id=21, reply_markup=_Markup([
        [_Btn("Принимаю ещё раз", "consent_renew:data")]
    ]))
    asyncio.run(reg_consent.consent_renew_accept(cb))

    rows = _raw_button_rows(21)
    assert rows[-1] == ("data", "Принимаю ещё раз")


# ── Mini App: draft_consent (снимок настройки, разметки на сервере нет) ──────────────────

def test_miniapp_draft_consent_records_setting_value(tmp_path):
    _ready(tmp_path)
    from miniapp.deps import Principal
    from miniapp.routers import form as form_mod

    asyncio.run(db.set_setting("consent_button_text", "Согласен(-на)"))
    principal = Principal(telegram_id=31, via="initdata", caps=frozenset(), city=None)

    result = asyncio.run(form_mod.draft_consent(key="personal_data", p=principal, _=principal))
    assert result == {"ok": True, "key": "personal_data"}
    assert _raw_button_rows(31) == [("personal_data", "Согласен(-на)")]
