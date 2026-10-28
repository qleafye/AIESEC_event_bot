"""Назначение внешней формы: «🏫 Делегации вузов» / «📝 Регистрация» / «📄 Только ответы».

- хранение и миграция старых форм (`services/ext_forms/ext_forms_purpose.py`): старые формы
  получают назначение по прежнему правилу, поведение делегаций не меняется;
- логика делегаций только у форм-делегаций (гейт `delegations.delegation_form_id`);
- форма «Регистрация» (`services/ext_forms/ext_forms_registration.py`): ответ → заявка,
  привязка без дубля, опознание по нику на /start;
- экраны назначения и настроек регистрации (`handlers/ext_forms/admin_ext_forms_purpose.py`)
  и шаг назначения мастера подключения.

Без сети: бот — фейк, менеджерские уведомления и приветствие одобренного — monkeypatch.
pytest-asyncio в проекте нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from database import ext_forms_db as ef
from domain.settings.schema import get_setting_typed
from services.delegations import delegations as dlg
from services.ext_forms import ext_forms_purpose as purp
from services.ext_forms import ext_forms_registration as reg
from services.settings.audit import set_setting_by_admin
from tests._dbtpl import fast_init_db

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ext_forms",
                   "synthetic_delegation_answer.json")
SEASON = "YL 26/2"
ADMIN = 7


def _run(coro):
    return asyncio.run(coro)


class FakeBot:
    id = 1

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return None


def _env(tmp_path, name="purpose.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(set_setting_by_admin(None, "event_season", SEASON))
    bot = FakeBot()
    dlg.init(bot, MemoryStorage())
    return bot


def _fixture_items(username="@IvanTest") -> list[dict]:
    with open(FIX, encoding="utf-8") as f:
        items = json.load(f)
    for it in items:
        if it["q"] == "q7":
            it["value"] = username
        if it["q"] == "q6":
            it["value"] = "3 бакалавриат"  # ЦА при дефолтной отсечке
    return items


def _form(title, *, purpose=None, username_q="q7", phone_q=None, external_id=None, **kw) -> int:
    return _run(ef.create_form(platform="yandex", external_id=external_id or f"ext-{title}",
                               title=title, key_username_q=username_q, key_phone_q=phone_q,
                               purpose=purpose, **kw))


def _answer(fid, aid, items, *, received_at="2026-10-01 12:00:01") -> dict:
    async def go():
        await ef.upsert_columns(fid, [(i["q"], i["label"]) for i in items])
        await ef.insert_answer(form_id=fid, answer_id=aid, answered_at="2026-10-01 12:00:00",
                               received_at=received_at, payload=items, raw=None,
                               matched_telegram_id=None, match_how=None)
        return await ef.get_answer(fid, aid)
    return _run(go())


def _delegation_keys():
    async def go():
        for key, q in (("delegation_q_fullname", "q1"), ("delegation_q_university", "q4"),
                       ("delegation_q_course", "q6"), ("delegation_q_email", "q3")):
            await set_setting_by_admin(None, key, q)
    _run(go())


def _select_delegations(fid):
    _run(set_setting_by_admin(None, "delegation_form_id", str(fid)))
    _run(set_setting_by_admin(None, "delegation_armed_form_id", str(fid)))


def _users() -> list[tuple]:
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT telegram_id, status, delegation, source FROM users ORDER BY telegram_id"
    ).fetchall()
    conn.close()
    return rows


def _purpose_in_db(fid):
    return _run(ef.get_form(fid))["purpose"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Назначение: хранение, старые формы, гейт делегаций
# ══════════════════════════════════════════════════════════════════════════════════════════

def _three_old_forms():
    """A — выбрана в delegation_form_id и включена, B — с вопросом «Ваш вуз», C — без вуза."""
    _delegation_keys()
    a = _form("A")
    _select_delegations(a)
    b = _form("B", username_q="b2")
    c = _form("C", username_q=None)
    _answer(a, "a1", _fixture_items())
    _answer(b, "b1", [{"q": "b1", "label": "Ваш вуз", "value": "МГУ"},
                      {"q": "b2", "label": "Ник", "value": "@other"}])
    _answer(c, "c1", [{"q": "c1", "label": "Имя", "value": "Петя"},
                      {"q": "c2", "label": "Отзыв", "value": "Всё понравилось"}])
    db_conn = sqlite3.connect(config.DB_PATH)
    db_conn.execute(
        "INSERT INTO users (telegram_id, username, full_name, status, season, event_city, "
        "registration_date) VALUES (501, 'IvanTest', 'Иван', 'pending', ?, 'msk', "
        "'2026-09-20 10:00:00')", (SEASON,))
    db_conn.commit()
    db_conn.close()
    return a, b, c


def _prod_run(*, backfill: bool):
    a, b, c = _three_old_forms()
    if backfill:
        assert _run(purp.backfill_purposes()) == 3
    res_a = _run(dlg.on_answer_available(a, "a1"))
    res_c = _run(dlg.on_answer_available(c, "c1"))
    reg_hooks = [_run(reg.on_answer_available(fid, aid))
                 for fid, aid in ((a, "a1"), (b, "b1"), (c, "c1"))]
    res_a.pop("row_id", None)
    return (a, b, c), res_a, res_c, _users(), reg_hooks


def test_prod_behaviour_unchanged_by_backfill(tmp_path):
    _env(tmp_path, "before.db")
    _, a_before, c_before, users_before, hooks_before = _prod_run(backfill=False)
    _env(tmp_path, "after.db")
    (a, b, c), a_after, c_after, users_after, hooks_after = _prod_run(backfill=True)
    assert a_before == a_after
    assert a_before["ta"] == "ok" and a_before["converted"]["converted"]
    assert c_before == c_after == {"skipped": "not_delegation_form"}
    assert users_before == users_after
    assert users_after[0][1] == "approved" and users_after[0][2]
    assert all(h == {"skipped": "not_registration_form"} for h in hooks_before + hooks_after)
    assert (_purpose_in_db(a), _purpose_in_db(b), _purpose_in_db(c)) == (
        "delegations", "delegations", "answers")
    assert _run(purp.backfill_purposes()) == 0


def test_purpose_of_old_form_does_not_write(tmp_path):
    _env(tmp_path)
    a, b, c = _three_old_forms()
    assert _run(purp.purpose_of(_run(ef.get_form(b)))) == "delegations"
    assert _run(purp.purpose_of(_run(ef.get_form(c)))) == "answers"
    assert _purpose_in_db(b) is None and _purpose_in_db(c) is None


def test_registration_form_with_university_is_not_delegations(tmp_path):
    _env(tmp_path)
    _delegation_keys()
    fid = _form("Рег", purpose="registration")
    _select_delegations(fid)
    _answer(fid, "r1", _fixture_items())
    db_conn = sqlite3.connect(config.DB_PATH)
    db_conn.execute("INSERT INTO users (telegram_id, username, full_name, status) "
                    "VALUES (502, 'IvanTest', 'Иван', 'pending')")
    db_conn.commit()
    db_conn.close()
    assert _run(dlg.delegation_form_id()) is None
    assert _run(dlg.is_armed()) is False
    assert _run(dlg.on_answer_available(fid, "r1")) == {"skipped": "not_delegation_form"}
    assert _users()[0][2] is None


def test_answer_before_purpose_change_is_skipped(tmp_path):
    _env(tmp_path)
    _delegation_keys()
    fid = _form("Д", purpose="delegations")
    _select_delegations(fid)
    _run(ef.set_form_purpose(fid, "delegations", "2026-10-05 00:00:00"))
    _answer(fid, "old", _fixture_items(), received_at="2026-10-04 23:59:59")
    _answer(fid, "new", _fixture_items("@Someone"), received_at="2026-10-05 00:00:01")
    assert _run(dlg.on_answer_available(fid, "old")) == {"skipped": "before_purpose"}
    assert _run(dlg.on_answer_available(fid, "new"))["ta"] == "ok"


def test_form_delegation_state_relevant_only_for_delegations(tmp_path):
    from services.delegations.delegations_off_alert import form_delegation_state
    _env(tmp_path)
    d = _form("Д", purpose="delegations")
    r = _form("Р", purpose="registration")
    _answer(r, "r1", _fixture_items())  # вопрос про вуз есть, но назначение другое
    assert _run(form_delegation_state(_run(ef.get_form(d))))["relevant"] is True
    assert _run(form_delegation_state(_run(ef.get_form(r))))["relevant"] is False


# ── экран «🏫 Делегации»: выбор формы ─────────────────────────────────────────────────────

class _User:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _Msg:
    def __init__(self, text="", user_id=ADMIN, username=None):
        self.text = text
        self.from_user = _User(user_id, username)
        self.edits = []
        self.answers = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))


class _Cb:
    def __init__(self, data, user_id=ADMIN):
        self.data = data
        self.from_user = _User(user_id)
        self.message = _Msg()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _cbs(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _last(cb):
    return (cb.message.edits or cb.message.answers)[-1]


def test_dlg_form_pick_shows_only_delegation_forms(tmp_path):
    from handlers.delegations import admin_delegations as mod
    _env(tmp_path)
    d = _form("Делегации", purpose="delegations")
    a = _form("Отзывы", purpose="answers")
    cb = _Cb("dlg_form_pick")
    _run(mod.dlg_form_pick(cb))
    calls = _cbs(_last(cb)[1])
    assert f"dlg_form:{d}" in calls and f"dlg_form:{a}" not in calls


def test_dlg_form_pick_alert_when_no_delegation_purpose(tmp_path):
    from handlers.delegations import admin_delegations as mod
    _env(tmp_path)
    a = _form("Отзывы", purpose="answers")
    cb = _Cb("dlg_form_pick")
    _run(mod.dlg_form_pick(cb))
    assert cb.answer_calls[-1] == (mod._NO_DLG_PURPOSE_ALERT, True)
    assert "🎯 Назначение" in mod._NO_DLG_PURPOSE_ALERT
    cb = _Cb(f"dlg_form:{a}")
    _run(mod.dlg_form(cb))
    assert cb.answer_calls[-1] == (mod._NO_DLG_PURPOSE_ALERT, True)
    assert not _run(get_setting_typed("delegation_form_id"))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Форма «📝 Регистрация»: ответ → заявка
# ══════════════════════════════════════════════════════════════════════════════════════════

REG_ITEMS = [
    {"q": "f", "label": "Фамилия и имя", "value": "Анна Тестова"},
    {"q": "p", "label": "Телефон", "value": " +7 900 000-00-00 "},
    {"q": "u", "label": "Ник в Telegram", "value": "@AnnaTest"},
]


def _reg_form(*, approve="queue", city=None, username_q="u", title="Отбор <МГУ>") -> int:
    fid = _form(title, purpose="registration", username_q=username_q, phone_q="p",
                reg_q_fullname="f")
    _run(ef.set_form_reg_approve(fid, approve))
    if city:
        _run(ef.set_form_reg_city(fid, city))
    return fid


def _patch_notify(monkeypatch) -> list:
    calls = []

    async def fake_notify(bot, **kw):
        calls.append(kw)
    import services.reg_digest as rd
    monkeypatch.setattr(rd, "notify_application", fake_notify)
    return calls


def _insert_user(tid, username, status="approved", source="Сарафан"):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("INSERT INTO users (telegram_id, username, full_name, status, source, season, "
                 "event_city) VALUES (?, ?, 'Старое Имя', ?, ?, ?, 'msk')",
                 (tid, username, status, source, SEASON))
    conn.commit()
    conn.close()


def _answer_row(fid, aid):
    return _run(ef.get_answer(fid, aid))


def test_registration_answer_creates_pending_application(tmp_path, monkeypatch):
    bot = _env(tmp_path)
    calls = _patch_notify(monkeypatch)
    fid = _reg_form(city="msk")
    _run(db.mark_reg_started(601, "AnnaTest"))
    _answer(fid, "x1", REG_ITEMS)
    res = _run(reg.on_answer_available(fid, "x1"))
    assert res["state"] == "created"
    user = _run(db.get_user(601))
    assert user["status"] == "pending"
    assert user["full_name"] == "Анна Тестова" and user["phone"] == "+7 900 000-00-00"
    assert user["event_city"] == "msk"
    label = "Внешняя форма «Отбор <МГУ>»"
    assert user["source"] == label and user["source_from_tag"] == 1
    conn = sqlite3.connect(config.DB_PATH)
    ev = conn.execute("SELECT event, source_tag FROM reg_events WHERE telegram_id = 601").fetchall()
    conn.close()
    assert ("form_completed", "ext_form") in ev
    row = _answer_row(fid, "x1")
    assert row["reg_state"] == "created" and row["matched_telegram_id"] == 601
    assert len(calls) == 1 and "&lt;МГУ&gt;" in calls[0]["admin_text"]
    assert "<МГУ>" not in calls[0]["admin_text"]
    assert [s[0] for s in bot.sent] == [601] and "пришла из формы" in bot.sent[0][1]
    assert (label, 1) in [tuple(r) for r in _run(db.get_source_stats())]
    assert label in _run(db.get_distinct_filter_values("source"))
    # Повтор — без второй заявки и второго сообщения.
    assert _run(reg.on_answer_available(fid, "x1")) == {"already": True}
    assert len(bot.sent) == 1 and len(calls) == 1


def test_registration_auto_approve(tmp_path, monkeypatch):
    _env(tmp_path)
    _patch_notify(monkeypatch)
    approved = []

    async def fake_approve(bot, tid, *, auto_approved=False):
        approved.append((tid, auto_approved))
        return None
    import handlers.reg.reg_schema as rs
    monkeypatch.setattr(rs, "approve_user", fake_approve)
    fid = _reg_form(approve="auto")
    _run(db.mark_reg_started(602, "AnnaTest"))
    _answer(fid, "x1", REG_ITEMS)
    _run(reg.on_answer_available(fid, "x1"))
    assert _run(db.get_user(602))["status"] == "approved"
    assert approved == [(602, True)]
    conn = sqlite3.connect(config.DB_PATH)
    j = conn.execute("SELECT decision FROM application_decisions WHERE telegram_id = 602").fetchall()
    conn.close()
    assert j == [("approved",)]


def test_registration_links_existing_user_without_duplicate(tmp_path, monkeypatch):
    bot = _env(tmp_path)
    calls = _patch_notify(monkeypatch)
    _insert_user(603, "AnnaTest")
    fid = _reg_form()
    _answer(fid, "x1", REG_ITEMS)
    assert _run(reg.on_answer_available(fid, "x1"))["state"] == "linked"
    assert _users() == [(603, "approved", None, "Сарафан")]
    row = _answer_row(fid, "x1")
    assert row["reg_state"] == "linked" and row["matched_telegram_id"] == 603
    assert bot.sent == [] and calls == []


def test_registration_waits_for_start(tmp_path, monkeypatch):
    bot = _env(tmp_path)
    _patch_notify(monkeypatch)
    fid = _reg_form()
    _answer(fid, "x1", REG_ITEMS)
    assert _run(reg.on_answer_available(fid, "x1")) == {"state": "waiting"}
    assert _answer_row(fid, "x1")["reg_state"] == "waiting"
    # Чужой ник — ничего.
    assert _run(reg.try_registration_start(_Msg(user_id=700, username="Other"), None, bot)) is False
    assert _users() == [] and _answer_row(fid, "x1")["reg_state"] == "waiting"
    # Владелец ника — заявка.
    assert _run(reg.try_registration_start(_Msg(user_id=604, username="annatest"), None, bot))
    user = _run(db.get_user(604))
    assert user["status"] == "pending" and user["username"] == db.store_username("@annatest")
    assert _answer_row(fid, "x1")["reg_state"] == "created"


def test_registration_start_with_existing_user_links(tmp_path, monkeypatch):
    bot = _env(tmp_path)
    _patch_notify(monkeypatch)
    fid = _reg_form()
    _answer(fid, "x1", REG_ITEMS)
    _run(reg.on_answer_available(fid, "x1"))
    _insert_user(605, "AnnaTest", status="pending")
    assert _run(reg.try_registration_start(_Msg(user_id=605, username="AnnaTest"), None, bot)) is False
    assert _answer_row(fid, "x1")["reg_state"] == "linked"
    assert _users() == [(605, "pending", None, "Сарафан")]


def test_registration_no_nick_and_ambiguous(tmp_path, monkeypatch):
    _env(tmp_path)
    _patch_notify(monkeypatch)
    no_q = _reg_form(username_q=None, title="Без ника")
    _answer(no_q, "x1", REG_ITEMS)
    assert _run(reg.on_answer_available(no_q, "x1")) == {"state": "no_nick"}
    fid = _reg_form(title="Двое")
    _insert_user(606, "AnnaTest")
    _insert_user(607, "AnnaTest")
    _answer(fid, "x1", REG_ITEMS)
    assert _run(reg.on_answer_available(fid, "x1")) == {"state": "ambiguous"}
    assert len(_users()) == 2


def test_registration_hook_ignores_other_purposes_and_old_answers(tmp_path, monkeypatch):
    _env(tmp_path)
    _patch_notify(monkeypatch)
    for purpose in ("answers", "delegations"):
        fid = _form(f"F-{purpose}", purpose=purpose, username_q="u")
        _answer(fid, "x1", REG_ITEMS)
        assert _run(reg.on_answer_available(fid, "x1")) == {"skipped": "not_registration_form"}
    fid = _reg_form(title="Сменили")
    _run(ef.set_form_purpose(fid, "registration", "2026-10-05 00:00:00"))
    _answer(fid, "old", REG_ITEMS, received_at="2026-10-04 10:00:00")
    assert _run(reg.on_answer_available(fid, "old")) == {"skipped": "before_purpose"}


def test_ext_reg_pending_text_registered():
    from domain.settings.schema import SETTINGS_SCHEMA
    from domain.settings.synonyms import SETTINGS_SYNONYMS as SYNONYMS
    from services.i18n.i18n_form_manual import FORM_DEFAULT_EN
    spec = SETTINGS_SCHEMA["ext_reg_pending_text"]
    assert spec["group"] == "reg" and spec["default"] in FORM_DEFAULT_EN
    assert len(SYNONYMS["ext_reg_pending_text"]) >= 2
