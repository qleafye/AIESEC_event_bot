"""Phase 15 Plan 01 (D-10/D-18): 📊 Статистика scoped by the manager's `staff.city` binding
+ the «🌐 Открыть дашборд» button.

Regression-critical: D-10 narrows `render_stats_text` by the manager's BOUND city
(`staff.city`, set once by an admin), NOT by the shapka's own selected-city toggle
(`_admin_city_scope`/`admin_selected_city`, 07.2). That toggle must remain provably
inert on this screen (test below).

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_city_export_stats_phase72.py / tests/test_manager_city_091.py.
"""
import asyncio
import re

from config import config
from database import db
from handlers import admin as admin_mod
import cities


ADMIN_ID = 930201
MANAGER_ID = 930202
UNBOUND_MANAGER_ID = 930203


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_stats_city_scope.db")


def _admin_ready(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _seed_city(telegram_id, event_city, status="pending", university="MGU"):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "university": university,
        "registration_date": f"2026-01-01 09:{telegram_id % 60:02d}:00",
        "event_city": event_city,
    }))
    asyncio.run(db.set_user_status(telegram_id, status))


def _seed_three_cities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, None)
    _seed_city(2, "msk")
    _seed_city(3, "spb")
    _seed_city(4, "spb")
    _seed_city(5, "tyumen")


def _bind_manager(uid, city, role="reg_manager"):
    asyncio.run(db.add_staff(uid, role, ADMIN_ID))
    asyncio.run(db.set_staff_city(uid, city))


# ── admin_id=None / no binding parity ────────────────────────────────────────────────────

def test_render_stats_text_no_admin_id_unchanged(tmp_path):
    _seed_three_cities(tmp_path)
    text_default = asyncio.run(admin_mod.render_stats_text())
    text_explicit_none = asyncio.run(admin_mod.render_stats_text(None))
    assert text_default == text_explicit_none
    assert "🏙 <b>По городам:</b>" in text_default


def test_render_stats_text_unbound_manager_sees_all_cities(tmp_path):
    _seed_three_cities(tmp_path)
    _bind_manager(UNBOUND_MANAGER_ID, None)
    text_unbound = asyncio.run(admin_mod.render_stats_text(UNBOUND_MANAGER_ID))
    text_none = asyncio.run(admin_mod.render_stats_text())
    assert text_unbound == text_none


def test_render_stats_text_superadmin_never_narrowed_even_with_binding(tmp_path):
    """D-12 convention (mirrors capability_holders): a superadmin's own screen is never
    narrowed, even in the edge case where they also happen to carry a staff.city binding."""
    _seed_three_cities(tmp_path)
    _bind_manager(ADMIN_ID, "spb")
    text_admin = asyncio.run(admin_mod.render_stats_text(ADMIN_ID))
    text_none = asyncio.run(admin_mod.render_stats_text())
    assert text_admin == text_none


# ── Bound manager: one city row, narrowed totals ─────────────────────────────────────────

def test_render_stats_text_bound_manager_sees_only_own_city_row(tmp_path):
    _seed_three_cities(tmp_path)
    _bind_manager(MANAGER_ID, "spb")
    text = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))

    spb_label = asyncio.run(cities.city_label("spb"))
    msk_label = asyncio.run(cities.city_label("msk"))
    tyumen_label = asyncio.run(cities.city_label("tyumen"))

    assert "🏙 <b>По городам:</b>" in text
    assert f"• {spb_label} —" in text
    assert msk_label not in text
    assert tyumen_label not in text
    assert "Итого" not in text  # D-10: single row, no duplicate total line


