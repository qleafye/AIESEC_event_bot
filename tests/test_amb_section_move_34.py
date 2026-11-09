"""Раздел «🤝 Амбассадоры» собран целиком: волны и ступени живут в нём.

Раздел в корне и при выключенном отборе. Выключенные команда и ступени свёрнуты в одну
строку, волны и баллы остаются в разделе. pytest-asyncio нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.settings import admin_sections as sec
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
    cbs = _callbacks(kb)
    # Пульт сверху. Ступени по умолчанию выключены — строка экрана свёрнута.
    assert cbs[:5] == [
        "amb_addon_ask:offer", "amb_addon_ask:team", "amb_addon_ask:steps",
        "amb_addon_ask:stats", "admin_game_waves",
    ]
    assert cbs[5:15] == [
        "amb_sep", "admin_amb_points", "ambt_excl_list:0", "admin_amb_attach",
        "amb_sep", "admin_amb_entry", "admin_amb_candidates", "admin_game_waves",
        "amb_sep", "amb_off_note",
    ]
    assert "admin_amb_tiers" not in cbs
    assert "settings_group:amb" not in cbs
    assert set(AMB_ROWS) - {"admin_amb_tiers"} <= set(cbs)
    seps = [b.text for row in kb.inline_keyboard for b in row if b.callback_data == "amb_sep"]
    assert seps == ["── Общее ──", "── Команда и волны ──", "── Ступени ──"]
    _t, game_kb = _run(sec.section_screen(ADMIN_ID, "game"))
    cbs = _callbacks(game_kb)
    assert "admin_game_waves" not in cbs and "admin_amb_tiers" not in cbs
    back = _run(sec.owner_back_button("admin_game_waves"))
    assert back.callback_data == "admin_sec:amb"


def test_off_section_stays_and_collapses_team(tmp_path):
    _ready(tmp_path, selection=False)
    screen = _run(sec.section_screen(ADMIN_ID, "amb"))
    assert screen is not None
    _t, kb = screen
    cbs = _callbacks(kb)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "admin_amb_entry" not in cbs and "admin_amb_candidates" not in cbs
    assert "admin_game_waves" in cbs
    assert "admin_amb_points" in cbs and "ambt_excl_list:0" in cbs and "admin_amb_attach" in cbs
    assert any(t.startswith("Выключено: команда с отбором") for t in texts)
    assert any(t.startswith("Выключено: ступени") for t in texts)
    _t, game_kb = _run(sec.section_screen(ADMIN_ID, "game"))
    game_cbs = _callbacks(game_kb)
    assert "admin_game_waves" not in game_cbs and "admin_amb_tiers" not in game_cbs
    assert "admin_amb_points" not in game_cbs
    assert _run(sec.owner_back_button("admin_amb_points")).callback_data == "admin_sec:amb"
    assert _run(sec.owner_back_button("admin_game_waves")).callback_data == "admin_sec:amb"


def test_waves_screen_back_follows_toggle(tmp_path):
    from handlers.game.admin_game_waves import _wave_list_screen
    _ready(tmp_path, selection=False)
    _t, kb = _run(_wave_list_screen(ADMIN_ID))
    assert _callbacks(kb)[-1] == "admin_sec:amb"
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    _t, kb = _run(_wave_list_screen(ADMIN_ID))
    assert _callbacks(kb)[-1] == "admin_sec:amb"


def test_moderate_game_holder_sees_both_sections(tmp_path):
    _ready(tmp_path, selection=True)
    kb = _run(__import__("handlers.settings.admin_core", fromlist=["x"]).build_admin_keyboard(ADMIN_ID))
    flat = _callbacks(kb)
    assert "admin_sec:game" in flat and "admin_sec:amb" in flat


# ── экран «💰 Баллы и приватность» ───────────────────────────────────────────────────────────

class _User:
    def __init__(self, uid=ADMIN_ID):
        self.id = uid


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.from_user = _User()
        self.edits, self.sent = [], []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))

    async def answer(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))


class _Cb:
    def __init__(self, data):
        self.data = data
        self.from_user = _User()
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def test_points_caps_resolve():
    from handlers.access.admin_caps import required_capability
    for data in ("admin_amb_points", "ambpt_coins", "ambpt_coins_cancel", "ambpt_toggle:hide",
                 "ambpt_toggle:wavenames", "state:AmbPointsEdit:waiting_for_value"):
        assert required_capability(callback_data=data) == "moderate_game", data


def test_points_screen_text_and_buttons(tmp_path):
    from handlers.amb import admin_amb_points as h
    _ready(tmp_path, selection=True)
    _run(db.set_setting("ambassador_referral_coins", "10"))
    text, kb = _run(h.render_points_screen())
    assert "Баллов за одобренного приглашённого: <b>10 (0 — выключено)</b>" in text
    assert "и в общий зачёт, и в текущую волну" in text
    assert "Скрывать имена приглашённых: <b>нет</b>" in text
    assert "Имена в рейтинге волны: <b>да</b>" in text
    assert _callbacks(kb) == ["ambpt_coins", "ambpt_toggle:hide", "ambpt_toggle:wavenames",
                              "ambpt_fill", "admin_sec:amb"]


def test_points_input_validation_and_save(tmp_path, caplog):
    import logging
    from handlers.amb import admin_amb_points as h
    from handlers.states import AmbPointsEdit
    _ready(tmp_path, selection=True)
    state = _state()
    cb = _Cb("ambpt_coins")
    _run(h.amb_points_start(cb, state))
    assert _run(state.get_state()) == AmbPointsEdit.waiting_for_value.state
    assert "например <code>10</code>" in cb.message.sent[-1][0]

    for body, expected in (("abc", "Не понял. Пришлите целое число, например 10, или 0."),
                           ("-5", "Баллы не могут быть меньше нуля. Пришлите 0 или больше.")):
        msg = _Msg(body)
        _run(h.amb_points_value(msg, state))
        assert msg.sent[-1][0] == expected
        assert _run(state.get_state()) == AmbPointsEdit.waiting_for_value.state

    with caplog.at_level(logging.INFO):
        msg = _Msg("25")
        _run(h.amb_points_value(msg, state))
    assert _run(db.get_setting("ambassador_referral_coins")) == "25"
    assert "admin=1 ambassador_referral_coins=25" in caplog.text
    assert msg.sent[-1][0].startswith("✅ Сохранено")
    assert _run(state.get_state()) is None


def test_points_toggles_flip_shared_settings(tmp_path):
    from handlers.amb import admin_amb_points as h
    _ready(tmp_path, selection=True)
    cb = _Cb("ambpt_toggle:hide")
    _run(h.amb_points_toggle(cb))
    assert _run(db.get_setting("amb_hide_invitee_names")) == "on"
    assert cb.answers[-1][1] is True and len(cb.answers[-1][0]) <= 200
    cb = _Cb("ambpt_toggle:wavenames")
    _run(h.amb_points_toggle(cb))
    assert _run(db.get_setting("wave_rating_show_names")) == "off"
    assert len(cb.answers[-1][0]) <= 200
    cb = _Cb("ambpt_toggle:zzz")
    _run(h.amb_points_toggle(cb))
    assert "устарела" in cb.answers[-1][0]


def test_points_stale_button_when_module_off(tmp_path):
    from handlers.amb import admin_amb_section as sect
    _ready(tmp_path, selection=False)
    assert not sect.is_section_callback("admin_amb_points")
    assert not sect.is_section_callback("ambpt_toggle:hide")
    assert _run(sect._section_off(_Cb("admin_amb_points"))) is False
    assert _run(sect._section_off(_Cb("admin_amb_entry"))) is True
