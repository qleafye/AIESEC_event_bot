"""Раздел «🤝 Амбассадоры» собран целиком: волны и ступени переехали из «🎮 Геймификации».

При выключенном «🤝 Отборе амбассадоров» раздела в корне нет, и волны со ступенями остаются
достижимы из «🎮 Геймификации» — Юлид после выката не теряет экраны. pytest-asyncio нет —
async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers import admin_sections as sec
from tests._dbtpl import fast_init_db

ADMIN_ID = 1
AMB_ROWS = ("admin_amb_entry", "admin_amb_candidates", "admin_amb_points", "admin_amb_tiers",
            "admin_game_waves", "ambt_excl_list:0", "admin_amb_attach")


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, selection):
    config.DB_PATH = str(tmp_path / "test_amb_section_move_34.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("amb_team_selection_enabled", "on" if selection else "off"))


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_declared_composition():
    amb = [sec.row_callback(r) for r in sec.section_rows("amb")]
    assert amb == list(AMB_ROWS)
    game = [sec.row_callback(r) for r in sec.section_rows("game")]
    assert "admin_game_waves" not in game and "admin_amb_tiers" not in game
    for keep in ("admin_game_tasks", "admin_game_review", "admin_coins_manual",
                 "admin_coins_journal", "admin_game_stats"):
        assert keep in game
    assert "ступени" in sec._SECTION_HINTS["amb"] and "волны" in sec._SECTION_HINTS["amb"]


def test_back_buttons_lead_to_owner_section():
    assert sec.section_of("admin_game_waves") == "amb"
    assert sec.section_of("admin_amb_tiers") == "amb"
    assert sec.back_button("admin_game_waves").callback_data == "admin_sec:amb"


def test_on_section_shows_everything_and_game_has_no_waves(tmp_path):
    _ready(tmp_path, selection=True)
    text, kb = _run(sec.section_screen(ADMIN_ID, "amb"))
    shown = _callbacks(kb)
    assert [c for c in shown if c != "admin_amb_points"][:6] == [c for c in AMB_ROWS if c != "admin_amb_points"]
    _t, game_kb = _run(sec.section_screen(ADMIN_ID, "game"))
    cbs = _callbacks(game_kb)
    assert "admin_game_waves" not in cbs and "admin_amb_tiers" not in cbs
    back = _run(sec.owner_back_button("admin_game_waves"))
    assert back.callback_data == "admin_sec:amb"


def test_off_section_hidden_but_waves_and_tiers_reachable_from_game(tmp_path):
    _ready(tmp_path, selection=False)
    assert _run(sec.section_screen(ADMIN_ID, "amb")) is None
    _t, game_kb = _run(sec.section_screen(ADMIN_ID, "game"))
    cbs = _callbacks(game_kb)
    assert cbs.index("admin_game_tasks") < cbs.index("admin_game_waves") < cbs.index("admin_amb_tiers")
    # строки только те две: вход, кандидаты, баллы в выключенном состоянии не протекают
    for hidden in ("admin_amb_entry", "admin_amb_candidates", "admin_amb_points", "admin_amb_attach"):
        assert hidden not in cbs
    back = _run(sec.owner_back_button("admin_game_waves"))
    assert back.callback_data == "admin_sec:game"


def test_waves_screen_back_follows_toggle(tmp_path):
    from handlers.admin_game_waves import _wave_list_screen
    _ready(tmp_path, selection=False)
    _t, kb = _run(_wave_list_screen(ADMIN_ID))
    assert _callbacks(kb)[-1] == "admin_sec:game"
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    _t, kb = _run(_wave_list_screen(ADMIN_ID))
    assert _callbacks(kb)[-1] == "admin_sec:amb"


def test_moderate_game_holder_sees_both_sections(tmp_path):
    _ready(tmp_path, selection=True)
    kb = _run(__import__("handlers.admin_core", fromlist=["x"]).build_admin_keyboard(ADMIN_ID))
    flat = _callbacks(kb)
    assert "admin_sec:game" in flat and "admin_sec:amb" in flat
