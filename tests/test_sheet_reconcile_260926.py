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

import cities
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
    from handlers.reg_schema import city_row_tab
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
        "missing_rows": [],
        "decisions_undelivered_note": sr.DECISIONS_UNDELIVERED_NOTE,
    }
    lines = sr.render_report_lines(report, city_label="Санкт-Петербург")
    chunks = sr.chunk_report_lines(lines)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096
    # Склейка кусков (через "\n") восстанавливает исходный текст -- ни одна строка не потеряна.
    assert "\n".join(chunks) == "\n".join(lines)


def test_render_report_lines_reports_honest_delivery_note():
    report = {
        "ok": True, "error": None, "user_count": 0,
        "missing_tabs": [], "headerless_tabs": [], "duplicate_rows": [], "status_mismatch": [],
        "unknown_sheet_ids": [], "missing_rows": [],
        "decisions_undelivered_note": sr.DECISIONS_UNDELIVERED_NOTE,
    }
    lines = sr.render_report_lines(report)
    text = "\n".join(lines)
    assert "не определить" in text
    assert "delivery_failed_at" in text


def test_csv_export_contains_every_section():
    report = {
        "ok": True, "error": None, "user_count": 1,
        "missing_tabs": [{"tab": "Тюмень", "kind": "main", "count": 2, "suggestion": None}],
        "headerless_tabs": ["СПб Акция"],
        "duplicate_rows": [{"tid": 1, "tab": "СПб", "count": 2}],
        "status_mismatch": [{"tid": 2, "tab": "СПб", "expected_label": "Одобрена", "sheet_label": "Новая"}],
        "unknown_sheet_ids": [{"tid": 3, "tab": "СПб"}],
        "missing_rows": [{"tid": 4, "name": "Имя", "username": "u", "tab": "СПб"}],
        "decisions_undelivered_note": sr.DECISIONS_UNDELIVERED_NOTE,
    }
    csv_bytes = sr.report_to_csv_bytes(report)
    text = csv_bytes.decode("utf-8-sig")
    for needle in ("Тюмень", "СПб Акция", "1", "2", "3", "4", "Одобрена", "Новая"):
        assert needle in text
