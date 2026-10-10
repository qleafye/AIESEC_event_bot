"""Статус амбассадора: колонки, миграция флагов в статус, единственный писатель с зеркалом
`is_ambassador`, место в лимите, пакет, запас, выборки, массовый отказ и архив сезона.

Слой — `database/amb_status_db.py`. pytest-asyncio в окружении нет — async через
`asyncio.run()`, временная БД — тот же приём, что `tests/test_amb_tiers_core_su5.py::_ready`.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import ast
import asyncio
import re
import sqlite3
import warnings
from pathlib import Path

from config import config
from database import amb_status_db as sdb
from database import db
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
AT = "2026-09-30 12:00:00"
REPO = REPO_ROOT

_NEW_COLUMNS = (
    "ambassador_status", "ambassador_status_at", "ambassador_status_by", "ambassador_slot_at",
    "ambassador_pack_at", "ambassador_reserve_at", "ambassador_declined_notified_at",
)


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_status_34.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, *, status="approved", season=SEASON, city=None, name=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": season,
        "event_city": city,
    }))
    if status:
        _run(db.set_user_status(tid, status))


def _row(tid, cols="ambassador_status, is_ambassador, is_ambassador_candidate, "
                   "ambassador_since, ambassador_left_at"):
    return _sql(f"SELECT {cols} FROM users WHERE telegram_id = ?", (tid,))[0]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: схема, миграция v3, писатель и зеркало
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_migration_clean_db_columns_and_version(tmp_path):
    config.DB_PATH = str(tmp_path / "clean.db")
    _run(db.init_db())
    cols = {r[1] for r in _sql("PRAGMA table_info(users)")}
    assert set(_NEW_COLUMNS) <= cols
    assert _sql("PRAGMA user_version")[0][0] == 4
    assert _sql("SELECT name FROM sqlite_master WHERE name = 'ambassador_season_archive'")
    assert _sql("SELECT name FROM sqlite_master WHERE name = 'idx_users_amb_status'")
    assert _sql("SELECT COUNT(*) FROM users")[0][0] == 0


def _legacy_three(tmp_path):
    """БД на user_version 2 с тремя делегатами до появления статуса."""
    config.DB_PATH = str(tmp_path / "legacy.db")
    _run(db.init_db())
    for tid in (1, 2, 3, 4):
        _seed(tid)
    _sql("UPDATE users SET ambassador_status = NULL")
    _sql("UPDATE users SET is_ambassador = 1, ambassador_since = '2026-01-01' WHERE telegram_id = 1")
    _sql("UPDATE users SET is_ambassador = 0, ambassador_left_at = '2026-02-01' WHERE telegram_id = 2")
    _sql("UPDATE users SET is_ambassador_candidate = 1 WHERE telegram_id = 3")
    _sql("UPDATE users SET is_ambassador = 1, is_ambassador_candidate = 1 WHERE telegram_id = 4")
    _sql("PRAGMA user_version = 2")


def test_migration_legacy_flags_to_status(tmp_path):
    _legacy_three(tmp_path)
    before = {tid: _row(tid)[1:] for tid in (1, 2, 3, 4)}
    _run(db.init_db())
    assert _row(1)[0] == "active"
    assert _row(2)[0] == "left"
    assert _row(3)[0] == "candidate"
    assert _row(4)[0] == "active"
    assert {tid: _row(tid)[1:] for tid in (1, 2, 3, 4)} == before
    assert _sql("PRAGMA user_version")[0][0] == 4


def test_migration_second_start_is_noop(tmp_path):
    _legacy_three(tmp_path)
    _run(db.init_db())
    snapshot = _sql("SELECT * FROM users ORDER BY telegram_id")
    # Человек вышел уже после миграции — повторный старт не должен вернуть его в active.
    _sql("UPDATE users SET ambassador_status = 'declined' WHERE telegram_id = 3")
    _run(db.init_db())
    assert _row(3)[0] == "declined"
    _sql("UPDATE users SET ambassador_status = 'candidate' WHERE telegram_id = 3")
    assert _sql("SELECT * FROM users ORDER BY telegram_id") == snapshot


def test_set_status_active_sets_mirror_and_since(tmp_path):
    _ready(tmp_path)
    _seed(10)
    assert _run(sdb.set_status(10, "active", at=AT, by=7)) is True
    assert _row(10) == ("active", 1, 0, AT, None)
    assert _row(10, "ambassador_status_at, ambassador_status_by") == (AT, 7)
    # CR-08: повторное active не переставляет дату вступления.
    assert _run(sdb.set_status(10, "active", at="2026-10-01 00:00:00")) is True
    assert _row(10)[3] == AT


def test_set_status_left_keeps_since(tmp_path):
    _ready(tmp_path)
    _seed(10)
    _run(sdb.set_status(10, "active", at=AT))
    later = "2026-10-05 10:00:00"
    assert _run(sdb.set_status(10, "left", at=later)) is True
    assert _row(10) == ("left", 0, 0, AT, later)
    # Возврат после выхода — свежая дата, left_at снят.
    again = "2026-10-06 10:00:00"
    assert _run(sdb.set_status(10, "active", at=again)) is True
    assert _row(10) == ("active", 1, 0, again, None)


def test_set_status_expect_mismatch_writes_nothing(tmp_path):
    _ready(tmp_path)
    _seed(10)
    _run(sdb.set_status(10, "active", at=AT))
    before = _sql("SELECT * FROM users WHERE telegram_id = 10")
    assert _run(sdb.set_status(10, "declined", at="x", expect=("candidate",))) is False
    assert _sql("SELECT * FROM users WHERE telegram_id = 10") == before
    # 'none' сопоставляется с NULL.
    _seed(11)
    assert _run(sdb.set_status(11, "candidate", at=AT, expect=("none",))) is True
    assert _row(11)[0] == "candidate"


def test_set_status_never_touches_candidate_answer(tmp_path):
    _ready(tmp_path)
    _seed(10)
    _sql("UPDATE users SET is_ambassador_candidate = 1 WHERE telegram_id = 10")
    for new in ("candidate", "active", "left", "declined", None):
        _run(sdb.set_status(10, new, at=AT))
        assert _row(10)[2] == 1
    assert _row(10)[0] is None


def test_set_status_unknown_rejected(tmp_path):
    _ready(tmp_path)
    try:
        _run(sdb.set_status(10, "boss", at=AT))
    except ValueError:
        return
    raise AssertionError("неизвестный статус должен давать ValueError")


def test_set_status_flag_wrapper_same_columns(tmp_path):
    _ready(tmp_path)
    _seed(10)
    assert _run(db.set_ambassador_flag(10, active=True, at=AT)) is True
    assert _row(10) == ("active", 1, 0, AT, None)
    # CR-08: повторный вызов — rowcount 0, дата не тронута.
    assert _run(db.set_ambassador_flag(10, active=True, at="2026-12-01")) is False
    assert _row(10)[3] == AT
    left = "2026-10-01 00:00:00"
    assert _run(db.set_ambassador_flag(10, active=False, at=left)) is True
    assert _row(10) == ("left", 0, 0, AT, left)
    assert _run(db.set_ambassador_flag(999, active=True, at=AT)) is False


def test_get_status_shape(tmp_path):
    _ready(tmp_path)
    _seed(10)
    st = _run(sdb.get_status(10))
    assert st["status"] == "none" and st["since"] is None and st["slot_at"] is None
    _run(sdb.set_status(10, "active", at=AT))
    st = _run(sdb.get_status(10))
    assert st["status"] == "active" and st["since"] == AT
    assert set(st) >= {"status", "since", "slot_at", "pack_at", "reserve_at", "left_at",
                       "declined_notified_at"}
    assert _run(sdb.get_status(424242)) is None


# ── Сторож писателя ─────────────────────────────────────────────────────────────────────────

# Файлы, которым разрешено писать is_ambassador / ambassador_status в SQL — с причиной.
_ALLOWED_STATUS_WRITERS = {
    "database/amb_status_db.py": "единственный писатель статуса и зеркала is_ambassador",
    "handlers/uat_seed.py": "сидер состояний /uat на стенде — не боевой путь",
    "tools/shoot_screens.py": "генератор скриншотов документации — не боевой путь",
}
_SKIP_DIRS = {"tests", ".planning", "venv", ".venv", "node_modules", ".git", "__pycache__",
              "site-packages", "worktrees",
              ".claude"}
_WRITE_RE = re.compile(
    r"(UPDATE\s+users\s+SET\b(?:(?!\bWHERE\b)[^;])*?\b(is_ambassador|ambassador_status)\b\s*="
    r"|INSERT\s+(?:OR\s+\w+\s+)?INTO\s+users\b[^;)]*?\b(is_ambassador|ambassador_status)\b\s*[,)])",
    re.I | re.S,
)


def _sql_literals(tree: ast.AST):
    """Все строковые литералы файла. ast уже склеил соседние литералы ("a " "b") в один, а у
    f-строки берём её постоянные куски — так запись не спрятать разбиением на строки."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "?".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )


