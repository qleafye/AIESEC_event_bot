"""Обобщённые ступени амбассадоров 1–5: карта ключей, конфигурация, проверка порогов,
заморозка прежних дефолтов для стека СкиллАп и пресет.

pytest-asyncio в окружении нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

import reg_presets
from amb_tier_keys import MAX_TIERS, _LEGACY_TIER_KEYS, tier_key
from config import config
from database import amb_tiers_db
from database import db
from services import amb_tiers
from settings_schema import SETTINGS_SCHEMA
from settings_validation import amb_threshold_order_error, validate_setting_value
from tests._dbtpl import fast_init_db

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_generic_34.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


# ── tier_key ────────────────────────────────────────────────────────────────────────────────

def test_tier_key_legacy_map():
    assert tier_key(2, "text") == "amb_tier2_granted_text"
    assert tier_key(2, "quota") == "amb_o2o_quota"
    assert tier_key(2, "waitlist") == "amb_tier2_waitlist_text"
    assert tier_key(2, "next") == "amb_next_step_o2o_text"
    assert tier_key(3, "next") == "amb_next_step_networking_text"
    assert tier_key(4, "text") == "amb_tier4_text"
    assert tier_key(1, "threshold") == "amb_tier1_threshold"
    assert tier_key(5, "quota_on") == "amb_tier5_quota_on"


@pytest.mark.parametrize("n", [0, 6, -1, True])
def test_tier_key_out_of_range(n):
    with pytest.raises(ValueError):
        tier_key(n, "text")


def test_tier_key_unknown_kind():
    with pytest.raises(ValueError):
        tier_key(1, "banana")


def test_every_tier_key_is_in_registry():
    for n in range(1, MAX_TIERS + 1):
        for kind in ("threshold", "text", "quota_on", "waitlist", "next"):
            assert tier_key(n, kind) in SETTINGS_SCHEMA, (n, kind)
        if (n, "quota") in _LEGACY_TIER_KEYS or n != 2:
            assert tier_key(n, "quota") in SETTINGS_SCHEMA, n


def test_keys_module_has_no_project_imports():
    source = (ROOT / "amb_tier_keys.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+(\S+)", source, re.M)
    assert set(imports) <= {"__future__"}, imports


# ── реестр ──────────────────────────────────────────────────────────────────────────────────

def test_neutral_defaults():
    assert SETTINGS_SCHEMA["amb_count_deadline"]["default"] == ""
    for n in range(1, MAX_TIERS + 1):
        for kind in ("text", "waitlist", "next"):
            key = tier_key(n, kind)
            if key not in SETTINGS_SCHEMA:
                continue
            default = SETTINGS_SCHEMA[key]["default"]
            assert "СкиллАп" not in default and "SkillUp" not in default, key
            assert "резюме" not in default and "нетворкинг" not in default, key
            assert not re.search(r"\d{4}|ноября", default), key
    assert SETTINGS_SCHEMA["amb_tiers_count"]["default"] == 3
    assert SETTINGS_SCHEMA["amb_tiers_require_approved"]["default"] == "on"
    assert SETTINGS_SCHEMA["amb_tier1_next_label"]["default"] == ""


def test_quota_and_team_slots_are_different_keys():
    quota = SETTINGS_SCHEMA["amb_tier3_quota"]
    slots = SETTINGS_SCHEMA["amb_slots_limit"]
    assert quota["label"] != slots["label"]
    assert "Не путать" in quota["prompt"]


def test_tiers_count_validator_bounds():
    assert validate_setting_value("amb_tiers_count", "5") == ("5", None)
    assert validate_setting_value("amb_tiers_count", "1") == ("1", None)
    value, error = validate_setting_value("amb_tiers_count", "6")
    assert value is None and "не больше 5" in error
    value, error = validate_setting_value("amb_tiers_count", "0")
    assert value is None and error


# ── tiers_config ────────────────────────────────────────────────────────────────────────────

def test_tiers_config_default_three(tmp_path):
    _ready(tmp_path)
    cfg = _run(amb_tiers.tiers_config())
    assert [c.n for c in cfg] == [1, 2, 3]
    assert [c.threshold for c in cfg] == [1, 3, 7]
    assert all(c.quota is None for c in cfg)
    assert cfg[1].text_key == "amb_tier2_granted_text"
    assert cfg[1].next_key == "amb_next_step_o2o_text"
    assert _run(amb_tiers.thresholds()) == (1, 3, 7)


def test_tiers_config_two_and_five_with_quota(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_tiers_count", "2"))
    _run(db.set_setting("amb_tier2_quota_on", "on"))
    _run(db.set_setting("amb_o2o_quota", "4"))
    cfg = _run(amb_tiers.tiers_config())
    assert [c.n for c in cfg] == [1, 2]
    assert cfg[1].quota == 4 and cfg[0].quota is None

    _run(db.set_setting("amb_tiers_count", "5"))
    _run(db.set_setting("amb_tier5_quota_on", "on"))
    _run(db.set_setting("amb_tier5_quota", "2"))
    cfg = _run(amb_tiers.tiers_config())
    assert [c.n for c in cfg] == [1, 2, 3, 4, 5]
    assert cfg[4].quota == 2 and cfg[4].waitlist_key == "amb_tier5_waitlist_text"
    assert cfg[3].threshold == 10 and cfg[4].threshold == 15


def test_tiers_count_clamped_when_stored_garbage(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_tiers_count", "9"))
    assert len(_run(amb_tiers.tiers_config())) == 5


# ── пороги ──────────────────────────────────────────────────────────────────────────────────

def test_threshold_order_generalised():
    current = {f"amb_tier{n}_threshold": v for n, v in enumerate((1, 3, 7, 10, 15), 1)}
    error = amb_threshold_order_error("amb_tier3_threshold", "3", current, 3)
    assert error and "ступени 3 должен быть больше, чем у ступени 2 (сейчас 3)" in error
    # ступень 4 за пределами count = 3 не проверяется
    assert amb_threshold_order_error("amb_tier4_threshold", "2", current, 3) is None
    assert amb_threshold_order_error("amb_tier4_threshold", "2", current, 4)
    assert amb_threshold_order_error("amb_tier5_threshold", "11", current, 5) is None


def test_cross_setting_error_uses_tiers_count(tmp_path):
    import settings_ops

    _ready(tmp_path)
    assert _run(settings_ops.cross_setting_error("amb_tier3_threshold", "3"))
    assert _run(settings_ops.cross_setting_error("amb_tier4_threshold", "2")) is None
    _run(db.set_setting("amb_tiers_count", "4"))
    assert _run(settings_ops.cross_setting_error("amb_tier4_threshold", "2"))
    # уменьшение числа ступеней всегда допустимо
    assert _run(settings_ops.cross_setting_error("amb_tiers_count", "1")) is None


# ── галочка «только с одобренной заявкой» ───────────────────────────────────────────────────

def test_can_earn_tiers_require_approved():
    pending = {"is_ambassador": 1, "status": "pending", "season": "S"}
    assert amb_tiers.can_earn_tiers(pending, "S") is False
    assert amb_tiers.can_earn_tiers(pending, "S", require_approved=False) is True
    approved = {**pending, "status": "approved"}
    assert amb_tiers.can_earn_tiers(approved, "S") is True
    assert amb_tiers.can_earn_tiers({**approved, "is_ambassador": 0}, "S",
                                    require_approved=False) is False


def test_require_approved_on_reads_setting(tmp_path):
    _ready(tmp_path)
    assert _run(amb_tiers.require_approved_on()) is True
    _run(db.set_setting("amb_tiers_require_approved", "off"))
    assert _run(amb_tiers.require_approved_on()) is False


# ── заморозка прежних дефолтов ──────────────────────────────────────────────────────────────

async def _freeze():
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await amb_tiers_db.freeze_legacy_tier_defaults(conn)
        await conn.commit()


def _set_version(value):
    _sql(f"PRAGMA user_version = {value}")


def test_freeze_writes_legacy_values_for_running_program(tmp_path):
    _ready(tmp_path)
    _sql("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('amb_qualified_program', 'on')")
    _sql("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('amb_tier2_threshold', '5')")
    _set_version(3)
    _run(_freeze())
    stored = dict(_sql("SELECT key, value FROM bot_settings WHERE key LIKE 'amb%'"))
    assert stored["amb_tier2_threshold"] == "5"  # сохранённое не перетирается
    assert stored["amb_tier3_threshold"] == "7"
    assert stored["amb_tier2_quota_on"] == "on"
    assert stored["amb_o2o_quota"] == "15"
    assert stored["amb_count_deadline"] == "2026-11-14 23:59"
    assert "СкиллАп" in stored["amb_tier1_text"]
    assert _sql("PRAGMA user_version")[0][0] == 4
    assert _run(amb_tiers.tiers_config())[1].quota == 15


def test_freeze_is_noop_without_program_and_on_repeat(tmp_path):
    _ready(tmp_path)
    before = _sql("SELECT COUNT(*) FROM bot_settings")[0][0]
    _set_version(3)
    _run(_freeze())
    assert _sql("SELECT COUNT(*) FROM bot_settings")[0][0] == before
    assert _sql("PRAGMA user_version")[0][0] == 4

    _sql("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('amb_qualified_program', 'on')")
    _run(_freeze())  # версия уже 4 — ничего не пишет
    assert _sql("SELECT COUNT(*) FROM bot_settings WHERE key = 'amb_tier3_text'")[0][0] == 0


def test_freeze_triggered_by_any_saved_tier_key(tmp_path):
    _ready(tmp_path)
    _sql("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('amb_tier1_text', 'мой')")
    _set_version(3)
    _run(_freeze())
    assert _sql("SELECT value FROM bot_settings WHERE key = 'amb_tier1_text'")[0][0] == "мой"
    assert _sql("SELECT value FROM bot_settings WHERE key = 'amb_tier2_quota_on'")[0][0] == "on"


# ── сторож версий миграций ──────────────────────────────────────────────────────────────────

def test_user_version_migrations_unique_and_ordered():
    found = {}
    for path in (ROOT / "database").glob("*.py"):
        for name, value in re.findall(r"^(_\w*USER_VERSION)\s*=\s*(\d+)", path.read_text(
                encoding="utf-8"), re.M):
            found[name] = int(value)
    assert sorted(found.values()) == list(range(1, len(found) + 1)), found
    assert found["_TIER_FREEZE_MIGRATION_USER_VERSION"] == 4

    source = (ROOT / "database" / "db.py").read_text(encoding="utf-8")
    body = source[source.index("await _migrate_local_timestamps_to_msk(db)"):]
    order = [body.index(call) for call in (
        "_migrate_local_timestamps_to_msk(db)", "_migrate_menu_schedule_into_program(db)",
        "amb_status_db.ensure_schema(db)", "freeze_legacy_tier_defaults(db)")]
    assert order == sorted(order)


# ── пресет ──────────────────────────────────────────────────────────────────────────────────

def test_skillup_preset_sets_legacy_tier_values():
    settings = reg_presets.REG_PRESETS["skillup"]["settings"]
    assert settings["amb_tier1_threshold"] == "1"
    assert settings["amb_tier2_threshold"] == "3"
    assert settings["amb_tier3_threshold"] == "7"
    assert settings["amb_tier2_quota_on"] == "on"
    assert settings["amb_o2o_quota"] == "15"
    assert settings["amb_count_deadline"] == "2026-11-14 23:59"
    assert settings["amb_tiers_require_approved"] == "on"
    for key, value in reg_presets.SKILLUP_TIER_SETTINGS.items():
        assert settings[key] == value


def test_skillup_tier_values_valid_for_registry():
    for key, value in reg_presets.SKILLUP_TIER_SETTINGS.items():
        assert key in SETTINGS_SCHEMA, key
        if SETTINGS_SCHEMA[key].get("type") in ("int", "enum"):
            assert validate_setting_value(key, value) == (value, None), key
