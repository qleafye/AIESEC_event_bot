"""Phase 33 (delegate-card admin actions): «🏙 Перевести в город» — общий примитив перевода
делегата между городами мероприятия.

Покрывает четыре слоя, по образцу соседних квиков (`tests/test_sheet_tab_rename_260919.py` —
fake gspread; `tests/test_admin_program_260924.py` — fake callback/message + city-bound staff):

- `services/sheets.py` (find_rows_by_id/delete_row_by_id) — вкладка НЕ создаётся на промахе
  (памятка standalone-script-sheet-traps), дубли отказывают, а не гадают.
- `services/city_move.move_user_city` — БД (users + трек + reg_drafts/reg_started/
  неотправленные очереди дайджеста), dry_run ничего не пишет, лист (add-new-then-delete-old,
  сбой листа не рвёт БД).
- `handlers/admin_city_move.py` — картинка UI (выбор города/подтверждение/применение/отмена),
  права на ОБА города, подделанные callback_data.
- `services/checkin.record_arrival` — «чужой город» сканера читает `users.event_city` вживую,
  переезжает само (координатор, D-checkin): после перевода СПб→Москва сессия в Москве
  пускает, в СПб — уже нет.

pytest-asyncio недоступна в этом окружении — `asyncio.run()` на каждый async-хелпер, БД —
шаблонная копия (`tests/_dbtpl.py::fast_init_db`)."""
from __future__ import annotations

import asyncio

import aiosqlite
import gspread
import pytest

import cities
from config import config
from database import db
from handlers import admin_city_move
from handlers.admin_caps import role_caps_key
import services.sheets as sheets_mod
from services.checkin import record_arrival
from services.city_move import (
    STATUS_MODE_KEEP,
    STATUS_MODE_TO_MODERATION,
    move_user_city,
    preview_city_move,
)
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 260925001
BOUND_MSK_ID = 260925002
BOUND_SPB_ID = 260925003
DELEGATE_ID = 260925101

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