def _status_writers() -> dict[str, list[int]]:
    """Литералы SQL, которые присваивают is_ambassador/ambassador_status (не читают).
    Граница слова отсекает is_ambassador_candidate и ambassador_status_at — другие колонки."""
    found: dict[str, list[int]] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for lineno, text in _sql_literals(tree):
            if _WRITE_RE.search(text):
                found.setdefault(rel.as_posix(), []).append(lineno)
    return found


def test_writer_guard_only_amb_status_db_writes_status():
    found = _status_writers()
    unexpected = {f: v for f, v in found.items() if f not in _ALLOWED_STATUS_WRITERS}
    assert not unexpected, (
        "SQL пишет is_ambassador/ambassador_status вне database/amb_status_db.py: "
        f"{unexpected} — перенесите запись в amb_status_db.set_status."
    )
    assert "database/amb_status_db.py" in found


def test_writer_guard_regex_catches_samples():
    assert _WRITE_RE.search("UPDATE users SET is_ambassador = 1 WHERE telegram_id = ?")
    assert _WRITE_RE.search("UPDATE users SET x = 1, ambassador_status = ? WHERE 1")
    assert not _WRITE_RE.search("SELECT * FROM users WHERE is_ambassador = 1")
    assert not _WRITE_RE.search("UPDATE users SET status = ? WHERE telegram_id = ?")
    assert not _WRITE_RE.search("UPDATE users SET x = 1 WHERE is_ambassador = 1")
    assert not _WRITE_RE.search("UPDATE users SET is_ambassador_candidate = 1 WHERE 1")
    assert not _WRITE_RE.search("UPDATE users SET ambassador_status_at = ? WHERE 1")
    assert _WRITE_RE.search("INSERT INTO users (telegram_id, is_ambassador) VALUES (?, ?)")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: место в лимите, пакет, запас, выборки, массовый отказ, архив сезона
