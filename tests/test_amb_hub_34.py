"""Хаб Mini App на `services.amb_screen.delegate_view` (паритет с «Моя ссылка» в боте) и
счётчики амбассадоров на дашборде. Харнесс — как у `tests/test_miniapp_referral_hub_260917.py`."""
from __future__ import annotations

import asyncio

import pytest

from services import amb_screen
from settings_schema import SETTINGS_SCHEMA
from tests.test_miniapp_routes import (
    DELEGATE_ID, _cfg, _client, _hdr, _set, _standard_seed, _use_tmp_db,
)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "amb_hub_34.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _patch_view(monkeypatch, view):
    async def _dv(tid):
        if isinstance(view, Exception):
            raise view
        return view
    monkeypatch.setattr(amb_screen, "delegate_view", _dv)


def _ref(client):
    return client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()["referral"]


def _view(**kw):
    base = {"state": "open", "status_key": None, "referral_points": None,
            "wave_place": None, "is_ambassador": False}
    base.update(kw)
    return base


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def test_ambassador_full_block(client, monkeypatch):
    _patch_view(monkeypatch, _view(
        state="active_pack", status_key="amb_status_pack_text", referral_points=7, is_ambassador=True,
        wave_place={"wave": "Волна 2", "number": 2, "place": 3, "total": 40}))
    r = _ref(client)
    assert r["status_text"] == _default("amb_status_pack_text")
    assert "7" in r["points_text"]
    assert "3" in r["wave_text"] and "40" in r["wave_text"]
    assert r["invites_text"]


def test_candidate_and_full(client, monkeypatch):
    _patch_view(monkeypatch, _view(state="candidate", status_key="amb_status_candidate_text"))
    r = _ref(client)
    assert r["status_text"] == _default("amb_status_candidate_text")
    assert r["points_text"] is None and r["wave_text"] is None
    _patch_view(monkeypatch, _view(state="full", status_key="amb_slots_full_text"))
    assert _ref(client)["status_text"] == _default("amb_slots_full_text")


def test_no_limit_no_status(client, monkeypatch):
    _patch_view(monkeypatch, _view(state="active_pack", is_ambassador=True))
    assert _ref(client)["status_text"] is None


def test_fail_soft(client, monkeypatch):
    _patch_view(monkeypatch, RuntimeError("boom"))
    r = _ref(client)
    assert r["status_text"] is None and r["points_text"] is None and r["wave_text"] is None
    assert r["link"] and r["label"] and r["invites_text"]


def test_module_off_same_as_before(client):
    r = _ref(client)
    assert r["status_text"] is None and r["points_text"] is None and r["wave_text"] is None
    assert r["link"] and r["invites_text"]


def test_english_lines(client, monkeypatch):
    from services import i18n
    _patch_view(monkeypatch, _view(
        state="active_pack", status_key="amb_status_pack_text", referral_points=5, is_ambassador=True))
    ru_status = _default("amb_status_pack_text")
    tr_map = {i18n.src_hash(ru_status): "EN status"}

    async def _ctx(tid, language_code=None):
        return "en", tr_map
    monkeypatch.setattr(i18n, "context", _ctx)
    r = _ref(client)
    assert r["status_text"] == "EN status"
    assert r["points_text"]  # нет перевода — русский fail-soft, строка не пропадает
