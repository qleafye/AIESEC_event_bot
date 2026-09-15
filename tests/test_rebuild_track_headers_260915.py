"""Квик 260915-4is: `build_sheet_batches` (handlers/admin_sheets.py) — единый строитель
«пользователь → вкладка → шапка → строка» для «♻️ Пересобрать» и «🔄 Синхронизация». До этой
правки обе операции брали набор колонок ТОЛЬКО по городу (active_sheet_headers(code)), даже
когда живой аппенд для той же именованной вкладки кладёт короткую/party-шапку
(short_sheet_headers/party_sheet_headers) — пересборка «СПб Акция» переписывала 17-колоночную
короткую вкладку полной шапкой Питера, а следующий живой аппенд снова клал короткую строку.

Идиома — tests/test_rebuild_city_routing_260818.py: хендлер/строитель зовётся напрямую,
всё замокано на уровне модуля admin_sheets, БД не трогается."""
import asyncio

from handlers import admin_sheets
from tests.test_rebuild_confirm_260813_sdl import _FakeCallback, ADMIN_ID


def _wire_common(monkeypatch, *, route, city_code_map=None, get_setting_map=None):
    """route(event_city, participant_type) -> tab name | None, мимикрирует city_row_tab.
    city_code_map переопределяет sheet_city_code (по умолчанию — событие_city как есть, что
    достаточно для тестов ниже: единственный нетривиальный город — «spb»). Возвращает счётчик
    вызовов каждой из трёх функций-шапок — для проверки кэша «один раз на (kind, code)»."""
    call_counts = {"active": 0, "short": 0, "party": 0}

    async def fake_city_row_tab(event_city, participant_type):
        return route(event_city, participant_type)

    async def fake_sheet_city_code(event_city):
        if city_code_map is not None:
            return city_code_map.get(event_city)
        return event_city

    async def fake_active_headers(code=None):
        call_counts["active"] += 1
        return [f"FULL:{code}"]

    async def fake_short_headers(code=None):
        call_counts["short"] += 1
        return [f"SHORT:{code}"]

    async def fake_party_headers(code=None):
        call_counts["party"] += 1
        return [f"PARTY:{code}"]

    async def fake_get_setting(key):
        return (get_setting_map or {}).get(key)

    monkeypatch.setattr(admin_sheets, "city_row_tab", fake_city_row_tab)
    monkeypatch.setattr(admin_sheets, "sheet_city_code", fake_sheet_city_code)
    monkeypatch.setattr(admin_sheets, "active_sheet_headers", fake_active_headers)
    monkeypatch.setattr(admin_sheets, "short_sheet_headers", fake_short_headers)
    monkeypatch.setattr(admin_sheets, "party_sheet_headers", fake_party_headers)
    monkeypatch.setattr(admin_sheets, "get_setting", fake_get_setting)
    monkeypatch.setattr(admin_sheets, "_sheet_value_map", lambda u: {})
    return call_counts


# ── 1-3: короткая/party/полная вкладка получает СВОЮ шапку ─────────────────────────────────

def test_short_batch_gets_short_headers_not_full(monkeypatch):
    def route(event_city, participant_type):
        return "СПб Акция" if (event_city, participant_type) == ("spb", "short") else None

    _wire_common(monkeypatch, route=route)
    users = [{"telegram_id": 1, "event_city": "spb", "participant_type": "short"}]

    batches = asyncio.run(admin_sheets.build_sheet_batches(users))
    named = [b for b in batches if b.tab is not None]
    assert len(named) == 1
    batch = named[0]
    assert batch.tab == "СПб Акция"
    assert batch.kind == "short"
    assert batch.headers == ["SHORT:spb"]
    assert batch.headers != ["FULL:spb"]  # регресс: раньше шапка бралась только по городу


def test_party_batch_gets_party_headers(monkeypatch):
    def route(event_city, participant_type):
        return "Party" if (event_city, participant_type) == ("spb", "party_overnight") else None

    _wire_common(monkeypatch, route=route)
    users = [{"telegram_id": 1, "event_city": "spb", "participant_type": "party_overnight"}]

    batches = asyncio.run(admin_sheets.build_sheet_batches(users))
    named = [b for b in batches if b.tab is not None]
    assert len(named) == 1
    assert named[0].kind == "party"
    assert named[0].headers == ["PARTY:spb"]


def test_full_track_batch_still_gets_active_headers(monkeypatch):
    """Регресс-проверка: полный трек города НЕ должен получить короткую/party шапку — вкладки
    городов с полным треком (например, обычная «СПб») продолжают работать как раньше."""
    def route(event_city, participant_type):
        return "СПб" if (event_city, participant_type) == ("spb", "full") else None

    _wire_common(monkeypatch, route=route)
    users = [{"telegram_id": 1, "event_city": "spb", "participant_type": "full"}]

    batches = asyncio.run(admin_sheets.build_sheet_batches(users))
    named = [b for b in batches if b.tab is not None]
    assert len(named) == 1
    assert named[0].kind == "main"
    assert named[0].headers == ["FULL:spb"]


# ── 4: город по умолчанию + short — не в основную вкладку, а в short_sheet_tab ──────────────

