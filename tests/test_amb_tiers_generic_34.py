"""Обобщённые ступени амбассадоров 1–5: карта ключей, конфигурация, проверка порогов,
заморозка прежних дефолтов для стека СкиллАп и пресет.

pytest-asyncio в окружении нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import json
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

def test_skillup_preset_leaves_tiers_to_manager():
    # Владелец 09.10: ступени — обещания конкретного события, пресет их не пишет. Прежние
    # значения СкиллАп 5 остаются только для миграции freeze_legacy_tier_defaults.
    settings = reg_presets.REG_PRESETS["skillup"]["settings"]
    assert not set(reg_presets.SKILLUP_TIER_SETTINGS) & set(settings)
    assert reg_presets.SKILLUP_TIER_SETTINGS["amb_count_deadline"] == "2026-11-14 23:59"


def test_skillup_tier_values_valid_for_registry():
    for key, value in reg_presets.SKILLUP_TIER_SETTINGS.items():
        assert key in SETTINGS_SCHEMA, key
        if SETTINGS_SCHEMA[key].get("type") in ("int", "enum"):
            assert validate_setting_value(key, value) == (value, None), key


# ── «прошли отбор» из журнала ───────────────────────────────────────────────────────────────

def _seed_invitee(tid, referrer, *, status, season, journal=True, excluded=False):
    from tests.test_amb_tiers_core_su5 import seed_journal_row

    _run(db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}",
        "registration_date": "2026-09-01 00:00:00", "referrer_id": referrer, "season": season,
    }))
    _run(db.set_user_status(tid, status))
    if status == "approved":
        _sql("UPDATE users SET approved_at = '2026-09-03 10:00:00' WHERE telegram_id = ?", (tid,))
    if journal:
        seed_journal_row(tid, referrer, season=season, excluded=excluded)
    if excluded:
        _sql("INSERT INTO ambassador_exclusions (invitee_id, reason, excluded_by, excluded_at) "
             "VALUES (?, 'test', 1, '2026-09-02 00:00:00')", (tid,))


def test_qualified_parity_with_chat_rating(tmp_path):
    from dashboard import chat_rating

    _ready(tmp_path)
    _run(db.set_setting("event_season", "SU26"))
    _run(db.add_user({"telegram_id": 100, "full_name": "Amb", "registration_date": "2026-09-01 00:00:00",
                      "season": "SU26"}))
    _seed_invitee(201, 100, status="approved", season="SU26")
    _seed_invitee(202, 100, status="approved", season="SU26")
    _seed_invitee(203, 100, status="pending", season="SU26", journal=False)
    _seed_invitee(204, 100, status="approved", season="SU26", excluded=True)
    _seed_invitee(205, 100, status="approved", season="YL26")
    counts = _run(amb_tiers_db.referral_counts(100, "SU26"))
    conn = sqlite3.connect(config.DB_PATH)
    try:
        brought = chat_rating._referral_dates(conn)
    finally:
        conn.close()
    assert counts["qualified"] == 2
    assert len(brought.get(100, [])) == counts["qualified"]
    assert counts["total"] == 3 and counts["pending"] == 1


def test_qualified_needs_live_status_and_journal(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_season", "SU26"))
    _seed_invitee(201, 100, status="approved", season="SU26", journal=False)
    assert _run(amb_tiers_db.referral_counts(100, "SU26"))["qualified"] == 0
    _seed_invitee(202, 100, status="pending", season="SU26")  # строка журнала, но вернули в ожидание
    c = _run(amb_tiers_db.referral_counts(100, "SU26"))
    assert c["qualified"] == 0 and c["total"] == 2


# ── выдача и уведомления для N ступеней ─────────────────────────────────────────────────────

def _set(**kv):
    for k, v in kv.items():
        _run(db.set_setting(k, str(v)))


def _five_tiers(**extra):
    _set(amb_qualified_program="on", amb_count_deadline="", event_season="SU26",
         amb_tiers_count=5, amb_tier1_threshold=1, amb_tier2_threshold=2, amb_tier3_threshold=4,
         amb_tier4_threshold=6, amb_tier5_threshold=8, **extra)


def _ambassador(tid, *, approved=True):
    _run(db.add_user({"telegram_id": tid, "full_name": f"Amb {tid}",
                      "registration_date": "2026-09-01 00:00:00", "season": "SU26"}))
    _run(db.set_user_status(tid, "approved" if approved else "pending"))
    _run(db.set_ambassador_flag(tid, active=True, at="2026-09-01 00:00:00"))


def _invite(referrer, n, start=1000):
    for i in range(n):
        _seed_invitee(start + referrer * 10 + i, referrer, status="approved", season="SU26")


def _tier_rows(tid):
    return [(r["tier"], r["o2o_status"]) for r in _run(amb_tiers_db.list_tiers(tid))]


def _events():
    return [json.loads(r[0]) for r in _sql("SELECT payload FROM miniapp_outbox ORDER BY id")]


def test_quota_on_tier_three_waitlists_over_limit(tmp_path):
    _ready(tmp_path)
    _five_tiers(amb_tier3_quota_on="on", amb_tier3_quota=2)
    for tid in (1, 2, 3):
        _ambassador(tid)
        _invite(tid, 4)
        _run(amb_tiers.check_tiers([tid], notify=False))
    assert [dict(_tier_rows(t))[3] for t in (1, 2, 3)] == ["granted", "granted", "waitlist"]
    assert dict(_tier_rows(1))[2] is None  # на ступенях без квоты статуса нет


def test_quotas_are_counted_per_tier(tmp_path):
    _ready(tmp_path)
    _five_tiers(amb_tier3_quota_on="on", amb_tier3_quota=1, amb_tier5_quota_on="on",
                amb_tier5_quota=1)
    for tid in (1, 2):
        _ambassador(tid)
        _invite(tid, 8)
        _run(amb_tiers.check_tiers([tid], notify=False))
    assert dict(_tier_rows(1))[3] == "granted" and dict(_tier_rows(2))[3] == "waitlist"
    assert dict(_tier_rows(1))[5] == "granted" and dict(_tier_rows(2))[5] == "waitlist"


def test_claim_race_last_slot_goes_to_one(tmp_path):
    _ready(tmp_path)
    _run(amb_tiers_db.claim_new_tiers(1, [3], "2026-09-01 00:00:00", {3: 2}))

    async def race():
        return await asyncio.gather(*(
            amb_tiers_db.claim_new_tiers(t, [3], "2026-09-01 00:00:01", {3: 2}) for t in (2, 3, 4)
        ))

    results = _run(race())
    statuses = sorted(r[0]["o2o_status"] for r in results)
    assert statuses == ["granted", "waitlist", "waitlist"]


def test_jump_notifies_top_and_quota_tiers(tmp_path):
    _ready(tmp_path)
    _five_tiers(amb_tier2_quota_on="on", amb_o2o_quota=5)
    _ambassador(1)
    _invite(1, 4)
    _run(amb_tiers.check_tiers([1]))
    assert [(e["tier"], e["left"]) for e in _events()] == [(2, 0), (3, 0)]
    rows = {r["tier"]: r["notified_at"] for r in _run(amb_tiers_db.list_tiers(1))}
    assert rows[1] is not None and rows[2] is None and rows[3] is None


def test_notify_text_key_by_tier_and_status():
    from services import amb_tiers_notify as n

    assert n._text_key(4, None) == "amb_tier4_text"
    assert n._text_key(3, "waitlist") == tier_key(3, "waitlist")
    assert n._text_key(2, "granted") == "amb_tier2_granted_text"
    assert n._text_key(2, "waitlist") == "amb_tier2_waitlist_text"
    assert n._text_key(9, None) is None


def test_require_approved_toggle(tmp_path):
    _ready(tmp_path)
    _five_tiers()
    _ambassador(1, approved=False)
    _invite(1, 2)
    _run(amb_tiers.check_tiers([1], notify=False))
    assert _tier_rows(1) == []
    _set(amb_tiers_require_approved="off")
    _run(amb_tiers.check_tiers([1], notify=False))
    assert [t for t, _ in _tier_rows(1)] == [1, 2]


def test_legacy_positional_quota_still_means_tier_two(tmp_path):
    _ready(tmp_path)
    rows = _run(amb_tiers_db.claim_new_tiers(1, [1, 2], "2026-09-01 00:00:00", 1))
    assert [(r["tier"], r["o2o_status"]) for r in rows] == [(1, None), (2, "granted")]
    rows = _run(amb_tiers_db.claim_new_tiers(2, [2], "2026-09-01 00:00:00", o2o_quota=1))
    assert rows[0]["o2o_status"] == "waitlist"


# ── прогресс, дашборд, бэкафилл, сверка ─────────────────────────────────────────────────────

def test_progress_targets_nearest_labelled_tier(tmp_path):
    from services import amb_progress

    _ready(tmp_path)
    _five_tiers()
    _set(amb_tier1_next_label="До ступени 1: {n}")
    _ambassador(1)
    view = _run(amb_progress.progress_view(1))
    assert (view["next_tier"], view["n"], view["next_kind"]) == (1, 1, "tier1")
    _invite(1, 1)
    view = _run(amb_progress.progress_view(1))
    assert (view["next_tier"], view["n"], view["next_kind"]) == (2, 1, "o2o")
    _invite(1, 3, start=5000)
    view = _run(amb_progress.progress_view(1))  # 4 прошли: следующая — ступень 4 (порог 6)
    assert (view["next_tier"], view["n"]) == (4, 2)
    _invite(1, 4, start=6000)
    view = _run(amb_progress.progress_view(1))
    assert view["next_tier"] is None and view["next_kind"] == "done" and view["n"] == 0


def test_progress_skips_empty_label_and_renders(tmp_path):
    from services import amb_progress

    _ready(tmp_path)
    _five_tiers()
    _ambassador(1)
    view = _run(amb_progress.progress_view(1))  # подпись ступени 1 пуста — цель ступень 2
    assert view["next_tier"] == 2 and view["n"] == 2

    async def tr_key(key):
        return {"amb_progress_text": "{total}/{qualified} {next_step}",
                "amb_next_step_o2o_text": "до 2: {n}"}[key]

    assert _run(amb_progress.render_progress(1, tr_key)) == "0/0 до 2: 2"


def test_dashboard_block_per_tier(tmp_path):
    from dashboard.amb_tiers_block import amb_tiers_block

    _ready(tmp_path)
    _five_tiers(amb_tier3_quota_on="on", amb_tier3_quota=1)
    for tid in (1, 2):
        _ambassador(tid)
        _invite(tid, 4)
        _run(amb_tiers.check_tiers([tid], notify=False))
    conn = sqlite3.connect(config.DB_PATH)
    try:
        class _Scope:
            season = None
            city = None

        block = amb_tiers_block(conn, _Scope())
    finally:
        conn.close()
    by = {t["tier"]: t for t in block["tiers"]}
    assert len(by) == 5 and by[3]["reached"] == 2 and by[4]["reached"] == 0
    assert (by[3]["quota"], by[3]["granted"], by[3]["waitlist"]) == (1, 1, 1)
    assert by[2]["quota"] is None
    assert block["qualified_total"] == 8 and block["active"] == 2
    assert (block["o2o_granted"], block["o2o_quota"], block["waitlist"]) == (1, 1, 1)


def test_preview_backfill_five_tiers_with_quota(tmp_path):
    _ready(tmp_path)
    _five_tiers(amb_tier3_quota_on="on", amb_tier3_quota=1)
    for tid in (1, 2):
        _ambassador(tid)
        _invite(tid, 4)
    preview = _run(amb_tiers.preview_backfill())
    statuses = {e["telegram_id"]: {t["tier"]: t["o2o_status"] for t in e["tiers"]} for e in preview}
    assert sorted(st[3] for st in statuses.values()) == ["granted", "waitlist"]
    assert all(st[1] is None and st[2] is None for st in statuses.values())
    assert _run(amb_tiers_db.list_tiers()) == []


def test_reconcile_grants_missed_and_requeues_stale_once(tmp_path):
    _ready(tmp_path)
    _five_tiers()
    _ambassador(1)
    _invite(1, 2)
    out = _run(amb_tiers.reconcile_tiers())
    assert out["granted"] == 2
    assert [e["tier"] for e in _events()] == [2]  # старшая новая ступень
    _sql("DELETE FROM miniapp_outbox")  # событие потерялось
    _sql("UPDATE ambassador_tiers SET reached_at = '2000-01-01 00:00:00'")
    out = _run(amb_tiers.reconcile_tiers())
    assert out["requeued"] == 1 and [e["tier"] for e in _events()] == [2]
    out = _run(amb_tiers.reconcile_tiers())  # необработанное событие уже есть
    assert out["requeued"] == 0 and len(_events()) == 1


def test_reconcile_program_off_and_never_raises(tmp_path):
    _ready(tmp_path)
    assert _run(amb_tiers.reconcile_tiers()) == {"granted": 0, "requeued": 0}
