"""Quick 260919-mlu: переименование вкладок Google-таблицы вместо их сиротства.

Прод-инцидент (память проекта sheet-tab-rename-trap): смена ключа-имени вкладки в настройках
не переименовывает реальный лист — `services/sheets.py::_get_sheet`/`_get_named_sheet` на
`WorksheetNotFound` заводят НОВУЮ пустую вкладку, а старая с данными остаётся сиротой. Три
поколения «Незавершённые» и два поколения «Гейма» на проде — тому доказательство.

Файл растёт по задачам квика (образец — 06-01-style структура, один файл на весь квик):
- Task 1: примитивы `services/sheets.py` (`list_worksheet_titles`, `rename_worksheet`);
- Task 2: `settings_ops.py` (`normalize_tab_prefix`, `current_tab_titles`, `plan_prefix_renames`);
- Task 3: развилка при смене одного ключа-имени (`handlers/admin_sheet_tabs.py`);
- Task 4: массовые кнопки «Добавить/Убрать префикс».

Идиомы — как в tests/test_sheets_main_tab_pin_260813.py / test_sheets_admin_alert.py:
plain `def test_*`, `asyncio.run(go())`, monkeypatch, никаких реальных вызовов Google.
"""
import asyncio

from aiogram.types import InlineKeyboardButton
import gspread
import pytest

import cities
from config import config
from database import db
from handlers import admin_sheet_tabs
from handlers.admin_caps import ADMIN_CAPS
import services.sheets as sheets
import settings_ops


@pytest.fixture(autouse=True)
def _restore_cities_registry():
    """Task 4 тесты зовут `cities.set_cities_for_test(...)` (город «spb» для проверки
    городских вкладок) и не восстанавливают реестр сами — без этой авто-фикстуры оставленный
    тестовый город утекает в СЛЕДУЮЩИЙ тест, запущенный в том же xdist-воркере (обнаружено:
    `test_admin_percity_ui.py` падал только в общем прогоне, никогда в одиночном — классический
    симптом утечки `cities.CITIES`, того же рода, что `tests/test_game_city_tabs_260820.py::
    _city_registry`). Автouse, потому что затрагивает ВЕСЬ файл дёшево и безопасно — Task 1/3
    тесты `cities` не трогают вовсе, Task 2 восстанавливает реестр сама через
    `_with_city_registry`, что с этой фикстурой просто идемпотентно."""
    saved = cities.all_cities()
    yield
    cities.set_cities_for_test(saved)


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_tab_rename_260919.db")


def _db_ready(tmp_path):
    """Task 2+ хелперы (settings_ops) читают/пишут реальные bot_settings — в отличие от Task 1
    (моки Google, настройки не нужны), здесь БД нужна инициализированной."""
    config.DB_PATH = str(tmp_path / "test_sheet_tab_rename_260919_ops.db")
    asyncio.run(db.init_db())


def _reset_sheets_module_state():
    """Mirrors tests/test_sheets_main_tab_pin_260813.py::_reset_module_state."""
    sheets._reset_sheet_cache()
    sheets.set_alert_bot(None)
    sheets._alert_bot_warned = False
    sheets._startup_tab_warning_sent = False


# ── Fake gspread plumbing ────────────────────────────────────────────────────────────────

class _FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self._rows = rows if rows is not None else [["header"]]
        self.update_title_calls = []

    def get_all_values(self):
        return self._rows

    def update_title(self, new_title):
        self.update_title_calls.append(new_title)
        self.title = new_title