def test_default_city_short_goes_to_configured_short_tab_not_main(monkeypatch):
    def route(event_city, participant_type):
        return None  # город по умолчанию / модуль городов выключен -> city_row_tab всегда None

    _wire_common(
        monkeypatch, route=route,
        get_setting_map={"short_sheet_tab": "Промо-Питер"},
    )
    users = [{"telegram_id": 1, "event_city": None, "participant_type": "short"}]

    batches = asyncio.run(admin_sheets.build_sheet_batches(users))
    main_batch = batches[0]
    assert main_batch.tab is None
    assert main_batch.rows == []  # короткий делегат НЕ попал в основную вкладку

    named = [b for b in batches if b.tab is not None]
    assert len(named) == 1
    assert named[0].tab == "Промо-Питер"  # значение из get_setting("short_sheet_tab"), не дефолт
    assert named[0].kind == "short"
    assert named[0].headers == ["SHORT:None"]


def test_default_city_short_falls_back_to_short_tab_default(monkeypatch):
    def route(event_city, participant_type):
        return None

    _wire_common(monkeypatch, route=route, get_setting_map={})  # настройки нет
    users = [{"telegram_id": 1, "event_city": None, "participant_type": "short"}]

    batches = asyncio.run(admin_sheets.build_sheet_batches(users))
    named = [b for b in batches if b.tab is not None]
    assert len(named) == 1
    assert named[0].tab == admin_sheets.SHORT_SHEET_TAB_DEFAULT == "Краткая"


# ── 5: rebuild_sheet морозит схему только для батчей kind == "main" ────────────────────────

def test_rebuild_freezes_schema_only_for_main_kind_batches(monkeypatch):
    main_batch = admin_sheets.SheetBatch(tab=None, kind="main", city_code=None, headers=["M"], rows=[[1]])
    full_named = admin_sheets.SheetBatch(tab="СПб", kind="main", city_code="spb", headers=["F"], rows=[[2]])
    short_named = admin_sheets.SheetBatch(tab="СПб Акция", kind="short", city_code="spb", headers=["S"], rows=[[3]])

    async def fake_build(users):
        return [main_batch, full_named, short_named]

    async def fake_users():
        return [{"telegram_id": 1}, {"telegram_id": 2}, {"telegram_id": 3}]

    async def fake_rebuild(headers, rows):
        return len(rows)

    async def fake_sync(tab, headers, rows):
        return len(rows)

    schema_calls = []

    async def fake_schema(headers, city_code=None):
        schema_calls.append((headers, city_code))

    monkeypatch.setattr(admin_sheets, "build_sheet_batches", fake_build)
    monkeypatch.setattr(admin_sheets, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_sheets, "rebuild_main_sheet", fake_rebuild)
    monkeypatch.setattr(admin_sheets, "sync_named_worksheet", fake_sync)
    monkeypatch.setattr(admin_sheets, "set_sheet_schema", fake_schema)

    cb = _FakeCallback(ADMIN_ID)
    asyncio.run(admin_sheets.rebuild_sheet(cb))

    assert (["M"], None) in schema_calls
    assert (["F"], "spb") in schema_calls
    assert not any(headers == ["S"] for headers, _code in schema_calls)  # короткая шапка НЕ морозится


# ── 6: шапка на пару (kind, code) считается один раз при нескольких пользователях ───────────

def test_headers_computed_once_per_kind_and_code(monkeypatch):
    def route(event_city, participant_type):
        return "СПб Акция"  # все трое попадают на одну и ту же именованную вкладку

    call_counts = _wire_common(monkeypatch, route=route)
    users = [
        {"telegram_id": 1, "event_city": "spb", "participant_type": "short"},
        {"telegram_id": 2, "event_city": "spb", "participant_type": "short"},
        {"telegram_id": 3, "event_city": "spb", "participant_type": "short"},
    ]

    asyncio.run(admin_sheets.build_sheet_batches(users))

    assert call_counts["short"] == 1
    # Шапка основной вкладки считается всегда ровно один раз, даже без строк на ней —
    # иначе rebuild_main_sheet получил бы пустую шапку и стёр основную вкладку.
    assert call_counts["active"] == 1
    assert call_counts["party"] == 0


# ── 7: headless dry-run зовёт build_sheet_batches, не трогает Sheets API ────────────────────

def test_headless_dry_run_uses_build_sheet_batches_and_skips_writes(monkeypatch, capsys):
    import tools.rebuild_sheet_headless as headless

    fake_batches = [
        admin_sheets.SheetBatch(tab=None, kind="main", city_code=None, headers=["A"], rows=[[1]]),
        admin_sheets.SheetBatch(tab="СПб Акция", kind="short", city_code="spb", headers=["B", "C"], rows=[[2, 3]]),
    ]

    async def fake_build(users):
        return fake_batches

    async def fake_users():
        return [{"telegram_id": 1}, {"telegram_id": 2}]

    rebuild_calls = []
    sync_calls = []

    async def fake_rebuild(headers, rows):
        rebuild_calls.append((headers, rows))
        return len(rows)

    async def fake_sync(tab, headers, rows):
        sync_calls.append((tab, headers, rows))
        return len(rows)

    monkeypatch.setattr(admin_sheets, "build_sheet_batches", fake_build)
    monkeypatch.setattr(admin_sheets, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_sheets, "rebuild_main_sheet", fake_rebuild)
    monkeypatch.setattr(admin_sheets, "sync_named_worksheet", fake_sync)

    code = asyncio.run(headless.main(False))

    assert code == 0
    assert rebuild_calls == []  # сухой прогон — Sheets API не тронут
    assert sync_calls == []
    out = capsys.readouterr().out
    assert "СПб Акция" in out
    assert "короткая" in out
    assert "Сухой прогон" in out
