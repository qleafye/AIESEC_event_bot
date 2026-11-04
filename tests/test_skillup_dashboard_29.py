"""Срезы дашборда: IT от трёх лет и желание в кейс. Тумблеры выключены, ПД нет."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from dashboard.queries import Scope, _SETTING_DEFAULTS
from dashboard.skillup_block import case_block, it_block
from domain.settings.schema import SETTINGS_SCHEMA

_ROOT = Path(__file__).resolve().parents[1]
_PII = ("full_name", "phone", "email", "vk_username", "resume")


def _conn(*, it=True, case=True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
    cols = ["season TEXT", "event_city TEXT", "study_field TEXT", "stack TEXT"]
    if it:
        cols.append("is_it_3plus INTEGER")
    if case:
        cols.append("case_optin TEXT")
    conn.execute(f"CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, {', '.join(cols)})")
    return conn


def _on(conn, key: str) -> None:
    conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, 'on')", (key,))


def _add(conn, tid, **fields) -> None:
    names = ", ".join(["telegram_id", *fields])
    marks = ", ".join("?" for _ in range(1 + len(fields)))
    conn.execute(
        f"INSERT INTO users ({names}) VALUES ({marks})",
        (tid, *fields.values()),
    )


def test_toggles_default_off():
    for key in ("dashboard_block_it3plus", "dashboard_block_case_optin"):
        entry = SETTINGS_SCHEMA[key]
        assert entry["default"] == "off"
        assert entry["group"] == "dashboard"
        assert _SETTING_DEFAULTS[key] == "off"


def test_off_returns_none():
    conn = _conn()
    _add(conn, 1, is_it_3plus=1, case_optin="Да", study_field="IT", stack="Python")
    assert it_block(conn, Scope()) is None
    assert case_block(conn, Scope()) is None

    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_ROOT / "dashboard" / "templates")))
    env.filters["thousands"] = lambda value: value
    html = env.get_template("dashboard.html").render(
        page_sections=[],
        section_ids={"sources"},
        season_options=[],
        scope=Scope(),
        viewer={},
        kpi={},
        show_city_switcher=False,
        utm=None,
        source_cut=None,
        referrals=None,
        event_name="Тест",
    )
    assert "В IT от 3 лет" not in html
    assert "Хотят в кейс" not in html


def test_it_count_and_percent():
    conn = _conn()
    _on(conn, "dashboard_block_it3plus")
    _add(conn, 1, is_it_3plus=1)
    _add(conn, 2, is_it_3plus=0)
    block = it_block(conn, Scope())
    assert block["count"] == 1
    assert block["total"] == 2
    assert block["percent"] == 50

    quiet = _conn()
    _on(quiet, "dashboard_block_it3plus")
    _add(quiet, 1, is_it_3plus=0)
    _add(quiet, 2, is_it_3plus=None)
    assert it_block(quiet, Scope())["count"] == 0


def test_case_optin_is_yes_label():
    conn = _conn()
    _on(conn, "dashboard_block_case_optin")
    _add(conn, 1, case_optin="Да", study_field="IT", stack="Go")
    _add(conn, 2, case_optin="1", study_field="IT", stack="Go")
    _add(conn, 3, case_optin="да", study_field="IT", stack="Go")
    block = case_block(conn, Scope())
    assert block["count"] == 1
    assert block["total"] == 3


def test_breakdown_empty_is_named():
    conn = _conn()
    _on(conn, "dashboard_block_case_optin")
    _add(conn, 1, case_optin="Да", study_field="", stack="")
    _add(conn, 2, case_optin="Да", study_field=None, stack=None)
    block = case_block(conn, Scope())
    assert block["fields"][0]["label"] == "не указано"
    assert block["fields"][0]["count"] == 2
    assert block["stack"][0]["label"] == "не указано"
    assert block["stack"][0]["count"] == 2


def test_stack_token_counted_twice():
    conn = _conn()
    _on(conn, "dashboard_block_case_optin")
    _add(conn, 1, case_optin="Да", study_field="IT", stack="Python, SQL")
    _add(conn, 2, case_optin="Нет", study_field="IT", stack="Python")
    _add(conn, 3, case_optin="Да", study_field="", stack="")
    block = case_block(conn, Scope())
    stacks = {row["label"]: row for row in block["stack"]}
    assert stacks["Python"]["count"] == 1
    assert stacks["SQL"]["count"] == 1
    assert stacks["Python"]["pct"] == 50
    assert stacks["не указано"]["count"] == 1


def test_no_second_referral_top():
    src = (_ROOT / "dashboard" / "skillup_block.py").read_text(encoding="utf-8")
    assert "referral_top" not in src
    html = (_ROOT / "dashboard" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert html.count("Топ пригласивших") == 1


def test_skillup_module_selects_no_pii():
    src = (_ROOT / "dashboard" / "skillup_block.py").read_text(encoding="utf-8")
    assert not [token for token in _PII if token in src]
    assert "domain.settings" not in src
    assert "aiogram" not in src


def test_missing_column_is_quiet():
    no_it = _conn(it=False)
    _on(no_it, "dashboard_block_it3plus")
    _add(no_it, 1, case_optin="Да", study_field="IT", stack="Go")
    assert it_block(no_it, Scope()) is None

    no_case = _conn(case=False)
    _on(no_case, "dashboard_block_case_optin")
    _add(no_case, 1, is_it_3plus=1, study_field="IT", stack="Go")
    assert case_block(no_case, Scope()) is None
