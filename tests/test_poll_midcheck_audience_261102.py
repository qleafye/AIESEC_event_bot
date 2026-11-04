"""Мидчек в мастере «📊 Опросы»: «пришли сегодня» и «отмечены на этой сессии».

Один резолвер на одновопросный опрос и на анкету. Пустая выборка не становится
рассылкой всем. Город шапки перебивает код из кнопки. pytest-asyncio нет.
"""
import asyncio
from datetime import timedelta

from database import db
from database import survey_db as sdb
from domain.cities import city_label
from handlers.comms import admin_poll_wizard as wiz
from handlers.comms.admin_poll_wizard import resolve_audience_tap
from handlers.states import PollCreate
from services.comms import polls as polls_svc
from services.forum.program import day_label, session_point_label
from services.infra.timeutil import msk_now
from services import scheduler as sched
from tests.test_polls_260822 import ADMIN_ID, FakeBot, _add_user, _cb, _msg, _ready, _state
from tests.test_roles_phase8 import _flat_callback_data


def _run(coro):
    return asyncio.run(coro)


def _header(monkeypatch, scope, label):
    async def view(_admin_id):
        return scope, label

    monkeypatch.setattr(wiz, "_admin_city_view", view)


def _spb(monkeypatch):
    _header(monkeypatch, ("spb", ()), "Санкт-Петербург")


def _all_cities(monkeypatch):
    _header(monkeypatch, None, "🌍 Все города")


def _today():
    return msk_now().strftime("%Y-%m-%d")


