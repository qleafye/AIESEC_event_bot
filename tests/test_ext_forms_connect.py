"""Мастер подключения Яндекс и Google форм. Харнесс — tests/test_roles_phase8.py."""
import asyncio

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from database import ext_forms_db as xdb
from handlers.ext_forms import admin_ext_forms_connect as wiz
from handlers.states import ExtFormConnect
from services import ext_forms_google as gg
from services import ext_forms_yandex as yx
from tests.test_roles_phase8 import (
    ADMIN_ID, STRANGER_ID, FakeMessage, _fresh_state, _roles_ready, dispatch_callback,
    dispatch_message,
)

FID = "6a94ad5c1f1eb5c9649c12bd"
LINK = f"https://forms.yandex.ru/cloud/{FID}/"
LINK_STATE = ExtFormConnect.link.state


def _run(coro):
    return asyncio.run(coro)


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _all_text(ev):
    return " ".join([ev.text or ""] + [a[0] for a in ev.answers])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    monkeypatch.setattr(FakeMessage, "message_id", 42, raising=False)
    spawned = []
    monkeypatch.setattr(wiz, "spawn", lambda coro: spawned.append(coro))
    return spawned


def _conn(status="ok"):
    cid = _run(xdb.create_connection(platform="yandex", org_id=None, org_header=None,
                                     access_token="t", refresh_token="r",
                                     expires_at="2099-01-01 00:00:00", created_by=1))
    if status != "ok":
        _run(xdb.set_connection_status(cid, status, alerted_at=None))
    return cid


def _fresh(monkeypatch):
    async def fresh(conn):
        return None if conn.get("status") == "needs_reauth" else conn
    monkeypatch.setattr(wiz, "ensure_fresh_token", fresh)


def _state(platform):
    st = _fresh_state(ADMIN_ID)
    _run(st.set_state(ExtFormConnect.link))
    _run(st.update_data(platform=platform))
    return st


# ---------- Яндекс: вход в мастер ----------

def test_add_yandex_without_connection_asks_login():
    res, ev = dispatch_callback("extf_add:yandex", ADMIN_ID)
    assert "Сначала подключите доступ" in ev.message.text
    assert "extf_oauth" in _datas(ev.message.markup)


def test_add_yandex_needs_reauth():
    _conn("needs_reauth")
    res, ev = dispatch_callback("extf_add:yandex", ADMIN_ID)
    assert "потерян" in ev.message.text and "extf_oauth" in _datas(ev.message.markup)


def test_add_yandex_asks_link_and_sets_state():
    _conn()
    st = _fresh_state(ADMIN_ID)
    res, ev = dispatch_callback("extf_add:yandex", ADMIN_ID, state=st)
    assert FID in ev.message.text
    assert _run(st.get_state()) == LINK_STATE
    assert _run(st.get_data())["platform"] == "yandex"


def test_wizard_is_settings_only():
    res, ev = dispatch_callback("extf_add:yandex", STRANGER_ID)
    assert res is UNHANDLED or not ev.message.text


# ---------- Яндекс: ссылка и доступ ----------

