"""Phase 14 Plan 06 (CITY-07) — cities registry moves from `.env` into the DB.

Widest blast-radius test in the phase: `cities.CITIES` used to be a module-level constant
parsed from `config.EVENT_CITIES` at import time; it is now a cache backed by the `cities`
table, reloaded on demand. `from cities import CITIES` is aliased by name into
`handlers/admin.py` and `handlers/registration.py` — the single highest-risk invariant this
file guards is "reload_cities() mutates the SAME list object", never rebinding `CITIES`.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_game_city_tasks_091.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import pathlib

from config import config
from database import db
import cities


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_cities_registry_260818.db")
    asyncio.run(db.init_db())


# ── Task 1: `cities` table + CRUD accessors ──────────────────────────────────────────────────

def test_init_db_creates_cities_table_and_is_idempotent(tmp_path):
    _db_ready(tmp_path)

    async def _inspect():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            async with conn.execute("PRAGMA table_info(cities)") as cursor:
                rows = await cursor.fetchall()
        return {row[1] for row in rows}

    columns = asyncio.run(_inspect())
    assert {"code", "label", "tab_base", "enabled", "sort_order", "created_at"} <= columns

    # Second init_db() must not raise (CREATE TABLE IF NOT EXISTS is idempotent).
    asyncio.run(db.init_db())


def test_insert_and_list_cities_rows_ordered_by_sort_order(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("tmn", "Тюмень", "", 1)
        await db.insert_city("msk", "Москва", "", 0)
        await db.insert_city("spb", "Санкт-Петербург", "SPB", 2)
        return await db.list_cities_rows()

    rows = asyncio.run(_run())
    assert [r["code"] for r in rows] == ["msk", "tmn", "spb"]


def test_update_city_changes_only_passed_fields(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("msk", "Москва", "", 0)
        ok = await db.update_city("msk", label="Мск")
        rows = await db.list_cities_rows()
        missing = await db.update_city("nope", label="x")
        return ok, rows[0], missing

    ok, row, missing = asyncio.run(_run())
    assert ok is True
    assert row["label"] == "Мск"
    assert row["tab_base"] == ""  # untouched field stays as inserted
    assert missing is False


def test_delete_city_row_removes_once_then_returns_false(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("msk", "Москва", "", 0)
        first = await db.delete_city_row("msk")
        second = await db.delete_city_row("msk")
        remaining = await db.count_cities()
        return first, second, remaining

    first, second, remaining = asyncio.run(_run())
    assert first is True
    assert second is False
    assert remaining == 0


def test_count_users_and_tasks_by_city_ignore_null(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (1, "spb")
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (2, "spb")
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (3, None)
            )
            await conn.execute(
                "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
                "created_at, event_city) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", "cat", 1, "text", "2026-01-01 00:00:00", "2026-01-01 00:00:00", "spb"),
            )
            await conn.execute(
                "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
                "created_at, event_city) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t2", "cat", 1, "text", "2026-01-01 00:00:00", "2026-01-01 00:00:00", None),
            )
            await conn.commit()
        users_spb = await db.count_users_by_city("spb")
        tasks_spb = await db.count_tasks_by_city("spb")
        return users_spb, tasks_spb

    users_spb, tasks_spb = asyncio.run(_run())
    assert users_spb == 2
    assert tasks_spb == 1


def test_db_py_never_imports_cities_module():
    """Structural: database/db.py stays a pure SQL layer -- no `import cities`/`from cities`
    (that would create an import cycle, since cities.py already imports database.db)."""
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("database", "db.py").read_text(encoding="utf-8")
    assert "import cities" not in src
    assert "from cities" not in src


# ── Task 2: cache, seed-from-.env, reload with in-place mutation, code generator ────────────

def _snapshot_cities():
    """Save + return a restorer for the module-level CITIES cache, so Task 2 tests don't leak
    state into Task 3/other test files that run in the same pytest session."""
    saved = list(cities.CITIES)

    def _restore():
        cities.set_cities_for_test(saved)

    return _restore


def test_seed_cities_if_empty_creates_rows_then_noop_on_second_call(tmp_path):
    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        first = asyncio.run(cities.seed_cities_if_empty())
        second = asyncio.run(cities.seed_cities_if_empty())
        rows = asyncio.run(db.list_cities_rows())
    finally:
        restore()
    expected = cities.parse_cities(config.EVENT_CITIES)
    assert first == len(expected)
    assert second == 0
    assert {r["code"] for r in rows} == {c["code"] for c in expected}


def test_seed_migrates_old_enabled_toggle_without_deleting_the_key(tmp_path):
    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        expected = cities.parse_cities(config.EVENT_CITIES)
        off_code = expected[-1]["code"]
        asyncio.run(db.set_setting(f"city_enabled__{off_code}", "off"))
        asyncio.run(cities.seed_cities_if_empty())
        rows = {r["code"]: r for r in asyncio.run(db.list_cities_rows())}
        still_set = asyncio.run(db.get_setting(f"city_enabled__{off_code}"))
    finally:
        restore()
    assert rows[off_code]["enabled"] == 0
    assert still_set == "off"  # old key is NOT deleted


def test_reload_cities_mutates_same_object_and_reflects_db(tmp_path):
    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        before_id = id(cities.CITIES)
        asyncio.run(db.insert_city("nsk", "Новосибирск", "", 0))
        result = asyncio.run(cities.reload_cities())
        after_id = id(cities.CITIES)
        codes = cities.city_codes()
    finally:
        restore()
    assert before_id == after_id
    assert result is cities.CITIES
    assert codes == ["nsk"]


def test_reload_cities_on_empty_table_does_not_clear_cache(tmp_path):
    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([{"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0}])
        result = asyncio.run(cities.reload_cities())
        # `result` aliases the live CITIES object (reload_cities returns it by reference on
        # the empty-rows path) -- snapshot its content NOW, before `restore()` mutates the
        # same object back in the `finally` below.
        result_snapshot = list(result)
        codes = cities.city_codes()
    finally:
        restore()
    assert codes == ["msk"]
    assert result_snapshot == [{"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0}]


def test_default_city_code_first_by_sort_order_and_emergency_fallback():
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 0},
            {"code": "tmn", "label": "Тюмень", "tab_base": "", "enabled": 1, "sort_order": 1},
        ])
        first = cities.default_city_code()
        cities.set_cities_for_test([])
        empty = cities.default_city_code()
    finally:
        restore()
    assert first == "spb"
    assert empty == "msk"


def test_is_city_enabled_column_is_source_of_truth_override_key_wins(tmp_path):
    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 0, "sort_order": 0},
        ])
        from_column = asyncio.run(cities.is_city_enabled("spb"))
        asyncio.run(db.set_setting("city_enabled__spb", "on"))
        from_override = asyncio.run(cities.is_city_enabled("spb"))
    finally:
        restore()
    assert from_column is False
    assert from_override is True


def test_make_city_code_translit_and_collision_suffixes():
    code = cities.make_city_code("Нижний Новгород", existing=set())
    assert code == "nizhniy_novgorod"
    assert code.isascii() and code.replace("_", "").isalnum() and len(code) <= 16

    first = cities.make_city_code("Тест", existing=set())
    second = cities.make_city_code("Тест", existing={first})
    third = cities.make_city_code("Тест", existing={first, second})
    assert second == f"{first}_2"
    assert third == f"{first}_3"


def test_set_cities_for_test_mutates_same_object():
    restore = _snapshot_cities()
    try:
        before_id = id(cities.CITIES)
        cities.set_cities_for_test([{"code": "x", "label": "X", "tab_base": "", "enabled": 1, "sort_order": 0}])
        after_id = id(cities.CITIES)
    finally:
        restore()
    assert before_id == after_id


# ── Task 3: deep-link without a stale dict, startup wiring, structural sweep ────────────────

def test_deep_link_resolves_for_a_city_added_after_start_pitfall_1(tmp_path):
    """14-RESEARCH.md Pitfall 1: `_city_tag_map()` must be recomputed on every call, not
    frozen at import time -- otherwise a city added from the bot never resolves its own
    deep-link token, silently falling back to the ordinary city-picker screen."""
    from handlers import registration as reg

    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        asyncio.run(db.insert_city("spb", "Санкт-Петербург", "СПб", 0))
        asyncio.run(cities.reload_cities())
        assert reg._extract_event_city("city_spb") == "spb"  # pre-existing token still works

        # Add a city AFTER the process is already up -- this is the scenario Pitfall 1 warns
        # about: a module dict frozen at import time would never see it.
        asyncio.run(db.insert_city("nsk", "Новосибирск", "", 1))
        asyncio.run(cities.reload_cities())
        assert reg._extract_event_city("city_nsk") == "nsk"
        assert reg._extract_event_city("city_spb") == "spb"  # old token unaffected by the add

        # Malformed/near-miss tokens never resolve (T-14-27: exact match only, no startswith).
        assert reg._extract_event_city("city_") is None
        assert reg._extract_event_city("CITY_NSK") is None
        assert reg._extract_event_city("city_nsk_extra") is None
    finally:
        restore()


def test_reload_visible_to_all_readers_pitfall_2(tmp_path):
    """14-RESEARCH.md Pitfall 2: a structural sweep across every reader of `CITIES` -- the
    admin/registration aliases, `city_codes()`, `city_scope()`'s exclude list,
    `refresh_city_filter_spec()`, and the delegate city-picker keyboard must all see a newly
    inserted city right after `reload_cities()`, with zero call-site changes."""
    from handlers import admin as admin_mod
    from handlers import registration as reg

    _db_ready(tmp_path)
    restore = _snapshot_cities()
    try:
        asyncio.run(db.insert_city("msk", "Москва", "", 0))
        asyncio.run(db.insert_city("nsk", "Новосибирск", "", 1))
        asyncio.run(cities.reload_cities())

        assert "nsk" in cities.city_codes()
        assert "nsk" in {c["code"] for c in admin_mod.CITIES}
        assert "nsk" in {c["code"] for c in reg.CITIES}

        default_scope = cities.city_scope("msk")
        assert default_scope is not None
        assert "nsk" in default_scope[1]  # default city's exclude list picks up the new code

        spec = [{"field": "event_city", "value": "msk", "exclude": []}]
        refreshed = cities.refresh_city_filter_spec(spec)
        assert refreshed is not None
        assert "nsk" in refreshed[0]["exclude"]

        kb = asyncio.run(reg._city_fork_kb())
        picked_codes = [
            btn.callback_data.split(":", 1)[1]
            for row in kb.inline_keyboard for btn in row
        ]
        assert "nsk" in picked_codes
    finally:
        restore()


def test_admin_and_registration_CITIES_are_the_same_object_as_cache():
    """Structural contract against future regression: `from cities import CITIES` in both
    god-files must alias the SAME list object cities.py mutates in place."""
    from handlers import admin as admin_mod
    from handlers import registration as reg

    assert admin_mod.CITIES is cities.CITIES
    assert reg.CITIES is cities.CITIES


def test_no_module_besides_cities_reads_config_EVENT_CITIES():
    """Structural sentry (mirrors test_gate_no_legacy_admin_check_remains's shape): after this
    plan, `config.EVENT_CITIES` (the actual .env read) may only appear in `cities.py` --
    the cold fallback value + the one-time `seed_cities_if_empty()` seed. Matches the literal
    attribute-access pattern (`config.EVENT_CITIES`), not a bare substring search, because two
    OTHER files (`handlers/admin.py`, `services/scheduler.py`, both out of this plan's file
    ownership per 14-CONTEXT.md's wave split) carry pre-existing PROSE comments that merely
    name "EVENT_CITIES" as historical context from Phase 07.1/07.2 -- those are not reads and
    are not this plan's regression to fix."""
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {root / "cities.py"}
    offenders = []
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        # `.claude/worktrees/*` — параллельные рабочие копии агентов: те же файлы второй раз.
        if parts[0] in {"tests", ".venv", "__pycache__", ".claude"} or ".venv" in parts:
            continue
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "config.EVENT_CITIES" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_main_py_seeds_and_reloads_cities_before_header_materialization():
    """Structural: `main.py::main()` must call `seed_cities_if_empty()` and `reload_cities()`
    (in that relative position) BEFORE `_maybe_ensure_city_sheet_headers()` is spawned -- that
    function already reads `cities.enabled_cities()`, so the cache must be populated first."""
    root = pathlib.Path(__file__).resolve().parents[1]
    src = root.joinpath("main.py").read_text(encoding="utf-8")
    seed_pos = src.index("seed_cities_if_empty")
    reload_pos = src.index("reload_cities")
    headers_pos = src.index("_maybe_ensure_city_sheet_headers")
    assert seed_pos < headers_pos
    assert reload_pos < headers_pos


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Plan 14-07 (CITY-07): «🏙 Города» admin CRUD screen — add/rename/tab-base/default/delete
# ═══════════════════════════════════════════════════════════════════════════════════════════

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import admin as admin_mod
from handlers import admin_cities  # Phase 13 (13-05): cities screen/CRUD/season moved here
from handlers.admin_caps import required_capability

ADMIN_ID = 941101
MANAGER_ID = 941102


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    async def send_message(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = FakeBot()
        self.answers_sent = []
        self.answer_markups = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)
        self.text = text

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _seed_cities_db(rows):
    """rows: list of (code, label, tab_base, sort_order[, enabled])."""
    async def _run():
        for r in rows:
            code, label, tab_base, sort_order = r[0], r[1], r[2], r[3]
            enabled = r[4] if len(r) > 4 else 1
            await db.insert_city(code, label, tab_base, sort_order, enabled)
        await cities.reload_cities()

    asyncio.run(_run())


def _bind_manager_to_city(uid, code):
    async def _run():
        await db.add_staff(uid, "reg_manager", ADMIN_ID)
        await db.set_staff_city(uid, code)

    asyncio.run(_run())


# ── Task 1: screen reads DB, access gate, enable/disable writes the column ──────────────────

def test_screen_shows_city_from_db_after_insert_and_reload(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань, 14 ноября", "", 1)])
        text = asyncio.run(admin_cities.render_cities_text())
        kb = asyncio.run(admin_cities.build_cities_keyboard())
        flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    finally:
        restore()
    assert "Казань, 14 ноября" in text
    assert "city_toggle:kzn" in flat


def test_keyboard_rows_rename_tab_toggle_default_and_delete_only_when_safe(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        asyncio.run(db.add_user({"telegram_id": 1, "event_city": "kzn", "registration_date": "2026-08-01"}))
        kb = asyncio.run(admin_cities.build_cities_keyboard())
        flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    finally:
        restore()
    assert "city_rename:msk" in flat and "city_rename:kzn" in flat
    assert "city_tab:msk" in flat and "city_tab:kzn" in flat
    assert "city_default:kzn" in flat  # msk IS default -> no ⭐ button for msk itself
    assert "city_default:msk" not in flat
    assert "city_del:msk" in flat  # no users/tasks on msk -> deletable
    assert "city_del:kzn" not in flat  # kzn has a bound delegate -> not deletable


def test_city_toggle_writes_enabled_column_deletes_legacy_key_and_reloads(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        asyncio.run(db.set_setting("city_enabled__kzn", "on"))  # legacy key present
        callback = FakeCallback("city_toggle:kzn")
        asyncio.run(admin_cities.city_toggle(callback))
        legacy_after = asyncio.run(db.get_setting("city_enabled__kzn"))
        enabled_after = asyncio.run(cities.is_city_enabled("kzn"))
        rows = {r["code"]: r for r in asyncio.run(db.list_cities_rows())}
    finally:
        restore()
    assert legacy_after is None  # legacy key removed
    assert rows["kzn"]["enabled"] == 0  # column flipped off
    assert enabled_after is False  # cache reflects it, not just the DB row


def test_cities_screen_denied_for_manager_bound_to_one_city(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        _bind_manager_to_city(MANAGER_ID, "kzn")
        callback = FakeCallback("admin_cities", user_id=MANAGER_ID)
        asyncio.run(admin_cities.show_admin_cities(callback))
        toggle_cb = FakeCallback("city_toggle:msk", user_id=MANAGER_ID)
        asyncio.run(admin_cities.city_toggle(toggle_cb))
        rows_after = {r["code"]: r for r in asyncio.run(db.list_cities_rows())}
    finally:
        restore()
    assert callback.answers[-1][1] is True  # show_alert
    assert "Казань" in callback.answers[-1][0]
    assert callback.message.answers_sent == []  # screen never rendered
    assert toggle_cb.answers[-1][1] is True
    assert rows_after["msk"]["enabled"] == 1  # write refused, DB untouched


def test_cities_screen_allowed_for_superadmin_and_unbound_manager(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        allowed_super = asyncio.run(admin_cities._cities_screen_allowed(ADMIN_ID))
        asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))  # unbound (city stays NULL)
        allowed_unbound = asyncio.run(admin_cities._cities_screen_allowed(MANAGER_ID))
    finally:
        restore()
    assert allowed_super is True
    assert allowed_unbound is True


def test_new_city_callbacks_resolve_to_settings_capability():
    for key in ("city_add", "city_rename:kzn", "city_tab:kzn", "city_default:kzn",
                "city_del:kzn", "city_del_go:kzn"):
        assert required_capability(callback_data=key) == "settings"
    assert required_capability(raw_state="CityForm:add_label") == "settings"


# ── Task 2: add wizard + rename / tab-base edit ──────────────────────────────────────────────

from handlers.states import CityForm


def test_city_add_starts_wizard_at_add_label(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        callback = FakeCallback("city_add")
        state = _new_state()
        asyncio.run(admin_cities.city_add(callback, state))
        reached_state = asyncio.run(state.get_state())
    finally:
        restore()
    assert reached_state == CityForm.add_label


def test_city_add_label_step_moves_to_add_tab(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        state = _new_state()
        asyncio.run(state.set_state(CityForm.add_label))
        msg = FakeMessage(text="Казань, 14 ноября")
        asyncio.run(admin_cities.city_add_label_step(msg, state))
        reached_state = asyncio.run(state.get_state())
        prompt = msg.answers_sent[-1]
    finally:
        restore()
    assert reached_state == CityForm.add_tab
    assert "Enter" in prompt or "—" in prompt


def test_city_add_tab_step_dash_creates_empty_tab_base_and_generates_hidden_code(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        state = _new_state()
        asyncio.run(state.update_data(city_new_label="Казань, 14 ноября"))
        asyncio.run(state.set_state(CityForm.add_tab))
        msg = FakeMessage(text="—")
        asyncio.run(admin_cities.city_add_tab_step(msg, state))
        rows = asyncio.run(db.list_cities_rows())
        confirmation = msg.answers_sent[0]
    finally:
        restore()
    new_row = [r for r in rows if r["label"] == "Казань, 14 ноября"][0]
    assert new_row["tab_base"] == ""
    assert new_row["code"] not in ("Казань, 14 ноября",)  # never the raw label
    assert new_row["code"] == "kazan"[:16] or new_row["code"].startswith("kazan")
    assert new_row["code"] not in confirmation  # code never shown to the human


def test_city_add_tab_step_text_sets_tab_base_and_wires_end_to_end(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        state = _new_state()
        asyncio.run(state.update_data(city_new_label="СПб"))
        asyncio.run(state.set_state(CityForm.add_tab))
        msg = FakeMessage(text="СПб")
        asyncio.run(admin_cities.city_add_tab_step(msg, state))
        codes_after = cities.city_codes()
        new_code = [c for c in cities.all_cities() if c["label"] == "СПб"][0]["code"]
        from handlers import registration as reg
        resolved = reg._extract_event_city(f"city_{new_code}")
    finally:
        restore()
    assert new_code in codes_after
    assert resolved == new_code


def test_city_add_label_collision_gets_suffix_both_cities_created(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        for _ in range(2):
            state = _new_state()
            asyncio.run(state.update_data(city_new_label="Тест"))
            asyncio.run(state.set_state(CityForm.add_tab))
            msg = FakeMessage(text="—")
            asyncio.run(admin_cities.city_add_tab_step(msg, state))
        rows = [r for r in asyncio.run(db.list_cities_rows()) if r["label"] == "Тест"]
    finally:
        restore()
    assert len(rows) == 2
    assert rows[0]["code"] != rows[1]["code"]


def test_city_add_reload_cities_called_new_city_resolves_deep_link(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        state = _new_state()
        asyncio.run(state.update_data(city_new_label="Новосибирск"))
        asyncio.run(state.set_state(CityForm.add_tab))
        msg = FakeMessage(text="—")
        asyncio.run(admin_cities.city_add_tab_step(msg, state))
        from handlers import registration as reg
        new_code = [c for c in cities.all_cities() if c["label"] == "Новосибирск"][0]["code"]
        resolved = reg._extract_event_city(f"city_{new_code}")
        codes_after = cities.city_codes()
    finally:
        restore()
    assert resolved == new_code
    assert new_code in codes_after


def test_city_rename_and_tab_edit_update_column_and_delete_legacy_override(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        asyncio.run(db.set_setting("city_label__kzn", "старая подпись"))
        asyncio.run(db.set_setting("city_tab__kzn", "СтараяВкладка"))

        rename_cb = FakeCallback("city_rename:kzn")
        rename_state = _new_state()
        asyncio.run(admin_cities.city_rename_start(rename_cb, rename_state))
        assert asyncio.run(rename_state.get_state()) == CityForm.edit_label
        label_msg = FakeMessage(text="Казань (новая)")
        asyncio.run(admin_cities.city_edit_label_step(label_msg, rename_state))

        tab_cb = FakeCallback("city_tab:kzn")
        tab_state = _new_state()
        asyncio.run(admin_cities.city_tab_start(tab_cb, tab_state))
        assert asyncio.run(tab_state.get_state()) == CityForm.edit_tab
        assert "переносит только новые" in tab_cb.message.answers_sent[-1]
        tab_msg = FakeMessage(text="НоваяВкладка")
        asyncio.run(admin_cities.city_edit_tab_step(tab_msg, tab_state))

        row = {r["code"]: r for r in asyncio.run(db.list_cities_rows())}["kzn"]
        legacy_label = asyncio.run(db.get_setting("city_label__kzn"))
        legacy_tab = asyncio.run(db.get_setting("city_tab__kzn"))
    finally:
        restore()
    assert row["label"] == "Казань (новая)"
    assert row["tab_base"] == "НоваяВкладка"
    assert legacy_label is None
    assert legacy_tab is None


def test_city_form_cancel_writes_nothing(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        before = asyncio.run(db.count_cities())
        state = _new_state()
        asyncio.run(state.update_data(city_new_label="Отменённый город"))
        asyncio.run(state.set_state(CityForm.add_tab))
        msg = FakeMessage(text="Отмена")
        asyncio.run(admin_cities.cancel_city_form(msg, state))
        after = asyncio.run(db.count_cities())
        reached_state = asyncio.run(state.get_state())
    finally:
        restore()
    assert before == after
    assert reached_state is None


def test_wizard_texts_never_mention_tab_base_or_code_literals(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        add_cb = FakeCallback("city_add")
        add_state = _new_state()
        asyncio.run(admin_cities.city_add(add_cb, add_state))
        label_msg = FakeMessage(text="Тестоград")
        asyncio.run(admin_cities.city_add_label_step(label_msg, add_state))
        texts = add_cb.message.answers_sent + label_msg.answers_sent
    finally:
        restore()
    for t in texts:
        assert "tab_base" not in t
        assert "код города" not in t.lower()


# ── Task 3: delete with server-side triple-check ─────────────────────────────────────────────

def test_city_del_shows_confirmation_and_db_unchanged(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        before = asyncio.run(db.count_cities())
        callback = FakeCallback("city_del:kzn")
        asyncio.run(admin_cities.city_delete_confirm(callback))
        after = asyncio.run(db.count_cities())
        text = callback.message.answers_sent[-1]
        kb = callback.message.answer_markups[-1]
        flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    finally:
        restore()
    assert before == after
    assert "Казань" in text
    assert "city_del_go:kzn" in flat


def test_city_del_refuses_when_delegate_appeared_between_show_and_click(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        asyncio.run(db.add_user({"telegram_id": 5, "event_city": "kzn", "registration_date": "2026-08-01"}))
        before = asyncio.run(db.count_cities())
        callback = FakeCallback("city_del:kzn")
        asyncio.run(admin_cities.city_delete_confirm(callback))
        after = asyncio.run(db.count_cities())
    finally:
        restore()
    assert before == after
    assert callback.answers[-1][1] is True
    assert "выключить" in callback.answers[-1][0]


def test_city_del_go_deletes_reloads_and_removes_from_city_codes(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        callback = FakeCallback("city_del_go:kzn")
        asyncio.run(admin_cities.city_delete_go(callback))
        codes_after = cities.city_codes()
        kb = callback.message.answer_markups[-1]
        flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    finally:
        restore()
    assert "kzn" not in codes_after
    assert not any(cd and cd.endswith(":kzn") for cd in flat)


def test_city_del_forbidden_for_default_city(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0)])
        before = asyncio.run(db.count_cities())
        callback = FakeCallback("city_del:msk")
        asyncio.run(admin_cities.city_delete_confirm(callback))
        after = asyncio.run(db.count_cities())
    finally:
        restore()
    assert before == after
    assert callback.answers[-1][1] is True
    assert "по умолчанию" in callback.answers[-1][0]


def test_city_del_go_deep_link_no_longer_resolves_after_deletion(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        from handlers import registration as reg
        assert reg._extract_event_city("city_kzn") == "kzn"
        callback = FakeCallback("city_del_go:kzn")
        asyncio.run(admin_cities.city_delete_go(callback))
        resolved_after = reg._extract_event_city("city_kzn")
    finally:
        restore()
    assert resolved_after is None


def test_city_del_go_double_tap_second_time_says_already_deleted(tmp_path):
    _db_ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID]
    restore = _snapshot_cities()
    try:
        _seed_cities_db([("msk", "Москва", "", 0), ("kzn", "Казань", "", 1)])
        first = FakeCallback("city_del_go:kzn")
        asyncio.run(admin_cities.city_delete_go(first))
        second = FakeCallback("city_del_go:kzn")
        asyncio.run(admin_cities.city_delete_go(second))
    finally:
        restore()
    assert first.answers[-1] == ("Город удалён", True)
    assert second.answers[-1] == ("Город уже удалён", True)