class _FakeSpreadsheet:
    """List (not dict) of worksheets, mirroring gspread's real `worksheets()` ordering — a
    dict keyed by title would go stale the moment `update_title` renames one in place."""

    def __init__(self, titles):
        self._list = [_FakeWorksheet(t) for t in titles]

    def worksheet(self, title):
        for ws in self._list:
            if ws.title == title:
                return ws
        raise gspread.WorksheetNotFound(title)

    def worksheets(self):
        return list(self._list)

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._list.append(ws)
        return ws


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread_client(monkeypatch, titles):
    fake_ss = _FakeSpreadsheet(titles)
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake_ss


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: services/sheets.py primitives
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_rename_worksheet_ok(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Незавершённые", "Краткая"])

    result = asyncio.run(sheets.rename_worksheet("Незавершённые", "NOT FILLED REGS"))

    assert result == "ok"
    ws = fake_ss.worksheet("NOT FILLED REGS")
    assert ws.title == "NOT FILLED REGS"
    assert ws.get_all_values() == [["header"]]  # данные на месте, лист тот же объект


def test_rename_worksheet_not_found(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Краткая"])

    result = asyncio.run(sheets.rename_worksheet("Нет такой", "Новое имя"))

    assert result == "not_found"
    assert all(not ws.update_title_calls for ws in fake_ss.worksheets())


def test_rename_worksheet_duplicate(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    fake_ss = _patch_gspread_client(monkeypatch, ["Незавершённые", "NOT FILLED REGS"])

    result = asyncio.run(sheets.rename_worksheet("Незавершённые", "NOT FILLED REGS"))

    assert result == "duplicate"
    titles = {ws.title for ws in fake_ss.worksheets()}
    assert titles == {"Незавершённые", "NOT FILLED REGS"}  # ни один title не тронут


def test_rename_worksheet_error_alerts_admins_and_does_not_raise(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])

    def boom(old, new):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_rename_worksheet_sync", boom)
    fake_bot = _FakeBot()
    sheets.set_alert_bot(fake_bot)

    result = asyncio.run(sheets.rename_worksheet("Старое", "Новое"))

    assert result == "error"
    assert [chat_id for chat_id, _ in fake_bot.sent] == [111, 222]


def test_rename_worksheet_ok_resets_caches(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Гейма", "Гейма бот"])

    # Предзаполняем кэши вручную — как если бы бот только что писал в обе вкладки.
    sheets._sheet = object()
    sheets._named_sheets["Гейма"] = object()
    sheets._named_sheets["Гейма бот"] = object()
    sheets._header_checked_tabs.add("Гейма")
    sheets._header_checked_tabs.add("Гейма бот")

    result = asyncio.run(sheets.rename_worksheet("Гейма", "Гейма бот 2"))

    assert result == "ok"
    assert sheets._sheet is None
    assert "Гейма" not in sheets._named_sheets
    assert "Гейма" not in sheets._header_checked_tabs
    assert "Гейма бот 2" not in sheets._named_sheets
    assert "Гейма бот 2" not in sheets._header_checked_tabs


def test_rename_worksheet_skipped_on_same_or_empty_names(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Краткая"])

    assert asyncio.run(sheets.rename_worksheet("Краткая", "Краткая")) == "skipped"
    assert asyncio.run(sheets.rename_worksheet("", "Новое")) == "skipped"
    assert asyncio.run(sheets.rename_worksheet("Краткая", "")) == "skipped"


def test_list_worksheet_titles_unconfigured_returns_none(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")

    assert asyncio.run(sheets.list_worksheet_titles()) is None


def test_list_worksheet_titles_returns_order(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    _reset_sheets_module_state()
    _patch_gspread_client(monkeypatch, ["Первая", "Реги бот", "Краткая"])

    titles = asyncio.run(sheets.list_worksheet_titles())

    assert titles == ["Первая", "Реги бот", "Краткая"]


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: settings_ops.py — normalize_tab_prefix / current_tab_titles / plan_prefix_renames
# ═══════════════════════════════════════════════════════════════════════════════════════════

_CITIES_T2 = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _with_city_registry(rows, fn):
    """save/restore cities registry вокруг вызова — тот же приём, что
    tests/test_game_city_tabs_260820.py::_city_registry, но без autouse-фикстуры (Task 1
    тесты этого файла cities вообще не трогают)."""
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in rows])
    try:
        return fn()
    finally:
        cities.set_cities_for_test(saved)


def test_normalize_tab_prefix_trailing_space_and_dash():
    assert settings_ops.normalize_tab_prefix("🤖") == "🤖 "
    assert settings_ops.normalize_tab_prefix("🤖 ") == "🤖 "
    assert settings_ops.normalize_tab_prefix("-") == ""
    assert settings_ops.normalize_tab_prefix("") == ""
    assert settings_ops.normalize_tab_prefix(None) == ""


def test_current_tab_titles_collects_keys_and_city_tracks_skips_moscow_and_preselect(tmp_path):
    _db_ready(tmp_path)

    targets = _with_city_registry(
        _CITIES_T2, lambda: asyncio.run(settings_ops.current_tab_titles()),
    )

    assert "preselect_tab" not in [t.key for t in targets]
    assert not any(t.city_code == "msk" for t in targets)  # пустая база — не собираем

    titles = [t.title for t in targets]
    for expected in (
        "Краткая", "Party", "Незавершённые", "Опросы", "Гейма", "История сдач",
        "История правок", "Вопросы",
    ):
        assert expected in titles

    spb_targets = [t for t in targets if t.city_code == "spb"]
    assert {t.title for t in spb_targets} == {
        "СПб", "СПб Акция", "СПб Party", "СПб Незавершённые", "СПб Гейма", "СПб История сдач",
    }
    assert len(spb_targets) == 6
    spb_main = next(t for t in spb_targets if t.kind == "main")
    assert spb_main.origin == "city"
    assert "Санкт-Петербург" in spb_main.label


def test_current_tab_titles_main_sheet_tab_missing_falls_back_to_env(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги из .env")

    targets = _with_city_registry([], lambda: asyncio.run(settings_ops.current_tab_titles()))

    main_targets = [t for t in targets if t.key == "main_sheet_tab"]
    assert len(main_targets) == 1
    assert main_targets[0].title == "Реги из .env"


def test_current_tab_titles_main_sheet_tab_fully_unset_is_skipped(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "")

    targets = _with_city_registry([], lambda: asyncio.run(settings_ops.current_tab_titles()))

    assert not any(t.key == "main_sheet_tab" for t in targets)


def _tab_target(title, key="k"):
    return settings_ops.TabTarget(
        title=title, origin="key", key=key, city_code=None, kind=None, label=title,
    )


def test_plan_prefix_renames_add_skips_missing_and_occupied():
    targets = [
        _tab_target("Незавершённые", "incomplete_sheet_tab"),
        _tab_target("Гейма", "game_matrix_tab"),
        _tab_target("Нет в таблице", "x"),
        _tab_target("Занятое", "y"),
    ]
    existing = ["Незавершённые", "Гейма", "Занятое", "🤖 Занятое"]

    renames, skipped = settings_ops.plan_prefix_renames(targets, existing, "🤖 ", add=True)

    assert (targets[0], "Незавершённые", "🤖 Незавершённые") in renames
    assert (targets[1], "Гейма", "🤖 Гейма") in renames
    assert any(r[1] == "Нет в таблице" and r[2] == "нет в таблице" for r in skipped)
    assert any(r[1] == "Занятое" and r[2] == "имя занято" for r in skipped)


def test_plan_prefix_renames_add_is_idempotent_on_second_pass():
    targets = [_tab_target("Незавершённые"), _tab_target("Гейма")]
    existing = ["Незавершённые", "Гейма"]
    renames, _ = settings_ops.plan_prefix_renames(targets, existing, "🤖 ", add=True)
    assert len(renames) == 2

    prefixed_targets = [
        _tab_target(new, t.key) for t, _old, new in renames
    ]
    prefixed_existing = [new for _t, _old, new in renames]

    renames_2, skipped_2 = settings_ops.plan_prefix_renames(
        prefixed_targets, prefixed_existing, "🤖 ", add=True,
    )
    assert renames_2 == []
    assert all(reason == "уже с префиксом" for _t, _old, reason in skipped_2)


def test_plan_prefix_renames_round_trip_add_then_remove_restores_names():
    names = ["Незавершённые", "Краткая", "Party", "Опросы", "История правок", "Вопросы"]
    targets = [_tab_target(n, n) for n in names]
    prefix = "🤖 "

    renames, skipped = settings_ops.plan_prefix_renames(targets, names, prefix, add=True)
    assert len(renames) == 6
    assert skipped == []

    prefixed_titles = [new for _t, _old, new in renames]
    prefixed_targets = [_tab_target(new, t.key) for t, _old, new in renames]

    back_renames, back_skipped = settings_ops.plan_prefix_renames(
        prefixed_targets, prefixed_titles, prefix, add=False,
    )
    assert sorted(new for _t, _old, new in back_renames) == sorted(names)
    assert back_skipped == []


def test_plan_prefix_renames_empty_prefix_skips_everything_both_directions():
    targets = [_tab_target("Незавершённые"), _tab_target("🤖 Гейма")]
    existing = ["Незавершённые", "🤖 Гейма"]

    add_renames, add_skipped = settings_ops.plan_prefix_renames(targets, existing, "", add=True)
    del_renames, del_skipped = settings_ops.plan_prefix_renames(targets, existing, "", add=False)

    assert add_renames == []
    assert del_renames == []
    assert all(reason == "без префикса" for _t, _old, reason in add_skipped)
    assert all(reason == "без префикса" for _t, _old, reason in del_skipped)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 3: handlers/admin_sheet_tabs.py — развилка при смене одного ключа-имени
# ═══════════════════════════════════════════════════════════════════════════════════════════

ADMIN_ID_T3 = 931919


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeCallback:
    """Тот же минимальный набор, что tests/test_sheet_tabs_settings_260815.py::
    _FakeSettingsCallback -- edit_text/answer, ничего больше."""

    def __init__(self, uid=ADMIN_ID_T3):
        self.from_user = _FakeUser(uid)
        self.message = self
        self.edited_text = None
        self.edited_markup = None
        self.answered = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited_text = text
        self.edited_markup = reply_markup

    async def answer(self, text=None, show_alert=False):
        self.answered.append((text, show_alert))


class _FakeFSMState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def _t3_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_tab_rename_260919_t3.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID_T3]


def test_tab_change_screen_old_absent_returns_none_regardless_of_new(tmp_path, monkeypatch):
    _t3_ready(tmp_path)

    async def probe_missing(title):
        return (False, 0)

    monkeypatch.setattr(admin_sheet_tabs, "tab_row_count", probe_missing)

    assert asyncio.run(admin_sheet_tabs.tab_change_screen("game_matrix_tab", "", "Новая")) is None
    assert asyncio.run(
        admin_sheet_tabs.tab_change_screen("game_matrix_tab", "Старая", "Новая"),
    ) is None  # probe_missing means old_probe[0] is False -- "no old tab" branch


def test_tab_change_screen_old_exists_new_absent_offers_rename_and_newtab(tmp_path, monkeypatch):
    _t3_ready(tmp_path)

    async def probe(title):
        if title == "Незавершённые":
            return (True, 956)
        return (False, 0)

    monkeypatch.setattr(admin_sheet_tabs, "tab_row_count", probe)

    screen = asyncio.run(
        admin_sheet_tabs.tab_change_screen("incomplete_sheet_tab", "Незавершённые", "NOT FILLED"),
    )
    assert screen is not None
    text, kb = screen
    assert "Незавершённые" in text
    assert "956" in text
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == ["sheet_tab_rename_go", "sheet_tab_newtab_go", "sheets_tab_cancel"]


def test_tab_change_screen_old_exists_new_exists_offers_reuse_only(tmp_path, monkeypatch):
    _t3_ready(tmp_path)

    async def probe(title):
        if title == "Незавершённые":
            return (True, 956)
        if title == "NOT FILLED REGS":
            return (True, 432)
        return (False, 0)

    monkeypatch.setattr(admin_sheet_tabs, "tab_row_count", probe)

    screen = asyncio.run(
        admin_sheet_tabs.tab_change_screen(
            "incomplete_sheet_tab", "Незавершённые", "NOT FILLED REGS",
        ),
    )
    assert screen is not None
    text, kb = screen
    assert "Незавершённые" in text and "NOT FILLED REGS" in text
    assert "956" in text and "432" in text
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callback_datas == ["sheet_tab_reuse_go", "sheets_tab_cancel"]
    assert "sheet_tab_rename_go" not in callback_datas


def test_sheet_tab_rename_go_ok_saves_setting_and_calls_rename_worksheet(tmp_path, monkeypatch):
    _t3_ready(tmp_path)
    calls = []

    async def fake_rename(old, new):
        calls.append((old, new))
        return "ok"

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)

    state = _FakeFSMState({
        "pending_tab_key": "incomplete_sheet_tab",
        "pending_tab_value": "NOT FILLED REGS",
        "pending_tab_old": "Незавершённые",
    })
    callback = _FakeCallback()

    async def go():
        await admin_sheet_tabs.sheet_tab_rename_go(callback, state)
        return await db.get_setting("incomplete_sheet_tab")

    saved = asyncio.run(go())
    assert saved == "NOT FILLED REGS"
    assert calls == [("Незавершённые", "NOT FILLED REGS")]
    assert "переименована" in callback.edited_text.lower()
    assert state._data == {}


def test_sheet_tab_rename_go_duplicate_does_not_save_setting(tmp_path, monkeypatch):
    _t3_ready(tmp_path)

    async def fake_rename(old, new):
        return "duplicate"

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)

    state = _FakeFSMState({
        "pending_tab_key": "incomplete_sheet_tab",
        "pending_tab_value": "NOT FILLED REGS",
        "pending_tab_old": "Незавершённые",
    })
    callback = _FakeCallback()

    async def go():
        await admin_sheet_tabs.sheet_tab_rename_go(callback, state)
        return await db.get_setting("incomplete_sheet_tab")

    saved = asyncio.run(go())
    assert saved is None
    assert "не получилось" in callback.edited_text.lower()


def test_sheet_tab_rename_go_main_sheet_tab_saves_even_when_old_came_from_env(
    tmp_path, monkeypatch,
):
    """Ловушка резолва основной вкладки: старое имя пришло НЕ из bot_settings (та пуста), а
    из .env -- после успешного переименования bot_settings.main_sheet_tab обязан получить
    новое имя, иначе ступень 1 резолва останется пустой и следующий же вызов _get_sheet
    угадает ступень 2/3 заново (или упадёт, если .env тоже поменяли)."""
    _t3_ready(tmp_path)
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "Реги из .env")

    async def fake_rename(old, new):
        assert old == "Реги из .env"
        return "ok"

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)

    state = _FakeFSMState({
        "pending_tab_key": "main_sheet_tab",
        "pending_tab_value": "Реги бот",
        "pending_tab_old": "Реги из .env",
    })
    callback = _FakeCallback()

    async def go():
        await admin_sheet_tabs.sheet_tab_rename_go(callback, state)
        return await db.get_setting("main_sheet_tab")

    assert asyncio.run(go()) == "Реги бот"