# ══════════════════════════════════════════════════════════════════════════════════════════

def _active(tid, **kw):
    _seed(tid, **kw)
    _run(sdb.set_status(tid, "active", at=AT))


def test_slot_claim_rules(tmp_path):
    _ready(tmp_path)
    _active(10)
    assert _run(sdb.try_claim_slot(10, limit=17, season=SEASON, at=AT)) is True
    assert _run(sdb.get_status(10))["slot_at"] == AT
    # Уже с местом — False, дата не меняется.
    assert _run(sdb.try_claim_slot(10, limit=17, season=SEASON, at="later")) is False
    assert _run(sdb.get_status(10))["slot_at"] == AT
    # Заявка не одобрена — места нет.
    _active(11, status="pending")
    assert _run(sdb.try_claim_slot(11, limit=17, season=SEASON, at=AT)) is False
    # Другой сезон — места нет.
    _active(12, season="YL 26/2")
    assert _run(sdb.try_claim_slot(12, limit=17, season=SEASON, at=AT)) is False
    # Не active — места нет.
    _seed(13)
    _run(sdb.set_status(13, "candidate", at=AT))
    assert _run(sdb.try_claim_slot(13, limit=17, season=SEASON, at=AT)) is False
    assert _run(sdb.slots_taken()) == 1


def test_slot_limit_full_and_unlimited(tmp_path):
    _ready(tmp_path)
    _active(10)
    _active(11)
    assert _run(sdb.try_claim_slot(10, limit=1, season=SEASON, at=AT)) is True
    assert _run(sdb.try_claim_slot(11, limit=1, season=SEASON, at=AT)) is False
    assert _run(sdb.try_claim_slot(11, limit=0, season=SEASON, at=AT)) is True
    assert _run(sdb.slots_taken()) == 2


