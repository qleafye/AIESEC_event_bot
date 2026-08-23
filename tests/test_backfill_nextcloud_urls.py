"""Бэкфилл ссылок на резюме при смене домена Nextcloud + сторож рунбука DEPLOY-DOMAIN.md."""
import asyncio
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

from scripts.backfill_nextcloud_urls import run, select_rows, swap_base

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "DEPLOY-DOMAIN.md"

OLD = "https://1.2.3.4:8443"
NEW = "https://cloud.example.org"
TAIL = "/s/AbCdEf123/download?path=%2F&files=Ivanov_ivan_42_20260801-120000.pdf"


# --- swap_base --------------------------------------------------------------------------


def test_swap_base_replaces_only_prefix():
    assert swap_base(OLD + TAIL, OLD, NEW) == NEW + TAIL


def test_swap_base_handles_port_in_old_prefix_like_domain():
    assert swap_base("https://old.example.org" + TAIL, "https://old.example.org", NEW) == NEW + TAIL
    assert swap_base(OLD + TAIL, OLD, NEW) == NEW + TAIL


def test_swap_base_leaves_other_domain_untouched():
    other = "https://elsewhere.example.org" + TAIL
    assert swap_base(other, OLD, NEW) == other


def test_swap_base_tolerates_trailing_slashes_in_bases():
    assert swap_base(OLD + TAIL, OLD + "/", NEW + "/") == NEW + TAIL


@pytest.mark.parametrize("value", [None, ""])
def test_swap_base_survives_empty_values(value):
    assert swap_base(value, OLD, NEW) == value


def test_swap_base_does_not_match_longer_host_with_same_prefix():
    """https://1.2.3.4:8443 не должен «съесть» https://1.2.3.4:84430 — сравнение по началу
    строки, как и задумано; здесь фиксируем, что хвост после порта остаётся как есть."""
    url = "https://1.2.3.4:84430" + TAIL
    assert swap_base(url, OLD, NEW) == NEW + "0" + TAIL


# --- run / select_rows на временной БД -------------------------------------------------


SEED = [
    (1, OLD + TAIL),
    (2, OLD + "/s/Other/download?path=%2F&files=b.pdf"),
    (3, "https://elsewhere.example.org" + TAIL),
    (4, None),
    (5, ""),
]


async def _with_db(tmp_path, body):
    """Открыть временную БД с таблицей users, засеять SEED, выполнить body(conn)."""
    conn = await aiosqlite.connect(tmp_path / "t.db")
    try:
        await conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, resume_url TEXT)")
        await conn.executemany("INSERT INTO users (telegram_id, resume_url) VALUES (?, ?)", SEED)
        await conn.commit()
        return await body(conn)
    finally:
        await conn.close()


async def _urls(conn):
    rows = await conn.execute_fetchall("SELECT telegram_id, resume_url FROM users ORDER BY telegram_id")
    return dict(rows)


def test_select_rows_returns_only_old_prefix(tmp_path):
    async def body(db):
        rows = await select_rows(db, OLD)
        assert [r[0] for r in rows] == [1, 2]
        assert [r[0] for r in await select_rows(db, OLD, limit=1)] == [1]

    asyncio.run(_with_db(tmp_path, body))


def test_dry_run_changes_nothing(tmp_path):
    async def body(db):
        before = await _urls(db)
        stats = await run(db, OLD, NEW, dry_run=True)
        assert stats == {"found": 2, "replaced": 2, "skipped": 0}
        assert await _urls(db) == before

    asyncio.run(_with_db(tmp_path, body))


def test_real_run_changes_exactly_matching_rows(tmp_path):
    async def body(db):
        stats = await run(db, OLD, NEW)
        assert stats == {"found": 2, "replaced": 2, "skipped": 0}
        after = await _urls(db)
        assert after[1] == NEW + TAIL
        assert after[2] == NEW + "/s/Other/download?path=%2F&files=b.pdf"
        assert after[3] == "https://elsewhere.example.org" + TAIL
        assert after[4] is None and after[5] == ""
        # повторный прогон — нечего менять
        assert (await run(db, OLD, NEW))["found"] == 0

    asyncio.run(_with_db(tmp_path, body))


def test_real_run_respects_limit(tmp_path):
    async def body(db):
        stats = await run(db, OLD, NEW, limit=1)
        assert stats["replaced"] == 1
        after = await _urls(db)
        assert after[1].startswith(NEW) and after[2].startswith(OLD)

    asyncio.run(_with_db(tmp_path, body))


def test_cli_help_lists_arguments():
    out = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_nextcloud_urls", "--help"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    ).stdout
    for flag in ("--old-base", "--new-base", "--dry-run", "--limit"):
        assert flag in out


# --- рунбук ------------------------------------------------------------------------------


def _runbook_working_part() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    head, sep, _tail = text.partition("## Чего делать НЕ нужно")
    assert sep, "в рунбуке должен быть заключительный раздел «Чего делать НЕ нужно»"
    return head


@pytest.mark.parametrize(
    "needle",
    [
        "docker network create",
        "172.31.0.0/16",
        "TUNNEL_TOKEN",
        "Public Hostname",
        "yl26-dashboard:8000",
        "nextcloud-app:80",
        "yl26-dashboard",
        "/setdomain",
        "overwriteprotocol",
        "overwritehost",
        "trusted_domains",
        "chown -R 1000:1000",
        "--dry-run",
        "-wal",
        "nextcloud-caddy",
        "YouLead26-test",
        "Если не получилось",
    ],
)
def test_runbook_keeps_key_steps(needle):
    assert needle in _runbook_working_part()


@pytest.mark.parametrize("forbidden", ["Caddy", "Let's Encrypt", "A-запис", "серое облако", "серым облаком"])
def test_runbook_working_part_has_no_legacy_scheme(forbidden):
    assert forbidden not in _runbook_working_part()


def test_readme_links_runbook():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/DEPLOY-DOMAIN.md" in readme
    assert "## Дашборд статистики" in readme