def test_sheet_tab_newtab_go_saves_setting_and_never_touches_old_tab(tmp_path, monkeypatch):
    def fail_if_called(*a, **kw):
        raise AssertionError("sheet_tab_newtab_go must never call rename_worksheet")

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fail_if_called)
    _t3_ready(tmp_path)

    state = _FakeFSMState({
        "pending_tab_key": "incomplete_sheet_tab",
        "pending_tab_value": "Новая пустая",
        "pending_tab_old": "Незавершённые",
    })
    callback = _FakeCallback()

    async def go():
        await admin_sheet_tabs.sheet_tab_newtab_go(callback, state)
        return await db.get_setting("incomplete_sheet_tab")

    saved = asyncio.run(go())
    assert saved == "Новая пустая"
    assert "Незавершённые" in callback.edited_text  # предупреждение про оставленный лист


def test_sheet_tab_reuse_go_saves_setting(tmp_path):
    _t3_ready(tmp_path)
    state = _FakeFSMState({
        "pending_tab_key": "incomplete_sheet_tab", "pending_tab_value": "NOT FILLED REGS",
    })
    callback = _FakeCallback()

    async def go():
        await admin_sheet_tabs.sheet_tab_reuse_go(callback, state)
        return await db.get_setting("incomplete_sheet_tab")

    assert asyncio.run(go()) == "NOT FILLED REGS"