def test_slot_race_two_connections_one_wins(tmp_path):
    _ready(tmp_path)
    for tid in range(100, 116):
        _active(tid)
        assert _run(sdb.try_claim_slot(tid, limit=17, season=SEASON, at=AT))
    _active(200)
    _active(201)
    assert _run(sdb.slots_taken()) == 16

    async def race():
        return await asyncio.gather(
            sdb.try_claim_slot(200, limit=17, season=SEASON, at=AT),
            sdb.try_claim_slot(201, limit=17, season=SEASON, at=AT),
        )

    assert sorted(_run(race())) == [False, True]
    assert _run(sdb.slots_taken()) == 17


def test_pack_keeps_slot_on_leave(tmp_path):
    _ready(tmp_path)
    _active(10)
    _active(11)
    for tid in (10, 11):
        _run(sdb.try_claim_slot(tid, limit=17, season=SEASON, at=AT))
    assert _run(sdb.set_pack(10, True, at=AT)) is True
    assert _run(sdb.get_status(10))["pack_at"] == AT
    _run(sdb.set_status(10, "left", at=AT))
    _run(sdb.set_status(11, "declined", at=AT))
    assert _run(sdb.get_status(10))["slot_at"] == AT
    assert _run(sdb.get_status(11))["slot_at"] is None
    assert _run(sdb.slots_taken()) == 1
    # Сброс статуса (none) без пакета тоже освобождает.
    _active(12)
    _run(sdb.try_claim_slot(12, limit=17, season=SEASON, at=AT))
    _run(sdb.set_status(12, None, at=AT))
    assert _run(sdb.slots_taken()) == 1


def test_pack_revoked_after_leave_frees_slot(tmp_path):
    _ready(tmp_path)
    _active(10)
    _run(sdb.try_claim_slot(10, limit=17, season=SEASON, at=AT))
    _run(sdb.set_pack(10, True, at=AT))
    _run(sdb.set_status(10, "left", at=AT))
    assert _run(sdb.set_pack(10, False, at=AT)) is True
    st = _run(sdb.get_status(10))
    assert st["pack_at"] is None and st["slot_at"] is None


def test_reserve_only_for_candidate(tmp_path):
    _ready(tmp_path)
    _seed(10)
    _run(sdb.set_status(10, "candidate", at=AT))
    assert _run(sdb.set_reserve(10, at=AT)) is True
    assert _run(sdb.get_status(10))["reserve_at"] == AT
    _active(11)
    assert _run(sdb.set_reserve(11, at=AT)) is False
    # Взяли из запаса — отметка снята.
    _run(sdb.set_status(10, "active", at=AT))
    assert _run(sdb.get_status(10))["reserve_at"] is None


def test_list_page_filters_order_and_city(tmp_path):
    _ready(tmp_path)
    _seed(1, city="msk")
    _seed(2, city="spb")
    _seed(3, city="msk")
    _run(sdb.set_status(1, "candidate", at="2026-09-01 10:00:00"))
    _run(sdb.set_status(2, "candidate", at="2026-09-02 10:00:00"))
    _run(sdb.set_status(3, "candidate", at="2026-09-03 10:00:00"))
    _run(sdb.set_reserve(1, at=AT))
    ids = [r["telegram_id"] for r in _run(sdb.list_page("candidates", offset=0, limit=10))]
    assert ids == [2, 3, 1]
    assert [r["telegram_id"] for r in _run(sdb.list_page("candidates", offset=1, limit=1))] == [3]
    assert _run(sdb.count_by_filter("candidates")) == 3
    scope = ("msk", ())
    ids = [r["telegram_id"] for r in _run(sdb.list_page("candidates", offset=0, limit=10,
                                                        city_scope=scope))]
    assert ids == [3, 1]
    assert _run(sdb.count_by_filter("candidates", city_scope=scope)) == 2

    _active(4)
    _active(5)
    _run(sdb.try_claim_slot(4, limit=17, season=SEASON, at=AT))
    assert {r["telegram_id"] for r in _run(sdb.list_page("team", offset=0, limit=10))} == {4, 5}
    assert [r["telegram_id"] for r in _run(sdb.list_page("no_pack", offset=0, limit=10))] == [5]
    _run(sdb.set_status(2, "declined", at=AT))
    assert [r["telegram_id"] for r in _run(sdb.list_page("declined", offset=0, limit=10))] == [2]
    row = _run(sdb.list_page("team", offset=0, limit=1))[0]
    assert {"telegram_id", "full_name", "username", "event_city", "status", "season",
            "ambassador_status", "ambassador_slot_at", "ambassador_pack_at"} <= set(row)
    try:
        _run(sdb.list_page("all", offset=0, limit=1))
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный фильтр должен давать ValueError")


