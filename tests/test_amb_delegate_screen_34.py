"""Единый экран «Моя ссылка»: состав (`services/amb_screen.delegate_view`) и бот.

pytest-asyncio в окружении нет — async через `asyncio.run()`, БД — `fast_init_db`.
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_screen, amb_tiers
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
AT = "2026-09-30 12:00:00"
BOT = "TestBot"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_delegate_screen_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def _noop(tid):
        return None

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _noop)


def _seed(tid, *, active=False, slot=False):
    _run(db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}", "season": SEASON,
    }))
    _run(db.set_user_status(tid, "approved"))
    if active:
        _run(sdb.set_status(tid, "active", at=AT))
    if slot:
        assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))


def _limit(n):
    _run(db.set_setting("amb_slots_limit", str(n)))


def _view(tid):
    return _run(amb_screen.delegate_view(tid))


# ── сервис: статус ─────────────────────────────────────────────────────────────────────────

def test_pack_with_limit(ready):
    _limit(17)
    _seed(10, active=True, slot=True)
    v = _view(10)
    assert (v["state"], v["status_key"], v["is_ambassador"]) == ("active_pack", "amb_status_pack_text", True)


def test_no_pack_with_limit(ready):
    _limit(17)
    _seed(10, active=True)
    v = _view(10)
    assert (v["state"], v["status_key"]) == ("active_no_pack", "amb_status_no_pack_text")


def test_no_limit_no_pack_line(ready):
    _limit(0)
    _seed(10, active=True, slot=True)
    _seed(11, active=True)
    assert _view(10)["status_key"] is None
    assert _view(11)["status_key"] is None


def test_candidate_full_declined_open(ready):
    _limit(1)
    _run(db.set_setting("amb_join_mode", "selection"))
    _seed(10)
    assert _view(10)["status_key"] is None  # open: кнопка
    _seed(11)
    _run(sdb.set_status(11, "candidate", at=AT))
    assert _view(11)["status_key"] == "amb_status_candidate_text"
    _seed(12)
    _run(sdb.set_status(12, "declined", at=AT))
    assert _view(12)["status_key"] == "amb_slots_full_text"
    _seed(13, active=True, slot=True)
    assert _view(10)["status_key"] == "amb_slots_full_text"  # места заняты


def test_module_off_is_quiet(ready):
    _run(db.set_setting("amb_team_selection_enabled", "off"))
    _limit(17)
    _seed(10, active=True, slot=True)
    v = _view(10)
    assert (v["status_key"], v["referral_points"], v["wave_place"]) == (None, None, None)


# ── сервис: баллы ──────────────────────────────────────────────────────────────────────────

def test_points_sum_with_reversal(ready):
    _seed(10, active=True)
    _run(db.add_coins(10, 30, "за приглашённого", None, "referral"))
    _run(db.add_coins(10, 10, "за приглашённого", None, "referral"))
    _run(db.add_coins(10, -10, "снят", None, "referral_reversal"))
    _run(db.add_coins(10, 99, "задание", None, "task"))
    assert _view(10)["referral_points"] == 30


def test_points_none_when_not_awarded(ready):
    _run(db.set_setting("ambassador_referral_coins", "0"))
    _seed(10, active=True)
    assert _view(10)["referral_points"] is None
    _run(db.add_coins(10, 5, "x", None, "referral"))
    assert _view(10)["referral_points"] == 5


# ── сервис: волна ──────────────────────────────────────────────────────────────────────────

def _patch_wave(monkeypatch, *, wave, own):
    from services import ambassador_waves as w

    async def _cur(city):
        return wave

    async def _rv(wid, viewer):
        return {"own": own, "rows": [], "prize_places": 3}

    monkeypatch.setattr(w, "current_wave_for_city_raw", _cur)
    monkeypatch.setattr(w, "wave_rating_view", _rv)


def test_wave_place(ready, monkeypatch):
    _seed(10, active=True)
    _patch_wave(monkeypatch, wave={"id": 1, "number": 2, "starts_at": "2026-10-05 00:00:00"},
                own={"place": 3, "total": 12, "points": 5, "gap_to_prize": 0})
    wp = _view(10)["wave_place"]
    assert (wp["wave"], wp["place"], wp["total"]) == ("Волна 2", 3, 12)


def test_wave_absent_or_not_participant(ready, monkeypatch):
    _seed(10, active=True)
    _patch_wave(monkeypatch, wave=None, own=None)
    assert _view(10)["wave_place"] is None
    _patch_wave(monkeypatch, wave={"id": 1, "number": 2, "starts_at": "2026-09-01 00:00:00"}, own=None)
    assert _view(10)["wave_place"] is None


def test_wave_after_join_not_eligible(ready, monkeypatch):
    _seed(10, active=True)  # ambassador_since = AT, позже старта волны
    _patch_wave(monkeypatch, wave={"id": 1, "number": 2, "starts_at": "2026-09-01 00:00:00"},
                own={"place": 1, "total": 1, "points": 0, "gap_to_prize": 0})
    assert _view(10)["wave_place"] is None


def test_fail_soft_parts(ready, monkeypatch):
    _limit(17)
    _seed(10, active=True, slot=True)

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(amb_screen, "referral_points_sum", _boom)
    from services import ambassador_waves as w
    monkeypatch.setattr(w, "current_wave_for_city_raw", _boom)
    v = _view(10)
    assert v["status_key"] == "amb_status_pack_text"
    assert v["referral_points"] is None and v["wave_place"] is None


# ── реестр ─────────────────────────────────────────────────────────────────────────────────

def test_registry_keys():
    for key in ("amb_status_pack_text", "amb_status_no_pack_text",
                "amb_referral_points_text", "amb_wave_place_text"):
        assert SETTINGS_SCHEMA[key]["group"] == "amb"
        assert "SkillUp" not in SETTINGS_SCHEMA[key]["default"]


# ── бот ────────────────────────────────────────────────────────────────────────────────────

class _Me:
    username = BOT


class _Bot:
    async def get_me(self):
        return _Me()


def _screen(uid, lang="ru"):
    from handlers import user_actions as ua
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from services.i18n import src_hash
    tr_map = {src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()} if lang == "en" else {}
    return _run(ua._referral_screen(uid, _Bot(), lang, tr_map))


def test_bot_full_screen_order(ready, monkeypatch):
    _limit(17)
    _seed(10, active=True, slot=True)
    _run(db.add_coins(10, 30, "за приглашённого", None, "referral"))
    _patch_wave(monkeypatch, wave={"id": 1, "number": 2, "starts_at": "2026-10-01 00:00:00"},
                own={"place": 3, "total": 12, "points": 30, "gap_to_prize": 0})
    # since раньше старта волны — участвует
    monkeypatch.setattr(
        "services.ambassador_waves.wave_eligible", lambda user, wave: True)
    text, kb = _screen(10)
    pos = [text.index(s) for s in (
        f"start=amb_10", "пакет амбассадора за тобой", "Баллы за приглашённых: 30",
        "Волна 2: ты на 3 месте из 12")]
    assert pos == sorted(pos)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ambleave" in datas and "ambpath:invite" in datas


def test_bot_no_limit_has_no_pack_line(ready):
    _limit(0)
    _seed(10, active=True, slot=True)
    text, _kb = _screen(10)
    assert "пакет" not in text.lower()


def test_bot_module_off_as_before(ready):
    _run(db.set_setting("amb_team_selection_enabled", "off"))
    _limit(17)
    _seed(10, active=True, slot=True)
    _run(db.add_coins(10, 30, "x", None, "referral"))
    text, _kb = _screen(10)
    assert "пакет" not in text.lower() and "Баллы за приглашённых" not in text


def test_bot_non_ambassador_unchanged(ready):
    _seed(11)
    _run(sdb.set_status(11, "candidate", at=AT))
    _run(db.set_setting("amb_join_mode", "selection"))
    text, kb = _screen(11)
    assert "рассматривается" in text and kb is None


def test_bot_english_lines(ready, monkeypatch):
    _limit(17)
    _seed(10, active=True, slot=True)
    _run(db.add_coins(10, 30, "x", None, "referral"))
    _patch_wave(monkeypatch, wave={"id": 1, "number": 2, "starts_at": "2026-10-01 00:00:00"},
                own={"place": 3, "total": 12, "points": 30, "gap_to_prize": 0})
    monkeypatch.setattr("services.ambassador_waves.wave_eligible", lambda user, wave: True)
    text, _kb = _screen(10, "en")
    assert "ambassador pack is yours" in text
    assert "Points for invitees: 30" in text
    assert "Волна" not in text and "place 3 of 12" in text
