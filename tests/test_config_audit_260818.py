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

    misc_keys = admin_mod._settings_group_keys("misc")
    assert "proxy_recheck_seconds" not in misc_keys
    assert "proxy_connect_timeout" not in misc_keys
