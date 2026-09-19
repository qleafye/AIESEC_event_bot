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

import gspread

import cities
from config import config
from database import db
import services.sheets as sheets
import settings_ops


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
