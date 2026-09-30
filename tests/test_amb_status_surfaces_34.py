"""Правила входа в амбассадоры (`services/amb_status.py`) на поверхностях «Моя ссылка» в боте
и финального экрана анкеты / «Хочу свою ссылку» в Mini App.

pytest-asyncio в окружении нет — async через `asyncio.run()`, временная БД — тот же приём,
что `tests/test_amb_status_wiring_34.py`; HTTP — харнесс `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from services import amb_tiers
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

SEASON = "RT 26"
AT = "2026-09-30 12:00:00"
BOT = "TestBot"


def _run(coro):
    return asyncio.run(coro)


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


@pytest.fixture
def ready(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "test_amb_status_surfaces_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    # Правила отбора и лимита живут только при включённом модуле «🤝 Отбор амбассадоров».
    _run(db.set_setting("amb_team_selection_enabled", "on"))
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    return calls


def _seed(tid, *, status="approved", season=SEASON):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": season,
    }))
    if status:
        _run(db.set_user_status(tid, status))


def _mode(mode):
    _run(db.set_setting("amb_join_mode", mode))


def _limit(n):
    _run(db.set_setting("amb_slots_limit", str(n)))


def _st(tid):
    return _run(sdb.get_status(tid))


def _fill_slots(n, *, start=5000):
    for i in range(n):
        tid = start + i
        _seed(tid)
        _run(sdb.set_status(tid, "active", at=AT))
        assert _run(sdb.try_claim_slot(tid, limit=0, season=SEASON, at=AT))
    return [start + i for i in range(n)]


# ══════════════════════════════════════════════════════════════════════════════════════════
# «Моя ссылка» в боте
# ══════════════════════════════════════════════════════════════════════════════════════════

class _Me:
    username = BOT


class _Bot:
    async def get_me(self):
        return _Me()


class _Msg:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def answer(self, text=None, **kw):
        pass


class _Cb:
    def __init__(self, uid, data):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "language_code": "ru"})()
        self.message = _Msg()
        self.alerts: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


def _screen(uid):
    from handlers import user_actions as ua
    return _run(ua._referral_screen(uid, _Bot()))


def _datas(kb):
    return [b.callback_data for row in (kb.inline_keyboard if kb else []) for b in row]


def test_screen_open_shows_join_button_and_link(ready):
    _seed(10, status="pending")
    text, kb = _screen(10)
    assert f"https://t.me/{BOT}?start=amb_10" in text
    assert _datas(kb) == ["ambjoin"]
    assert _default("amb_slots_full_text") not in text


@pytest.mark.parametrize("case", ["full", "declined"])
def test_screen_full_or_declined_no_button_full_line(ready, case):
    _seed(11, status="pending")
    if case == "full":
        _limit(1)
        _fill_slots(1)
    else:
        _run(sdb.set_status(11, "declined", at=AT))
    text, kb = _screen(11)
    assert kb is None
    assert _default("amb_slots_full_text") in text
    assert "start=amb_11" in text  # ссылка для приглашений — всем


def test_screen_selection_candidate_sees_status_line(ready):
    _mode("selection")
    _seed(12, status="pending")
    _run(sdb.set_status(12, "candidate", at=AT))
    text, kb = _screen(12)
    assert kb is None
    assert _default("amb_status_candidate_text") in text
    assert "start=amb_12" in text


def test_screen_instant_candidate_can_still_join(ready):
    """Мигрированные «да» в режиме «сразу» — кнопка есть."""
    _seed(13, status="pending")
    _run(sdb.set_status(13, "candidate", at=AT))
    _text, kb = _screen(13)
    assert _datas(kb) == ["ambjoin"]


def test_screen_ambassador_keeps_path_and_leave(ready):
    _seed(14)
    _run(sdb.set_status(14, "active", at=AT))
    _text, kb = _screen(14)
    datas = _datas(kb)
    assert "ambleave" in datas and "ambpath:invite" in datas and "ambjoin" not in datas


def test_screen_state_failure_shows_button(ready, monkeypatch):
    from services import amb_status

    async def _boom(_tid):
        raise RuntimeError("БД легла")

    monkeypatch.setattr(amb_status, "delegate_state", _boom)
    _seed(15, status="pending")
    _text, kb = _screen(15)
    assert _datas(kb) == ["ambjoin"]


def _join(uid):
    from handlers import user_actions as ua
    cb = _Cb(uid, "ambjoin")
    _run(ua.ambassador_join(cb, _Bot()))
    return cb


def test_ambjoin_instant_joins_and_redraws(ready):
    _seed(20)
    cb = _join(20)
    assert _st(20)["status"] == "active"
    assert _st(20)["slot_at"]  # одобрен, лимита нет — место выдано
    assert ready == [20]
    text, kw = cb.message.edits[0]
    assert "ambleave" in _datas(kw["reply_markup"])
    assert cb.alerts == [(None, False)]


def test_ambjoin_selection_becomes_candidate_and_redraws(ready):
    _mode("selection")
    _seed(21, status="pending")
    cb = _join(21)
    assert _st(21)["status"] == "candidate"
    assert not _run(db.get_user(21))["is_ambassador"]
    text, kw = cb.message.edits[0]
    assert _default("amb_status_candidate_text") in text
    assert kw["reply_markup"] is None
    assert ready == []


@pytest.mark.parametrize("case", ["full", "declined"])
def test_ambjoin_stale_button_alerts_without_write(ready, case):
    _seed(22, status="pending")
    if case == "full":
        _limit(1)
        _fill_slots(1)
    else:
        _run(sdb.set_status(22, "declined", at=AT))
    before = _st(22)
    cb = _join(22)
    assert _st(22) == before
    assert not cb.message.edits
    assert cb.alerts == [(_default("amb_slots_full_text"), True)]


def test_ambjoin_long_full_text_trimmed_to_alert_limit(ready):
    _run(db.set_setting("amb_slots_full_text", "м" * 400))
    _limit(1)
    _fill_slots(1)
    _seed(23, status="pending")
    cb = _join(23)
    text, alert = cb.alerts[0]
    assert alert and len(text) <= 200 and text.endswith("…")


def _leave(uid):
    from handlers import user_actions as ua
    cb = _Cb(uid, "ambleave_go")
    _run(ua.ambassador_leave_confirm(cb))
    return cb


def test_ambleave_go_frees_slot_without_pack(ready):
    _limit(1)
    holder = _fill_slots(1)[0]
    _seed(30, status="pending")
    assert _screen(30)[1] is None  # места заняты
    _leave(holder)
    assert _st(holder)["status"] == "left"
    assert _st(holder)["slot_at"] is None
    assert _datas(_screen(30)[1]) == ["ambjoin"]  # кнопка у других вернулась


def test_ambleave_go_with_pack_keeps_slot(ready):
    _limit(1)
    holder = _fill_slots(1)[0]
    assert _run(sdb.set_pack(holder, True, at=AT))
    _leave(holder)
    assert _st(holder)["status"] == "left"
    assert _st(holder)["slot_at"]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Mini App: экран «Заявка принята» и POST /app/api/reg/ambassador
# ══════════════════════════════════════════════════════════════════════════════════════════

from tests.test_miniapp_form import _seed_draft, bot_api  # noqa: E402,F401 — фикстура bot_api
from tests.test_miniapp_routes import (  # noqa: E402
    DELEGATE_ID, UNREGISTERED_ID, _cfg, _client, _hdr, _set, _standard_seed,
)
from tests.test_miniapp_routes import _use_tmp_db as _use_tmp_http_db  # noqa: E402

APP_BOT = "YouLead_test_bot"


@pytest.fixture
def http(tmp_path, monkeypatch):
    path = _use_tmp_http_db(tmp_path, "test_amb_status_surfaces_34_http.db")
    _standard_seed()
    _set("amb_team_selection_enabled", "on")  # правила отбора — только при включённом модуле
    calls: list[int] = []

    async def _fake_tiers(tid):
        calls.append(int(tid))

    monkeypatch.setattr(amb_tiers, "check_tiers_for_new_ambassador", _fake_tiers)
    client = _client(_cfg(path))
    client.tier_calls = calls
    return client


def _force_full(monkeypatch):
    from services import amb_status

    async def _full():
        return True

    monkeypatch.setattr(amb_status, "slots_full", _full)


def _submit(client, patch=None):
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов", **(patch or {})})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.usefixtures("bot_api")
def test_finale_open_offers_block(http):
    _set("reg_offer_ref_link", "on")
    body = _submit(http)
    assert body["ambassador"]["cta"]
    assert "ambassador_note" not in body


@pytest.mark.usefixtures("bot_api")
@pytest.mark.parametrize("case", ["full", "declined"])
def test_finale_full_or_declined_no_block(http, monkeypatch, case):
    _set("reg_offer_ref_link", "on")
    if case == "full":
        _force_full(monkeypatch)
    else:
        from services import amb_status

        async def _declined(_tid):
            return "declined"

        monkeypatch.setattr(amb_status, "delegate_state", _declined)
    body = _submit(http)
    assert "ambassador" not in body
    assert "ambassador_note" not in body


@pytest.mark.usefixtures("bot_api")
def test_finale_selection_candidate_gets_ack_and_link(http):
    """Кандидату подтверждение обещает ссылку — она приходит тем же ответом, даже при
    выключенном предложении (так настроен РилТолк)."""
    _set("amb_join_mode", "selection")
    body = _submit(http, {"is_ambassador_candidate": True})
    assert _st(UNREGISTERED_ID)["status"] == "candidate"
    assert "ambassador" not in body
    assert body["ambassador_note"] == _default("amb_candidate_ack_text")
    link = body["ambassador_link"]
    assert link["link"] == f"https://t.me/{APP_BOT}?start=amb_{UNREGISTERED_ID}"
    assert link["copy_button"] and link["note"]


def _want_app(client):
    resp = client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_endpoint_instant_active_with_link(http):
    body = _want_app(http)
    assert body["state"] == "active"
    assert body["link"] == f"https://t.me/{APP_BOT}?start=amb_{DELEGATE_ID}"
    assert body["heading"] and body["copy_button"] and body["copied_toast"] and body["note"]
    assert _st(DELEGATE_ID)["status"] == "active"
    assert http.tier_calls == [DELEGATE_ID]


def test_endpoint_selection_candidate_with_link_and_note(http):
    _set("amb_join_mode", "selection")
    body = _want_app(http)
    assert body["state"] == "candidate"
    assert body["status_note"] == _default("amb_candidate_ack_text")
    assert body["link"] == f"https://t.me/{APP_BOT}?start=amb_{DELEGATE_ID}"
    assert _st(DELEGATE_ID)["status"] == "candidate"
    assert not _run(db.get_user(DELEGATE_ID))["is_ambassador"]
    again = _want_app(http)  # повторный тап — тот же ответ, без записи
    assert again["state"] == "candidate"


@pytest.mark.parametrize("case", ["full", "declined"])
def test_endpoint_full_or_declined_writes_nothing(http, monkeypatch, case):
    if case == "full":
        _force_full(monkeypatch)
    else:
        _run(sdb.set_status(DELEGATE_ID, "declined", at=AT))
    before = _st(DELEGATE_ID)
    body = _want_app(http)
    assert body == {"state": "full", "message": _default("amb_slots_full_text")}
    assert _st(DELEGATE_ID) == before
    assert http.tier_calls == []


def test_endpoint_ignores_body_telegram_id(http):
    """telegram_id — только из initData: чужой id в теле не делает амбассадором другого."""
    resp = http.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID),
                     json={"telegram_id": UNREGISTERED_ID})
    assert resp.status_code == 200
    assert _run(sdb.get_status(UNREGISTERED_ID)) is None


def test_form_js_handles_full_state_and_notes():
    from pathlib import Path

    from tests.test_miniapp_frontend import _js_without_comments

    src = _js_without_comments(Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js"
                               / "screens" / "form.js")
    assert 'res.state === "full"' in src
    assert "say(res.message" in src
    assert "res.status_note" in src
    assert "res.ambassador_note" in src and "res.ambassador_link" in src
