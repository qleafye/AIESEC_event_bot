"""Квик 12.09 (UI-аудит, пункты 5, 6) — воронка дашборда не врёт нулями без трекинга,
пустые KPI объяснены видимым текстом (не только hover-подсказкой).

Пункт 5: первые три ступени воронки (`_FUNNEL_EVENT_STAGES`) считаются по событийному
трекингу (`reg_events`); остальные — по состоянию записи (`users.status`/`payment_status`).
У делегатов, импортированных мимо анкеты, событий нет — раньше это давало «Дошли до
конца: 0» рядом с ненулевой «На модерации», менеджер читал это как «все потерялись».
`step.untracked` вместо этого честно говорит «нет данных трекинга».

Пункт 6: `kpi.conversion is none` и три «средних времени» — раньше hover-only `title`
(тач-недоступность), теперь видимая строка `.delta`.

Хелперы — `tests/test_dashboard_routes.py` (`_use_tmp_db`/`_seed`/`_cfg`/`_client`/`_login`).
"""
from __future__ import annotations

import asyncio

from database import db as bot_db

from dashboard.main import _funnel_display

from tests.test_dashboard_routes import ADMIN_ID, _cfg, _client, _login, _use_tmp_db


def _run(coro):
    return asyncio.run(coro)


def _insert_user(**columns):
    cols = ", ".join(columns.keys())
    placeholders = ", ".join("?" for _ in columns)

    async def go():
        async with bot_db._connect() as conn:
            await conn.execute(f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(columns.values()))
            await conn.commit()

    _run(go())


# ── юнит: dashboard.main._funnel_display ──────────────────────────────────────────────────

def test_untracked_true_for_zero_event_stages_when_status_stages_nonzero():
    rows = [
        ("Зашли", 0), ("Начали анкету", 0), ("Дошли до конца", 0),
        ("На модерации", 4), ("Одобрено", 3),
    ]
    display = _funnel_display(rows)
    by_label = {s["label"]: s for s in display["steps"]}
    assert by_label["Зашли"]["untracked"] is True
    assert by_label["Начали анкету"]["untracked"] is True
    assert by_label["Дошли до конца"]["untracked"] is True
    assert by_label["На модерации"]["untracked"] is False
    assert by_label["Одобрено"]["untracked"] is False


def test_untracked_false_everywhere_when_event_stages_nonzero():
    rows = [
        ("Зашли", 10), ("Начали анкету", 5), ("Дошли до конца", 2),
        ("На модерации", 4), ("Одобрено", 3),
    ]
    display = _funnel_display(rows)
    assert all(not s["untracked"] for s in display["steps"])


def test_untracked_false_when_funnel_entirely_zero():
    """Полностью нулевая воронка — противоречить нечему, работает прежняя заглушка
    has_data (страница показывает «Появится после первых входов», не отдельные плашки)."""
    rows = [
        ("Зашли", 0), ("Начали анкету", 0), ("Дошли до конца", 0),
        ("На модерации", 0), ("Одобрено", 0),
    ]
    display = _funnel_display(rows)
    assert all(not s["untracked"] for s in display["steps"])
    assert display["has_data"] is False


# ── роутерный: страница дашборда ────────────────────────────────────────────────────────

def test_dashboard_page_shows_untracked_note_not_zero_percent(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _insert_user(
        telegram_id=700001, status="pending", registration_date="2026-01-05 10:00:00",
    )
    client = _client(_cfg(db_path))
    _login(client, ADMIN_ID)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "нет данных трекинга" in resp.text
    # Событийные ступени не показывают «0% от …» — это и есть невозможная последовательность
    # из аудита («Дошли до конца: 0%» рядом с ненулевой «На модерации»). Легитимный «0% от
    # отправленных на модерацию» у статусной ступени «Одобрено» (реально ноль одобренных) —
    # не про это, его быть не должно только у событийных подписей ниже.
    for baseline_label in ("зашедших", "начавших анкету", "дошедших до конца"):
        assert f"% от {baseline_label}" not in resp.text


def test_dashboard_page_shows_visible_empty_kpi_labels_not_only_title_attr(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    _login(client, ADMIN_ID)

    resp = client.get("/")
    assert resp.status_code == 200
    # Пункт 6: видимые подписи пустого состояния — не title="…" (hover-only). Решение
    # владельца 23.09 (DASHBOARD-IA-PROPOSAL-260923): конверсия переехала из плитки KPI в
    # заголовок воронки, подпись пустого состояния — рядом с заголовком, тем же текстом.
    assert "Конверсия появится, когда бот начнёт отслеживать входы" in resp.text
    assert 'title="Конверсия появится, когда бот начнёт отслеживать входы"' not in resp.text
    assert "Появится после первых решений по заявкам" in resp.text
