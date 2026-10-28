"""Делегации в одном месте: состояние делегаций у внешней формы, предупреждение «форма
с вопросом про вуз подключена, а делегации выключены», кнопки между экранами и разбор
ошибок Google при сверке листа делегаций.

Почему: владелец подключил форму в «📝 Внешние формы» и считал, что делегации работают, —
бот отправил делегатам автоотказ. Сетевых вызовов нет: рассылка, тихие часы и права —
monkeypatch'ем.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

from config import config
from database import ext_forms_db as ef
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed
from domain.settings.synonyms import SETTINGS_SYNONYMS as SYNONYMS
from services import scheduler as sched
from services.delegations import delegations_off_alert as A
from services.infra.timeutil import msk_now
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db

_FMT = "%Y-%m-%d %H:%M:%S"


def _run(coro):
    return asyncio.run(coro)


def _ago(hours: float) -> str:
    return (msk_now() - timedelta(hours=hours)).strftime(_FMT)


def _db(tmp_path):
    config.DB_PATH = str(tmp_path / "dlg_one_place.db")
    fast_init_db()


def _form(title="Делегации YL", *, uni=True, status="active") -> int:
    async def go():
        fid = await ef.create_form(platform="yandex", external_id=f"x{title}" * 4, title=title)
        cols = [("q1", "ФИО")]
        if uni:
            cols.append(("q4", "Университет"))
        await ef.upsert_columns(fid, cols)
        if status != "active":
            await ef.set_form_status(fid, status)
        return fid
    return _run(go())


def _set(key, value):
    _run(set_setting_by_admin(None, key, value))


def _get(fid) -> dict:
    return _run(ef.get_form(fid))


def _state(fid) -> dict:
    return _run(A.form_delegation_state(_get(fid)))


def _patch_send(monkeypatch, *, quiet=False, fail=False):
    calls = []

    async def fake_notify(bot, cap, text, **kw):
        if fail:
            raise RuntimeError("telegram down")
        calls.append((cap, text, kw))
        return 1

    async def fake_window(city):
        return (None, None)

    monkeypatch.setattr(A, "notify_by_capability", fake_notify)
    monkeypatch.setattr(A, "window_for_city", fake_window)
    monkeypatch.setattr(A, "is_quiet", lambda now, s, e: quiet)
    return calls


# ── настройка ──

def test_off_alert_key_registered_on_by_default(tmp_path):
    entry = SETTINGS_SCHEMA["delegation_off_alert"]
    assert (entry["type"], entry["group"], entry["default"]) == ("enum", "apps", "on")
    assert entry["options"] == ["on", "off"]
    assert entry["prompt"] is None
    assert "Напоминать о выключенных делегациях" in entry["label"]
    assert len(SYNONYMS["delegation_off_alert"]) >= 2
    _db(tmp_path)
    assert _run(get_setting_typed("delegation_off_alert")) == "on"


def test_new_columns_in_list_and_get(tmp_path):
    _db(tmp_path)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, "2026-10-01 10:00:00", "2026-10-02 10:00:00"))
    got = _get(fid)
    assert (got["dlg_off_seen_at"], got["dlg_off_alerted_at"]) == (
        "2026-10-01 10:00:00", "2026-10-02 10:00:00")
    row = [f for f in _run(ef.list_forms()) if f["id"] == fid][0]
    assert row["dlg_off_seen_at"] == "2026-10-01 10:00:00"
    assert row["dlg_off_alerted_at"] == "2026-10-02 10:00:00"


# ── состояние формы ──

def test_state_university_question_delegations_off(tmp_path):
    _db(tmp_path)
    fid = _form()
    st = _state(fid)
    assert st["relevant"] is True
    assert st["armed_here"] is False
    assert st["armed_other_title"] is None
    assert st["half_on"] is True
    assert st["stale_off"] is False


def test_state_form_without_university_not_relevant(tmp_path):
    _db(tmp_path)
    fid = _form("Опрос", uni=False)
    st = _state(fid)
    assert st["relevant"] is False
    assert st["half_on"] is False


def test_state_chosen_form_without_university_is_relevant(tmp_path):
    _db(tmp_path)
    fid = _form("Опрос", uni=False)
    _set("delegation_form_id", str(fid))
    assert _state(fid)["relevant"] is True


def test_state_armed_here_and_other(tmp_path):
    _db(tmp_path)
    a = _form("<МГУ>")
    b = _form("Вторая")
    _set("delegation_form_id", str(a))
    _set("delegation_armed_form_id", str(a))
    sa, sb = _state(a), _state(b)
    assert sa["armed_here"] is True and sa["half_on"] is False
    assert sb["armed_here"] is False
    assert sb["armed_other_title"] == "<МГУ>"
    assert sb["half_on"] is False


def test_state_stale_off_after_a_day_and_toggle(tmp_path):
    _db(tmp_path)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(25), None))
    assert _state(fid)["stale_off"] is True
    _set("delegation_off_alert", "off")
    assert _state(fid)["stale_off"] is False
    _set("delegation_off_alert", "on")
    _run(ef.set_form_dlg_off(fid, _ago(2), None))
    assert _state(fid)["stale_off"] is False


# ── фоновое предупреждение ──

def test_alert_first_pass_marks_then_sends_once(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form("<МГУ>")
    assert _run(A.alert_off_delegations(object())) == 0
    assert calls == []
    assert _get(fid)["dlg_off_seen_at"]
    _run(ef.set_form_dlg_off(fid, _ago(25), None))
    assert _run(A.alert_off_delegations(object())) == 1
    assert len(calls) == 1
    cap, text, kw = calls[0]
    assert cap == "moderate_reg"
    assert kw.get("parse_mode") == "HTML"
    assert "Делегации выключены больше суток" in text
    assert "Форма «&lt;МГУ&gt;» подключена" in text
    assert "<МГУ>" not in text
    assert "delegation" not in text
    assert _get(fid)["dlg_off_alerted_at"]
    assert _run(A.alert_off_delegations(object())) == 0
    assert len(calls) == 1


def test_alert_two_forms_one_message(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    a, b = _form("А"), _form("Б")
    for fid in (a, b):
        _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _run(A.alert_off_delegations(object())) == 1
    assert len(calls) == 1
    text = calls[0][1]
    assert "Формы «А», «Б» подключены, в них есть вопрос про вуз" in text
    assert _get(a)["dlg_off_alerted_at"] and _get(b)["dlg_off_alerted_at"]


def test_alert_toggle_off_touches_nothing(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form()
    _set("delegation_off_alert", "off")
    assert _run(A.alert_off_delegations(object())) == 0
    assert calls == []
    assert _get(fid)["dlg_off_seen_at"] is None


def test_alert_armed_resets_columns(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), _ago(28)))
    _set("delegation_form_id", str(fid))
    _set("delegation_armed_form_id", str(fid))
    assert _run(A.alert_off_delegations(object())) == 0
    got = _get(fid)
    assert (got["dlg_off_seen_at"], got["dlg_off_alerted_at"]) == (None, None)
    assert calls == []


def test_alert_paused_form_not_half_on(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form(status="paused")
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _run(A.alert_off_delegations(object())) == 0
    got = _get(fid)
    assert (got["dlg_off_seen_at"], got["dlg_off_alerted_at"]) == (None, None)
    assert calls == []


def test_alert_quiet_hours_waits(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch, quiet=True)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _run(A.alert_off_delegations(object())) == 0
    assert calls == []
    assert _get(fid)["dlg_off_alerted_at"] is None


def test_alert_not_more_than_once_a_day(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    a, b = _form("А"), _form("Б")
    _run(ef.set_form_dlg_off(a, _ago(40), _ago(2)))
    _run(ef.set_form_dlg_off(b, _ago(30), None))
    assert _run(A.alert_off_delegations(object())) == 0
    assert calls == []
    assert _get(b)["dlg_off_alerted_at"] is None


def test_alert_send_failure_retries_later(tmp_path, monkeypatch):
    _db(tmp_path)
    _patch_send(monkeypatch, fail=True)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _run(A.alert_off_delegations(object())) == 0
    assert _get(fid)["dlg_off_alerted_at"] is None


def test_alert_runs_inside_ext_forms_notify_job():
    src = inspect.getsource(sched.ext_forms_notify_job)
    assert "alert_off_delegations" in src
