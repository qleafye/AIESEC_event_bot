"""Мастер подключения личной Яндекс Формы (API отвечает org_required) и инструкция интеграции."""
import asyncio

import pytest

from config import config
from database import ext_forms_db as xdb
from handlers.ext_forms import admin_ext_forms_connect as wiz
from handlers.ext_forms import admin_ext_forms_setup as setup
from handlers.states import ExtFormConnect
from services import ext_forms_yandex as yx
from tests.test_roles_phase8 import (
    ADMIN_ID, FakeMessage, _fresh_state, _roles_ready, dispatch_callback, dispatch_message,
)

FID = "6aa022f0068ff027eaba0bcb"
LINK = f"https://forms.yandex.ru/u/{FID}/"
LINK_STATE = ExtFormConnect.link.state
TITLE_STATE = ExtFormConnect.push_title.state


def _run(coro):
    return asyncio.run(coro)


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _all_text(ev):
    return " ".join([ev.text or ""] + [a[0] for a in ev.answers])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    monkeypatch.setattr(FakeMessage, "message_id", 42, raising=False)
    spawned = []
    monkeypatch.setattr(wiz, "spawn", lambda coro: spawned.append(coro))
    return spawned


def _conn():
    return _run(xdb.create_connection(platform="yandex", org_id=None, org_header=None,
                                      access_token="t", refresh_token="r",
                                      expires_at="2099-01-01 00:00:00", created_by=1))


def _org_required(monkeypatch, calls=None):
    async def fresh(conn):
        return conn

    async def boom(conn, sid):
        if calls is not None:
            calls.append(sid)
        raise yx.YandexApiError("org_required")
    monkeypatch.setattr(wiz, "ensure_fresh_token", fresh)
    monkeypatch.setattr(wiz.yx, "get_survey", boom)


def _link_state():
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormConnect.link))
    _run(st.update_data(platform="yandex"))
    return st


def test_org_required_offers_push_button(monkeypatch):
    _conn()
    _org_required(monkeypatch)
    st = _link_state()
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "личном аккаунте" in _all_text(ev)
    kb = ev.answers[-1][2]
    assert "extf_push" in _datas(kb) and "extf_oauth" in _datas(kb)
    assert _run(st.get_data())["external_id"] == FID


def test_no_connection_still_offers_push_button():
    st = _link_state()
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "войдите через Яндекс" in _all_text(ev)
    assert "extf_push" in _datas(ev.answers[-1][2])


def test_push_button_asks_title_then_text_creates_form(monkeypatch):
    _conn()
    _org_required(monkeypatch)
    st = _link_state()
    dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    res, ev = dispatch_callback("extf_push", ADMIN_ID, state=st)
    assert "Как назвать форму" in ev.message.answers[-1][0]
    assert "extf_push_title_default" in _datas(ev.message.answers[-1][2])
    assert _run(st.get_state()) == TITLE_STATE

    async def no_api(*a, **k):
        raise AssertionError("backfill не нужен")
    monkeypatch.setattr(wiz, "backfill_form", no_api)
    res, ev = dispatch_message("  Отбор волонтёров  ", ADMIN_ID, raw_state=TITLE_STATE, state=st)
    form = _run(xdb.get_form_by_external("yandex", FID))
    assert form["title"] == "Отбор волонтёров"
    assert form["ingest_mode"] == "push" and form["connection_id"] is None
    assert form["secret"] and len(form["secret"]) >= 32
    assert form["key_username_q"] is None and form["key_phone_q"] is None
    assert "✅ Форма подключена" in _all_text(ev)
    d = _datas(ev.answers[-1][2])
    assert {f"extf_hook:{form['id']}", f"extf_import:{form['id']}", f"extf_tab:{form['id']}",
            f"extf_card:{form['id']}"} <= set(d)
    assert _run(st.get_state()) is None


def test_default_title_button(monkeypatch):
    _conn()
    _org_required(monkeypatch)
    st = _link_state()
    dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    dispatch_callback("extf_push", ADMIN_ID, state=st)
    res, ev = dispatch_callback("extf_push_title_default", ADMIN_ID, state=st)
    assert _run(xdb.get_form_by_external("yandex", FID))["title"] == "Яндекс Форма"
    assert "✅ Форма подключена" in " ".join(a[0] for a in ev.message.answers)


def test_empty_title_falls_back_and_long_is_cut(monkeypatch):
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormConnect.push_title))
    _run(st.update_data(platform="yandex", external_id=FID))
    dispatch_message("Я" * 300, ADMIN_ID, raw_state=TITLE_STATE, state=st)
    assert len(_run(xdb.get_form_by_external("yandex", FID))["title"]) == 100


def test_duplicate_opens_existing_card(monkeypatch):
    _conn()
    _org_required(monkeypatch)
    _run(xdb.create_form(platform="yandex", external_id=FID, title="Старая", secret="s",
                         ingest_mode="push"))
    st = _link_state()
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "уже подключена" in _all_text(ev)
    assert _run(st.get_state()) is None


def test_double_tap_does_not_duplicate(monkeypatch):
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormConnect.push_title))
    _run(st.update_data(platform="yandex", external_id=FID))
    dispatch_callback("extf_push_title_default", ADMIN_ID, state=st)
    res, ev = dispatch_callback("extf_push_title_default", ADMIN_ID, state=st)
    assert "начните подключение заново" in " ".join(a[0] for a in ev.message.answers)
    assert len([f for f in _run(xdb.list_forms()) if f["external_id"] == FID]) == 1


def test_push_without_state_restarts():
    res, ev = dispatch_callback("extf_push", ADMIN_ID)
    assert ev.answers[-1][1] is True


def test_cancel_in_push_title_clears_state():
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormConnect.push_title))
    dispatch_message("Отмена", ADMIN_ID, raw_state=TITLE_STATE, state=st)
    assert _run(st.get_state()) is None


# ---------- инструкция ----------

def test_hook_text_push_has_full_instruction(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl.example.org/")
    text = setup._hook_text({"title": "Ф", "platform": "yandex", "secret": "SEC",
                             "ingest_mode": "push"})
    for needle in ("https://yl.example.org/app/hooks/yform/SEC", "Запрос JSON-RPC POST",
                   "answer_id", "answers", "Ответы на вопросы", "формат", "JSON", "created",
                   "Загрузить старые ответы"):
        assert needle in text
    assert "сверкой" not in text


def test_hook_text_api_unchanged(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl.example.org/")
    text = setup._hook_text({"title": "Ф", "platform": "yandex", "secret": "SEC",
                             "ingest_mode": "api"})
    assert "Поля «Запрос заданным методом»" in text and "answer_id" not in text


def test_rehook_push_has_no_reconcile_promise():
    fid = _run(xdb.create_form(platform="yandex", external_id=FID, title="Ф", secret="s",
                               ingest_mode="push"))
    res, ev = dispatch_callback(f"extf_rehook:{fid}", ADMIN_ID)
    assert "новые ответы не придут" in ev.message.text
    assert "10 минут" not in ev.message.text