def test_new_task3_callbacks_are_capability_mapped():
    for cb in ("sheet_tab_rename_go", "sheet_tab_reuse_go", "sheet_tab_newtab_go"):
        assert ADMIN_CAPS.get(cb) == "settings"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Task 4: массовые кнопки «Добавить/Убрать префикс»
# ═══════════════════════════════════════════════════════════════════════════════════════════

ADMIN_ID_T4 = 931944
_T4_CITY = {
    "code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1,
}
# 8 ключей-целей (main_sheet_tab не настроен -- пропущен) + 6 городских (СПб: main + 5 треков).
_T4_INITIAL_TITLES = [
    "Краткая", "Party", "Незавершённые", "Опросы", "Гейма", "История сдач",
    "История правок", "Вопросы",
    "СПб", "СПб Акция", "СПб Party", "СПб Незавершённые", "СПб Гейма", "СПб История сдач",
]


def _t4_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_tab_rename_260919_t4.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID_T4]
    asyncio.run(db.insert_city("spb", "Санкт-Петербург", "СПб", 1))
    cities.set_cities_for_test([dict(_T4_CITY)])


def _make_fake_sheet(titles):
    """Стейтфул-мок Google-таблицы: rename_worksheet реально переименовывает в списке, чтобы
    второй прогон плана видел уже переименованные листы (идемпотентность / round-trip)."""
    state = {"titles": list(titles)}
    call_order = []

    async def fake_list_titles():
        return list(state["titles"])

    async def fake_rename(old, new):
        call_order.append((old, new))
        if old not in state["titles"]:
            return "not_found"
        if new in state["titles"]:
            return "duplicate"
        state["titles"][state["titles"].index(old)] = new
        return "ok"

    return state, call_order, fake_list_titles, fake_rename


