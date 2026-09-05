"""Phase 25 Plan 03 (CITYQ-03) — Google-таблица: колонки и снимок схемы по вкладке города.

Назначение файла (контракт, а не просто набор проверок), по образцу
`tests/test_percity_questions_25.py`/`tests/test_content_percity_offparity.py`:

    «Вкладка города считает набор колонок и хранит снимок схемы СВОИМ городом — тем же
    правилом, что решает, на какую вкладку уехала строка (`city_row_tab`). Выключенный модуль
    городов — нулевая разница с сегодняшним поведением на любом вызове с любым city_code.»

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path (та же конвенция, что у соседей,
см. tests/test_city_sheets_phase71.py).
"""
import asyncio

from config import config
from database import db
from handlers import reg_schema
from handlers import registration as reg
from handlers import admin_reg_config
import services.sheets as sheets_mod


def _db_ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _drain():
    """Общий приём соседей (test_party_header_block6.py): _refresh_*_sheet_header зовёт
    ensure_named_sheet_header через background.spawn (fire-and-forget) — без gather тест
    проверил бы calls до того, как задача успела выполниться."""
    async def _g():
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    return _g


# ── 1) active_sheet_headers(city_code) — своя ширина, порядок не меняется ──────────────────

def test_active_sheet_headers_spb_shorter_by_exactly_the_disabled_columns(tmp_path):
    _db_ready(tmp_path, "test_percity25_headers.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        # Глобально включаем оба вопроса, у СПб выключаем только один — так разница видна
        # и предсказуема (а не "весь реестр по дефолту off").
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_goal", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")

        default_headers = await reg_schema.active_sheet_headers()
        spb_headers = await reg_schema.active_sheet_headers("spb")
        return default_headers, spb_headers

    default_headers, spb_headers = asyncio.run(scenario())
    assert "Форматы форума" in default_headers
    assert "Форматы форума" not in spb_headers
    assert "Цель участия" in default_headers and "Цель участия" in spb_headers
    # Ровно на одну колонку короче — никакая другая колонка не пострадала.
    assert len(default_headers) == len(spb_headers) + 1
    # Порядок оставшихся колонок не поменялся (проекция, не пересортировка).
    assert [h for h in default_headers if h != "Форматы форума"] == spb_headers


# ── 2) sheet_city_code параллелен city_row_tab на тех же трёх ранних выходах ───────────────

def test_sheet_city_code_matches_city_row_tab_none_ness(tmp_path):
    _db_ready(tmp_path, "test_percity25_code_vs_tab.db")

    async def scenario():
        cases = []
        # (a) модуль выключен вовсе — оба None для любого города.
        for city in (None, "msk", "spb", "tyumen", "atlantis"):
            cases.append((
                "module_off", city,
                await reg_schema.sheet_city_code(city),
                await reg_schema.city_row_tab(city, None),
            ))
        await db.set_setting("event_city_enabled", "on")
        # (b) модуль включён — default/None/неизвестный код все схлопываются в None;
        #     spb/tyumen дают непустой код и непустую вкладку.
        for city in (None, "msk", "spb", "tyumen", "atlantis"):
            cases.append((
                "module_on", city,
                await reg_schema.sheet_city_code(city),
                await reg_schema.city_row_tab(city, None),
            ))
        return cases

    cases = asyncio.run(scenario())
    for _label, _city, code, tab in cases:
        assert (code is None) == (tab is None), (_label, _city, code, tab)


# ── 3) Снимок схемы — по вкладке ─────────────────────────────────────────────────────────

