"""Phase 5 Plan 6 (Party Sheet Routing) tests.

pytest-asyncio is unavailable in this env, so async helpers are driven via asyncio.run()
and config.DB_PATH/config.GOOGLE_SHEET_ID are pointed at test-safe values — same convention
as tests/test_db_phase5.py / tests/test_registration_phase5.py.
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg
import services.sheets as sheets


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


def _clear_sheet_id(monkeypatch):
    """Credential-absent path: the primary testable path per the plan's <behavior> bullets."""
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")


# ── Task 1: append_to_named_sheet / ensure_named_sheet_header fail-soft when unconfigured ──

def test_append_to_named_sheet_noop_when_sheet_id_unset(monkeypatch):
    _clear_sheet_id(monkeypatch)

    async def go():
        await sheets.append_to_named_sheet("Party", [123, "Test"])

    asyncio.run(go())  # must not raise


def test_ensure_named_sheet_header_noop_when_sheet_id_unset(monkeypatch):
    _clear_sheet_id(monkeypatch)

    async def go():
        await sheets.ensure_named_sheet_header("Party", ["A", "B"])

    asyncio.run(go())  # must not raise


def test_append_to_named_sheet_swallows_raising_sync_call(monkeypatch):
    """A raising sync append must never propagate out of the async wrapper (fail-soft, D-11/D-12
    threat T-05-06-02). Credentials are present so the sync path is actually exercised; the
    retry loop's sleeps are patched to no-ops so the test stays fast."""
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")

    def boom(tab_name, data):
        raise RuntimeError("simulated gspread failure")

    monkeypatch.setattr(sheets, "_append_to_named_sheet_sync", boom)

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(sheets.asyncio, "sleep", fast_sleep)

    async def go():
        await sheets.append_to_named_sheet("Party", [123, "Test"])

    asyncio.run(go())  # must not raise despite every retry attempt failing


# ── Task 1: _get_named_sheet cache — same tab returns same object, different tabs distinct ──

class _FakeWorksheet:
    def __init__(self, title):
        self.title = title


class _FakeSpreadsheet:
    def __init__(self):
        self._sheets = {}
        self.opened = []

    def worksheet(self, title):
        if title not in self._sheets:
            import gspread
            raise gspread.WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self._sheets[title] = ws
        return ws


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread_client(monkeypatch):
    fake_ss = _FakeSpreadsheet()
    monkeypatch.setattr(sheets.gspread, "service_account", lambda filename: _FakeClient(fake_ss))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "fake-id")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "fake-creds.json")
    return fake_ss