def test_prefix_add_screen_shows_plan_without_a_single_write(tmp_path, monkeypatch):
    _t4_ready(tmp_path)
    _state, _calls, fake_list_titles, _fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)
    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)

    def fail_if_called(*a, **kw):
        raise AssertionError("экран подтверждения не должен звать rename_worksheet")

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fail_if_called)
    callback = _FakeCallback(uid=ADMIN_ID_T4)

    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add(callback))

    button_texts = [b.text for row in callback.edited_markup.inline_keyboard for b in row]
    button_datas = [b.callback_data for row in callback.edited_markup.inline_keyboard for b in row]
    assert any("14 вкладок" in t for t in button_texts)
    assert "sheet_tabs_prefix_add_go" in button_datas
    assert "🎯 Отобранные" in callback.edited_text
    assert "IMPORTRANGE" in callback.edited_text


def test_prefix_add_go_renames_saves_settings_and_updates_city_base_once(tmp_path, monkeypatch):
    _t4_ready(tmp_path)
    state, _calls, fake_list_titles, fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)
    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)
    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)

    city_update_calls = []

    async def fake_update_city(code, *, tab_base=None, **kw):
        city_update_calls.append((code, tab_base))
        return True

    delete_calls = []

    async def fake_delete_setting_by_admin(admin_id, key):
        delete_calls.append(key)

    async def fake_reload_cities():
        return []

    monkeypatch.setattr(admin_sheet_tabs, "update_city", fake_update_city)
    monkeypatch.setattr(admin_sheet_tabs, "delete_setting_by_admin", fake_delete_setting_by_admin)
    monkeypatch.setattr(admin_sheet_tabs, "reload_cities", fake_reload_cities)

    callback = _FakeCallback(uid=ADMIN_ID_T4)

    async def go():
        await admin_sheet_tabs.sheet_tabs_prefix_add_go(callback)
        return await db.get_setting("short_sheet_tab"), await db.get_setting("incomplete_sheet_tab")

    short_tab, incomplete_tab = asyncio.run(go())

    assert short_tab == "🤖 Краткая"
    assert incomplete_tab == "🤖 Незавершённые"
    assert "🤖 Краткая" in state["titles"]
    assert "🤖 СПб" in state["titles"]
    assert city_update_calls == [("spb", "🤖 СПб")]  # РОВНО один раз
    assert delete_calls == ["city_tab__spb"]
    assert "Переименовано 14" in callback.edited_text


