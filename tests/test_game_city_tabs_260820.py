"""Quick GAME-CITY-TABS: per-city gamification sheet tabs.

- services/game_sheets.py pure filters (tasks by task city / NULL="all", submissions by the
  DELEGATE's city, both through cities.normalize_city so NULL/unknown fall to the default city);
- game_tab_plan(): module OFF -> exactly the two whole-event tabs; module ON -> + a matrix/
  history pair per ENABLED city with a non-empty tab base, named base + registry suffix
  (city_tab_suffix__game / city_tab_suffix__game_history, defaults «Гейма»/«История сдач»);
- rebuild_game_sheets(): 2 tabs OFF, 2 + 2N ON, one failing tab never skips the rest;
- confirm/report screens name every tab; a city-bound manager sees «their» city's pair.
"""
import asyncio

import pytest

import cities
from config import config
from database import db
from handlers import admin as admin_mod  # noqa: F401 -- seam-imports admin_gamification
from handlers import admin_gamification
import services.game_sheets as game_sheets

ADMIN_ID = 930991
MANAGER_ID = 930992
MSK_USER = 930993
SPB_USER = 930994
TYUMEN_USER = 930995

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
    {"code": "tyumen", "label": "Тюмень", "tab_base": "Тюмень", "enabled": 1, "sort_order": 2},
]


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_city_tabs.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]
    config.EVENT_CITY_DEFAULT = "msk"


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.answers_sent = []
        self.text = None
        self.markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()

    async def answer(self, text=None, show_alert=False):
        pass


# ── pure filters ────────────────────────────────────────────────────────────────────────────

def test_filter_tasks_null_city_means_all_cities_and_unknown_falls_to_default():
    tasks = [
        {"id": 1, "event_city": None},
        {"id": 2, "event_city": "spb"},
        {"id": 3, "event_city": "msk"},
        {"id": 4, "event_city": "gone_city"},  # unknown -> default city (normalize_city)
    ]
    assert [t["id"] for t in game_sheets.filter_tasks_for_city(tasks, "spb")] == [1, 2]
    assert [t["id"] for t in game_sheets.filter_tasks_for_city(tasks, "msk")] == [1, 3, 4]
    assert [t["id"] for t in game_sheets.filter_tasks_for_city(tasks, "tyumen")] == [1]


def test_filter_submissions_by_delegate_city_null_falls_to_default():
    subs = [
        {"id": 1, "user_event_city": "spb"},
        {"id": 2, "user_event_city": None},      # pre-cities row -> default city
        {"id": 3, "user_event_city": "tyumen"},
        {"id": 4, "user_event_city": "spb"},
    ]
    assert [s["id"] for s in game_sheets.filter_submissions_for_city(subs, "spb")] == [1, 4]
    assert [s["id"] for s in game_sheets.filter_submissions_for_city(subs, "msk")] == [2]
    assert [s["id"] for s in game_sheets.filter_submissions_for_city(subs, "tyumen")] == [3]


# ── game_tab_plan: names from registry / defaults ───────────────────────────────────────────

def test_tab_plan_module_off_is_exactly_the_two_shared_tabs(tmp_path):
    _db_ready(tmp_path)
    plan = asyncio.run(game_sheets.game_tab_plan())
    assert [(e["kind"], e["city"], e["tab"]) for e in plan] == [
        ("matrix", None, "Гейма"),
        ("history", None, "История сдач"),
    ]