def test_decline_remaining_and_notice_once(tmp_path):
    _ready(tmp_path)
    for tid in (1, 2):
        _seed(tid)
        _run(sdb.set_status(tid, "candidate", at=AT))
    _active(3)
    assert sorted(_run(sdb.decline_remaining(at=AT, by=77))) == [1, 2]
    assert _run(sdb.get_status(1))["status"] == "declined"
    assert _run(sdb.get_status(3))["status"] == "active"
    assert _run(sdb.decline_remaining(at=AT, by=77)) == []
    assert _run(sdb.claim_decline_notice(1, at=AT)) is True
    assert _run(sdb.claim_decline_notice(1, at=AT)) is False
    assert _run(sdb.claim_decline_notice(3, at=AT)) is False


def test_release_slot_and_unapproved_holders(tmp_path):
    _ready(tmp_path)
    _active(10)
    _active(11)
    for tid in (10, 11):
        _run(sdb.try_claim_slot(tid, limit=17, season=SEASON, at=AT))
    _run(sdb.set_pack(11, True, at=AT))
    _run(db.set_user_status(10, "rejected"))
    _run(db.set_user_status(11, "rejected"))
    assert _run(sdb.unapproved_slot_holders(SEASON)) == [10]
    assert _run(sdb.release_slot(10)) is True
    st = _run(sdb.get_status(10))
    assert st["slot_at"] is None and st["status"] == "active"
    assert _run(sdb.release_slot(11)) is False
    assert _run(sdb.get_status(11))["slot_at"] == AT
    assert _run(sdb.unapproved_slot_holders(SEASON)) == []


def test_archive_and_reset_season(tmp_path):
    _ready(tmp_path)
    _active(10)
    _run(sdb.try_claim_slot(10, limit=17, season=SEASON, at=AT))
    _run(sdb.set_pack(10, True, at=AT))
    _seed(11)
    _run(sdb.set_status(11, "candidate", at=AT))
    _run(sdb.set_reserve(11, at=AT))
    _active(12)
    _run(sdb.set_status(12, "left", at="2026-09-20 00:00:00"))
    _seed(13)
    assert _run(sdb.count_with_status()) == 3
    assert len(_run(sdb.export_rows())) == 3

    assert _run(sdb.archive_and_reset_season(SEASON, at="2026-10-01 00:00:00")) == 3
    arch = _sql("SELECT telegram_id, season, status, since, slot_at, pack_at "
                "FROM ambassador_season_archive ORDER BY telegram_id")
    assert arch == [(10, SEASON, "active", AT, AT, AT),
                    (11, SEASON, "candidate", None, None, None),
                    (12, SEASON, "left", AT, None, None)]
    rows = _sql("SELECT telegram_id, ambassador_status, is_ambassador, ambassador_slot_at, "
                "ambassador_pack_at, ambassador_reserve_at, ambassador_since, ambassador_left_at "
                "FROM users ORDER BY telegram_id")
    assert rows == [(10, None, 0, None, None, None, AT, None),
                    (11, None, 0, None, None, None, None, None),
                    (12, None, 0, None, None, None, AT, "2026-09-20 00:00:00"),
                    (13, None, 0, None, None, None, None, None)]
    assert _run(sdb.count_with_status()) == 0
    assert _run(sdb.slots_taken()) == 0
    assert len(_run(sdb.export_archive_rows())) == 3
    # Повтор с тем же сезоном не падает; пустое имя сезона — 'legacy'.
    assert _run(sdb.archive_and_reset_season(SEASON, at="x")) == 0
    _active(14)
    _run(sdb.archive_and_reset_season("", at="x"))
    assert _sql("SELECT season FROM ambassador_season_archive WHERE telegram_id = 14") == [("legacy",)]