def test_get_named_sheet_returns_same_object_for_repeated_calls(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    first = sheets._get_named_sheet("Party")
    second = sheets._get_named_sheet("Party")
    assert first is second


def test_get_named_sheet_distinct_entries_for_different_tabs(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    party = sheets._get_named_sheet("Party")
    other = sheets._get_named_sheet("Отобранные")
    assert party is not other
    assert party.title == "Party"
    assert other.title == "Отобранные"


def test_reset_named_sheet_cache_removes_only_named_entry(monkeypatch):
    sheets._named_sheets.clear()
    _patch_gspread_client(monkeypatch)

    sheets._get_named_sheet("Party")
    sheets._get_named_sheet("Отобранные")
    assert "Party" in sheets._named_sheets
    assert "Отобранные" in sheets._named_sheets

    sheets._reset_named_sheet_cache("Party")

    assert "Party" not in sheets._named_sheets
    assert "Отобранные" in sheets._named_sheets


# ── Task 2: PARTY_SHEET_COLUMNS / party_sheet_headers / party_sheet_row (D-11, D-12) ────────

_UNGATED_HEADERS = {"ID Telegram", "Username", "Дата регистрации", "Статус", "ФИО", "Трек"}


def _base_party_data(participant_type="party_overnight"):
    return {
        "telegram_id": 42,
        "username": "@tester",
        "registration_date": "2026-07-21 10:00:00",
        "status": "pending",
        "full_name": "Test User",
        "participant_type": participant_type,
        "phone": "+70000000000",
        "vk_username": "vk.com/tester",
        "allergies": "-",
        "food_pref": "-",
        "housing": "-",
        "bed_sharing": "-",
        "bed_partner": "-",
    }


def test_party_sheet_columns_no_university_course_resume():
    headers = [h for h, _g, _f in reg.PARTY_SHEET_COLUMNS]
    assert "Трек" in headers
    assert not any(("ВУЗ" in h or "Резюме" in h or "Курс" in h) for h in headers)


def test_party_sheet_headers_default_includes_ungated_columns(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg.party_sheet_headers()

    headers = asyncio.run(go())
    assert _UNGATED_HEADERS.issubset(set(headers))


def test_party_sheet_headers_phone_off_removes_exactly_one_header(tmp_path):
    """reg_q_phone defaults to global "off" (REG_DEFAULTS), so the __party override must first
    be turned ON (explicit inherit -> on) before turning it back off proves the toggle removes
    exactly the "Телефон" header and nothing else."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_phone__party", "on")
        before = await reg.party_sheet_headers()
        await db.set_setting("reg_q_phone__party", "off")
        after = await reg.party_sheet_headers()
        return before, after

    before, after = asyncio.run(go())
    assert "Телефон" in before
    assert "Телефон" not in after
    removed = set(before) - set(after)
    assert removed == {"Телефон"}
    # everything else is unchanged
    assert (set(before) - removed) == set(after)


def test_party_sheet_row_length_matches_headers_multiple_configs(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        results = []
        headers1 = await reg.party_sheet_headers()
        row1 = await reg.party_sheet_row(_base_party_data())
        results.append((len(headers1), len(row1)))

        await db.set_setting("reg_q_phone__party", "off")
        headers2 = await reg.party_sheet_headers()
        row2 = await reg.party_sheet_row(_base_party_data())
        results.append((len(headers2), len(row2)))

        await db.set_setting("reg_q_vk__party", "off")
        headers3 = await reg.party_sheet_headers()
        row3 = await reg.party_sheet_row(_base_party_data("party_noovernight"))
        results.append((len(headers3), len(row3)))
        return results

    results = asyncio.run(go())
    for h_len, r_len in results:
        assert h_len == r_len


def test_party_sheet_row_track_cell_overnight(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        headers = await reg.party_sheet_headers()
        row = await reg.party_sheet_row(_base_party_data("party_overnight"))
        return dict(zip(headers, row))

    values = asyncio.run(go())
    assert values["Трек"] == "С ночёвкой"


def test_party_sheet_row_track_cell_noovernight(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        headers = await reg.party_sheet_headers()
        row = await reg.party_sheet_row(_base_party_data("party_noovernight"))
        return dict(zip(headers, row))

    values = asyncio.run(go())
    assert values["Трек"] == "Без ночёвки"


def test_party_sheet_row_neutralizes_formula_injection(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        headers = await reg.party_sheet_headers()
        data = _base_party_data()
        data["full_name"] = "=cmd()"
        row = await reg.party_sheet_row(data)
        return dict(zip(headers, row))

    values = asyncio.run(go())
    assert values["ФИО"] == "'=cmd()"


# ── Task 2: append_to_party_sheet resolves tab from party_sheet_tab / default ───────────────

def test_append_to_party_sheet_uses_default_tab_when_unset(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    captured = {}

    async def fake_append(tab_name, data):
        captured["tab"] = tab_name
        captured["data"] = data

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_append)

    async def go():
        await db.init_db()
        await reg.append_to_party_sheet([1, 2, 3])

    asyncio.run(go())
    assert captured["tab"] == reg.PARTY_SHEET_TAB_DEFAULT


def test_append_to_party_sheet_uses_configured_tab(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    captured = {}

    async def fake_append(tab_name, data):
        captured["tab"] = tab_name

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_append)

    async def go():
        await db.init_db()
        await db.set_setting("party_sheet_tab", "MyPartyTab")
        await reg.append_to_party_sheet([1, 2, 3])

    asyncio.run(go())
    assert captured["tab"] == "MyPartyTab"


# ── Task 2: D-12 exclusivity — finalize_registration schedules exactly ONE append ───────────

class _FakeFromUser:
    def __init__(self, telegram_id, username="tester"):
        self.id = telegram_id
        self.username = username


class _FakeMessage:
    def __init__(self, telegram_id):
        self.from_user = _FakeFromUser(telegram_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _FakeState:
    def __init__(self, data):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        pass


async def _run_finalize_and_drain(message, state, bot):
    """finalize_registration schedules its sheet append via asyncio.create_task
    (fire-and-forget) — drain any tasks it spawned so the fake append callback has run
    before the test asserts on it."""
    await reg.finalize_registration(message, state, bot)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def test_finalize_registration_party_track_schedules_party_append_only(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    party_calls = []
    full_calls = []

    async def fake_party_append(data):
        party_calls.append(data)

    async def fake_full_append(data):
        full_calls.append(data)

    monkeypatch.setattr(reg, "append_to_party_sheet", fake_party_append)
    monkeypatch.setattr(reg, "append_to_sheet", fake_full_append)

    message = _FakeMessage(555)
    state = _FakeState({"full_name": "Party Guest", "participant_type": "party_overnight"})

    async def go():
        await db.init_db()
        await db.set_setting("party_approval", "manual")  # stays pending, no approve_user call
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())

    assert len(party_calls) == 1
    assert len(full_calls) == 0


def test_finalize_registration_full_track_schedules_main_append_only(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    party_calls = []
    full_calls = []

    async def fake_party_append(data):
        party_calls.append(data)

    async def fake_full_append(data):
        full_calls.append(data)

    monkeypatch.setattr(reg, "append_to_party_sheet", fake_party_append)
    monkeypatch.setattr(reg, "append_to_sheet", fake_full_append)

    message = _FakeMessage(556)
    state = _FakeState({"full_name": "Full Delegate", "participant_type": "full"})

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")  # stays pending, no approve_user call
        await _run_finalize_and_drain(message, state, bot=None)

    asyncio.run(go())

    assert len(full_calls) == 1
    assert len(party_calls) == 0
