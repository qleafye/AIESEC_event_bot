"""Google-таблица события — настройкой в боте («📊 Данные → 🔗 Какая таблица»).

Резолвер `services/sheet_target.py`: значение из бота, иначе `.env` (прод на .env не должен
заметить разницы); ввод ссылкой; проверка доступа сервисного аккаунта до сохранения; смена
таблицы сбрасывает кэш листа; кнопка «📄 Открыть таблицу» берёт тот же резолвер. gspread
подменён целиком — в сеть тесты не ходят.
"""
import asyncio
import json

import gspread
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import admin as admin_mod  # noqa: F401 — регистрирует шов
from handlers import admin_sheet_target as st_handlers
from handlers.admin_caps import ADMIN_CAPS
from handlers.states import SheetTarget
from services import sheet_target
import services.sheets as sheets
from tests._dbtpl import fast_init_db

SUPER = 910001
OTHER = 910002
ID_A = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
ID_B = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210-_zyxw"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "sheet_target.db"))
    fast_init_db()
    key = tmp_path / "creds.json"
    key.write_text(json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com"}), encoding="utf-8")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", str(key))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", ID_A)
    monkeypatch.setattr(config, "ADMIN_IDS", [SUPER])
    sheet_target.invalidate()
    sheets._reset_sheet_cache()
    yield
    sheet_target.invalidate()
    sheets._reset_sheet_cache()


# ── Разбор ссылки ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    f"https://docs.google.com/spreadsheets/d/{ID_B}/edit#gid=0",
    f"https://docs.google.com/spreadsheets/d/{ID_B}",
    f"docs.google.com/spreadsheets/u/0/d/{ID_B}/edit?usp=sharing",
    f"  {ID_B}  ",
    f"<https://docs.google.com/spreadsheets/d/{ID_B}/edit>",
])
def test_parse_sheet_ref_accepts_link_and_bare_id(text):
    assert sheet_target.parse_sheet_ref(text) == ID_B


@pytest.mark.parametrize("text", [None, "", "таблица", "hello", "https://example.com/x", "abc123"])
def test_parse_sheet_ref_rejects_garbage(text):
    assert sheet_target.parse_sheet_ref(text) is None


# ── Резолвер ──────────────────────────────────────────────────────────────────────────────

def test_resolver_falls_back_to_env_when_bot_value_absent(env):
    assert sheet_target.sheet_id() == ID_A
    assert sheet_target.bot_sheet_id() is None
    assert sheet_target.sheets_enabled() is True


def test_resolver_prefers_bot_value(env):
    asyncio.run(db.set_setting(sheet_target.SETTING_KEY, ID_B))
    sheet_target.invalidate()
    assert sheet_target.sheet_id() == ID_B
    assert sheet_target.sheet_url() == f"https://docs.google.com/spreadsheets/d/{ID_B}/edit"


def test_resolver_without_db_file_uses_env_and_does_not_create_file(tmp_path, monkeypatch):
    path = tmp_path / "missing.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", ID_A)
    sheet_target.invalidate()
    assert sheet_target.sheet_id() == ID_A
    assert not path.exists()
    sheet_target.invalidate()


