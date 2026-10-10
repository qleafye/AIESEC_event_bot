"""Phase 33 (delegate-card admin actions) — «🔍 Сверить с БД» (33-SEED, п.5 карточки заменён
сверкой: недоставленные решения всё равно искать только по БД).

Два уровня фейков, по образцу соседних квиков:

- Tier 1 (gspread-уровень, `_FakeSpreadsheet`/`_FakeWorksheet` — те же классы, что
  `tests/test_city_move_260925.py`, `_FakeWorksheet` дополнен `get_all_values()`): читающая
  половина `build_report` и добавление недостающих строк (`apply_append_missing`) — реальный
  `services.sheets._open_named_or_main_sync`/`list_worksheet_titles`/
  `append_to_existing_named_sheet` (no-create контракт проверяется по-настоящему,
  `add_worksheet` роняет AssertionError, если его вообще позвали).
- Tier 2 (монкипатч высокоуровневых функций `services.sheets.ensure_sheet_header`/
  `ensure_named_sheet_header`/`update_status_in_sheet` — record-only фейки): реконсиляция
  шапки и запись статуса уже покрыты СВОИМИ тестами (`tests/test_sheets_phase5.py`,
  `tests/test_sheet_status_city_tab_260819.py`) — здесь важно только то, что sheet_reconcile
  зовёт их с правильными аргументами, а не то, как они сами устроены внутри.

pytest-asyncio недоступна — `asyncio.run()` на каждый async-хелпер, БД — шаблонная копия
(`tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

import gspread
import pytest

import domain.cities as cities
from config import config
from database import db
import services.sheets as sheets_mod
from services import sheet_reconcile as sr
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260926401
BOUND_MSK_ID = 260926402
BOUND_SPB_ID = 260926403

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path, name="test_sheet_reconcile_260926.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


async def _seed_user(telegram_id, *, city="spb", participant_type="short", status="approved",
                      full_name="Тест Тестов", username="testdel", season=None):
    await db.add_user({
        "telegram_id": telegram_id, "full_name": full_name, "username": username,
        "registration_date": "2026-01-01", "event_city": city, "participant_type": participant_type,
    })
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
        if season is not None:
            await conn.execute("UPDATE users SET season = ? WHERE telegram_id = ?", (season, telegram_id))
        await conn.commit()


async def _resolve_tab(city, participant_type):
    from handlers.reg.reg_schema import city_row_tab
    return await city_row_tab(city, participant_type)


def _reset_sheets_state():
    sheets_mod._reset_sheet_cache()
    sheets_mod._named_sheets.clear()
    sheets_mod._header_checked_tabs.clear()


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Tier 1 — gspread-уровень (реальный no-create путь)
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _FakeWorksheet:
    def __init__(self, title, header, rows):
        self.title = title
        self.header = header  # list[str] | None (None = «без заголовков», первая строка — данные)
        self.rows = rows  # list[list] — только данные, без шапки

    def get_all_values(self):
        # Настоящий gspread всегда отдаёт строки (даже для числовых ячеек) — real API contract,
        # значит и фейк обязан стрингифицировать, иначе id, дописанный реальным
        # `append_to_existing_named_sheet` (кладёт telegram_id ИНТОМ из batch.rows), читается
        # назад как int и ломает `.strip()` в build_report.
        if self.header is not None:
            return [list(self.header)] + [[str(c) for c in r] for r in self.rows]
        return [[str(c) for c in r] for r in self.rows]

    def append_row(self, data, value_input_option=None):
        self.rows.append(list(data))


class _FakeSpreadsheet:
    def __init__(self, worksheets: dict):
        self._by_title = dict(worksheets)
        for ws in self._by_title.values():
            ws.spreadsheet = self  # нужно `_get_sheet().spreadsheet.worksheets()` — успешный
            # резолв главной вкладки (test_row_found_on_main_tab_is_not_appended_as_duplicate).
        self.add_worksheet_calls: list[str] = []

    def worksheet(self, title):
        if title not in self._by_title:
            raise gspread.WorksheetNotFound(title)
        return self._by_title[title]

    def worksheets(self):
        return list(self._by_title.values())

    def add_worksheet(self, title, rows, cols):
        self.add_worksheet_calls.append(title)
        raise AssertionError(f"add_worksheet({title!r}) must never be called — reconcile is no-create")


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread(monkeypatch, worksheets: dict):
    fake_ss = _FakeSpreadsheet(worksheets)
    monkeypatch.setattr(sheets_mod.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake_ss


def _noop_header_ensure(monkeypatch, calls: list):
    """Tier 2 — шапка вкладки уже покрыта своими тестами (tests/test_sheets_phase5.py); здесь
    важно только то, что apply_append_missing зовёт ensure_*_header ДО append (record-only)."""
    async def _fake_ensure_sheet_header(headers):
        calls.append((None, list(headers)))

    async def _fake_ensure_named_sheet_header(tab_name, headers):
        calls.append((tab_name, list(headers)))

    monkeypatch.setattr(sheets_mod, "ensure_sheet_header", _fake_ensure_sheet_header)
    monkeypatch.setattr(sheets_mod, "ensure_named_sheet_header", _fake_ensure_named_sheet_header)


def test_missing_tab_reported_and_not_created(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926501, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        fake_ss = _patch_gspread(monkeypatch, {})  # вкладки вообще нет на листе
        report = await sr.build_report()
        return report, fake_ss, tab

    report, fake_ss, tab = _run(scenario())

    assert report["ok"] is True
    assert fake_ss.add_worksheet_calls == []
    missing = {it["tab"]: it for it in report["missing_tabs"]}
    assert tab in missing
    assert missing[tab]["count"] == 1
    assert not report["missing_rows"]  # делегат не в missing_rows — вкладки нет вовсе


def test_missing_tab_suggests_close_name(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926502, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        renamed = tab + "!"  # один лишний символ -- достаточно для difflib
        fake_ss = _patch_gspread(monkeypatch, {renamed: _FakeWorksheet(renamed, ["id", "Статус"], [])})
        report = await sr.build_report()
        return report, tab, renamed

    report, tab, renamed = _run(scenario())
    entry = next(it for it in report["missing_tabs"] if it["tab"] == tab)
    assert entry["suggestion"] == renamed


def test_missing_row_on_existing_tab(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926503, city="spb", participant_type="short", full_name="Аня П.", username="anya")
        tab = await _resolve_tab("spb", "short")
        _patch_gspread(monkeypatch, {tab: _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])})
        return await sr.build_report(), tab

    report, tab = _run(scenario())
    assert len(report["missing_rows"]) == 1
    item = report["missing_rows"][0]
    assert item["tid"] == 260926503
    assert item["name"] == "Аня П."
    assert item["username"] == "@anya"  # database.db.store_username канон -- всегда с «@»
    assert item["tab"] == tab


def test_row_on_main_tab_reported_as_other_tab_not_appended(tmp_path, monkeypatch):
    """Делегат СПб реально лежит на главной вкладке (ручная правка/перевод города) — «Дописать
    недостающие строки» не должен завести вторую строку рядом с уже существующей; отчёт относит
    его к «строка не на своей вкладке», а не к «нет строки»."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        main_title = "Заявки"
        await db.set_setting("main_sheet_tab", main_title)
        await _seed_user(260926520, city="spb", participant_type="short", full_name="На главной", username="glav")
        tab = await _resolve_tab("spb", "short")
        main_ws = _FakeWorksheet(main_title, ["id", "ФИО", "Статус"], [["260926520", "На главной", "Новая"]])
        spb_ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])
        fake_ss = _patch_gspread(monkeypatch, {main_title: main_ws, tab: spb_ws})
        report = await sr.build_report()
        result = await sr.apply_append_missing()
        return report, result, fake_ss, spb_ws, tab

    report, result, fake_ss, spb_ws, tab = _run(scenario())

    assert report["missing_rows"] == []
    assert len(report["other_tab_rows"]) == 1
    entry = report["other_tab_rows"][0]
    assert entry["tid"] == 260926520
    assert entry["name"] == "На главной"
    assert entry["username"] == "@glav"
    assert entry["own_tab"] == tab
    assert entry["found_tabs"] == ["Заявки"]

    assert result["ok"] is True
    assert result["done"] == 0  # нечего дописывать -- строка уже есть, просто не там
    assert len(spb_ws.rows) == 0  # вторая строка на СПб-вкладке НЕ создана
    assert fake_ss.add_worksheet_calls == []