def test_link_without_id(monkeypatch):
    _conn()
    _fresh(monkeypatch)
    st = _state("yandex")
    res, ev = dispatch_message("привет", ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "Не нашёл в ссылке номер формы" in _all_text(ev)
    assert _run(st.get_state()) == LINK_STATE


@pytest.mark.parametrize("reason,phrase", [
    ("forbidden", "нет доступа к этой форме"),
    ("unauthorized", "нет доступа к этой форме"),
    ("not_found", "Форма не найдена"),
    ("upstream_unavailable", "Яндекс сейчас не отвечает"),
])
def test_access_errors_explained(monkeypatch, reason, phrase):
    _conn()
    _fresh(monkeypatch)

    async def boom(conn, sid):
        raise yx.YandexApiError(reason)
    monkeypatch.setattr(wiz.yx, "get_survey", boom)
    st = _state("yandex")
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert phrase in _all_text(ev)
    assert _run(st.get_state()) == LINK_STATE


def _yandex_ok(monkeypatch, labels=(("1", "Ник в тг"), ("2", "Телефон"), ("3", "Город"))):
    async def survey(conn, sid):
        return {"name": "Анкета <b>"}

    async def questions(conn, sid):
        return {"pages": [{"items": [{"id": k, "label": v} for k, v in labels]}]}
    monkeypatch.setattr(wiz.yx, "get_survey", survey)
    monkeypatch.setattr(wiz.yx, "get_questions", questions)


def test_link_ok_shows_guess_keys(monkeypatch):
    _conn()
    _fresh(monkeypatch)
    _yandex_ok(monkeypatch)
    st = _state("yandex")
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    text = " ".join(a[0] for a in ev.answers)
    assert "Ник в Telegram — вопрос «Ник в тг»" in text
    assert "Телефон — вопрос «Телефон»" in text
    assert "Анкета &lt;b&gt;" in text
    kb = ev.answers[-1][2]
    assert "extf_keys_ok" in _datas(kb) and "extf_key:u" in _datas(kb) and "extf_key:p" in _datas(kb)
    d = _run(st.get_data())
    assert d["external_id"] == FID and d["uq"] == "1" and d["pq"] == "2"


def test_already_connected_opens_card(monkeypatch):
    _conn()
    _fresh(monkeypatch)
    _yandex_ok(monkeypatch)
    _run(xdb.create_form(platform="yandex", external_id=FID, title="Старая", secret="s"))
    st = _state("yandex")
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "уже подключена" in _all_text(ev)
    assert _run(st.get_state()) is None


def test_no_guess_warns_and_other_question_flow(monkeypatch):
    _conn()
    _fresh(monkeypatch)
    _yandex_ok(monkeypatch, labels=(("1", "Имя"), ("2", "Город")))
    st = _state("yandex")
    res, ev = dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "не выбран" in ev.answers[-1][0] and "Без ключей" in ev.answers[-1][0]
    res, ev = dispatch_callback("extf_key:u", ADMIN_ID, state=st)
    assert "extf_keyset:u:1" in _datas(ev.message.markup)
    assert "Такого вопроса нет" in _texts(ev.message.markup)
    res, ev = dispatch_callback("extf_keyset:u:1", ADMIN_ID, state=st)
    assert "Ник в Telegram — вопрос «Город»" in ev.message.text
    assert _run(st.get_data())["uq"] == "2"
    res, ev = dispatch_callback("extf_keyset:u:none", ADMIN_ID, state=st)
    assert _run(st.get_data())["uq"] is None


# ---------- Яндекс: создание ----------

def test_keys_ok_creates_form_and_backfills(monkeypatch, _env):
    cid = _conn()
    _fresh(monkeypatch)
    _yandex_ok(monkeypatch)
    st = _state("yandex")
    dispatch_message(LINK, ADMIN_ID, raw_state=LINK_STATE, state=st)

    async def backfill(fid):
        return 7
    monkeypatch.setattr(wiz, "backfill_form", backfill)
    res, ev = dispatch_callback("extf_keys_ok", ADMIN_ID, state=st)
    form = _run(xdb.get_form_by_external("yandex", FID))
    assert form["connection_id"] == cid and form["secret"] and len(form["secret"]) >= 32
    assert (form["key_username_q"], form["key_phone_q"]) == ("1", "2")
    assert "подтягиваю старые ответы" in ev.message.text.lower()
    d = _datas(ev.message.markup)
    assert f"extf_tab:{form['id']}" in d and f"extf_hook:{form['id']}" in d
    assert _run(st.get_state()) is None and len(_env) == 1
    msg = FakeMessage()
    _run(wiz._backfill_and_report(msg, form["id"]))
    assert "7 ответов" in msg.answers[0][0]
    for c in _env:
        c.close()


def test_backfill_unauthorized_asks_relogin(monkeypatch):
    async def backfill(fid):
        raise yx.YandexApiError("unauthorized")
    monkeypatch.setattr(wiz, "backfill_form", backfill)
    msg = FakeMessage()
    _run(wiz._backfill_and_report(msg, 1))
    text, _, kb = msg.answers[0]
    assert "войдите через Яндекс заново" in text and "extf_oauth" in _datas(kb)


def test_backfill_other_error_is_soft(monkeypatch):
    async def backfill(fid):
        raise RuntimeError("x")
    monkeypatch.setattr(wiz, "backfill_form", backfill)
    msg = FakeMessage()
    _run(wiz._backfill_and_report(msg, 1))
    assert "при следующей сверке" in msg.answers[0][0]


def test_keys_ok_after_restart():
    res, ev = dispatch_callback("extf_keys_ok", ADMIN_ID)
    assert "Мастер прервался" in ev.message.text
    assert "admin_ext_forms" in _datas(ev.message.markup)


# ---------- Google ----------

SHEET = "https://docs.google.com/spreadsheets/d/1AbC_xyz/edit"
VALUES = [["Отметка времени", "Ник в тг", "Ник в тг", "Телефон"], ["01.10.2026 12:00:00", "@a", "b", "9"]]


def _google(monkeypatch, tabs=((0, "Ответы"),), values=VALUES):
    monkeypatch.setattr(gg, "service_account_email", lambda: "bot@x.iam.gserviceaccount.com")

    async def list_tabs(sid):
        return "Таблица", list(tabs)

    async def read_values(sid, gid):
        return values
    monkeypatch.setattr(wiz.gg, "list_tabs", list_tabs)
    monkeypatch.setattr(wiz.gg, "read_values", read_values)


def test_add_google_names_service_account(monkeypatch):
    _google(monkeypatch)
    st = _fresh_state(ADMIN_ID)
    res, ev = dispatch_callback("extf_add:google", ADMIN_ID, state=st)
    assert "bot@x.iam.gserviceaccount.com" in ev.message.text and "Читатель" in ev.message.text
    assert _run(st.get_state()) == LINK_STATE


def test_add_google_without_key(monkeypatch):
    monkeypatch.setattr(gg, "service_account_email", lambda: None)
    res, ev = dispatch_callback("extf_add:google", ADMIN_ID)
    assert "не подключены на сервере бота" in ev.message.text


def test_google_bad_link():
    st = _state("google")
    res, ev = dispatch_message("http://x", ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "Не понял ссылку" in _all_text(ev)


def test_google_error_human(monkeypatch):
    _google(monkeypatch)

    async def boom(sid):
        raise gg.GoogleFormError("no_access", "Дайте доступ для бота с ролью «Читатель»")
    monkeypatch.setattr(wiz.gg, "list_tabs", boom)
    st = _state("google")
    res, ev = dispatch_message(SHEET, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "Дайте доступ" in _all_text(ev)


def test_google_single_tab_goes_to_keys_and_creates(monkeypatch):
    _google(monkeypatch)
    st = _state("google")
    res, ev = dispatch_message(SHEET, ADMIN_ID, raw_state=LINK_STATE, state=st)
    d = _run(st.get_data())
    assert [q[0] for q in d["questions"]] == ["Ник в тг", "Ник в тг (2)", "Телефон"]
    assert d["uq"] == "Ник в тг" and d["pq"] == "Телефон"

    async def sync(form):
        return 3
    monkeypatch.setattr(wiz.gg, "sync_google_form", sync)
    res, ev = dispatch_callback("extf_keys_ok", ADMIN_ID, state=st)
    form = _run(xdb.get_form_by_external("google", "1AbC_xyz", 0))
    assert form["secret"] is None and form["gsheet_gid"] == 0
    assert "подтянуто ответов: 3" in ev.message.text
    d = _datas(ev.message.markup)
    assert f"extf_tab:{form['id']}" in d and not any(x.startswith("extf_hook") for x in d)


def test_google_many_tabs_pick(monkeypatch):
    _google(monkeypatch, tabs=((0, "Лист1"), (55, "Ответы")))
    st = _state("google")
    res, ev = dispatch_message(SHEET, ADMIN_ID, raw_state=LINK_STATE, state=st)
    kb = ev.answers[-1][2]
    assert _datas(kb) == ["extf_gtab:0", "extf_gtab:1"]
    res, ev = dispatch_callback("extf_gtab:1", ADMIN_ID, state=st)
    assert _run(st.get_data())["gid"] == 55
    assert "Таблица — Ответы" in _run(st.get_data())["title"]


def test_google_no_timestamp_warns(monkeypatch):
    _google(monkeypatch, values=[["ФИО", "Ник"], ["Иван", "x"]])
    st = _state("google")
    res, ev = dispatch_message(SHEET, ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "Отметки времени" in ev.answers[-1][0]
    assert _datas(ev.answers[-1][2]) == ["extf_gwarn_ok", "extf_gwarn_no"]
    res, ev = dispatch_callback("extf_gwarn_ok", ADMIN_ID, state=st)
    assert "extf_keys_ok" in _datas(ev.message.answers[-1][2])
    res, ev = dispatch_callback("extf_gwarn_no", ADMIN_ID, state=st)
    assert "Отменено" in ev.message.text


def test_cancel_clears_state():
    st = _state("yandex")
    res, ev = dispatch_message("Отмена", ADMIN_ID, raw_state=LINK_STATE, state=st)
    assert "Отменено" in _all_text(ev) and _run(st.get_state()) is None
