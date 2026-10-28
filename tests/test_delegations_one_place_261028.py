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


def _alert():
    return _run(A.alert_off_delegations(object()))


def _patch_send(monkeypatch, *, quiet=False, fail=False):
    calls = []

    async def fake_notify(bot, cap, text, **kw):
        if fail:
            raise RuntimeError("telegram down")
        calls.append((cap, text, kw))
        return 1

    async def fake_window(city):
        return (None, None)

    monkeypatch.setattr(A, "_sender", fake_notify)
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
    assert _alert() == 0
    assert calls == []
    assert _get(fid)["dlg_off_seen_at"]
    _run(ef.set_form_dlg_off(fid, _ago(25), None))
    assert _alert() == 1
    assert len(calls) == 1
    cap, text, kw = calls[0]
    assert cap == "moderate_reg"
    assert kw.get("parse_mode") == "HTML"
    assert "Делегации выключены больше суток" in text
    assert "Форма «&lt;МГУ&gt;» подключена" in text
    assert "<МГУ>" not in text
    assert "delegation" not in text
    assert _get(fid)["dlg_off_alerted_at"]
    assert _alert() == 0
    assert len(calls) == 1


def test_alert_two_forms_one_message(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    a, b = _form("А"), _form("Б")
    for fid in (a, b):
        _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _alert() == 1
    assert len(calls) == 1
    text = calls[0][1]
    assert "Формы «А», «Б» подключены, в них есть вопрос про вуз" in text
    assert _get(a)["dlg_off_alerted_at"] and _get(b)["dlg_off_alerted_at"]


def test_alert_toggle_off_touches_nothing(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form()
    _set("delegation_off_alert", "off")
    assert _alert() == 0
    assert calls == []
    assert _get(fid)["dlg_off_seen_at"] is None


def test_alert_armed_resets_columns(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), _ago(28)))
    _set("delegation_form_id", str(fid))
    _set("delegation_armed_form_id", str(fid))
    assert _alert() == 0
    got = _get(fid)
    assert (got["dlg_off_seen_at"], got["dlg_off_alerted_at"]) == (None, None)
    assert calls == []


def test_alert_paused_form_not_half_on(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    fid = _form(status="paused")
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _alert() == 0
    got = _get(fid)
    assert (got["dlg_off_seen_at"], got["dlg_off_alerted_at"]) == (None, None)
    assert calls == []


def test_alert_quiet_hours_waits(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch, quiet=True)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _alert() == 0
    assert calls == []
    assert _get(fid)["dlg_off_alerted_at"] is None


def test_alert_not_more_than_once_a_day(tmp_path, monkeypatch):
    _db(tmp_path)
    calls = _patch_send(monkeypatch)
    a, b = _form("А"), _form("Б")
    _run(ef.set_form_dlg_off(a, _ago(40), _ago(2)))
    _run(ef.set_form_dlg_off(b, _ago(30), None))
    assert _alert() == 0
    assert calls == []
    assert _get(b)["dlg_off_alerted_at"] is None


def test_alert_send_failure_retries_later(tmp_path, monkeypatch):
    _db(tmp_path)
    _patch_send(monkeypatch, fail=True)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _alert() == 0
    assert _get(fid)["dlg_off_alerted_at"] is None


def test_alert_runs_inside_ext_forms_notify_job():
    src = inspect.getsource(sched.ext_forms_notify_job)
    assert "alert_off_delegations(_bot)" in src
    assert "notify_by_capability" not in src


def test_sender_registered_by_handlers_layer():
    from handlers.access.admin_caps import notify_by_capability
    import handlers.delegations.admin_delegations  # noqa: F401 — регистрирует отправителя
    assert A._sender is notify_by_capability


def test_alert_without_sender_sends_nothing(tmp_path, monkeypatch):
    _db(tmp_path)
    _patch_send(monkeypatch)
    monkeypatch.setattr(A, "_sender", None)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(30), None))
    assert _alert() == 0
    assert _get(fid)["dlg_off_alerted_at"] is None


# ── карточка внешней формы ──

from handlers.delegations import admin_delegations as D  # noqa: E402
from handlers.ext_forms import admin_ext_forms as X  # noqa: E402
from handlers.delegations.admin_delegations import CITY_LOCKED_TEXT  # noqa: E402

ADMIN = 7


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _Cb:
    def __init__(self, data="x", uid=ADMIN):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _caps(monkeypatch, *, moderate=True, settings=True, locked=False):
    have = {"moderate_reg": moderate, "settings": settings}

    async def fake_has(uid, cap):
        return have.get(cap, False)

    async def fake_locked(uid):
        return locked

    monkeypatch.setattr(X, "has_capability", fake_has)
    monkeypatch.setattr(D, "has_capability", fake_has)
    monkeypatch.setattr(D, "delegations_locked", fake_locked)