def test_duplicate_rows_reported_not_removed(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926504, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        rows = [["260926504", "A", "Новая"], ["260926504", "B", "Новая"]]
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], rows)
        _patch_gspread(monkeypatch, {tab: ws})
        report = await sr.build_report()
        return report, ws

    report, ws = _run(scenario())
    assert len(report["duplicate_rows"]) == 1
    assert report["duplicate_rows"][0] == {"tid": 260926504, "tab": None, "count": 2} or \
        report["duplicate_rows"][0]["count"] == 2
    assert len(ws.rows) == 2  # ничего не удалено
    # Дубль не участвует в сверке статуса (неясно, какая строка живая).
    assert report["status_mismatch"] == []


def test_status_mismatch_detected(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926505, city="spb", participant_type="short", status="approved")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926505", "Т", "Новая"]])
        _patch_gspread(monkeypatch, {tab: ws})
        return await sr.build_report()

    report = _run(scenario())
    assert len(report["status_mismatch"]) == 1
    item = report["status_mismatch"][0]
    assert item["tid"] == 260926505
    assert item["expected_label"] == "Одобрена"
    assert item["sheet_label"] == "Новая"


def test_headerless_tab_flagged_and_first_row_treated_as_data(tmp_path, monkeypatch):
    """Инцидент 13.09 (lost-applications-260914): первая строка — telegram_id, не заголовок."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926506, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, None, [["260926506", "Т"]])  # без заголовков вовсе
        _patch_gspread(monkeypatch, {tab: ws})
        return await sr.build_report(), tab

    report, tab = _run(scenario())
    assert tab in report["headerless_tabs"]
    # Строка ЕСТЬ (id совпал) -- делегат не должен попасть в «нет строки».
    assert report["missing_rows"] == []


def test_unknown_sheet_id_reported_not_touched(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926507, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        rows = [["260926507", "Т", "Новая"], ["999999999", "Призрак", "Новая"]]
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], rows)
        _patch_gspread(monkeypatch, {tab: ws})
        return await sr.build_report(), ws

    report, ws = _run(scenario())
    assert {"tid": 999999999, "tab": None} not in report["unknown_sheet_ids"]  # см. ниже — tab не None
    assert any(it["tid"] == 999999999 for it in report["unknown_sheet_ids"])
    assert len(ws.rows) == 2  # ничего не удалено


class _QuotaGuardWorksheet(_FakeWorksheet):
    """Координатор 25.09 (живой прогон на стенде): служебные/незавершённые/гейма вкладки не
    должны читаться вовсе (квота) — этот фейк роняет тест, если `get_all_values()` всё же
    позвали на нём."""

    def get_all_values(self):
        raise AssertionError(f"get_all_values() не должен звать на служебной вкладке {self.title!r}")


def test_incomplete_service_and_game_tabs_do_not_produce_unknown_ids(tmp_path, monkeypatch):
    """Координатор 25.09 (находка живого прогона): «Незавершённые»/«🤖 Автоотказы»/«Гейма» с
    числовыми id внутри НЕ дают ложных «Строки с id, которого нет в БД» — бот их вообще не
    читает (id там из reg_started/своих таблиц, не из users)."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await db.set_setting("auto_reject_sheet_tab", "🤖 Автоотказы")
        await _seed_user(260926540, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")

        delegate_ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926540", "Т", "Новая"]])
        incomplete_ws = _QuotaGuardWorksheet(
            "Незавершённые", ["id", "username"], [["999999901", "@ghost1"]],
        )
        auto_reject_ws = _QuotaGuardWorksheet(
            "🤖 Автоотказы", ["id", "ФИО"], [["999999902", "Призрак2"]],
        )
        game_ws = _QuotaGuardWorksheet(
            "Гейма", ["id"], [["999999903"]],
        )
        _patch_gspread(monkeypatch, {
            tab: delegate_ws, "Незавершённые": incomplete_ws,
            "🤖 Автоотказы": auto_reject_ws, "Гейма": game_ws,
        })
        return await sr.build_report()

    report = _run(scenario())
    assert report["ok"] is True
    ghost_ids = {it["tid"] for it in report["unknown_sheet_ids"]}
    assert 999999901 not in ghost_ids
    assert 999999902 not in ghost_ids
    assert 999999903 not in ghost_ids
    assert report["unknown_sheet_ids"] == []
    assert report["unknown_tabs"] == []  # известны боту по реестру — не «неизвестные»


def test_unknown_tab_listed_without_reading_rows(tmp_path, monkeypatch):
    """Вкладка, которую бот не знает ни в одной категории — отдельной строкой «Неизвестные
    вкладки», без разбора строк (и без единого сетевого похода за её данными)."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926541, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        delegate_ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926541", "Т", "Новая"]])
        stray_ws = _QuotaGuardWorksheet("Черновик менеджера", ["что-то"], [["whatever"]])
        _patch_gspread(monkeypatch, {tab: delegate_ws, "Черновик менеджера": stray_ws})
        return await sr.build_report()

    report = _run(scenario())
    assert report["unknown_tabs"] == ["Черновик менеджера"]
    assert report["unknown_sheet_ids"] == []


def test_past_season_user_excluded(tmp_path, monkeypatch):
    """482 импортированных делегата прошлого сезона не должны заваливать отчёт ложными
    «нет строки» (память проекта: past-delegates-import-260910)."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await db.set_setting("event_season", "YL 26/2")
        await _seed_user(260926508, city="spb", participant_type="short", season="YL 26/1")
        tab = await _resolve_tab("spb", "short")
        _patch_gspread(monkeypatch, {tab: _FakeWorksheet(tab, ["id", "Статус"], [])})
        return await sr.build_report()

    report = _run(scenario())
    assert report["user_count"] == 0
    assert report["missing_rows"] == []
    assert report["missing_tabs"] == []


def test_city_scope_limits_to_managers_city(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926509, city="spb", participant_type="short", full_name="СПб Делегат")
        await _seed_user(260926510, city="msk", participant_type="short", full_name="МСК Делегат")
        spb_tab = await _resolve_tab("spb", "short")
        msk_tab = await _resolve_tab("msk", "short")
        _patch_gspread(monkeypatch, {})  # обе вкладки отсутствуют -- различие видно по missing_tabs
        scope = cities.city_scope("spb")
        report = await sr.build_report(city_scope=scope)
        return report, spb_tab, msk_tab

    report, spb_tab, msk_tab = _run(scenario())
    assert report["user_count"] == 1
    tabs = {it["tab"] for it in report["missing_tabs"]}
    assert spb_tab in tabs
    assert msk_tab not in tabs or msk_tab == spb_tab


def test_apply_append_missing_does_not_create_missing_tab_elsewhere(tmp_path, monkeypatch):
    """Комбинированный сценарий: одна вкладка ВООБЩЕ отсутствует (город A), у другой (город B)
    есть недостающая строка. «Дописать» чинит ТОЛЬКО существующую, вкладка города A по-прежнему
    не создаётся — та же проверка, что просит SEED, но на реальном исполнении, не только на
    сверке."""
    _db_ready(tmp_path)
    _reset_sheets_state()
    header_calls: list = []
    _noop_header_ensure(monkeypatch, header_calls)

    async def scenario():
        await _enable_cities_module()
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
            {"code": "spb", "label": "СПб", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
            {"code": "tmn", "label": "Тюмень", "tab_base": "Тюмень", "enabled": 1, "sort_order": 2},
        ])
        await _seed_user(260926511, city="spb", participant_type="short")  # вкладка ЕСТЬ
        await _seed_user(260926512, city="tmn", participant_type="short")  # вкладки НЕТ вовсе
        spb_tab = await _resolve_tab("spb", "short")
        tmn_tab = await _resolve_tab("tmn", "short")
        ws = _FakeWorksheet(spb_tab, ["id", "ФИО", "Статус"], [])
        fake_ss = _patch_gspread(monkeypatch, {spb_tab: ws})

        result = await sr.apply_append_missing()
        report_after = await sr.build_report()
        return result, report_after, fake_ss, ws, spb_tab, tmn_tab

    result, report_after, fake_ss, ws, spb_tab, tmn_tab = _run(scenario())

    assert result["ok"] is True
    assert result["done"] == 1
    assert result["failed"] == []
    assert fake_ss.add_worksheet_calls == []  # Тюмень НЕ создана
    assert any(str(r[0]) == "260926511" for r in ws.rows)  # СПб дописана
    assert any(it["tab"] == tmn_tab for it in report_after["missing_tabs"])  # Тюмень по-прежнему «нет вкладки»
    assert header_calls and header_calls[0][0] == spb_tab