def test_prefix_add_go_track_tabs_renamed_before_city_main(tmp_path, monkeypatch):
    _t4_ready(tmp_path)
    _state, calls, fake_list_titles, fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)
    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)
    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)
    monkeypatch.setattr(admin_sheet_tabs, "update_city", lambda *a, **kw: _noop_true())
    monkeypatch.setattr(admin_sheet_tabs, "reload_cities", lambda: _noop_none())

    callback = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add_go(callback))

    city_call_olds = [old for old, _new in calls if old in _T4_INITIAL_TITLES[8:]]
    assert city_call_olds[-1] == "СПб"  # главный лист города переименован ПОСЛЕДНИМ


async def _noop_true():
    return True


async def _noop_none():
    return None


def test_prefix_add_go_error_on_one_tab_does_not_stop_the_rest(tmp_path, monkeypatch):
    _t4_ready(tmp_path)
    state, _calls, fake_list_titles, fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)

    async def flaky_rename(old, new):
        if old == "Гейма":
            return "error"
        return await fake_rename(old, new)

    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)
    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", flaky_rename)
    monkeypatch.setattr(admin_sheet_tabs, "update_city", lambda *a, **kw: _noop_true())
    monkeypatch.setattr(admin_sheet_tabs, "reload_cities", lambda: _noop_none())

    callback = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add_go(callback))

    assert "Переименовано 13" in callback.edited_text
    assert "Не удалось 1" in callback.edited_text
    assert "Гейма" in state["titles"]  # не переименован -- осталось старое имя
    assert "🤖 Гейма" not in state["titles"]