def test_tab_plan_module_on_adds_a_pair_per_enabled_city_with_tab_base(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    plan = asyncio.run(game_sheets.game_tab_plan())
    assert [e["tab"] for e in plan] == [
        "Гейма", "История сдач",
        "СПб Гейма", "СПб История сдач",          # default suffix carries its own leading space
        "Тюмень Гейма", "Тюмень История сдач",
    ]
    # Moscow (empty tab base) gets no per-city pair -- its rows are on the shared tabs.
    assert all(e["city"] != "msk" for e in plan)
    assert plan[2]["city_label"] == "Санкт-Петербург"


def test_tab_plan_respects_registry_suffixes_and_disabled_cities(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_tab_suffix__game", "Игра"))          # manager typed no space
    asyncio.run(db.set_setting("city_tab_suffix__game_history", " Сдачи"))
    asyncio.run(db.set_setting("game_matrix_tab", "GAME"))
    asyncio.run(db.set_setting("city_enabled__tyumen", "off"))
    plan = asyncio.run(game_sheets.game_tab_plan())
    assert [e["tab"] for e in plan] == ["GAME", "История сдач", "СПб Игра", "СПб Сдачи"]


def test_tab_suffix_literals_cover_new_kinds():
    assert cities.TAB_SUFFIX["game"] == " Гейма"
    assert cities.TAB_SUFFIX["game_history"] == " История сдач"


# ── rebuild_game_sheets with a mocked writer ────────────────────────────────────────────────

def _seed_city_data():
    """Two tasks (one for all cities, one spb-only) + three delegates in three cities."""
    async def go():
        for uid, code in ((MSK_USER, "msk"), (SPB_USER, "spb"), (TYUMEN_USER, "tyumen")):
            await db.add_user({"telegram_id": uid, "username": f"u{uid}", "full_name": f"User {uid}",
                               "registration_date": "2026-08-01 10:00:00", "event_city": code})
        t_all = await db.create_task("Всем", "Light", 10, "text", "2026-08-30 23:59:00", ADMIN_ID)
        t_spb = await db.create_task("Питер", "Light", 20, "text", "2026-08-30 23:59:00", ADMIN_ID,
                                     event_city="spb")
        await db.create_submission(t_all, MSK_USER, "text", "готово", "2026-08-14 10:00:00")
        await db.create_submission(t_all, SPB_USER, "text", "готово", "2026-08-14 10:01:00")
        await db.create_submission(t_spb, SPB_USER, "text", "готово", "2026-08-14 10:02:00")
        await db.create_submission(t_all, TYUMEN_USER, "text", "готово", "2026-08-14 10:03:00")
    asyncio.run(go())


def test_rebuild_module_off_writes_exactly_two_tabs(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed_city_data()
    written = {}

    async def _fake_sync(title, headers, rows):
        written[title] = (headers, rows)
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)
    result = asyncio.run(admin_gamification.rebuild_game_sheets())
    assert result == (3, 4)
    assert list(written) == ["Гейма", "История сдач"]


def test_rebuild_module_on_writes_two_plus_two_per_city_filtered(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city_data()
    written = {}

    async def _fake_sync(title, headers, rows):
        written[title] = (headers, rows)
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)
    result = asyncio.run(admin_gamification.rebuild_game_sheets())
    assert len(result) == 2 + 2 * 2  # msk has no tab base -> only spb + tyumen pairs
    assert list(written) == [
        "Гейма", "История сдач", "СПб Гейма", "СПб История сдач", "Тюмень Гейма", "Тюмень История сдач",
    ]
    # shared tabs unchanged: every participant, every task column
    assert len(written["Гейма"][1]) == 3 and written["Гейма"][0][4:] == ["Всем", "Питер"]
    assert len(written["История сдач"][1]) == 4
    # spb: only the spb delegate; both tasks visible (all-cities + spb-only)
    spb_h, spb_rows = written["СПб Гейма"]
    assert spb_h[4:] == ["Всем", "Питер"]
    assert [r[0] for r in spb_rows] == [SPB_USER]
    assert len(written["СПб История сдач"][1]) == 2
    # tyumen: only the tyumen delegate; the spb-only task is not a column there
    ty_h, ty_rows = written["Тюмень Гейма"]
    assert ty_h[4:] == ["Всем"]
    assert [r[0] for r in ty_rows] == [TYUMEN_USER]
    assert len(written["Тюмень История сдач"][1]) == 1
    assert asyncio.run(db.get_setting("game_sheet_last_synced_at")) is not None


def test_rebuild_one_city_tab_failure_does_not_skip_the_rest(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city_data()
    calls = []

    async def _fake_sync(title, headers, rows):
        calls.append(title)
        if title == "СПб Гейма":
            raise RuntimeError("quota")  # even a raising writer must not abort the loop
        return len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)
    result = asyncio.run(admin_gamification.rebuild_game_sheets())
    assert len(calls) == 6
    assert result[2] == -1 and all(w >= 0 for i, w in enumerate(result) if i != 2)
    # partial failure -> no "up to date" timestamp
    assert asyncio.run(db.get_setting("game_sheet_last_synced_at")) is None


def test_autosync_treats_any_failed_tab_as_failure(tmp_path, monkeypatch):
    """services.game_sync's ok-check must understand the longer tuple (2 + 2N)."""
    import services.game_sync as game_sync
    game_sync.reset_for_tests()
    alerts = []

    async def _fake_alert(text):
        alerts.append(text)

    monkeypatch.setattr(game_sync, "_send_admin_alert", _fake_alert)

    async def fake_rebuild():
        return (1, 1, 1, -1, 1, 1)

    game_sync.set_rebuild(fake_rebuild)
    try:
        asyncio.run(game_sync._run_after(0))
        assert len(alerts) == 1
    finally:
        game_sync.reset_for_tests()
        game_sync.set_rebuild(None)


# ── screens ─────────────────────────────────────────────────────────────────────────────────

def test_report_lists_every_tab(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city_data()

    async def _fake_sync(title, headers, rows):
        return -1 if title == "Тюмень Гейма" else len(rows)

    monkeypatch.setattr(admin_gamification, "sync_named_worksheet", _fake_sync)
    callback = _FakeCallback("admin_game_sync_sheet_go")
    asyncio.run(admin_gamification.sync_game_sheets(callback))
    text = callback.message.answers_sent[-1]
    assert "Гейма: 3 строк" in text
    assert "СПб Гейма: 1 строк" in text
    assert "СПб История сдач: 2 строк" in text
    assert "Тюмень Гейма: ⚠️ ошибка синхронизации" in text
    assert "Тюмень История сдач: 1 строк" in text


def test_confirm_screen_names_shared_and_city_tabs(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    callback = _FakeCallback("admin_game_sync_sheet")
    asyncio.run(admin_gamification.sync_game_sheets_confirm(callback))
    text = callback.message.text
    for name in ("«Гейма»", "«История сдач»", "«СПб Гейма»", "«СПб История сдач»",
                 "«Тюмень Гейма»", "«Тюмень История сдач»"):
        assert name in text, name
    assert "только Санкт-Петербург" in text and "только Тюмень" in text
    assert "все города" in text
    assert "руками" in text.lower() and "пропадут" in text.lower()


def test_confirm_screen_module_off_unchanged_wording(tmp_path):
    _db_ready(tmp_path)
    callback = _FakeCallback("admin_game_sync_sheet")
    asyncio.run(admin_gamification.sync_game_sheets_confirm(callback))
    text = callback.message.text
    assert "две вкладки" in text
    assert "СПб" not in text and "все города" not in text


def test_city_bound_manager_sees_own_city_pair_only(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.add_staff(MANAGER_ID, "manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MANAGER_ID, "spb"))
    callback = _FakeCallback("admin_game_sync_sheet", user_id=MANAGER_ID)
    asyncio.run(admin_gamification.sync_game_sheets_confirm(callback))
    text = callback.message.text
    assert "«Гейма»" in text and "«История сдач»" in text  # shared tabs always listed
    assert "«СПб Гейма»" in text and "«СПб История сдач»" in text
    assert "Тюмень" not in text
    assert "других городов" in text  # honest: the button still rebuilds everything
