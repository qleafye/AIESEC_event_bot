"""Счётчики «📇 Список заявок» и /stats считают только текущий сезон (`event_season`);
прошлые сезоны — отдельно. Сезон не задан — как раньше, все строки.
Предикат тот же, что у `_approved_current_season_frag`: season IS NULL OR season = текущий."""
import asyncio

from database import db
from handlers import admin as admin_mod
from handlers.applications import admin_app_list
from tests.test_applications_list_260914 import _add
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_roles_phase8 import ADMIN_ID, _roles_ready


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """2 текущих (один с сезоном, один без), 3 прошлых одобренных, 1 прошлый отклонённый."""
    await db.set_setting("event_season", "YL 26/2")
    rows = [
        (1, "approved", "YL 26/2"), (2, "approved", None),
        (3, "approved", "YL 26/1"), (4, "approved", "YL 26/1"), (5, "approved", "YL 26/1"),
        (6, "rejected", "YL 26/1"), (7, "pending", "YL 26/2"),
    ]
    for tid, status, season in rows:
        await _add(tid, name=f"Дел{tid}", username=f"@d{tid}", status=status,
                   approved_at="2026-09-01 10:00:00", rejected_at="2026-09-01 10:00:00")
        async with db._connect() as conn:
            await conn.execute("UPDATE users SET season = ? WHERE telegram_id = ?", (season, tid))
            await conn.commit()


def test_count_applications_current_season_only(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        assert await db.count_applications(season="YL 26/2") == {"approved": 2, "rejected": 0, "pending": 1}
        assert await db.count_applications() == {"approved": 5, "rejected": 1, "pending": 1}

    _run(go())


def test_list_applications_page_filters_by_season(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        cur = await db.list_applications_page(status="approved", season="YL 26/2")
        assert {r["telegram_id"] for r in cur} == {1, 2}
        allrows = await db.list_applications_page(status="approved")
        assert len(allrows) == 5

    _run(go())


def test_screen_default_current_season_with_header_and_toggle(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        text, kb = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "Сезон: YL 26/2" in text
        assert "✅ 2 · ❌ 0 · ⏳ 1" in text
        buttons = [b for row in kb.inline_keyboard for b in row]
        texts = [b.text for b in buttons]
        assert "✅ 🗓 Только текущий сезон" in texts
        assert "Все сезоны" in texts
        assert "apl:approved:0:all" in [b.callback_data for b in buttons]

    _run(go())


def test_screen_all_seasons_keeps_choice_in_callbacks(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        text, kb = await admin_app_list.render_app_list_screen(ADMIN_ID, all_seasons=True)
        assert "Сезон: все сезоны" in text
        assert "✅ 5 · ❌ 1 · ⏳ 1" in text
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert "✅ Все сезоны" in [b.text for b in buttons]
        assert "apl:rejected:0:all" in [b.callback_data for b in buttons]

    _run(go())


def test_callback_all_suffix_renders_all_seasons(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        cb = FakeCallback("apl:approved:0:all", user_id=ADMIN_ID)
        await admin_app_list.apl_page(cb)
        assert "Сезон: все сезоны" in cb.message.text
        assert "✅ 5 · ❌ 1 · ⏳ 1" in cb.message.text

    _run(go())


def test_no_season_setting_no_filter_no_toggle(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(1, status="approved", approved_at="2026-09-01 10:00:00")
        text, kb = await admin_app_list.render_app_list_screen(ADMIN_ID)
        assert "Сезон:" not in text
        assert "✅ 1 · ❌ 0 · ⏳ 0" in text
        assert not any("сезон" in b.text.lower() for row in kb.inline_keyboard for b in row)

    _run(go())


def test_stats_current_season_and_past_line(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _seed()
        text = await admin_mod.render_stats_text()
        assert "Всего регистраций: 3" in text
        assert "Прошлые сезоны: 4" in text

    _run(go())


def test_stats_no_past_line_without_past_rows_or_season(tmp_path):
    _roles_ready(tmp_path)

    async def go():
        await _add(1, status="approved", approved_at="2026-09-01 10:00:00")
        text = await admin_mod.render_stats_text()
        assert "Всего регистраций: 1" in text
        assert "Прошлые сезоны" not in text
        await db.set_setting("event_season", "YL 26/2")
        text = await admin_mod.render_stats_text()
        assert "Прошлые сезоны" not in text

    _run(go())