def _day(offset):
    return (msk_now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def _buttons(markup):
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _cbs(markup):
    return [cb for _, cb in _buttons(markup)]


def _entry(tid, day=None):
    stamp = None if day is None else f"{day} 12:00:00"
    _run(db.record_checkin(tid, "entry", source="test", scanned_at=stamp))


def _session(city, day, title, start="10:00", end="11:00"):
    return _run(db.create_program_session(city, day, start, end, title))


def _attend(tid, sid):
    _run(db.record_session_checkin(tid, sid, [], source="test"))


def _tap(choice, state, surface="poll", draft_id=None):
    return _run(resolve_audience_tap(
        choice, ADMIN_ID, state, surface=surface, draft_id=draft_id,
    ))


def _arm(state, name):
    _run(state.set_state(name))


def _city_value(spec):
    return next(item["value"] for item in spec if item.get("field") == "event_city")


def _allowed_poll_aud(callback_data):
    if not str(callback_data).startswith("poll_aud:"):
        return True
    body = callback_data[len("poll_aud:"):]
    if body in {"today", "session", "back", "all", "approved"}:
        return True
    if body.startswith(("track:", "city:")):
        return True
    head, _, tail = body.partition(":")
    if head == "today" and tail and ":" not in tail:
        return True
    if head == "sesscity" and tail and ":" not in tail:
        return True
    if head == "sessday" and len(tail) == 10 and tail[4] == "-" and tail[7] == "-":
        return True
    if head == "sess" and tail.isdigit():
        return True
    return False


def test_audience_kb_has_midcheck_buttons(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    text, kb = _run(wiz._audience_kb(ADMIN_ID))
    buttons = _buttons(kb)
    texts = " ".join(label for label, _cb in buttons)
    assert any("Пришли сегодня" in label and data == "poll_aud:today" for label, data in buttons)
    assert any(
        "Отмечены на этой сессии" in label and data == "poll_aud:session"
        for label, data in buttons
    )
    for forbidden in ("checkin_entry", "checkin_session", "session_id", "today"):
        assert forbidden not in texts
    assert not any(data.startswith("poll_aud:city:") for _, data in buttons)
    assert "Город из шапки" in text


def test_today_respects_header_city_and_ignores_forged_code(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    _run(_add_user(11, city="spb", status="approved"))
    _run(_add_user(12, city="spb", status="approved"))
    _run(_add_user(13, city="spb", status="approved"))
    _run(_add_user(14, city="msk", status="approved"))
    _entry(11, _today())
    _entry(12, _day(-1))
    _entry(14, _today())
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    tap = _tap("today", state)
    assert tap["kind"] == "ready"
    entry = next(item for item in tap["spec"] if item["field"] == "checkin_entry")
    assert entry["value"] == db.CHECKIN_YES and entry["day"] == db.CHECKIN_DAY_TODAY == "today"
    assert _city_value(tap["spec"]) == "spb"
    assert "exclude" in next(item for item in tap["spec"] if item["field"] == "event_city")
    assert tap["ids"] == [11]
    forged = _tap("today:msk", state)
    assert forged["kind"] == "ready" and _city_value(forged["spec"]) == "spb"
    assert forged["ids"] == [11]
    assert all(_allowed_poll_aud(data) for _, data in _buttons(tap.get("markup")))


def test_zero_today_refuses_and_is_not_everyone(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    _run(_add_user(21, city="spb"))
    _run(_add_user(22, city="msk"))
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    tap = _tap("today", state)
    assert tap["kind"] == "refuse"
    assert _run(state.get_state()) == "PollCreate:audience"
    assert "audience" not in _run(state.get_data())
    assert "отправлять некому" in tap["text"]
    callbacks = _cbs(tap["markup"])
    assert "poll_send_now" not in callbacks
    assert not any(item.startswith("psv_send:") for item in callbacks)
    spec = [
        {"field": "checkin_entry", "value": db.CHECKIN_YES, "day": "today", "label": "пришли сегодня"},
        {"field": "event_city", "value": "spb", "exclude": [], "label": "Санкт-Петербург"},
    ]
    assert _run(polls_svc.resolve_poll_audience({"id": 1, "audience": spec})) == []
    assert set(_run(db.get_all_users_ids())) == {21, 22}


def test_all_delegates_still_everyone_unknown_is_not(tmp_path):
    _ready(tmp_path)
    _run(_add_user(31))
    _run(_add_user(32))
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    everyone = _tap("all", state)
    assert everyone["kind"] == "ready" and everyone["spec"] == []
    assert set(everyone["ids"]) == {31, 32}
    unknown = _tap("nope", state)
    assert unknown["kind"] == "refuse" and unknown["spec"] != []
    assert "Не понял" in unknown["text"]
    assert _run(state.get_state()) == "PollCreate:audience"
    assert "audience" not in _run(state.get_data())


def test_session_button_filters_city_and_rejects_foreign(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    sid = _session("spb", _today(), "Открытие")
    msk_sid = _session("msk", _today(), "Чужая")
    _run(_add_user(41, city="spb", status="approved"))
    _run(_add_user(42, city="spb", status="approved"))
    _run(_add_user(43, city="msk", status="approved"))
    _run(_add_user(44, city="msk", status="approved"))
    _run(_add_user(45, city="spb", status="pending"))
    _attend(41, sid)
    _attend(43, sid)
    _attend(44, msk_sid)
    _attend(45, sid)
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    screen = _tap("session", state)
    assert screen["kind"] == "screen"
    buttons = _buttons(screen["markup"])
    labels = [label for label, _data in buttons]
    row = _run(db.get_program_session(sid))
    row["hall_name"] = None
    assert session_point_label(row) in labels
    assert all("session:" not in label for label in labels)
    assert f"poll_aud:sess:{sid}" in _cbs(screen["markup"])
    assert all(str(msk_sid) not in label for label in labels)
    assert all("Чужая" not in label for label in labels)
    assert all(_allowed_poll_aud(data) for _, data in buttons)
    picked = _tap(f"sess:{sid}", state)
    assert picked["kind"] == "ready"
    item = next(part for part in picked["spec"] if part["field"] == "checkin_session")
    assert item["value"] == db.SESSION_ATTENDED
    assert item["session_id"] == sid and type(item["session_id"]) is int
    assert "day" not in item
    assert _city_value(picked["spec"]) == "spb"
    assert picked["ids"] == [41]
    foreign = _tap(f"sess:{msk_sid}", state)
    assert foreign["kind"] == "refuse" and "другого города" in foreign["text"]
    assert "poll_send_now" not in _cbs(foreign["markup"])
    assert _run(state.get_state()) == "PollCreate:audience"
    assert "audience" not in _run(state.get_data())


def test_chief_without_city_does_not_query_every_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _all_cities(monkeypatch)
    sid = _session("msk", _today(), "Московская")
    _run(_add_user(51, city="msk", status="approved"))
    _run(_add_user(52, city="spb", status="approved"))
    _attend(51, sid)
    _attend(52, sid)
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    tap = _tap(f"sess:{sid}", state)
    assert tap["kind"] == "screen"
    assert tap["spec"] is None and not tap["ids"]
    assert _run(state.get_data()).get("mid_city") in (None, "")
    assert "audience" not in _run(state.get_data())
    texts = [label for label, _data in _buttons(tap["markup"])]
    assert _run(city_label("msk")) in texts
    assert "msk" not in " ".join(texts)
    assert any(data == "poll_aud:sesscity:msk" for _, data in _buttons(tap["markup"]))


def test_no_session_today_offers_other_days(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    other = _day(2)
    _session("spb", other, "Завтрак", start="09:00", end="10:00")
    _session("msk", other, "Чужой день")
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    days = _tap("session", state)
    assert days["kind"] == "screen"
    assert _run(state.get_state()) == "PollCreate:audience"
    label = day_label(other)
    buttons = _buttons(days["markup"])
    assert (label, f"poll_aud:sessday:{other}") in buttons
    assert all(_allowed_poll_aud(data) for _, data in buttons)
    sessions = _tap(f"sessday:{other}", state)
    shown = [text for text, _data in _buttons(sessions["markup"])]
    assert any("Завтрак" in text for text in shown)
    assert all("Чужой день" not in text for text in shown)
    assert all("session:" not in text for text in shown)


def test_all_cities_header_picks_a_city_before_the_spec(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _all_cities(monkeypatch)
    _run(_add_user(61, city="msk", status="approved"))
    _run(_add_user(62, city="spb", status="approved"))
    _entry(61)
    _entry(62)
    state = _state(ADMIN_ID)
    _arm(state, PollCreate.audience)
    asked = _tap("today", state)
    assert asked["kind"] == "screen" and asked["spec"] is None and not asked["ids"]
    buttons = _buttons(asked["markup"])
    texts = " ".join(label for label, _data in buttons)
    assert _run(city_label("msk")) in texts and _run(city_label("spb")) in texts
    for code in ("msk", "spb", "tyumen"):
        assert code not in texts
    assert any(data == "poll_aud:today:msk" for _, data in buttons)
    assert all(_allowed_poll_aud(data) for _, data in buttons)
    sessions = _tap("session", state)
    assert sessions["kind"] == "screen" and not sessions["ids"]
    assert any(data == "poll_aud:sesscity:spb" for _, data in _buttons(sessions["markup"]))
    missing = _tap("today:atlantis", state)
    assert missing["kind"] == "refuse" and "Такого города нет" in missing["text"]
    assert missing["spec"] != []
    picked = _tap("today:msk", state)
    assert picked["kind"] == "ready" and _city_value(picked["spec"]) == "msk"
    assert picked["ids"] == [61]
    assert any(item.get("day") == "today" for item in picked["spec"])


def test_audience_label_hides_field_codes():
    today = polls_svc.audience_label([
        {"field": "checkin_entry", "value": "yes", "day": "today", "label": "пришли сегодня"},
    ])
    session = polls_svc.audience_label([
        {
            "field": "checkin_session", "value": "attended", "session_id": 5,
            "label": "отмечены на «Открытие»",
        },
    ])
    bare = polls_svc.audience_label([
        {"field": "checkin_entry", "value": "yes", "day": "today"},
        {"field": "checkin_session", "value": "attended", "session_id": 9},
    ])
    for text in (today, session, bare):
        assert "checkin_entry" not in text and "checkin_session" not in text
        assert "session:" not in text
    assert "пришли сегодня" in today and "пришли сегодня" in bare
    assert "Открытие" in session
    assert "отмечены на сессии" in bare


def test_schedule_keeps_today_sentinel_and_refreshes(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    jobs = []
    monkeypatch.setattr(wiz, "schedule_poll_job", lambda pid, when: jobs.append((pid, when)))
    _run(_add_user(71, city="spb", status="approved"))
    _entry(71)
    state, bot = _state(ADMIN_ID), FakeBot()
    _cb("poll_new_single", state, bot)
    _msg("Как зал?", state, bot)
    _msg("Хорошо; Плохо", state, bot)
    _cb("poll_opts_done", state, bot)
    _cb("poll_settings_next", state, bot)
    _, ev = _cb("poll_aud:today", state, bot)
    preview = ev.message.answers[-1][0]
    assert "пересчитается" in preview
    confirm = _flat_callback_data(ev.message.answers[-1][2])
    assert "poll_send_now" in confirm and "poll_schedule" in confirm
    assert not any(item.startswith("poll_mid") or item.startswith("poll_sched_") for item in confirm)
    _, ev = _cb("poll_schedule", state, bot)
    assert "ДД.ММ.ГГГГ ЧЧ:ММ" in ev.message.text
    when = sched._now_moscow_naive() + timedelta(days=1)
    _msg(when.strftime("%d.%m.%Y %H:%M"), state, bot)
    polls = _run(db.list_polls(statuses=("scheduled",)))
    assert len(polls) == 1
    audience = polls[0]["audience"]
    assert audience and all(isinstance(item, dict) for item in audience)
    assert any(item.get("day") == "today" for item in audience)
    assert jobs and jobs[0][0] == polls[0]["id"]
    stored = _run(db.get_poll(polls[0]["id"]))
    assert _run(polls_svc.resolve_poll_audience(stored)) == [71]
    _run(_add_user(72, city="spb", status="approved"))
    _entry(72)
    assert set(_run(polls_svc.resolve_poll_audience(stored))) == {71, 72}


def _draft(state, bot):
    _cb("psv_new", state, bot)
    _msg("Итоги дня", state, bot)
    pid = _run(db.list_polls(statuses=("draft",)))[0]["id"]
    _run(sdb.add_survey_question(pid, "text", "Как вам?"))
    _run(sdb.update_survey_texts(pid, intro="Привет"))
    _, ev = _cb(f"psv_go:{pid}", state, bot)
    return pid, ev


def test_survey_same_refuse_and_header_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _spb(monkeypatch)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid, go = _draft(state, bot)
    assert "poll_cancel" not in _flat_callback_data(go.message.markup)
    assert f"psv_draft:{pid}" in _flat_callback_data(go.message.markup)
    _, ev = _cb("poll_aud:today", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:audience"
    assert "audience" not in _run(state.get_data())
    assert "отправлять некому" in ev.message.text
    assert "анкету" in ev.message.text and "опрос" not in ev.message.text
    callbacks = _flat_callback_data(ev.message.markup)
    assert "poll_cancel" not in callbacks
    assert not any(item.startswith("psv_send:") for item in callbacks)
    assert f"psv_draft:{pid}" in callbacks

    _run(_add_user(81, city="spb", status="approved"))
    _entry(81)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid, go = _draft(state, bot)
    assert "poll_cancel" not in _flat_callback_data(go.message.markup)
    _, ev = _cb("poll_aud:today", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:confirm"
    preview = ev.message.answers[-1][0]
    assert "1 чел." in preview and "пришли сегодня" in preview and "пересчитается" in preview
    confirm = _flat_callback_data(ev.message.answers[-1][2])
    assert f"psv_send:{pid}" in confirm and f"psv_sched:{pid}" in confirm
    assert f"psv_draft:{pid}" in confirm and "poll_cancel" not in confirm
    assert _city_value(_run(state.get_data())["audience"]) == "spb"