def test_render_stats_text_bound_manager_narrows_total_and_universities(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed_city(1, "spb", university="SPBGU")
    _seed_city(2, "spb", university="SPBGU")
    _seed_city(3, "msk", university="MGU")
    _bind_manager(MANAGER_ID, "spb")

    text = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    assert "Всего регистраций: 2" in text
    assert "SPBGU" in text
    assert "MGU" not in text


def test_render_stats_text_bound_manager_header_shows_city_label(tmp_path):
    _seed_three_cities(tmp_path)
    _bind_manager(MANAGER_ID, "spb")
    text = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    spb_label = asyncio.run(cities.city_label("spb"))
    assert f"📊 <b>Статистика — {spb_label}:</b>" in text


def test_render_stats_text_bound_manager_default_city_collapses_null_and_garbage(tmp_path):
    """Default-city binding must catch NULL/unknown-code rows too, same collapse rule the
    unscoped block already applies (WR-06)."""
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    default_code = cities.default_city_code()
    _seed_city(1, None)
    _seed_city(2, "__garbage__")
    _seed_city(3, "spb")
    _bind_manager(MANAGER_ID, default_code)

    text = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    m = re.search(r"всего (\d+)", text)
    assert m is not None
    assert int(m.group(1)) == 2


# ── Shapka toggle must stay inert on this screen ─────────────────────────────────────────

def test_render_stats_text_selected_admin_city_does_not_affect_bound_manager_view(tmp_path):
    """Regression against accidentally rewiring this screen onto `_admin_city_scope` —
    D-10 scopes by staff.city binding, never by the shapka's own selected-city toggle."""
    _seed_three_cities(tmp_path)
    _bind_manager(MANAGER_ID, "spb")
    asyncio.run(cities.set_admin_city(MANAGER_ID, "msk"))
    text_with_msk_selected = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    asyncio.run(cities.set_admin_city(MANAGER_ID, "tyumen"))
    text_with_tyumen_selected = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    assert text_with_msk_selected == text_with_tyumen_selected

    spb_label = asyncio.run(cities.city_label("spb"))
    assert f"• {spb_label} —" in text_with_msk_selected


def test_admin_city_scope_grep_count_did_not_grow():
    """Acceptance criterion from 15-01-PLAN.md: the shapka toggle helper is not newly wired
    into render_stats_text (its reference count in the source must not have grown)."""
    import inspect
    src = inspect.getsource(admin_mod.render_stats_text)
    assert "_admin_city_scope" not in src


# ── Module-off parity ─────────────────────────────────────────────────────────────────────

def test_render_stats_text_module_off_bound_manager_still_gets_unscoped_text(tmp_path):
    """cities_module_on() == False -> D-10 scoping never engages, byte-identical to today
    even if a staff.city value happens to be set."""
    _admin_ready(tmp_path)
    _seed_city(1, "spb")
    _seed_city(2, "msk")
    _bind_manager(MANAGER_ID, "spb")
    text_manager = asyncio.run(admin_mod.render_stats_text(MANAGER_ID))
    text_none = asyncio.run(admin_mod.render_stats_text())
    assert text_manager == text_none
    assert "По городам" not in text_manager


# ── D-18: «🌐 Открыть дашборд» button ─────────────────────────────────────────────────────

def test_stats_keyboard_no_button_when_dashboard_url_empty(tmp_path):
    _admin_ready(tmp_path)
    config.DASHBOARD_PUBLIC_URL = ""
    kb = asyncio.run(admin_mod._stats_keyboard_for(ADMIN_ID))
    all_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "🌐 Открыть дашборд" not in all_texts


def test_stats_keyboard_button_appears_with_correct_url(tmp_path):
    _admin_ready(tmp_path)
    config.DASHBOARD_PUBLIC_URL = "https://yl26.alekseev.info"
    try:
        kb = asyncio.run(admin_mod._stats_keyboard_for(ADMIN_ID))
        first_row = kb.inline_keyboard[0]
        assert len(first_row) == 1
        assert first_row[0].text == "🌐 Открыть дашборд"
        assert first_row[0].url == "https://yl26.alekseev.info"
    finally:
        config.DASHBOARD_PUBLIC_URL = ""


def test_cmd_stats_and_show_admin_stats_use_stats_keyboard(tmp_path):
    _admin_ready(tmp_path)
    config.DASHBOARD_PUBLIC_URL = "https://yl26.alekseev.info"
    try:
        class FakeUser:
            def __init__(self, uid):
                self.id = uid

        class FakeAnswerMessage:
            def __init__(self, uid):
                self.from_user = FakeUser(uid)
                self.sent = []
                self.markups = []

            async def answer(self, text, parse_mode=None, reply_markup=None):
                self.sent.append(text)
                self.markups.append(reply_markup)

        msg = FakeAnswerMessage(ADMIN_ID)
        asyncio.run(admin_mod.cmd_stats(msg))
        assert msg.markups[-1] is not None
        texts = [btn.text for row in msg.markups[-1].inline_keyboard for btn in row]
        assert "🌐 Открыть дашборд" in texts
    finally:
        config.DASHBOARD_PUBLIC_URL = ""