def test_apply_append_missing_recomputes_before_applying(tmp_path, monkeypatch):
    """Состояние листа изменилось МЕЖДУ открытием отчёта и тапом кнопки — apply пересчитывает
    заново, а не применяет старый снимок."""
    _db_ready(tmp_path)
    _reset_sheets_state()
    _noop_header_ensure(monkeypatch, [])

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926513, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])
        _patch_gspread(monkeypatch, {tab: ws})

        stale_report = await sr.build_report()
        assert len(stale_report["missing_rows"]) == 1
        # Строка появилась на листе САМА (менеджер вписал руками) до тапа «Дописать».
        ws.rows.append(["260926513", "Т", "Новая"])

        result = await sr.apply_append_missing()
        return result, ws

    result, ws = _run(scenario())
    assert result["done"] == 0  # пересчёт увидел, что строка уже есть -- дописывать нечего
    assert len(ws.rows) == 1  # не задвоили


def test_apply_append_missing_double_tap_does_not_duplicate(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()
    _noop_header_ensure(monkeypatch, [])

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926514, city="spb", participant_type="short")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])
        _patch_gspread(monkeypatch, {tab: ws})

        first, second = await asyncio.gather(sr.apply_append_missing(), sr.apply_append_missing())
        return first, second, ws

    first, second, ws = _run(scenario())
    oks = [r for r in (first, second) if r["ok"]]
    refused = [r for r in (first, second) if not r["ok"]]
    assert len(refused) == 1
    assert "уже выполняется" in refused[0]["error"]
    assert len(ws.rows) == 1  # только один тап реально дописал


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Tier 2 — монкипатч высокоуровневых функций (частичный сбой / фикс статусов)
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_apply_append_missing_partial_failure_does_not_abort_rest(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()
    _noop_header_ensure(monkeypatch, [])

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926515, city="spb", participant_type="short", full_name="Первый")
        await _seed_user(260926516, city="spb", participant_type="short", full_name="Второй")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])
        _patch_gspread(monkeypatch, {tab: ws})

        calls = {"n": 0}
        real_append = sheets_mod.append_to_existing_named_sheet

        async def flaky_append(tab_name, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return "error"
            return await real_append(tab_name, data)

        monkeypatch.setattr(sheets_mod, "append_to_existing_named_sheet", flaky_append)
        return await sr.apply_append_missing(), ws

    result, ws = _run(scenario())
    assert result["ok"] is True
    assert result["done"] == 1
    assert len(result["failed"]) == 1
    assert len(ws.rows) == 1


def test_apply_append_missing_unexpected_crash_preserves_partial_done(tmp_path, monkeypatch):
    """Неожиданное исключение (настоящий raise, не штатный код «error» из fail-soft-контракта
    append_to_existing_named_sheet) НЕ должно стереть уже накопленный done -- хендлер
    (handlers/sheets/admin_sheet_reconcile.py) должен суметь сказать «записано N из M», а не просто
    «упало». Возврат несёт `ok=False, crashed=True` плюс то, что реально успело."""
    _db_ready(tmp_path)
    _reset_sheets_state()
    _noop_header_ensure(monkeypatch, [])

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926521, city="spb", participant_type="short", full_name="Первый")
        await _seed_user(260926522, city="spb", participant_type="short", full_name="Второй")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [])
        _patch_gspread(monkeypatch, {tab: ws})

        calls = {"n": 0}

        async def crashing_append(tab_name, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return "ok"
            raise RuntimeError("таблица не ответила (тест)")

        monkeypatch.setattr(sheets_mod, "append_to_existing_named_sheet", crashing_append)
        return await sr.apply_append_missing()

    result = _run(scenario())
    assert result["ok"] is False
    assert result["crashed"] is True
    assert result["done"] == 1
    assert result["total"] == 2


def test_apply_fix_statuses_writes_only_single_row_matches(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926517, city="spb", participant_type="short", status="approved")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926517", "Т", "Новая"]])
        _patch_gspread(monkeypatch, {tab: ws})

        calls = []

        async def fake_update_status(tid, label):
            calls.append((tid, label))
            return True

        monkeypatch.setattr(sheets_mod, "update_status_in_sheet", fake_update_status)
        result = await sr.apply_fix_statuses()
        return result, calls

    result, calls = _run(scenario())
    assert result["ok"] is True
    assert result["done"] == 1
    assert calls == [(260926517, "Одобрена")]


def test_apply_fix_statuses_reports_write_failure(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926518, city="spb", participant_type="short", status="rejected")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926518", "Т", "Новая"]])
        _patch_gspread(monkeypatch, {tab: ws})

        async def fake_update_status(tid, label):
            return False

        monkeypatch.setattr(sheets_mod, "update_status_in_sheet", fake_update_status)
        return await sr.apply_fix_statuses()

    result = _run(scenario())
    assert result["ok"] is True
    assert result["done"] == 0
    assert len(result["failed"]) == 1


def test_apply_fix_statuses_unexpected_crash_preserves_partial_done(tmp_path, monkeypatch):
    """Тот же посыл, что у `apply_append_missing`'s одноимённого теста -- неожиданный raise
    посреди цикла не должен стереть уже накопленный done."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926523, city="spb", participant_type="short", status="approved", full_name="Первый")
        await _seed_user(260926524, city="spb", participant_type="short", status="approved", full_name="Второй")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [
            ["260926523", "Первый", "Новая"], ["260926524", "Второй", "Новая"],
        ])
        _patch_gspread(monkeypatch, {tab: ws})

        calls = {"n": 0}

        async def crashing_update_status(tid, label):
            calls["n"] += 1
            if calls["n"] == 1:
                return True
            raise RuntimeError("таблица не ответила (тест)")

        monkeypatch.setattr(sheets_mod, "update_status_in_sheet", crashing_update_status)
        return await sr.apply_fix_statuses()

    result = _run(scenario())
    assert result["ok"] is False
    assert result["crashed"] is True
    assert result["done"] == 1
    assert result["total"] == 2


def test_apply_fix_statuses_recomputes_before_applying(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926519, city="spb", participant_type="short", status="approved")
        tab = await _resolve_tab("spb", "short")
        ws = _FakeWorksheet(tab, ["id", "ФИО", "Статус"], [["260926519", "Т", "Новая"]])
        _patch_gspread(monkeypatch, {tab: ws})

        calls = []

        async def fake_update_status(tid, label):
            calls.append((tid, label))
            return True

        monkeypatch.setattr(sheets_mod, "update_status_in_sheet", fake_update_status)

        # Менеджер уже поправил ячейку руками ДО тапа «Выправить» -- пересчёт должен увидеть
        # это и ничего не писать повторно.
        ws.rows[0][2] = "Одобрена"

        result = await sr.apply_fix_statuses()
        return result, calls

    result, calls = _run(scenario())
    assert result["done"] == 0
    assert calls == []


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Ревью-вопрос: build_sheet_batches гарантирует ли параллельность users/rows?
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_build_sheet_batches_users_rows_stay_aligned_for_zip(tmp_path):
    """`apply_append_missing` строит `row_by_tid = {u["telegram_id"]: r for u, r in
    zip(batch.users, batch.rows)}` -- безопасно ТОЛЬКО если build_sheet_batches добавляет
    строку и пользователя строго парой, один к одному, в одном порядке (обещание в докстринге
    `SheetBatch.users`: «идёт ПАРАЛЛЕЛЬНО rows»). По коду это так: КАЖДАЯ ветка build_sheet_batches
    (и main, и именная) делает `.rows.append(row)` и `.users.append(u)` вместе, в одной итерации
    цикла, без пути, где один список растёт без другого -- инвариант структурный, не случайный.
    Явно фиксируем это здесь: несколько пользователей на разных вкладках/городах, и для каждого
    zip-пары «ФИО» в строке должно совпасть с ИМЕННО этим пользователем (ловит смещение индекса,
    если инвариант когда-нибудь сломают)."""
    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(260926530, city="spb", participant_type="short", full_name="Аня СПб")
        await _seed_user(260926531, city="spb", participant_type="short", full_name="Боря СПб")
        await _seed_user(260926532, city="msk", participant_type="short", full_name="Вика Мск")
        users = await db.get_all_users_dicts()
        from handlers.sheets.admin_sheets import build_sheet_batches
        return await build_sheet_batches(users)

    batches = _run(scenario())
    seen_users = 0
    for batch in batches:
        assert len(batch.users) == len(batch.rows), (
            f"вкладка {batch.tab!r}: users и rows разъехались по длине — zip() в "
            "apply_append_missing тихо обрежет/сместит пары"
        )
        if "ФИО" not in batch.headers:
            continue
        name_col = batch.headers.index("ФИО")
        for u, row in zip(batch.users, batch.rows):
            assert row[name_col] == (u.get("full_name") or "-"), (
                f"строка для {u.get('telegram_id')} несёт чужое ФИО — zip() рассинхронизировался"
            )
            seen_users += 1
    assert seen_users == 3  # ни один делегат не потерялся молча


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Рендер / чанкование / CSV — чистые функции, без Sheets
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_long_report_is_chunked_under_telegram_limit():
    # missing_rows/duplicate_rows/status_mismatch/unknown_sheet_ids режутся до первых 10 в самом
    # render_report_lines (человеку незачем читать сотню строк в чате -- для этого CSV) —
    # категория БЕЗ такой обрезки (число вкладок города/трека естественно ограничено) —
    # missing_tabs; ею и раздуваем отчёт до нужного объёма.
    report = {
        "ok": True, "error": None, "user_count": 500,
        "missing_tabs": [
            {"tab": f"🤖 Город {i} — короткая анкета участника", "kind": "short", "count": i,
             "suggestion": None}
            for i in range(400)
        ],
        "headerless_tabs": [], "duplicate_rows": [], "status_mismatch": [], "unknown_sheet_ids": [],
        "missing_rows": [], "other_tab_rows": [],
        "decision_delivery": {"failed": [], "blocked": [], "resendable": [], "queued": [], "unknown": []},
    }
    lines = sr.render_report_lines(report, city_label="Санкт-Петербург")
    chunks = sr.chunk_report_lines(lines)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096
    # Склейка кусков (через "\n") восстанавливает исходный текст -- ни одна строка не потеряна.
    assert "\n".join(chunks) == "\n".join(lines)


def test_render_report_lines_reports_decision_delivery_counts():
    """Координатор 25.09: раздел «Недоставленные решения» больше не честная отписка
    («по базе это не определить») — реальные числа из `decision_delivery`, посчитанные
    `services.decision_delivery.summarize_deliveries`."""
    report = {
        "ok": True, "error": None, "user_count": 3,
        "missing_tabs": [], "headerless_tabs": [], "duplicate_rows": [], "status_mismatch": [],
        "unknown_sheet_ids": [], "missing_rows": [], "other_tab_rows": [],
        "decision_delivery": {
            "failed": [
                {"tid": 1, "name": "Заблокировал", "username": "@b", "city": "msk",
                 "decision": "approved", "error": "бот заблокирован делегатом"},
                {"tid": 2, "name": "Не дошло", "username": "@n", "city": "msk",
                 "decision": "rejected", "error": "чат не найден"},
            ],
            "blocked": [
                {"tid": 1, "name": "Заблокировал", "username": "@b", "city": "msk",
                 "decision": "approved", "error": "бот заблокирован делегатом"},
            ],
            "resendable": [
                {"tid": 2, "name": "Не дошло", "username": "@n", "city": "msk",
                 "decision": "rejected", "error": "чат не найден"},
            ],
            "queued": [],
            "unknown": [
                {"tid": 3, "name": "До миграции", "username": "@u", "city": "msk",
                 "decision": "approved", "error": None},
            ],
        },
    }
    lines = sr.render_report_lines(report)
    text = "\n".join(lines)
    assert "Решения не доставлены" in text
    assert "(2, из них бот заблокирован: 1)" in text
    assert "Не дошло" in text  # попал в первые 10
    assert "чат не найден" in text
    assert "неизвестно (до учёта доставки): 1" in text


def test_csv_export_contains_every_section():
    report = {
        "ok": True, "error": None, "user_count": 1,
        "missing_tabs": [{"tab": "Тюмень", "kind": "main", "count": 2, "suggestion": None}],
        "headerless_tabs": ["СПб Акция"],
        "duplicate_rows": [{"tid": 1, "tab": "СПб", "count": 2}],
        "status_mismatch": [{"tid": 2, "tab": "СПб", "expected_label": "Одобрена", "sheet_label": "Новая"}],
        "unknown_sheet_ids": [{"tid": 3, "tab": "СПб"}],
        "missing_rows": [{"tid": 4, "name": "Имя", "username": "u", "tab": "СПб"}],
        "other_tab_rows": [{
            "tid": 5, "name": "Другой", "username": "u2", "own_tab": "СПб", "found_tabs": ["Заявки"],
        }],
        "decision_delivery": {
            "failed": [], "blocked": [
                {"tid": 6, "name": "Блок", "username": "u6", "city": "msk",
                 "decision": "approved", "error": "бот заблокирован делегатом"},
            ],
            "resendable": [
                {"tid": 7, "name": "НеДошло", "username": "u7", "city": "spb",
                 "decision": "rejected", "error": "чат не найден"},
            ],
            "queued": [],
            "unknown": [
                {"tid": 8, "name": "Неизвестно", "username": "u8", "city": "msk",
                 "decision": "approved", "error": None},
            ],
        },
    }
    csv_bytes = sr.report_to_csv_bytes(report)
    text = csv_bytes.decode("utf-8-sig")
    for needle in (
        "Тюмень", "СПб Акция", "1", "2", "3", "4", "5", "Одобрена", "Новая", "Заявки",
        "Блок", "НеДошло", "Неизвестно", "бот заблокирован делегатом", "чат не найден",
    ):
        assert needle in text