def test_resolver_empty_everywhere_disables_sheets(env, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    assert sheet_target.sheet_id() == ""
    assert sheet_target.sheets_enabled() is False


def test_service_account_email_from_key(env):
    assert sheet_target.service_account_email() == "bot@proj.iam.gserviceaccount.com"


# ── Кэш листа сбрасывается при смене таблицы ─────────────────────────────────────────────

class _WS:
    def __init__(self, title):
        self.title = title


class _SH:
    def __init__(self, sid):
        self.sid = sid
        self.title = f"Таблица {sid[:4]}"

    def worksheet(self, title):
        return _WS(f"{self.sid}:{title}")


class _GC:
    def __init__(self, opened):
        self.opened = opened

    def open_by_key(self, sid):
        self.opened.append(sid)
        return _SH(sid)


def test_get_sheet_reopens_after_table_change(env, monkeypatch):
    opened = []
    monkeypatch.setattr(gspread, "service_account", lambda filename=None: _GC(opened))
    monkeypatch.setattr(sheets, "_load_main_tab_setting", lambda: "Регистрации")

    first = sheets._get_sheet()
    assert first.title == f"{ID_A}:Регистрации"
    assert sheets._get_sheet() is first  # кэш держится, пока таблица та же
    assert opened == [ID_A]

    asyncio.run(db.set_setting(sheet_target.SETTING_KEY, ID_B))
    sheet_target.invalidate()  # то, что делает сохранение из бота
    second = sheets._get_sheet()
    assert second.title == f"{ID_B}:Регистрации"
    assert opened == [ID_A, ID_B]


# ── Проверка доступа ─────────────────────────────────────────────────────────────────────

def test_check_access_no_access(env, monkeypatch):
    class _Denied:
        def open_by_key(self, sid):
            raise gspread.exceptions.SpreadsheetNotFound("nope")

    monkeypatch.setattr(gspread, "service_account", lambda filename=None: _Denied())
    assert sheet_target.check_access_sync(ID_B) == ("no_access", None)


def test_check_access_ok_returns_title(env, monkeypatch):
    monkeypatch.setattr(gspread, "service_account", lambda filename=None: _GC([]))
    verdict, title = sheet_target.check_access_sync(ID_B)
    assert verdict == "ok" and title.startswith("Таблица")


def test_check_access_no_key(env, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")
    assert sheet_target.check_access_sync(ID_B)[0] == "no_key"


# ── Реестр, веб, права ───────────────────────────────────────────────────────────────────

def test_registry_key_hidden_from_web_and_group_screens():
    from settings_ops import editable_keys
    from settings_schema import SETTINGS_SCHEMA
    from handlers.admin_settings import SETTINGS_FIELDS

    assert "google_sheet_id" in SETTINGS_SCHEMA
    assert "google_sheet_id" not in editable_keys()
    assert "google_sheet_id" not in {k for k, _, _ in SETTINGS_FIELDS}


def test_caps_registered_and_row_is_superadmin_only():
    from handlers.admin_sections import section_rows, visible_rows

    from handlers.admin_caps import required_capability

    for cb in ("admin_sheet_target", "sheet_target_set", "sheet_target_apply",
               "sheet_target_env", "sheet_target_env_go"):
        assert required_capability(callback_data=cb) == "settings", cb
    assert ADMIN_CAPS["state:SheetTarget:*"] == "settings"
    assert ("screen_admin", "admin_sheet_target", "🔗 Какая таблица") in section_rows("data")
    caps = {"settings"}
    assert any(r[1] == "admin_sheet_target" for r in visible_rows("data", caps, True))
    assert not any(r[1] == "admin_sheet_target" for r in visible_rows("data", caps, False))


def test_open_sheet_link_uses_resolver(env):
    from handlers.admin_sections import sheet_url

    assert sheet_url().endswith(f"/{ID_A}/edit")
    asyncio.run(db.set_setting(sheet_target.SETTING_KEY, ID_B))
    sheet_target.invalidate()
    assert sheet_url().endswith(f"/{ID_B}/edit")


def test_open_sheet_button_shown_when_only_bot_value(env, monkeypatch):
    from handlers.admin_sections import build_section_keyboard

    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    asyncio.run(db.set_setting(sheet_target.SETTING_KEY, ID_B))
    sheet_target.invalidate()
    kb = asyncio.run(build_section_keyboard("data", SUPER, caps={"settings"}))
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert f"https://docs.google.com/spreadsheets/d/{ID_B}/edit" in urls


# ── Экран бота ───────────────────────────────────────────────────────────────────────────

class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid, text=None):
        self.from_user = _User(uid)
        self.text = text
        self.sent = []

    async def answer(self, text=None, *a, reply_markup=None, **k):
        self.sent.append((text, reply_markup))


class _Cb:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_non_superadmin_is_refused_everywhere(env):
    async def go():
        st = _state(OTHER)
        for handler, data in ((st_handlers.sheet_target_screen, "admin_sheet_target"),
                              (st_handlers.sheet_target_set, "sheet_target_set"),
                              (st_handlers.sheet_target_apply, "sheet_target_apply")):
            cb = _Cb(data, OTHER)
            await handler(cb, st)
            assert cb.answers[0][1] is True and "суперадмин" in cb.answers[0][0]
            assert cb.message.sent == []
        for handler in (st_handlers.sheet_target_env, st_handlers.sheet_target_env_go):
            cb = _Cb("x", OTHER)
            await handler(cb)
            assert cb.answers[0][1] is True
        # Текст в состоянии от не-суперадмина ничего не сохраняет.
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(OTHER, ID_B)
        await st_handlers.sheet_target_receive(msg, st)
        assert await db.get_setting(sheet_target.SETTING_KEY) is None

    asyncio.run(go())


def test_bad_link_keeps_waiting_with_human_hint(env):
    async def go():
        st = _state(SUPER)
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(SUPER, "какая-то таблица")
        await st_handlers.sheet_target_receive(msg, st)
        assert "docs.google.com/spreadsheets" in msg.sent[0][0]
        assert await st.get_state() == SheetTarget.waiting_ref.state

    asyncio.run(go())


def test_no_access_names_service_account_and_does_not_save(env, monkeypatch):
    monkeypatch.setattr(sheet_target, "check_access_sync", lambda sid: ("no_access", None))

    async def go():
        st = _state(SUPER)
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(SUPER, f"https://docs.google.com/spreadsheets/d/{ID_B}/edit")
        await st_handlers.sheet_target_receive(msg, st)
        text = msg.sent[-1][0]
        assert "Настройки доступа" in text
        assert "bot@proj.iam.gserviceaccount.com" in text
        assert "Редактор" in text
        assert await db.get_setting(sheet_target.SETTING_KEY) is None
        assert await st.get_state() == SheetTarget.waiting_ref.state  # можно прислать ещё раз

    asyncio.run(go())


def test_google_error_does_not_save(env, monkeypatch):
    monkeypatch.setattr(sheet_target, "check_access_sync", lambda sid: ("error", "timeout"))

    async def go():
        st = _state(SUPER)
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(SUPER, ID_B)
        await st_handlers.sheet_target_receive(msg, st)
        assert "не ответил" in msg.sent[-1][0]
        assert await db.get_setting(sheet_target.SETTING_KEY) is None

    asyncio.run(go())


def test_ok_then_confirm_saves_and_resets_cache(env, monkeypatch):
    monkeypatch.setattr(sheet_target, "check_access_sync", lambda sid: ("ok", "YL 26/2"))
    sheets._sheet = object()  # «старая» таблица в кэше

    async def go():
        st = _state(SUPER)
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(SUPER, f"https://docs.google.com/spreadsheets/d/{ID_B}/edit#gid=0")
        await st_handlers.sheet_target_receive(msg, st)
        text, kb = msg.sent[-1]
        assert "YL 26/2" in text and "sheet_target_apply" in _cbs(kb)
        assert await db.get_setting(sheet_target.SETTING_KEY) is None  # до подтверждения — нет

        cb = _Cb("sheet_target_apply", SUPER)
        await st_handlers.sheet_target_apply(cb, st)
        assert await db.get_setting(sheet_target.SETTING_KEY) == ID_B
        assert sheet_target.sheet_id() == ID_B
        assert sheets._sheet is None
        assert "Задана здесь, в боте" in cb.message.sent[-1][0]

        # Повторное нажатие стейл-кнопки ничего не перезаписывает.
        cb2 = _Cb("sheet_target_apply", SUPER)
        await st_handlers.sheet_target_apply(cb2, st)
        assert cb2.answers[0][1] is True

    asyncio.run(go())


def test_same_table_is_not_rechecked(env, monkeypatch):
    def boom(sid):
        raise AssertionError("не должно проверять ту же таблицу")

    monkeypatch.setattr(sheet_target, "check_access_sync", boom)

    async def go():
        st = _state(SUPER)
        await st.set_state(SheetTarget.waiting_ref)
        msg = _Msg(SUPER, ID_A)
        await st_handlers.sheet_target_receive(msg, st)
        assert "уже пишет" in msg.sent[-1][0]

    asyncio.run(go())


def test_return_to_env_table(env):
    async def go():
        await db.set_setting(sheet_target.SETTING_KEY, ID_B)
        sheet_target.invalidate()
        assert "sheet_target_env" in _cbs(st_handlers.screen_kb())

        cb = _Cb("sheet_target_env", SUPER)
        await st_handlers.sheet_target_env(cb)
        assert "Вернуть" in cb.message.sent[-1][0]

        cb = _Cb("sheet_target_env_go", SUPER)
        await st_handlers.sheet_target_env_go(cb)
        assert await db.get_setting(sheet_target.SETTING_KEY) is None
        assert sheet_target.sheet_id() == ID_A
        assert "Задана при установке бота" in cb.message.sent[-1][0]
        assert "sheet_target_env" not in _cbs(st_handlers.screen_kb())

    asyncio.run(go())


def test_screen_without_any_table(env, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    text = asyncio.run(st_handlers.screen_text())
    assert "не подключена" in text
    assert "bot@proj.iam.gserviceaccount.com" in text
