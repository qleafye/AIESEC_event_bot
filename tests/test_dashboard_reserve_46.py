"""Срезы резерва на дашборде: живое ведро автоотказов не двигается."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from config import config
from dashboard import db as dash_db
from dashboard.config import DashboardConfig
from dashboard.main import build_page_context, templates
from dashboard.queries import Scope, auto_reject_people_count, reserve_counts
from tests._dbtpl import fast_init_db
from tests._paths import REPO_ROOT

DAY = "2026-11-09"
OTHER = "2026-11-08"
VIEWER = {"telegram_id": 1, "bound_city": None}


def _use_tmp_db(tmp_path, name="dashboard_reserve.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    fast_init_db()
    return path


async def _insert(table: str, rows: list[dict]) -> None:
    from database import db as bot_db

    async with bot_db._connect() as conn:
        for row in rows:
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())
            )
        await conn.commit()


def _seed(**tables: list[dict]) -> None:
    async def run():
        for table, rows in tables.items():
            if rows:
                await _insert(table, rows)

    asyncio.run(run())


def _user(tid: int, **overrides) -> dict:
    row = {
        "telegram_id": tid,
        "status": "rejected",
        "registration_date": "2026-11-01 10:00:00",
    }
    row.update(overrides)
    return row


def _log(tid: int, **overrides) -> dict:
    row = {
        "telegram_id": tid,
        "rule_ids": "[1]",
        "reject_texts": "[]",
        "attempt_count": 1,
        "first_triggered_at": "2026-11-01 10:00:00",
        "last_triggered_at": "2026-11-01 10:00:00",
        "returned_to_moderation_at": None,
    }
    row.update(overrides)
    return row


def _invite(tid: int, day: str, city: str | None = "msk") -> dict:
    return {
        "telegram_id": tid,
        "day": day,
        "city": city,
        "invited_by": 1,
        "invited_at": f"{day} 09:00:00",
    }


def _checkin(tid: int, day: str, point: str = "entry") -> dict:
    return {
        "telegram_id": tid,
        "point": point,
        "scanned_at": f"{day} 10:00:00",
        "source": "miniapp",
        "approx_time": 0,
        "created_at": f"{day} 10:00:00",
        "day": day,
    }


def _counts(path: str, scope: Scope | None = None, day: str | None = DAY) -> dict:
    with dash_db.read_conn(path) as conn:
        return reserve_counts(conn, scope or Scope(), day)


def _people(path: str, scope: Scope | None = None) -> int:
    with dash_db.read_conn(path) as conn:
        return auto_reject_people_count(conn, scope or Scope())


def test_invite_does_not_shrink_live_bucket(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[_user(1)],
        auto_reject_log=[_log(1)],
    )
    assert _people(path) == 1
    _seed(reserve_invites=[_invite(1, DAY)])
    assert _people(path) == 1
    assert _counts(path)["invited"] == 1


def test_approval_still_removes_from_live_bucket(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[_user(1, status="approved")],
        auto_reject_log=[_log(1)],
        reserve_invites=[_invite(1, DAY)],
        checkins=[_checkin(1, DAY)],
    )
    assert _people(path) == 0
    counts = _counts(path)
    assert counts["arrived"] == 0
    assert counts["invited"] == 1


def test_invited_counts_scope_day_not_another(tmp_path, monkeypatch):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[_user(1), _user(2)],
        auto_reject_log=[_log(1), _log(2)],
        reserve_invites=[_invite(1, DAY, city=None), _invite(2, OTHER)],
    )

    def _boom():
        raise AssertionError("день экрана уже выбран, Москву заново не считаем")

    monkeypatch.setattr("dashboard.queries.msk_now", _boom)
    counts = _counts(path, day=DAY)
    assert counts["day"] == DAY
    assert counts["invited"] == 1


def test_invited_without_day_uses_dashboard_today(tmp_path, monkeypatch):
    path = _use_tmp_db(tmp_path)
    today = "2026-11-09"
    _seed(
        users=[_user(1), _user(2)],
        reserve_invites=[_invite(1, today), _invite(2, OTHER)],
    )
    monkeypatch.setattr(
        "dashboard.queries.msk_now",
        lambda: datetime(2026, 11, 9, 15, 0, 0),
    )
    counts = _counts(path, day=None)
    assert counts["day"] == today
    assert counts["invited"] == 1


def test_arrived_counts_live_entry_not_invite_only_or_approved(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        users=[
            _user(1),  # живой резерв, вход в день
            _user(2),  # живой резерв, только приглашение
            _user(3, status="approved"),  # звали и пустили, но уже не резерв
            _user(4),  # живой резерв, вход в другой день
            _user(5),  # живой резерв, отметка не на входе
        ],
        auto_reject_log=[_log(1), _log(2), _log(3), _log(4), _log(5)],
        reserve_invites=[
            _invite(1, DAY),
            _invite(2, DAY),
            _invite(3, DAY),
            _invite(4, DAY),
            _invite(5, DAY),
        ],
        checkins=[
            _checkin(1, DAY),
            _checkin(3, DAY),
            _checkin(4, OTHER),
            _checkin(5, DAY, point="session:1"),
        ],
    )
    assert _people(path) == 4  # одобренный выпал
    assert _counts(path)["arrived"] == 1
    assert _counts(path)["invited"] == 5


def test_invited_city_is_empty_or_scope_city_and_same_user_scope(tmp_path):
    path = _use_tmp_db(tmp_path)
    _seed(
        cities=[
            {"code": "msk", "label": "Москва", "enabled": 1, "sort_order": 0,
             "created_at": "2026-01-01 00:00:00"},
            {"code": "spb", "label": "СПб", "enabled": 1, "sort_order": 1,
             "created_at": "2026-01-01 00:00:00"},
        ],
        bot_settings=[
            {"key": "event_city_enabled", "value": "on"},
            {"key": "event_season", "value": "YL26"},
        ],
        users=[
            _user(1, event_city="msk", season="YL26"),
            _user(2, event_city="msk", season="YL26"),
            _user(3, event_city="spb", season="YL26"),
            _user(4, event_city="msk", season="OLD"),
        ],
        reserve_invites=[
            _invite(1, DAY, city="msk"),
            _invite(2, DAY, city=""),
            _invite(3, DAY, city="spb"),
            _invite(4, DAY, city="msk"),
        ],
    )
    assert _counts(path, Scope(city="msk"))["invited"] == 2
    assert _counts(path, Scope(city="spb"))["invited"] == 1
    assert _counts(path, Scope())["invited"] == 3


def test_zeros_when_slice_is_empty(tmp_path):
    path = _use_tmp_db(tmp_path)
    counts = _counts(path)
    assert counts == {"day": DAY, "invited": 0, "arrived": 0}


def _cfg(db_path: str) -> DashboardConfig:
    return DashboardConfig(
        db_path=db_path,
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="bot",
        bot_token="1:test",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )


def _render(path: str) -> str:
    with dash_db.read_conn(path) as conn:
        ctx = build_page_context(conn, _cfg(path), Scope(), VIEWER)
    return templates.env.get_template("dashboard.html").render(**ctx)


def _label_num(html: str, label: str) -> str | None:
    found = re.findall(
        rf'class="num">([^<]*)</div>\s*<div class="lbl">{re.escape(label)}</div>',
        html,
    )
    return found[0] if found else None


def test_page_shows_reserve_zeros_next_to_rules(tmp_path):
    path = _use_tmp_db(tmp_path)
    off = _render(path)
    assert "Позвали сегодня" not in off
    assert "Пришли как резерв" not in off

    _seed(bot_settings=[{"key": "reject_rules_enabled", "value": "on"}])
    html = _render(path)
    assert '<h2 class="block-title">Резерв</h2>' in html
    assert _label_num(html, "Позвали сегодня") == "0"
    assert _label_num(html, "Пришли как резерв") == "0"
    rules_at = html.find("Какое правило сколько отсеяло")
    reserve_at = html.find('<h2 class="block-title">Резерв</h2>')
    # Правил нет — блок разбивки скрыт, срез резерва всё равно на месте.
    assert rules_at == -1
    assert reserve_at != -1


def test_reserve_numbers_are_not_on_forum_template():
    forum = Path(REPO_ROOT, "dashboard", "templates", "forum.html").read_text(encoding="utf-8")
    dashboard = Path(REPO_ROOT, "dashboard", "templates", "dashboard.html").read_text(encoding="utf-8")
    assert "Позвали сегодня" in dashboard
    assert "Пришли как резерв" in dashboard
    assert "Позвали сегодня" not in forum
    assert "Пришли как резерв" not in forum