def _db_ready(tmp_path, name="test_city_move_260925.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


async def _enable_cities_module():
    await db.set_setting("event_city_enabled", "on")


async def _disable_sheet_logs_autosync():
    """Только для тестов, что патчат `gspread.service_account` напрямую (Part B2 ниже):
    `record_answer_history` (перевод города пишет её при смене event_city) сама планирует
    фоновую синхронизацию листа «История правок» (`services/sheet_logs.py::
    schedule_sheet_logs_sync`, дефолт настройки `sheet_logs_autosync` — "on") — фоновая
    корутина летит на ТОМ ЖЕ event loop и гоняется за нашим же fake-gspread клиентом,
    добавляя гонку и add_worksheet_calls, не имеющие отношения к переводу города. У тестов
    через `_install_fake_sheets` этой гонки нет — там `config.GOOGLE_SHEET_ID` не патчится,
    автосинхрон сам выходит по первой проверке."""
    await db.set_setting("sheet_logs_autosync", "off")


async def _set_registration_mode(code: str, mode: str):
    key = cities.per_city_key("registration_mode", code)
    await db.set_setting(key, mode)


async def _seed_user(telegram_id, *, city="spb", participant_type=None, status="approved",
                      full_name="Тест Тестов"):
    await db.add_user({
        "telegram_id": telegram_id, "full_name": full_name, "registration_date": "2026-01-01",
        "event_city": city, "participant_type": participant_type,
    })
    if status != "approved":  # add_user never touches status; schema default is 'approved'
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
            await conn.commit()


async def _setup_bound_staff():
    await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
    await db.add_staff(BOUND_MSK_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_MSK_ID, "msk")
    await db.add_staff(BOUND_SPB_ID, "reg_manager", SUPERADMIN_ID)
    await db.set_staff_city(BOUND_SPB_ID, "spb")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part A: services/sheets.py — find_rows_by_id / delete_row_by_id, fake gspread
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self._rows = rows  # list of lists, row[0] = telegram_id
        self.deleted = []

    def col_values(self, col):
        idx = col - 1
        out = ["ID"]
        for r in self._rows:
            out.append(str(r[idx]) if idx < len(r) else "")
        return out

    def delete_rows(self, row_index):
        # row_index is 1-based including header; data row 2 -> self._rows[0]
        self.deleted.append(row_index)
        del self._rows[row_index - 2]

    def append_row(self, data, value_input_option=None):
        self._rows.append(list(data))


class _FakeSpreadsheet:
    def __init__(self, worksheets: dict):
        self._by_title = dict(worksheets)
        self.add_worksheet_calls = []

    def worksheet(self, title):
        if title not in self._by_title:
            raise gspread.WorksheetNotFound(title)
        return self._by_title[title]

    def worksheets(self):
        return list(self._by_title.values())

    def add_worksheet(self, title, rows, cols):
        # Тревога сама по себе, а не только исключение: перевод города ловит исключения
        # внутри своего try/except и превращает их в report["sheet"]["error"], так что тест
        # обязан проверить именно этот счётчик, а не полагаться на всплытие AssertionError.
        self.add_worksheet_calls.append(title)
        raise AssertionError(f"add_worksheet({title!r}) must never be called — city-move contract is no-create")


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


def _reset_sheets_state():
    sheets_mod._reset_sheet_cache()
    sheets_mod._named_sheets.clear()
    sheets_mod._header_checked_tabs.clear()


def test_find_rows_by_id_missing_named_tab_returns_none_without_creating(monkeypatch):
    _reset_sheets_state()
    fake_ss = _patch_gspread(monkeypatch, {})  # spreadsheet has NO tabs at all

    result = _run(sheets_mod.find_rows_by_id("Нет такой", DELEGATE_ID))

    assert result is None
    assert fake_ss.worksheets() == []  # add_worksheet would have raised AssertionError anyway


def test_find_rows_by_id_finds_matching_row_on_named_tab(monkeypatch):
    _reset_sheets_state()
    ws = _FakeWorksheet("СПб Акция", [[DELEGATE_ID, "Тест"], [999, "Другой"]])
    _patch_gspread(monkeypatch, {"СПб Акция": ws})

    result = _run(sheets_mod.find_rows_by_id("СПб Акция", DELEGATE_ID))

    assert result == [2]  # 1-based, header at row 1


def test_delete_row_by_id_missing_tab_returns_not_found_tab_and_does_not_create(monkeypatch):
    _reset_sheets_state()
    fake_ss = _patch_gspread(monkeypatch, {})

    result = _run(sheets_mod.delete_row_by_id("Пропавшая", DELEGATE_ID))

    assert result == "not_found_tab"
    assert fake_ss.worksheets() == []


def test_delete_row_by_id_removes_exactly_one_matching_row(monkeypatch):
    _reset_sheets_state()
    ws = _FakeWorksheet("СПб Акция", [[DELEGATE_ID, "Тест"], [999, "Другой"]])
    _patch_gspread(monkeypatch, {"СПб Акция": ws})

    result = _run(sheets_mod.delete_row_by_id("СПб Акция", DELEGATE_ID))

    assert result == "ok"
    assert ws.deleted == [2]
    assert [r[0] for r in ws._rows] == [999]


def test_delete_row_by_id_duplicate_refuses_to_guess(monkeypatch):
    _reset_sheets_state()
    ws = _FakeWorksheet("СПб Акция", [[DELEGATE_ID, "A"], [DELEGATE_ID, "B"]])
    _patch_gspread(monkeypatch, {"СПб Акция": ws})

    result = _run(sheets_mod.delete_row_by_id("СПб Акция", DELEGATE_ID))

    assert result == "duplicate"
    assert ws.deleted == []  # ничего не удалено


def test_delete_row_by_id_no_matching_row_on_existing_tab(monkeypatch):
    _reset_sheets_state()
    ws = _FakeWorksheet("СПб Акция", [[999, "Другой"]])
    _patch_gspread(monkeypatch, {"СПб Акция": ws})

    result = _run(sheets_mod.delete_row_by_id("СПб Акция", DELEGATE_ID))

    assert result == "not_found_row"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part B: services/city_move.move_user_city — orchestration (fake sheets_service, real DB)
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _FakeSheetStore:
    """Monkeypatched onto `services.sheets` — city_move.py calls these through its own
    `sheets_service` alias, which is the SAME module object, so patching the module's
    attributes here is visible there too."""

    def __init__(self):
        self.tabs: dict[str, list[list]] = {}
        self.appends: list[tuple] = []
        self.deletes: list[tuple] = []

    @staticmethod
    def _key(tab_name):
        return "__main__" if tab_name is None else tab_name

    def seed(self, tab_name, rows):
        self.tabs[self._key(tab_name)] = [list(r) for r in rows]

    async def find_rows_by_id(self, tab_name, telegram_id):
        key = self._key(tab_name)
        if key not in self.tabs:
            return None
        target = str(telegram_id)
        return [i for i, r in enumerate(self.tabs[key], start=2) if str(r[0]) == target]

    async def append_to_sheet(self, data):
        self.tabs.setdefault("__main__", []).append(list(data))
        self.appends.append((None, list(data)))

    async def append_to_named_sheet(self, tab_name, data, headers=None):
        self.tabs.setdefault(tab_name, []).append(list(data))
        self.appends.append((tab_name, list(data)))

    async def list_worksheet_titles(self):
        return [t for t in self.tabs if t != self._key(None)]

    async def append_to_existing_named_sheet(self, tab_name, data):
        # Контракт "никогда не создаёт" (ревью 🔴): в отличие от append_to_named_sheet выше,
        # вкладка ДОЛЖНА быть заранее посеяна (store.seed) — иначе "not_found_tab", без
        # автосоздания ключа в self.tabs.
        key = self._key(tab_name)
        if key not in self.tabs:
            return "not_found_tab"
        self.tabs[key].append(list(data))
        self.appends.append((tab_name, list(data)))
        return "ok"

    async def delete_row_by_id(self, tab_name, telegram_id):
        key = self._key(tab_name)
        if key not in self.tabs:
            return "not_found_tab"
        target = str(telegram_id)
        rows = self.tabs[key]
        matches = [i for i, r in enumerate(rows) if str(r[0]) == target]
        if not matches:
            return "not_found_row"
        if len(matches) > 1:
            return "duplicate"
        del rows[matches[0]]
        self.deletes.append((tab_name, telegram_id))
        return "ok"


def _install_fake_sheets(monkeypatch):
    store = _FakeSheetStore()
    monkeypatch.setattr(sheets_mod, "find_rows_by_id", store.find_rows_by_id)
    monkeypatch.setattr(sheets_mod, "append_to_sheet", store.append_to_sheet)
    monkeypatch.setattr(sheets_mod, "append_to_named_sheet", store.append_to_named_sheet)
    monkeypatch.setattr(sheets_mod, "delete_row_by_id", store.delete_row_by_id)
    monkeypatch.setattr(sheets_mod, "list_worksheet_titles", store.list_worksheet_titles)
    monkeypatch.setattr(sheets_mod, "append_to_existing_named_sheet", store.append_to_existing_named_sheet)
    return store


async def _resolve_tabs(city, participant_type):
    from services.reg_finalize import _resolve_update_tab
    return await _resolve_update_tab(city, participant_type)


def test_move_updates_users_event_city_and_records_history(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")  # msk default registration_mode="short" too
        store.seed(old_tab, [[DELEGATE_ID, "Тест Тестов"]])
        store.seed(new_tab, [])

        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report

    report = _run(scenario())

    assert report["ok"] is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "msk"
    assert "users" in report["db_changes"]

    history = _run(db.get_answer_history(DELEGATE_ID))
    assert history, "record_answer_history(source='admin') должен оставить запись"
    assert history[0]["source"] == "admin"
    cols = {c["column"] for c in history[0]["changes"]}
    assert "event_city" in cols


def test_move_track_never_auto_switches_even_when_destination_mode_differs(tmp_path, monkeypatch):
    """Решение координатора 25.09 (отменяет прежнее авто-переключение): СПб (short) -> Москва
    с явным registration_mode=full — трек делегата ОСТАЁТСЯ short, ничего не пересчитывается.
    `preview_city_move` только сигнализирует несовместимость (`track_supported=False`)."""
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _set_registration_mode("msk", "full")
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")  # трек не меняется -> тот же маршрут, что и раньше
        store.seed(old_tab, [[DELEGATE_ID, "Тест Тестов"]])
        store.seed(new_tab, [])

        preview = await preview_city_move("short", "msk")
        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return preview, report

    preview, report = _run(scenario())

    assert preview["track_supported"] is False
    assert report["before"]["participant_type"] == "short"
    assert report["after"]["participant_type"] == "short"  # трек НЕ сменился
    user = _run(db.get_user(DELEGATE_ID))
    assert user["participant_type"] == "short"


def test_move_party_track_never_changes(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _set_registration_mode("msk", "full")
        await _seed_user(DELEGATE_ID, city="spb", participant_type="party_overnight")
        old_tab = await _resolve_tabs("spb", "party_overnight")
        new_tab = await _resolve_tabs("msk", "party_overnight")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        preview = await preview_city_move("party_overnight", "msk")
        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return preview, report

    preview, report = _run(scenario())
    assert preview["track_supported"] is True  # party не зависит от registration_mode
    assert report["before"]["participant_type"] == "party_overnight"
    assert report["after"]["participant_type"] == "party_overnight"


def test_move_status_keep_leaves_status_untouched(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short", status="approved")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        return await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())
    assert report["status_changed"] is False
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "approved"


def test_move_status_to_moderation_reverts_to_pending(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short", status="approved")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        return await move_user_city(
            DELEGATE_ID, "msk", status_mode=STATUS_MODE_TO_MODERATION, by_admin=SUPERADMIN_ID,
        )

    report = _run(scenario())
    assert report["status_changed"] is True
    assert report["after"]["status"] == "pending"
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "pending"


def test_move_status_to_moderation_is_noop_if_already_pending(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short", status="pending")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        return await move_user_city(
            DELEGATE_ID, "msk", status_mode=STATUS_MODE_TO_MODERATION, by_admin=SUPERADMIN_ID,
        )

    report = _run(scenario())
    assert report["status_changed"] is False  # уже pending -- revert_user_to_pending не звался


def test_move_dry_run_writes_nothing(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        return await move_user_city(
            DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID, dry_run=True,
        )

    report = _run(scenario())

    assert report["ok"] is True
    assert report["dry_run"] is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "spb"  # НЕ изменилось
    assert not store.appends
    assert not store.deletes
    history = _run(db.get_answer_history(DELEGATE_ID))
    assert history == []


def test_move_updates_reg_drafts_reg_started_and_unsent_queues_only(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        await db.upsert_reg_draft(DELEGATE_ID, kind="edit", event_city="spb", source="bot")
        await db.mark_reg_started(DELEGATE_ID, "seeded", event_city="spb")
        now = "2026-09-25 10:00:00"
        unsent_id = await db.enqueue_reg_digest(DELEGATE_ID, "spb", now)
        sent_id = await db.enqueue_reg_digest(DELEGATE_ID, "spb", now)
        await db.mark_reg_digest_sent([sent_id], now)
        gunsent_id = await db.enqueue_game_digest(1, DELEGATE_ID, 1, "spb", now)

        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])

        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report, unsent_id, sent_id, gunsent_id

    report, unsent_id, sent_id, gunsent_id = _run(scenario())

    assert "reg_drafts" in report["db_changes"]
    assert "reg_started" in report["db_changes"]
    assert "reg_submit_digest_queue" in report["db_changes"]
    assert "game_submit_digest_queue" in report["db_changes"]

    draft = _run(db.get_reg_draft(DELEGATE_ID))
    assert draft["event_city"] == "msk"

    unsent_rows = _run(db.list_unsent_reg_digest("msk"))
    assert any(r["id"] == unsent_id for r in unsent_rows)

    async def _sent_row():
        async with db._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT city FROM reg_submit_digest_queue WHERE id = ?", (sent_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row)

    # уже отправленная строка НЕ переписана на новый город
    sent_row = _run(_sent_row())
    assert sent_row["city"] == "spb"

    g_unsent_rows = _run(db.list_unsent_game_digest("msk"))
    assert any(r["id"] == gunsent_id for r in g_unsent_rows)


def test_move_appends_new_before_deleting_old(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)
    order = []
    orig_append = store.append_to_existing_named_sheet
    orig_delete = store.delete_row_by_id

    async def spy_append(tab_name, data):
        order.append(("append", tab_name))
        return await orig_append(tab_name, data)

    async def spy_delete(tab_name, telegram_id):
        order.append(("delete", tab_name))
        return await orig_delete(tab_name, telegram_id)

    monkeypatch.setattr(sheets_mod, "append_to_existing_named_sheet", spy_append)
    monkeypatch.setattr(sheets_mod, "delete_row_by_id", spy_delete)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report, old_tab, new_tab

    report, old_tab, new_tab = _run(scenario())
    assert report["sheet"]["moved"] is True
    assert order == [("append", new_tab), ("delete", old_tab)]


def test_move_sheet_failure_does_not_block_db_write(tmp_path, monkeypatch):
    """Старая вкладка отсутствует (`not_found_tab`) — DB-перенос всё равно применяется, отчёт
    называет проблему словами."""
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        new_tab = await _resolve_tabs("msk", "short")
        # НЕ сеем old_tab вовсе -- delete_row_by_id должен вернуть not_found_tab
        store.seed(new_tab, [])
        return await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())

    assert report["sheet"]["moved"] is False
    assert report["sheet"]["error"]
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "msk"  # БД переехала несмотря на ошибку листа
    assert "users" in report["db_changes"]


def test_move_unknown_city_refuses(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb")
        return await move_user_city(DELEGATE_ID, "nonexistent", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())
    assert report["ok"] is False
    assert report["error"]


def test_move_same_city_refuses(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb")
        return await move_user_city(DELEGATE_ID, "spb", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())
    assert report["ok"] is False


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part B2: целевая вкладка «не создаётся никогда» — РЕАЛЬНЫЙ gspread-фейк (Part A infra), не
# _FakeSheetStore: только так `add_worksheet` действительно проверяем (счётчик на
# _FakeSpreadsheet), а не гадаем, что где-то внутри try/except не проглотило исключение.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_move_writes_to_target_tab_when_it_already_exists_no_create(tmp_path, monkeypatch):
    """SEED-прецедент: spb/short/approved -> msk. Трек остаётся short (нет авто short->full,
    см. test_move_track_never_auto_switches...). Целевая вкладка трека уже есть на листе —
    пишем в неё, add_worksheet не вызывается НИ РАЗУ за весь перевод."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _disable_sheet_logs_autosync()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short", status="approved")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        fake_ss = _patch_gspread(monkeypatch, {
            old_tab: _FakeWorksheet(old_tab, [[DELEGATE_ID, "Тест Тестов"]]),
            new_tab: _FakeWorksheet(new_tab, []),
        })
        report = await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report, fake_ss, old_tab, new_tab

    report, fake_ss, old_tab, new_tab = _run(scenario())

    assert report["ok"] is True
    assert report["sheet"]["target_tab"] == new_tab
    assert report["sheet"]["target_exists"] is True
    assert report["sheet"]["write_tab"] == new_tab
    assert report["sheet"]["moved"] is True
    assert report["sheet"]["error"] is None
    assert fake_ss.add_worksheet_calls == []
    assert [r[0] for r in fake_ss._by_title[new_tab]._rows] == [DELEGATE_ID]
    assert fake_ss._by_title[old_tab]._rows == []  # старая строка удалена
    user = _run(db.get_user(DELEGATE_ID))
    assert user["participant_type"] == "short"  # трек не сменился
    assert user["event_city"] == "msk"


def test_move_falls_back_to_city_main_tab_when_track_tab_missing(tmp_path, monkeypatch):
    """msk/short -> spb: у СПб есть вкладка короткой формы, но её ЕЩЁ нет на листе — запись
    падает на главную вкладку города («СПб»), которая есть. add_worksheet не вызывается."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _disable_sheet_logs_autosync()
        await _seed_user(DELEGATE_ID, city="msk", participant_type="short", status="approved")
        old_tab = await _resolve_tabs("msk", "short")
        target_tab = await _resolve_tabs("spb", "short")
        fallback_tab = await _resolve_tabs("spb", None)
        assert target_tab != fallback_tab
        fake_ss = _patch_gspread(monkeypatch, {
            old_tab: _FakeWorksheet(old_tab, [[DELEGATE_ID, "Тест"]]),
            fallback_tab: _FakeWorksheet(fallback_tab, []),
            # target_tab НАРОЧНО отсутствует на листе
        })
        report = await move_user_city(DELEGATE_ID, "spb", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report, fake_ss, old_tab, target_tab, fallback_tab

    report, fake_ss, old_tab, target_tab, fallback_tab = _run(scenario())

    assert report["ok"] is True
    assert report["sheet"]["target_tab"] == target_tab
    assert report["sheet"]["target_exists"] is False
    assert report["sheet"]["fallback_tab"] == fallback_tab
    assert report["sheet"]["fallback_exists"] is True
    assert report["sheet"]["write_tab"] == fallback_tab
    assert report["sheet"]["moved"] is True
    assert fake_ss.add_worksheet_calls == []
    assert [r[0] for r in fake_ss._by_title[fallback_tab]._rows] == [DELEGATE_ID]
    assert target_tab not in fake_ss._by_title  # вкладка так и не появилась
    assert fake_ss._by_title[old_tab]._rows == []


def test_move_writes_nowhere_when_no_city_tab_exists_report_is_honest(tmp_path, monkeypatch):
    """msk/short -> spb, но у СПб на листе нет вообще НИ ОДНОЙ вкладки: не пишем и не удаляем
    ничего в таблице, add_worksheet не вызывается, отчёт честно называет обе отсутствующие
    вкладки, а БД всё равно переезжает (сбой листа не блокирует перевод в БД)."""
    _db_ready(tmp_path)
    _reset_sheets_state()

    async def scenario():
        await _enable_cities_module()
        await _disable_sheet_logs_autosync()
        await _seed_user(DELEGATE_ID, city="msk", participant_type="short", status="approved")
        old_tab = await _resolve_tabs("msk", "short")
        target_tab = await _resolve_tabs("spb", "short")
        fallback_tab = await _resolve_tabs("spb", None)
        fake_ss = _patch_gspread(monkeypatch, {
            old_tab: _FakeWorksheet(old_tab, [[DELEGATE_ID, "Тест"]]),
            # ни target_tab, ни fallback_tab на листе нет
        })
        report = await move_user_city(DELEGATE_ID, "spb", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)
        return report, fake_ss, old_tab, target_tab, fallback_tab

    report, fake_ss, old_tab, target_tab, fallback_tab = _run(scenario())

    assert report["ok"] is True  # перевод как таковой не отказан — только лист не обновлён
    assert report["sheet"]["write_tab"] is False
    assert report["sheet"]["moved"] is False
    assert "нет вкладки" in report["sheet"]["error"]
    assert target_tab in report["sheet"]["error"]
    assert fallback_tab in report["sheet"]["error"]
    assert fake_ss.add_worksheet_calls == []
    assert target_tab not in fake_ss._by_title
    assert fallback_tab not in fake_ss._by_title
    assert [r[0] for r in fake_ss._by_title[old_tab]._rows] == [DELEGATE_ID]  # лист не тронут
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "spb"  # БД всё равно переехала


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part C: handlers/admin_city_move.py — UI flow, права, подделанные callback_data
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.edits = []  # (text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_citymove_start_lists_other_cities_excluding_current(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb")
        cb = _FakeCallback(f"citymv_start:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_city_move.citymove_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    buttons = _cbs(kb)
    assert f"citymv_pick:{DELEGATE_ID}:msk" in buttons
    assert not any(b and b.endswith(":spb") for b in buttons if b and b.startswith("citymv_pick"))


def test_citymove_start_denied_when_old_city_out_of_scope(tmp_path):
    """Менеджер привязан к msk, делегат в spb — старый город вне зоны ответственности."""
    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb")
        cb = _FakeCallback(f"citymv_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await admin_city_move.citymove_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True  # show_alert


def test_citymove_pick_shows_track_unsupported_warning(tmp_path, monkeypatch):
    """У Москвы registration_mode=full — краткий трек делегата там не заводится, но трек НЕ
    меняется (см. services/city_move.py): экран только предупреждает словами."""
    _db_ready(tmp_path)
    _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _set_registration_mode("msk", "full")
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        cb = _FakeCallback(f"citymv_pick:{DELEGATE_ID}:msk", SUPERADMIN_ID)
        await admin_city_move.citymove_pick_city(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "нет анкеты" in text
    assert "останется с треком" in text
    buttons = _cbs(kb)
    assert f"citymv_apply:{DELEGATE_ID}:msk:{STATUS_MODE_KEEP}" in buttons
    assert f"citymv_apply:{DELEGATE_ID}:msk:{STATUS_MODE_TO_MODERATION}" in buttons


def test_citymove_pick_denies_forged_destination_out_of_scope(tmp_path):
    """Менеджер привязан к spb (свой родной город делегата), но подделывает callback_data с
    кодом msk — на который у него нет права."""
    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb")
        cb = _FakeCallback(f"citymv_pick:{DELEGATE_ID}:msk", BOUND_SPB_ID)
        await admin_city_move.citymove_pick_city(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers and cb.answers[0][1] is True


def test_citymove_apply_executes_move_and_reports_result(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        cb = _FakeCallback(f"citymv_apply:{DELEGATE_ID}:msk:{STATUS_MODE_KEEP}", SUPERADMIN_ID)
        await admin_city_move.citymove_apply(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "переведён" in text
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "msk"


def test_citymove_apply_denies_forged_old_city_out_of_scope(tmp_path, monkeypatch):
    """TOCTOU: право перепроверяется ЗАНОВО на шаге применения, не только на шаге выбора."""
    _db_ready(tmp_path)
    _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _setup_bound_staff()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        cb = _FakeCallback(f"citymv_apply:{DELEGATE_ID}:msk:{STATUS_MODE_KEEP}", BOUND_MSK_ID)
        await admin_city_move.citymove_apply(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "spb"  # ничего не применилось


def test_citymove_cancel_changes_nothing(tmp_path):
    _db_ready(tmp_path)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb")
        cb = _FakeCallback(f"citymv_cancel:{DELEGATE_ID}", SUPERADMIN_ID)
        await admin_city_move.citymove_cancel(cb)
        return cb

    cb = _run(scenario())
    assert "отменён" in cb.message.edits[0][0]
    user = _run(db.get_user(DELEGATE_ID))
    assert user["event_city"] == "spb"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Part D: сканер чек-ина — «чужой город» читает event_city вживую (координатор)
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_checkin_wrong_city_gate_follows_the_move(tmp_path, monkeypatch):
    """После перевода СПб -> Москва делегат допускается на сессию в Москве и НЕ допускается
    на сессию в СПб — `services.checkin.record_arrival` сравнивает `users.event_city`
    (`services/checkin.py::checkin_denial`/сессионная проверка), эта функция не правится, а
    только вызывается."""
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short", status="approved")

        msk_sid = await db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие")
        spb_sid = await db.create_program_session("spb", "2026-10-30", "10:00", "11:00", "Открытие")

        user = await db.get_user(DELEGATE_ID)
        before_msk = await record_arrival(
            user, f"session:{msk_sid}", source="miniapp", scanned_at="2026-10-30 10:05:00",
        )

        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

        user_after = await db.get_user(DELEGATE_ID)
        after_msk = await record_arrival(
            user_after, f"session:{msk_sid}", source="miniapp", scanned_at="2026-10-30 10:06:00",
        )
        after_spb = await record_arrival(
            user_after, f"session:{spb_sid}", source="miniapp", scanned_at="2026-10-30 10:07:00",
        )
        return before_msk, after_msk, after_spb

    before_msk, after_msk, after_spb = _run(scenario())

    assert before_msk["status"] == "wrong_city"  # до перевода СПб-делегата в Москву не пускали
    assert after_msk["status"] == "new"  # после перевода — пускают
    assert after_spb["status"] == "wrong_city"  # и больше не пускают на сессию старого города


# ── Старая и новая вкладка совпали (у обоих городов нет своих вкладок) — обновить на месте ──

def test_move_same_tab_updates_row_in_place_without_duplicate(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)
    updates = []

    async def fake_update_row_by_id(tab_name, telegram_id, row):
        rows = store.tabs[store._key(tab_name)]
        for i, r in enumerate(rows):
            if str(r[0]) == str(telegram_id):
                rows[i] = list(row)
                updates.append((tab_name, telegram_id))
                return True
        return False

    monkeypatch.setattr(sheets_mod, "update_row_by_id", fake_update_row_by_id)

    async def main_tab_only(city, participant_type):
        return None

    async def targets_main(new_city, participant_type):
        return {"target_tab": None, "target_exists": True, "fallback_tab": None,
                "fallback_exists": True, "write_tab": None}

    import services.city_move as cm
    import services.reg_finalize as rf
    monkeypatch.setattr(rf, "_resolve_update_tab", main_tab_only)
    monkeypatch.setattr(cm, "_resolve_sheet_targets", targets_main)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        store.seed(None, [[DELEGATE_ID, "Тест Тестов"]])
        return await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())

    assert report["sheet"]["moved"] is True
    assert report["sheet"]["error"] is None
    assert updates == [(None, DELEGATE_ID)]
    assert store.appends == [] and store.deletes == []
    assert [r[0] for r in store.tabs["__main__"]] == [DELEGATE_ID]  # ровно одна строка


def test_sheet_error_text_has_no_internal_codes(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    store = _install_fake_sheets(monkeypatch)

    async def failing_delete(tab_name, telegram_id):
        return "error"

    monkeypatch.setattr(sheets_mod, "delete_row_by_id", failing_delete)

    async def scenario():
        await _enable_cities_module()
        await _seed_user(DELEGATE_ID, city="spb", participant_type="short")
        old_tab = await _resolve_tabs("spb", "short")
        new_tab = await _resolve_tabs("msk", "short")
        store.seed(old_tab, [[DELEGATE_ID, "Тест"]])
        store.seed(new_tab, [])
        return await move_user_city(DELEGATE_ID, "msk", status_mode=STATUS_MODE_KEEP, by_admin=SUPERADMIN_ID)

    report = _run(scenario())
    err = report["sheet"]["error"] or ""
    assert "таблица недоступна" in err
    assert "'error'" not in err and "код" not in err
