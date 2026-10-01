"""Выбор вкладки для копии и адрес приёмника. Харнесс — tests/test_roles_phase8.py."""
import asyncio

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from config import config
from database import ext_forms_db as xdb
from handlers import admin_ext_forms_setup as setup
from tests.test_roles_phase8 import (
    ADMIN_ID, STRANGER_ID, _fresh_state, _roles_ready, dispatch_callback,
)


def _run(coro):
    return asyncio.run(coro)


def _datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _form(platform="yandex", title="Анкета <b>", secret="sec123"):
    return _run(xdb.create_form(platform=platform, external_id="x1", title=title, secret=secret))


@pytest.fixture
def tabs(monkeypatch):
    state = {"titles": ["Главный", "Лист2"], "created": []}

    async def titles():
        return state["titles"]

    async def create(fid, title):
        state["created"].append((fid, title))
        if state.get("result") == "ok":
            await xdb.set_form_mirror(fid, title, None)
        return state.get("result", "ok")

    monkeypatch.setattr(setup.sheets, "list_worksheet_titles", titles)
    monkeypatch.setattr(setup, "create_mirror_tab", create)
    return state


def test_tab_picker_buttons_and_escape(tmp_path, tabs):
    _roles_ready(tmp_path)
    fid = _form()
    st = _fresh_state(ADMIN_ID)
    res, ev = dispatch_callback(f"extf_tab:{fid}", ADMIN_ID, state=st)
    assert res is not UNHANDLED
    t, d = _texts(ev.message.markup), _datas(ev.message.markup)
    assert "➕ Новая вкладка «Анкета <b>»" in t
    assert "Главный" in t and "Лист2" in t and "Без копии в таблицу" in t
    assert f"extf_tabpick:{fid}:1" in d and f"extf_tabnew:{fid}" in d
    assert "&lt;b&gt;" in ev.message.text
    assert _run(st.get_data())["extf_tabs"] == ["Главный", "Лист2"]


def test_tab_picker_sheet_off(tmp_path, tabs):
    _roles_ready(tmp_path)
    tabs["titles"] = None
    fid = _form()
    _, ev = dispatch_callback(f"extf_tab:{fid}", ADMIN_ID)
    assert "Таблица события не подключена" in ev.message.text
    assert _datas(ev.message.markup)[0] == f"extf_tabnone:{fid}"


def test_tabnew_creates_once(tmp_path, tabs):
    _roles_ready(tmp_path)
    tabs["result"] = "ok"
    fid = _form()
    _, ev = dispatch_callback(f"extf_tabnew:{fid}", ADMIN_ID)
    assert tabs["created"] == [(fid, "Анкета <b>")]
    assert _run(xdb.get_form(fid))["mirror_tab"] == "Анкета <b>"
    assert "Вкладка таблицы" in ev.message.text


def test_tabnew_exists_repeats_screen(tmp_path, tabs):
    _roles_ready(tmp_path)
    tabs["result"] = "exists"
    fid = _form()
    _, ev = dispatch_callback(f"extf_tabnew:{fid}", ADMIN_ID)
    assert ev.answers[0][0].startswith("Вкладка с таким названием уже есть")
    assert f"extf_tabnone:{fid}" in _datas(ev.message.markup)
    assert _run(xdb.get_form(fid))["mirror_tab"] is None


def test_tabpick_sets_tab_and_clears_error_without_create(tmp_path, tabs):
    _roles_ready(tmp_path)
    fid = _form()
    _run(xdb.set_form_mirror(fid, "Старая", "вкладка пропала"))
    st = _fresh_state(ADMIN_ID)
    dispatch_callback(f"extf_tab:{fid}", ADMIN_ID, state=st)
    dispatch_callback(f"extf_tabpick:{fid}:1", ADMIN_ID, state=st)
    f = _run(xdb.get_form(fid))
    assert f["mirror_tab"] == "Лист2" and f["mirror_error"] is None
    assert tabs["created"] == []


def test_tabpick_stale_index(tmp_path, tabs):
    _roles_ready(tmp_path)
    fid = _form()
    _, ev = dispatch_callback(f"extf_tabpick:{fid}:5", ADMIN_ID)
    assert ev.answers[0][0] == "Список вкладок устарел — откройте выбор ещё раз"
    assert _run(xdb.get_form(fid))["mirror_tab"] is None


def test_tabnone(tmp_path, tabs):
    _roles_ready(tmp_path)
    fid = _form()
    _run(xdb.set_form_mirror(fid, "Лист", "ошибка"))
    dispatch_callback(f"extf_tabnone:{fid}", ADMIN_ID)
    f = _run(xdb.get_form(fid))
    assert f["mirror_tab"] is None and f["mirror_error"] is None


def test_hook_info_yandex(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl.example.org/")
    fid = _form()
    _, ev = dispatch_callback(f"extf_hook:{fid}", ADMIN_ID)
    txt = ev.message.text
    assert "<code>https://yl.example.org/app/hooks/yform/sec123</code>" in txt
    for needle in ("«Интеграции»", "«API»", "«Запрос JSON-RPC POST»", "«Сохранить»",
                   "«Запрос заданным методом»", "«Параметры»", "«Выполненные интеграции»",
                   "подтянет сам"):
        assert needle in txt
    assert f"extf_rehook:{fid}" in _datas(ev.message.markup)


def test_hook_info_no_public_url(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    fid = _form()
    _, ev = dispatch_callback(f"extf_hook:{fid}", ADMIN_ID)
    assert "Адрес сайта события не настроен" in ev.message.text
    assert "раз в 10 минут" in ev.message.text


def test_hook_info_google(tmp_path):
    _roles_ready(tmp_path)
    fid = _form(platform="google", secret=None)
    _, ev = dispatch_callback(f"extf_hook:{fid}", ADMIN_ID)
    assert "адрес не нужен" in ev.message.text
    assert all("rehook" not in d for d in _datas(ev.message.markup))


def test_rehook_needs_confirmation(tmp_path, monkeypatch):
    _roles_ready(tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://yl.example.org")
    fid = _form()
    _, ev = dispatch_callback(f"extf_rehook:{fid}", ADMIN_ID)
    assert "Старый адрес перестанет работать" in ev.message.text
    assert _run(xdb.get_form(fid))["secret"] == "sec123"
    _, ev = dispatch_callback(f"extf_rehook_ok:{fid}", ADMIN_ID)
    new = _run(xdb.get_form(fid))["secret"]
    assert new != "sec123" and len(new) >= 24
    assert new in ev.message.text


def test_setup_requires_settings(tmp_path, tabs):
    _roles_ready(tmp_path)
    fid = _form()
    for data in (f"extf_tab:{fid}", f"extf_hook:{fid}", f"extf_rehook_ok:{fid}"):
        res, _ = dispatch_callback(data, STRANGER_ID)
        assert res is UNHANDLED or _run(xdb.get_form(fid))["secret"] == "sec123"
