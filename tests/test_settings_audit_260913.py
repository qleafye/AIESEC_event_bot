"""Квик 260913-16o (задача 1): сторож воронки `settings_audit.py`.

Инцидент прода 06.09: `full_approval` переключили в «авто», 38 заявок одобрились молча, и
установить автора было нечем — `database.db.set_setting` логирует только ключ и значение.
73 из 75 прямых вызовов `set_setting`/`delete_setting` в `handlers/*.py` переведены на воронку
`settings_audit.set_setting_by_admin` / `delete_setting_by_admin`, которая пишет `admin=<id>`
ПЕРЕД настоящей записью.

Два теста здесь:
(а) обходит `handlers/*.py` и убеждается, что прямых вызовов `set_setting(`/`delete_setting(`
    (не `_by_admin`, не `db.`) не осталось нигде, КРОМЕ ровно двух документированных
    исключений (allowlist — не вызовы из-под пользователя, автора взять неоткуда);
(б)/(в) сама воронка действительно пишет `admin=<id>` в лог и реально меняет `bot_settings`.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — асинхронные
хелперы гоняются через asyncio.run(), config.DB_PATH указывает на файл в tmp_path (тот же
приём, что у tests/test_settings_consumers_phase6.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from config import config
from database import db

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "handlers"

# Ровно два осознанных исключения (см. docstring settings_audit.py и 260913-16o-PLAN.md,
# задача 1): вызовы не из-под пользователя-админа, автора взять неоткуда.
ALLOWLIST: dict[str, str] = {
    "admin_gamification.py": (
        "rebuild_game_sheets_detailed(): технический штамп game_sheet_last_synced_at, "
        "не правка настройки менеджером"
    ),
    "reg_schema.py": (
        "set_sheet_schema(): зовётся из main.py при старте бота (не из-под callback/message "
        "админа), см. main.py"
    ),
}

# Прямой вызов `set_setting(`/`delete_setting(` — не `set_setting_by_admin(`/
# `delete_setting_by_admin(` (между именем функции и открывающей скобкой должен идти
# ровно "(", а не "_by_admin(") и не `db.set_setting(` (внутренний вызов самой воронки,
# которого в handlers/ быть не должно, но паттерн его тоже не спутает — префикс "db." в
# handlers/ не встречается для этих двух функций).
_CALL_RE = re.compile(r"\b(?:set_setting|delete_setting)\(")


def _scan_violations() -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for path in sorted(HANDLERS_DIR.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [ln.strip() for ln in lines if _CALL_RE.search(ln)]
        if hits:
            violations[path.name] = hits
    return violations


def test_no_raw_set_setting_calls_outside_allowlist():
    violations = _scan_violations()
    assert set(violations.keys()) == set(ALLOWLIST.keys()), (
        "незапланированные прямые вызовы set_setting()/delete_setting() в handlers/ "
        f"(воронку settings_audit.py обойти нельзя): {violations}"
    )
    for file, hits in violations.items():
        assert len(hits) == 1, (
            f"{file}: ожидался ровно один документированный прямой вызов, найдено "
            f"{len(hits)}: {hits}"
        )


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_settings_audit_260913.db")
    asyncio.run(db.init_db())


def test_set_setting_by_admin_logs_author_and_persists(tmp_path, caplog):
    _db_ready(tmp_path)
    from settings_audit import set_setting_by_admin

    with caplog.at_level(logging.INFO, logger="settings_audit"):
        asyncio.run(set_setting_by_admin(777, "test_key", "on"))

    assert any("admin=777" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    assert asyncio.run(db.get_setting("test_key")) == "on"


def test_delete_setting_by_admin_logs_author_and_removes(tmp_path, caplog):
    _db_ready(tmp_path)
    from settings_audit import delete_setting_by_admin, set_setting_by_admin

    asyncio.run(set_setting_by_admin(777, "test_key", "on"))
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="settings_audit"):
        asyncio.run(delete_setting_by_admin(777, "test_key"))

    assert any("admin=777" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    assert asyncio.run(db.get_setting("test_key")) is None


def test_admin_id_none_is_allowed_and_logged_as_is(tmp_path, caplog):
    """Пять хелперов без объекта пользователя в кадре (admin_settings_lists._write_items и
    соседи) прокидывают `admin_id=None`, если вызывающий не смог его определить — воронка
    не должна падать, строка лога всё равно отличима префиксом `admin=`."""
    _db_ready(tmp_path)
    from settings_audit import set_setting_by_admin

    with caplog.at_level(logging.INFO, logger="settings_audit"):
        asyncio.run(set_setting_by_admin(None, "test_key", "on"))

    assert any("admin=None" in r.getMessage() for r in caplog.records)
    assert asyncio.run(db.get_setting("test_key")) == "on"
