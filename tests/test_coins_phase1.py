"""Phase 1 coins tests: /coins arg parsing, leaderboard rendering, capability-gated guard."""
import asyncio

from config import config
from database import db
from handlers import admin as admin_mod
from handlers.admin import _parse_coins_amount
from handlers.admin_caps import required_capability
from handlers.user_actions import render_leaderboard


# ── /coins amount parsing ────────────────────────────────────────────────────

def test_parse_plus():
    assert _parse_coins_amount("+10") == 10


def test_parse_minus():
    assert _parse_coins_amount("-3") == -3


def test_parse_unsigned():
    assert _parse_coins_amount("10") == 10


def test_parse_non_numeric():
    assert _parse_coins_amount("abc") is None


def test_parse_empty():
    assert _parse_coins_amount("") is None


# ── /coins is capability-gated (structural — authorization not bypassable) ────
#
# Phase 8 / D-01: the old `is_admin` filter is gone (08-04, one-shot migration, D-03) --
# the structural guarantee this test protects didn't disappear, its CARRIER changed from
# a literal in the decorator's source text to a resolvable entry in ADMIN_CAPS, enforced
# by CapabilityMiddleware at dispatch time instead of a per-handler filter argument.

def test_coins_handler_is_capability_guarded():
    # 1) the handler is still registered on admin.router (not silently dropped by the
    #    is_admin removal).
    names = [
        h.callback.__name__
        for h in admin_mod.router.message.handlers
    ]
    assert "cmd_coins" in names

    # 2) the command resolves to a real capability in the map -- deny-by-default (D-02)
    #    means an unmapped command would be locked for everyone, including admins.
    # Phase 14 (14-04, GAME-09): repointed off the applications-queue right -- coins are
    # geyma's territory now.
    assert required_capability(command="coins") == "moderate_game"


# ── leaderboard rendering ────────────────────────────────────────────────────
#
# Phase 17.1 (17.1-01): render_leaderboard стал async -- заголовок/пустой экран/строка «твоё
# место» читаются из SETTINGS_SCHEMA, поэтому тестам нужна живая БД (tmp_path + init_db,
# как в остальных тестах этого окружения; pytest-asyncio тут нет -> asyncio.run).


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_coins_phase1.db")
    asyncio.run(db.init_db())


def test_render_leaderboard_lists_names_ranked(tmp_path):
    _db_ready(tmp_path)
    rows = [
        {"user_id": 1, "balance": 90, "full_name": "Alice"},
        {"user_id": 2, "balance": 50, "full_name": "Bob"},
    ]
    out = asyncio.run(render_leaderboard(rows, requester_id=2, requester_rank=2, requester_balance=50))
    assert "Alice" in out and "Bob" in out
    assert "1. Alice" in out          # ranked, top first
    assert "Твоё место" in out and "2" in out  # requester rank line


def test_render_leaderboard_escapes_html(tmp_path):
    _db_ready(tmp_path)
    rows = [{"user_id": 1, "balance": 10, "full_name": "<b>x</b>"}]
    out = asyncio.run(render_leaderboard(rows, requester_id=1, requester_rank=1, requester_balance=10))
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_render_leaderboard_empty(tmp_path):
    _db_ready(tmp_path)
    out = asyncio.run(render_leaderboard([], requester_id=1, requester_rank=None, requester_balance=0))
    assert "Пока" in out
    assert "—" in out  # no rank shown for a user with no coins