def test_prefix_add_go_twice_second_run_gives_empty_plan(tmp_path, monkeypatch):
    _t4_ready(tmp_path)
    state, _calls, fake_list_titles, fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)
    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)
    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)
    monkeypatch.setattr(admin_sheet_tabs, "update_city", lambda *a, **kw: _noop_true())
    monkeypatch.setattr(admin_sheet_tabs, "reload_cities", lambda: _noop_none())

    callback = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add_go(callback))
    callback2 = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add_go(callback2))

    assert "Переименовано 0" in callback2.edited_text


def test_prefix_round_trip_add_then_remove_restores_original_names(tmp_path, monkeypatch):
    """update_city/reload_cities НЕ мокаются заглушкой — только spy поверх настоящей функции:
    второй прогон плана (del) должен увидеть РЕАЛЬНО обновлённую базу города, иначе
    current_tab_titles() на втором прогоне продолжит считать базу старой и «Убрать префикс»
    не найдёт городские вкладки (см. инцидент этого же теста при разработке — реестр city_tab_base
    читает настоящую таблицу cities, не FSM/память теста)."""
    _t4_ready(tmp_path)
    state, _calls, fake_list_titles, fake_rename = _make_fake_sheet(_T4_INITIAL_TITLES)
    real_update_city = admin_sheet_tabs.update_city
    city_update_calls = []

    async def spy_update_city(code, *, tab_base=None, **kw):
        city_update_calls.append((code, tab_base))
        return await real_update_city(code, tab_base=tab_base, **kw)

    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", fake_list_titles)
    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fake_rename)
    monkeypatch.setattr(admin_sheet_tabs, "update_city", spy_update_city)
    # reload_cities НЕ мокается -- должен реально освежить cities.CITIES из БД.

    add_cb = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add_go(add_cb))
    assert "Переименовано 14" in add_cb.edited_text

    del_cb = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_del_go(del_cb))
    assert "Переименовано 14" in del_cb.edited_text

    assert sorted(state["titles"]) == sorted(_T4_INITIAL_TITLES)
    assert asyncio.run(db.get_setting("short_sheet_tab")) == "Краткая"
    assert city_update_calls == [("spb", "🤖 СПб"), ("spb", "СПб")]


def test_prefix_screen_reports_table_unavailable_when_titles_is_none(tmp_path, monkeypatch):
    _t4_ready(tmp_path)

    async def unavailable():
        return None

    monkeypatch.setattr(admin_sheet_tabs, "list_worksheet_titles", unavailable)

    def fail_if_called(*a, **kw):
        raise AssertionError("не должен звать rename_worksheet, когда таблица недоступна")

    monkeypatch.setattr(admin_sheet_tabs, "rename_worksheet", fail_if_called)

    callback = _FakeCallback(uid=ADMIN_ID_T4)
    asyncio.run(admin_sheet_tabs.sheet_tabs_prefix_add(callback))

    assert "недоступна" in callback.edited_text.lower() or "не удалось" in callback.edited_text.lower()
    assert callback.edited_markup.inline_keyboard == [
        [InlineKeyboardButton(text="← Отмена", callback_data="sheets_tab_cancel")],
    ]


def test_new_task4_callbacks_are_capability_mapped():
    for cb in (
        "sheet_tabs_prefix_add", "sheet_tabs_prefix_del",
        "sheet_tabs_prefix_add_go", "sheet_tabs_prefix_del_go",
    ):
        assert ADMIN_CAPS.get(cb) == "settings"
