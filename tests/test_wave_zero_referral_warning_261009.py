"""Волна при «🤝 Баллы за приглашённого» = 0 (владелец 09.10): карточка волны и экран
«▶️ Запустить волну» предупреждают, что приглашённые в рейтинг не попадут, и дают кнопку
«💰 Задать баллы» на экран баллов. Поведение рейтинга не меняется."""
import asyncio

from config import config
from database import db
from handlers import admin_game_waves as w
from tests._dbtpl import fast_init_db
from tests.test_wave_activate_guard_wr16_260922 import ADMIN_ID, FakeCallback, _new_state

WARN = "Баллов за приглашённого — 0"


def _run(coro):
    return asyncio.run(coro)


def _wave(tmp_path):
    config.DB_PATH = str(tmp_path / "wave_zero_referral.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    wid = _run(db.create_wave("2099-10-01 00:00:00", "2099-10-15 23:59:59", created_by=ADMIN_ID))
    _run(db.create_task("Задание", "Light", 10, "text", "2099-10-15 23:59:59", ADMIN_ID, wave_id=wid))
    return wid


def _callbacks(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def test_card_warns_and_links_to_points_when_zero(tmp_path):
    wid = _wave(tmp_path)
    text, kb = _run(w._wave_card_screen(ADMIN_ID, _run(db.get_wave(wid))))
    assert WARN in text
    assert ("💰 Задать баллы", "admin_amb_points") in _callbacks(kb)


def test_card_silent_when_coins_set(tmp_path):
    wid = _wave(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    text, kb = _run(w._wave_card_screen(ADMIN_ID, _run(db.get_wave(wid))))
    assert WARN not in text
    assert "admin_amb_points" not in [cb for _, cb in _callbacks(kb)]


def test_activate_confirm_warns_but_still_allows_start(tmp_path):
    wid = _wave(tmp_path)
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert WARN in cb.message.edits[-1]
    _run(db.set_setting("ambassador_referral_coins", "50"))
    cb = FakeCallback(f"waveactivate:{wid}")
    _run(w.wave_activate_confirm(cb, _new_state()))
    assert WARN not in cb.message.edits[-1]
