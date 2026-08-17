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