def _buttons(kb):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def _card(fid):
    cb = _Cb()
    assert _run(X.render_form_card(cb, fid))
    text, kb = cb.message.edits[-1]
    return text, _buttons(kb)


def test_card_off_shows_state_and_pick_button(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form()
    text, buttons = _card(fid)
    assert "🏫 Делегации: выключены" in text
    assert text.index("Уведомления о новых ответах") < text.index("🏫 Делегации")
    datas = [d for _, d in buttons]
    assert ("🏫 Настроить делегации", f"dlg_form:{fid}") in buttons
    assert datas.index(f"dlg_form:{fid}") + 1 == datas.index(f"extf_disable:{fid}")
    assert "больше суток" not in text


def test_card_armed_here_goes_to_screen(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form()
    _set("delegation_form_id", str(fid))
    _set("delegation_armed_form_id", str(fid))
    text, buttons = _card(fid)
    assert "🏫 Делегации: включены" in text
    assert ("🏫 Настроить делегации", "admin_delegations") in buttons


def test_card_locked_manager_goes_to_screen(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch, locked=True)
    fid = _form()
    _, buttons = _card(fid)
    assert ("🏫 Настроить делегации", "admin_delegations") in buttons


def test_card_armed_other_title_escaped(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    a = _form("<МГУ>")
    b = _form("Вторая")
    _set("delegation_form_id", str(a))
    _set("delegation_armed_form_id", str(a))
    text, _ = _card(b)
    assert "🏫 Делегации: включены для формы «&lt;МГУ&gt;»" in text


def test_card_unrelated_form_unchanged(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form("Опрос", uni=False)
    text, buttons = _card(fid)
    assert "🏫 Делегации" not in text
    assert all("Настроить делегации" not in t for t, _ in buttons)
    f = _run(X._form_with_stats(fid))
    assert _buttons(X._card_kb(f)) == buttons
    assert X._card_text(f) == text


def test_card_without_moderate_right_explains_who(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch, moderate=False)
    fid = _form()
    text, buttons = _card(fid)
    assert "— настраивает менеджер с правом «📋 Модерация заявок»: «📋 Заявки → 🏫 Делегации»" in text
    assert all("Настроить делегации" not in t for t, _ in buttons)


def test_card_stale_warning_and_toggle(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form()
    _run(ef.set_form_dlg_off(fid, _ago(25), None))
    text, _ = _card(fid)
    assert ("⚠️ В форме есть вопрос про вуз, а делегации выключены больше суток — "
            "бот не одобряет делегатов из этой формы.") in text
    _set("delegation_off_alert", "off")
    text, _ = _card(fid)
    assert "больше суток" not in text


# ── экран «🏫 Делегации» ──

def test_no_form_screen_connect_button(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    cb = _Cb("admin_delegations")
    _run(D.admin_delegations(cb))
    text, kb = cb.message.edits[-1]
    assert "📊 Данные → 📝 Внешние формы" not in text
    assert "Подключить форму может" not in text
    buttons = _buttons(kb)
    assert ("📝 Выбрать форму", "dlg_form_pick") in buttons
    assert ("➕ Подключить форму", "admin_ext_forms") in buttons


def test_no_form_screen_without_settings_right(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch, settings=False)
    cb = _Cb("admin_delegations")
    _run(D.admin_delegations(cb))
    text, kb = cb.message.edits[-1]
    assert "Подключить форму может менеджер с правом «⚙️ Настройки»." in text
    assert all(d != "admin_ext_forms" for _, d in _buttons(kb))


def test_form_pick_empty_with_and_without_settings(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    cb = _Cb("dlg_form_pick")
    _run(D.dlg_form_pick(cb))
    text, kb = cb.message.edits[-1]
    assert "Ни одна форма ещё не подключена" in text
    assert _buttons(kb) == [("➕ Подключить форму", "admin_ext_forms"),
                            ("← К делегациям", "admin_delegations")]
    assert all(not alert for _, alert in cb.answer_calls)
    _caps(monkeypatch, settings=False)
    cb = _Cb("dlg_form_pick")
    _run(D.dlg_form_pick(cb))
    assert cb.answer_calls == [(D._NO_FORMS_ALERT, True)]
    assert "⚙️ Настройки" in D._NO_FORMS_ALERT


def test_form_pick_list_has_connect_before_back(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form()
    cb = _Cb("dlg_form_pick")
    _run(D.dlg_form_pick(cb))
    _, kb = cb.message.edits[-1]
    datas = [d for _, d in _buttons(kb)]
    assert datas == [f"dlg_form:{fid}", "admin_ext_forms", "admin_delegations"]


def test_off_alert_toggle(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch)
    fid = _form()
    _set("delegation_form_id", str(fid))
    cb = _Cb("admin_delegations")
    _run(D.admin_delegations(cb))
    texts = [t for t, _ in _buttons(cb.message.edits[-1][1])]
    assert "🔔 Напоминать о выключенных делегациях: ✅" in texts
    cb = _Cb("dlg_off_alert")
    _run(D.dlg_off_alert(cb))
    assert _run(get_setting_typed("delegation_off_alert")) == "off"
    assert cb.answer_calls[0][0] == "Напоминание о выключенных делегациях: выключено"
    texts = [t for t, _ in _buttons(cb.message.edits[-1][1])]
    assert "🔔 Напоминать о выключенных делегациях: ☐" in texts
    cb = _Cb("dlg_off_alert")
    _run(D.dlg_off_alert(cb))
    assert _run(get_setting_typed("delegation_off_alert")) == "on"
    assert cb.answer_calls[0][0] == "Напоминание о выключенных делегациях: включено"


def test_off_alert_toggle_locked(tmp_path, monkeypatch):
    _db(tmp_path)
    _caps(monkeypatch, locked=True)
    cb = _Cb("dlg_off_alert")
    _run(D.dlg_off_alert(cb))
    assert cb.answer_calls == [(CITY_LOCKED_TEXT, True)]
    assert _run(get_setting_typed("delegation_off_alert")) == "on"


# ── ошибки Google при сверке листа делегаций ──

import logging  # noqa: E402

import gspread  # noqa: E402

from handlers.delegations import admin_delegations_sheet as S  # noqa: E402
from services.delegations import delegations_sheet_errors as E  # noqa: E402
from tests.test_delegations_admin import _FakeCallback, _callbacks, _form as _dform, _last_edit, _select  # noqa: E402
from tests.test_delegations_admin_sheet import _pick, sheet  # noqa: E402,F401


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = "x"

    def json(self):
        return {"error": {"code": self.status_code, "message": "x", "status": "S"}}


def _api(code):
    return gspread.exceptions.APIError(_Resp(code))


def test_google_status():
    assert E.google_status(_api(403)) == 403
    assert E.google_status(RuntimeError()) is None

    class _NoResp(gspread.exceptions.APIError):
        def __init__(self):
            pass

    assert E.google_status(_NoResp()) is None


def test_explain_codes(monkeypatch):
    monkeypatch.setattr(E, "service_account_email", lambda: "bot@x.iam.gserviceaccount.com")
    t = E.explain(_api(403), "UR REGS")
    assert "Нет доступа к таблице" in t and "«UR REGS»" in t
    assert "bot@x.iam.gserviceaccount.com" in t and "роль «Редактор»" in t
    monkeypatch.setattr(E, "service_account_email", lambda: None)
    assert "сервисному аккаунту бота" in E.explain(_api(403), "UR REGS")
    assert "Подождите минуту" in E.explain(_api(429), "UR REGS")
    assert "Таблица не найдена" in E.explain(_api(404), "UR REGS")
    assert "(код 500)" in E.explain(_api(500), "UR REGS")
    assert "&lt;x&gt;" in E.explain(_api(500), "<x>")
    assert E.explain(RuntimeError("quota"), "UR REGS") is None


def test_dlg_check_403_explained_and_logged(tmp_path, sheet, monkeypatch, caplog):
    monkeypatch.setattr(E, "service_account_email", lambda: "bot@x.iam.gserviceaccount.com")
    _db(tmp_path)
    fid = _dform()
    _select(fid)
    _, st = _pick(fid)
    sheet["raise"] = _api(403)
    cb = _FakeCallback("dlg_check")
    with caplog.at_level(logging.WARNING):
        _run(S.dlg_check(cb, st))
    text, _, kb = _last_edit(cb)
    assert "Нет доступа к таблице" in text and "bot@x.iam.gserviceaccount.com" in text
    assert {"dlg_check", "dlg_sheet", "admin_delegations"} <= set(_callbacks(kb))
    assert "код Google 403" in caplog.text


def test_dlg_check_other_error_keeps_old_text(tmp_path, sheet, caplog):
    _db(tmp_path)
    fid = _dform()
    _select(fid)
    _, st = _pick(fid)
    sheet["raise"] = RuntimeError("quota")
    cb = _FakeCallback("dlg_check")
    with caplog.at_level(logging.WARNING):
        _run(S.dlg_check(cb, st))
    assert "Не получилось прочитать лист «UR REGS»" in _last_edit(cb)[0]
    assert "код Google None" in caplog.text
