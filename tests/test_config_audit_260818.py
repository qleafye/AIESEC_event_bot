"""Phase 14 Plan 02 (CFG-01, CFG-02) — «.env только секреты/bootstrap» audit.

Task 1: GOOGLE_SHEET_TAB -> разовый seed в bot_settings.main_sheet_tab (main.py).
Task 2: PROXY_RECHECK_SECONDS/PROXY_CONNECT_TIMEOUT -> реестр, группа «🔧 Система».
Task 3: подписи треков в пикере фильтра рассылки (IN-01) + регресс `_csv_safe` на основном
листе (уже реализовано коммитом 8a9e889, tests/test_block7_low.py — здесь только паритетная
проверка, чтобы её нельзя было потерять молча).

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через asyncio.run(),
config.DB_PATH указывает на файл в tmp_path, по конвенции соседних файлов
(tests/test_game_archive_260818.py и др.).
"""
import asyncio

from config import config
from database import db
import settings_schema


def _db_ready(tmp_path, name="test_config_audit_260818.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── Task 1: GOOGLE_SHEET_TAB one-time seed ───────────────────────────────────────────────────

def test_seed_main_sheet_tab_from_env_writes_when_empty(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    import main

    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "TG CANDIDATES")

    result = asyncio.run(main.seed_main_sheet_tab_from_env())

    assert result is True
    assert asyncio.run(db.get_setting("main_sheet_tab")) == "TG CANDIDATES"


def test_seed_main_sheet_tab_from_env_does_not_overwrite_manager_choice(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    import main

    asyncio.run(db.set_setting("main_sheet_tab", "МСК"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", "TG CANDIDATES")

    result = asyncio.run(main.seed_main_sheet_tab_from_env())

    assert result is False
    assert asyncio.run(db.get_setting("main_sheet_tab")) == "МСК"


def test_seed_main_sheet_tab_from_env_empty_or_quoted_env_is_noop(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    import main

    for raw in ("", "   ", '""', "''"):
        monkeypatch.setattr(config, "GOOGLE_SHEET_TAB", raw)
        result = asyncio.run(main.seed_main_sheet_tab_from_env())
        assert result is False
    assert asyncio.run(db.get_setting("main_sheet_tab")) is None


# ── Task 2: proxy timings -> registry, group «🔧 Система» ────────────────────────────────────

def test_proxy_settings_schema_entries():
    assert settings_schema.SETTINGS_SCHEMA["proxy_recheck_seconds"]["type"] == "int"
    assert settings_schema.SETTINGS_SCHEMA["proxy_recheck_seconds"]["group"] == "system"
    assert settings_schema.SETTINGS_SCHEMA["proxy_recheck_seconds"]["default"] == 600
    assert settings_schema.SETTINGS_SCHEMA["proxy_connect_timeout"]["type"] == "int"
    assert settings_schema.SETTINGS_SCHEMA["proxy_connect_timeout"]["group"] == "system"
    assert settings_schema.SETTINGS_SCHEMA["proxy_connect_timeout"]["default"] == 5


def test_proxy_settings_prompts_mention_restart():
    assert "перезапуск" in settings_schema.SETTINGS_SCHEMA["proxy_recheck_seconds"]["prompt"].lower()
    assert "перезапуск" in settings_schema.SETTINGS_SCHEMA["proxy_connect_timeout"]["prompt"].lower()


def test_proxy_settings_wired_into_admin_system_group():
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here

    keys = {k for k, _, _ in admin_mod.SETTINGS_FIELDS}
    assert "proxy_recheck_seconds" in keys
    assert "proxy_connect_timeout" in keys
    system_keys = admin_mod._settings_group_keys("system")
    assert "proxy_recheck_seconds" in system_keys
    assert "proxy_connect_timeout" in system_keys
    assert admin_mod._settings_group_label("system") == "🔧 Система"


def test_seed_proxy_settings_from_env_writes_once_and_respects_existing_row(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    import main

    monkeypatch.setattr(config, "PROXY_RECHECK_SECONDS", 900)
    monkeypatch.setattr(config, "PROXY_CONNECT_TIMEOUT", 10)

    asyncio.run(main.seed_proxy_settings_from_env())
    assert asyncio.run(db.get_setting("proxy_recheck_seconds")) == "900"
    assert asyncio.run(db.get_setting("proxy_connect_timeout")) == "10"

    # Manager already edited one of the two from the bot — a second seed pass must not touch it.
    asyncio.run(db.set_setting("proxy_recheck_seconds", "1200"))
    asyncio.run(main.seed_proxy_settings_from_env())
    assert asyncio.run(db.get_setting("proxy_recheck_seconds")) == "1200"


def test_seed_proxy_settings_from_env_noop_when_env_matches_default(tmp_path):
    _db_ready(tmp_path, name="test_config_audit_260818_default.db")
    import main

    # config module-level defaults are already the registry defaults (600 / 5) unless a
    # previous test's monkeypatch leaked — reset explicitly so this test is self-contained.
    asyncio.run(main.seed_proxy_settings_from_env())
    assert asyncio.run(db.get_setting("proxy_recheck_seconds")) is None
    assert asyncio.run(db.get_setting("proxy_connect_timeout")) is None


def test_settings_group_misc_does_not_swallow_system_keys():
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here

    misc_keys = admin_mod._settings_group_keys("misc")
    assert "proxy_recheck_seconds" not in misc_keys
    assert "proxy_connect_timeout" not in misc_keys


# ── Task 3: track labels in the broadcast «Трек» picker (IN-01) ──────────────────────────────

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeState:
    def __init__(self, **data):
        self._data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)
        return dict(self._data)

    async def set_state(self, state):
        self.state = state


def _btn_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _btn_datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_track_labels_cover_every_track_and_are_not_the_raw_code():
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here

    for code in ("full", "party_overnight", "party_noovernight", "short"):
        assert code in admin_broadcasts._TRACK_LABELS
        assert admin_broadcasts._TRACK_LABELS[code] != code


def test_show_value_picker_participant_type_shows_labels_codes_in_callback(tmp_path):
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here

    _db_ready(tmp_path, name="test_config_audit_260818_track.db")
    asyncio.run(db.add_user({
        "telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01",
        "participant_type": "full",
    }))
    asyncio.run(db.add_user({
        "telegram_id": 2, "full_name": "B", "registration_date": "2026-01-01",
        "participant_type": "party_noovernight",
    }))

    cb = _FakeCallback("filter_f_participant_type")
    state = _FakeState()
    asyncio.run(admin_broadcasts._show_value_picker(cb, state, "participant_type", "Выберите:"))

    texts = _btn_texts(cb.message.markup)
    datas = _btn_datas(cb.message.markup)
    # Human labels on screen...
    assert any("Вечеринка без ночёвки" in t for t in texts)
    assert "party_noovernight" not in texts
    # ...but the VALUE buttons' callback_data still carry an INDEX (not the raw code, not the
    # label either — same index-based contract as every other picker field, see
    # _value_picker_kb's docstring). Nav/back buttons ("filter_back") are excluded from this
    # check — they are not value buttons.
    value_button_datas = [d for d in datas if d.startswith("filter_opt:")]
    assert value_button_datas, datas
    data = asyncio.run(state.get_data())
    assert set(data["filter_options"]) == {"full", "party_noovernight"}


def test_show_value_picker_participant_type_unknown_value_is_fail_soft(tmp_path):
    from handlers import admin as admin_mod
    from handlers import admin_broadcasts  # Phase 13 (13-05): broadcast handlers moved here

    _db_ready(tmp_path, name="test_config_audit_260818_track_unknown.db")
    asyncio.run(db.add_user({
        "telegram_id": 1, "full_name": "A", "registration_date": "2026-01-01",
        "participant_type": "some_future_track",
    }))

    cb = _FakeCallback("filter_f_participant_type")
    state = _FakeState()
    # Must not raise — an unrecognised code falls back to itself as the label.
    asyncio.run(admin_broadcasts._show_value_picker(cb, state, "participant_type", "Выберите:"))
    texts = _btn_texts(cb.message.markup)
    assert "some_future_track" in texts


# ── Task 3 regression: _csv_safe parity on the main sheet (already shipped in 8a9e889;
# tests/test_block7_low.py::test_main_tab_active_sheet_row_neutralizes_formula covers the
# `=HYPERLINK` case — this test locks down the other three CSV-injection prefixes so the
# parity with the party sheet (which _csv_safe protects identically) cannot regress silently.

def test_main_tab_active_sheet_row_neutralizes_all_injection_prefixes(tmp_path):
    from handlers import registration as reg

    _db_ready(tmp_path, name="test_config_audit_260818_csv.db")

    async def go():
        # phone/comments are OFF by default (settings_schema.py); flip them on so their cells
        # actually land in the projected row for this test.
        await db.set_setting("reg_q_phone", "on")
        await db.set_setting("reg_q_comments", "on")
        return await reg.active_sheet_row({
            "telegram_id": 1,
            "full_name": "=SUM(1)",
            "comments": "+1",
            "expectations": "-1",
            "phone": "@x",
            "registration_date": "2026-07-01 10:00:00",
        })

    row = asyncio.run(go())
    raw_cells = {"=SUM(1)", "+1", "-1", "@x"}
    for cell in row:
        if isinstance(cell, str):
            assert cell not in raw_cells, f"unsafe raw cell leaked into the sheet row: {cell!r}"
    safe_cells = [c for c in row if isinstance(c, str) and c[1:] in raw_cells]
    assert len(safe_cells) == 4, row
    assert all(c.startswith("'") for c in safe_cells)