def test_set_sheet_schema_city_writes_composed_key_and_get_reads_it_back(tmp_path):
    _db_ready(tmp_path, "test_percity25_snapshot_write.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await reg_schema.set_sheet_schema(["ID Telegram", "ФИО"], "spb")
        raw = await db.get_setting("sheet_header_schema__city__spb")
        got = await reg.get_sheet_schema("spb")
        return raw, got

    raw, got = asyncio.run(scenario())
    assert raw == '["ID Telegram", "ФИО"]'
    assert got == ["ID Telegram", "ФИО"]


def test_get_sheet_schema_falls_back_to_global_snapshot_when_no_per_city_one(tmp_path):
    """Переходное состояние до перезапуска/пересборки: город ещё не получил свой снимок —
    get_sheet_schema("spb") возвращает ГЛОБАЛЬНЫЙ снимок, а не падает и не читает live."""
    _db_ready(tmp_path, "test_percity25_snapshot_fallback.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await reg_schema.set_sheet_schema(["A", "B", "C"])  # только глобальный
        return await reg.get_sheet_schema("spb")

    assert asyncio.run(scenario()) == ["A", "B", "C"]


# ── 4) active_sheet_row проецируется на снимок СВОЕГО города ───────────────────────────────

def test_active_sheet_row_projects_onto_the_city_snapshot(tmp_path):
    _db_ready(tmp_path, "test_percity25_row_projection.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await reg_schema.set_sheet_schema(["ID Telegram", "ФИО", "Телефон"])  # глобальный: 3
        await reg_schema.set_sheet_schema(["ID Telegram", "ФИО"], "spb")  # СПб: 2 (короче)
        data = {"telegram_id": 42, "full_name": "Иванов", "phone": "+7"}
        main_row = await reg.active_sheet_row(data, None)
        spb_row = await reg.active_sheet_row(data, "spb")
        return main_row, spb_row

    main_row, spb_row = asyncio.run(scenario())
    assert len(main_row) == 3
    assert len(spb_row) == 2


# ── 5) incomplete_city_batches — разные headers для СПб и по умолчанию ────────────────────

def test_incomplete_city_batches_headers_differ_when_spb_has_its_own_override(tmp_path):
    _db_ready(tmp_path, "test_percity25_incomplete_headers.db")

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")
        await db.mark_reg_started(1, "a", event_city="msk")
        await db.mark_reg_started(2, "b", event_city="spb")

        batches = await reg_schema.incomplete_city_batches()
        return {tab: headers for tab, headers, _rows in batches}

    by_tab = asyncio.run(scenario())
    default_tab = "Незавершённые"
    spb_tab = "СПб Незавершённые"
    assert default_tab in by_tab and spb_tab in by_tab
    assert "Форматы форума" in by_tab[default_tab]
    assert "Форматы форума" not in by_tab[spb_tab]


# ── 6) _refresh_sheet_header(setting_key=...) без города — избирательно по вкладкам ────────

def test_refresh_sheet_header_global_toggle_skips_city_with_its_own_override(tmp_path, monkeypatch):
    _db_ready(tmp_path, "test_percity25_refresh_global.db")

    async def prepare():
        await db.set_setting("event_city_enabled", "on")
        # СПб уже переопределил этот вопрос под себя — глобальный тумблер его не касается.
        await db.set_setting("reg_q_formats__city__spb", "off")
        # Тюмень — без своего переопределения — обязана получить новую шапку.

    asyncio.run(prepare())

    named_calls = []

    async def fake_ensure_named(tab, headers):
        named_calls.append((tab, headers))

    async def fake_ensure_main(headers):
        return None

    monkeypatch.setattr(sheets_mod, "ensure_named_sheet_header", fake_ensure_named)
    monkeypatch.setattr(admin_reg_config, "ensure_sheet_header", fake_ensure_main)

    async def go():
        await admin_reg_config._refresh_sheet_header(setting_key="reg_q_formats")
        await _drain()()

    asyncio.run(go())

    tabs = [t for t, _h in named_calls]
    assert "СПб" not in tabs  # своё переопределение — глобальный тумблер не трогает
    assert "Тюмень" in tabs  # без своего — обязана обновиться


# ── 7) Паритет: выключенный модуль городов — нулевая разница с любым city_code ─────────────

def test_module_off_parity_across_all_sheet_schema_surfaces(tmp_path):
    """Свежая база: `event_city_enabled` НЕ выставляется вовсе. Городской снимок для «spb»
    посеян заранее — он не должен повлиять НИ на один резолвер, и сам ключ не должен даже
    читаться (иначе городской слой протёк бы мимо гейта `cities_module_on()`)."""
    _db_ready(tmp_path, "test_percity25_module_off.db")

    async def scenario():
        await db.set_setting("sheet_header_schema__city__spb", '["ПОДМЕНА", "НЕ ДОЛЖНО ПРОЯВИТЬСЯ"]')
        await db.set_setting("reg_q_formats__city__spb", "off")

        for city_code in ("spb", "tyumen", "atlantis", None):
            headers_city = await reg_schema.active_sheet_headers(city_code)
            headers_global = await reg_schema.active_sheet_headers(None)
            assert headers_city == headers_global

            schema_city = await reg.get_sheet_schema(city_code)
            schema_global = await reg.get_sheet_schema(None)
            assert schema_city == schema_global

            code = await reg_schema.sheet_city_code(city_code if city_code else None)
            assert code is None

        data = {"telegram_id": 1, "full_name": "Х"}
        row_city = await reg.active_sheet_row(data, "spb")
        row_global = await reg.active_sheet_row(data, None)
        assert row_city == row_global

        # Ключ городского снимка не должен был прочитаться ни разу — иначе get_sheet_schema
        # вернул бы посеянную "ПОДМЕНА"-подделку вместо глобального live-фоллбэка.
        assert "ПОДМЕНА" not in row_city

    asyncio.run(scenario())
