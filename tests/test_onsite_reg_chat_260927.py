"""Регистрация на месте (FORUM-CHECKIN.md D-41), чатовая часть: короткая анкета по ссылке
`?start=walkin_<город>` (handlers/onsite_reg.py + перехват в handlers/registration.py::
cmd_start) и экран менеджера «📝 Регистрация на месте» (handlers/admin_onsite_reg.py, строка
хаба «🎪 Форум: функции»).

pytest-asyncio недоступна — async через `asyncio.run()` (конвенция проекта)."""
from __future__ import annotations

import asyncio
import sqlite3

import cities as cities_mod
from config import config
from database import db
from handlers import onsite_reg as onsite
from handlers import registration as reg
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import _fresh_state

ADMIN_ID = 927201
MANAGER_ID = 927210
WALKER_ID = 927220


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="onsite_chat.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _two_cities():
    return [
        {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
    ]


class _Cities:
    """Контекст: два города + модуль городов включён."""

    def __enter__(self):
        self.saved = list(cities_mod.CITIES)
        cities_mod.set_cities_for_test(_two_cities())
        _run(db.set_setting("event_city_enabled", "on"))
        return self

    def __exit__(self, *exc):
        cities_mod.set_cities_for_test(self.saved)


def _enable(city="spb"):
    key = cities_mod.per_city_key("onsite_reg_enabled", city)
    _run(db.set_setting(key, "on"))


def _setting(key):
    from settings_schema import get_setting_typed
    return _run(get_setting_typed(key))


# ── Фейки aiogram ─────────────────────────────────────────────────────────────────────────

class _User:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _Contact:
    def __init__(self, phone):
        self.phone_number = phone


class _Msg:
    def __init__(self, uid, text=None, username="walker", contact=None):
        self.from_user = _User(uid, username, "Walker")
        self.chat = _Chat(uid)
        self.text = text
        self.contact = contact
        self.answers = []  # [(text, kwargs)]

    async def answer(self, text, *a, **k):
        self.answers.append((text, k))

    async def answer_photo(self, *a, **k):
        self.answers.append(("<photo>", k))

    def texts(self):
        return [t for t, _k in self.answers]


class _Cb:
    def __init__(self, uid, data):
        self.from_user = _User(uid, "walker")
        self.data = data
        self.message = _Msg(uid)
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


class _Cmd:
    def __init__(self, args):
        self.args = args


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))

    async def get_chat_member(self, *a, **k):
        raise RuntimeError("нет сети в тестах")


def _start(uid, args, state):
    msg = _Msg(uid)
    _run(reg.cmd_start(msg, state, bot=_Bot(), command=_Cmd(args)))
    return msg


def _insert_user(uid, status, season, city="spb"):
    con = sqlite3.connect(config.DB_PATH)
    con.execute(
        "INSERT INTO users (telegram_id, full_name, status, season, event_city, email, phone) "
        "VALUES (?, 'Петров Пётр', ?, ?, ?, 'p@x.ru', '+70000000000')",
        (uid, status, season, city),
    )
    con.commit()
    con.close()


def _user_row(uid):
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
    con.close()
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 1: короткая анкета в чате
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_walkin_toggle_off_answers_closed_and_starts_nothing(tmp_path):
    _ready(tmp_path)
    with _Cities():
        state = _fresh_state(WALKER_ID)
        msg = _start(WALKER_ID, "walkin_spb", state)
        assert msg.texts() == [_setting("onsite_reg_closed_text")]
        assert _run(state.get_state()) is None
        assert _user_row(WALKER_ID) is None


def _walk_to_name(state):
    msg = _start(WALKER_ID, "walkin_spb", state)
    assert _run(state.get_state()) == onsite.OnsiteReg.consent.state
    intro = _setting("onsite_reg_intro_text")
    assert msg.answers[0][0] == intro
    kb = msg.answers[0][1]["reply_markup"]
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "onsite_consent"
    assert btn.text == _setting("onsite_reg_consent_button_text")
    cb = _Cb(WALKER_ID, "onsite_consent")
    _run(onsite.onsite_consent(cb, state))
    assert _run(state.get_state()) == onsite.OnsiteReg.name.state
    assert _setting("onsite_reg_name_prompt_text") in cb.message.texts()
    return cb


def test_walkin_full_flow_creates_pending_walkin_row(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL 26/2"))
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        _walk_to_name(state)

        m = _Msg(WALKER_ID, "Иванова Мария")
        _run(onsite.onsite_name(m, state))
        assert _run(state.get_state()) == onsite.OnsiteReg.phone.state
        assert m.answers[0][0] == _setting("onsite_reg_phone_prompt_text")
        kb = m.answers[0][1]["reply_markup"]
        assert kb.keyboard[0][0].request_contact is True

        m = _Msg(WALKER_ID, contact=_Contact("79991234567"))
        _run(onsite.onsite_phone_contact(m, state))
        assert _run(state.get_state()) == onsite.OnsiteReg.university.state
        assert m.answers[0][0] == _setting("onsite_reg_university_prompt_text")
        skip = m.answers[0][1]["reply_markup"].inline_keyboard[0][0]
        assert skip.callback_data == "onsite_skip"

        m = _Msg(WALKER_ID, "СПбГУ")
        _run(onsite.onsite_university(m, state))

    row = _user_row(WALKER_ID)
    assert row["status"] == "pending"
    assert row["onsite_kind"] == "walkin"
    assert row["event_city"] == "spb"
    assert row["season"] == "YL 26/2"
    assert row["full_name"] == "Иванова Мария"
    assert row["phone"] == "+79991234567"
    assert row["university"] == "СПбГУ"
    assert row["username"] == "@walker"
    consents = _run(db.get_user_consents(WALKER_ID))
    assert "onsite" in consents
    done = _setting("onsite_reg_done_text").replace("{name}", "Иванова Мария")
    assert m.answers[-1][0] == done
    from aiogram.types import ReplyKeyboardRemove
    assert isinstance(m.answers[-1][1]["reply_markup"], ReplyKeyboardRemove)
    assert _run(state.get_state()) is None
    # D-02: никакого QR и фото после анкеты
    assert "<photo>" not in m.texts()


def test_walkin_text_phone_and_skip_university(tmp_path):
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        _walk_to_name(state)
        _run(onsite.onsite_name(_Msg(WALKER_ID, "  Иванова Мария-Анна  "), state))
        _run(onsite.onsite_phone_text(_Msg(WALKER_ID, "+7 999 123-45-67"), state))
        assert _run(state.get_state()) == onsite.OnsiteReg.university.state
        cb = _Cb(WALKER_ID, "onsite_skip")
        _run(onsite.onsite_skip(cb, state))
    row = _user_row(WALKER_ID)
    assert row["university"] is None
    assert row["full_name"] == "Иванова Мария-Анна"
    assert row["phone"] == "+7 999 123-45-67"
    assert _run(state.get_state()) is None


def test_walkin_bad_name_keeps_step(tmp_path):
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        _walk_to_name(state)
        for bad in ("Мария", "Иванова 123", "12 34"):
            m = _Msg(WALKER_ID, bad)
            _run(onsite.onsite_name(m, state))
            assert m.texts() == [_setting("onsite_reg_bad_name_text")]
            assert _run(state.get_state()) == onsite.OnsiteReg.name.state


def test_walkin_bad_phone_keeps_step(tmp_path):
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        _walk_to_name(state)
        _run(onsite.onsite_name(_Msg(WALKER_ID, "Иванова Мария"), state))
        for bad in ("не скажу", "Пропустить", "12", ""):
            m = _Msg(WALKER_ID, bad)
            _run(onsite.onsite_phone_text(m, state))
            assert m.texts() == [_setting("onsite_reg_bad_phone_text")]
            assert _run(state.get_state()) == onsite.OnsiteReg.phone.state


def test_walkin_approved_current_season_sees_my_qr_hint(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL 26/2"))
    _insert_user(WALKER_ID, "approved", "YL 26/2")
    before = _user_row(WALKER_ID)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        msg = _start(WALKER_ID, "walkin_spb", state)
    assert msg.texts() == [_setting("onsite_reg_already_approved_text")]
    assert _user_row(WALKER_ID) == before
    assert _run(state.get_state()) is None


def test_walkin_existing_pending_rejected_past_season_see_existing_text(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_season", "YL 26/2"))
    cases = [("pending", "YL 26/2"), ("rejected", "YL 26/2"), ("approved", "YL 26/1")]
    with _Cities():
        _enable("spb")
        for i, (status, season) in enumerate(cases):
            uid = WALKER_ID + 100 + i
            _insert_user(uid, status, season)
            before = _user_row(uid)
            state = _fresh_state(uid)
            msg = _start(uid, "walkin_spb", state)
            assert msg.texts() == [_setting("onsite_reg_existing_text")], status
            assert _user_row(uid) == before
            assert _run(state.get_state()) is None


def test_walkin_without_city_uses_default_city_when_cities_module_off(tmp_path):
    _ready(tmp_path)
    # модуль городов выключен (дефолт) -> глобальный тумблер
    _run(db.set_setting("onsite_reg_enabled", "on"))
    state = _fresh_state(WALKER_ID)
    msg = _start(WALKER_ID, "walkin", state)
    assert msg.answers[0][0] == _setting("onsite_reg_intro_text")
    data = _run(state.get_data())
    assert data["onsite_city"] == cities_mod.default_city_code()


def test_walkin_unknown_city_falls_through_to_normal_start(tmp_path):
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        msg = _start(WALKER_ID, "walkin_xxx", state)
        st = _run(state.get_state())
        assert st is None or not st.startswith("OnsiteReg")
        assert _setting("onsite_reg_intro_text") not in msg.texts()
        assert _setting("onsite_reg_closed_text") not in msg.texts()
        assert msg.answers  # обычный /start что-то ответил
        assert _user_row(WALKER_ID) is None


def test_walkin_final_step_lost_race_answers_existing(tmp_path):
    """Строка появилась параллельно (например, человек успел подать полную анкету) —
    create_onsite_user вернул False, анкета не затёрта."""
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        _walk_to_name(state)
        _run(onsite.onsite_name(_Msg(WALKER_ID, "Иванова Мария"), state))
        _run(onsite.onsite_phone_text(_Msg(WALKER_ID, "+79991234567"), state))
        _insert_user(WALKER_ID, "pending", "YL 26/2")
        m = _Msg(WALKER_ID, "СПбГУ")
        _run(onsite.onsite_university(m, state))
    assert m.answers[-1][0] == _setting("onsite_reg_existing_text")
    assert _user_row(WALKER_ID)["full_name"] == "Петров Пётр"
    assert _run(state.get_state()) is None


def test_start_walkin_intercepts_before_language_question(tmp_path, monkeypatch):
    """D-41: перехват стоит до offer_language/предотбора — человек у стойки не должен упереться
    в экран языка или отсев по таблице."""
    _ready(tmp_path)
    called = []

    async def _fake_offer(*a, **k):
        called.append(1)
        return True

    import handlers.reg_lang as reg_lang
    monkeypatch.setattr(reg_lang, "offer_language", _fake_offer)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        msg = _start(WALKER_ID, "walkin_spb", state)
    assert not called
    assert msg.answers[0][0] == _setting("onsite_reg_intro_text")


def test_onsite_router_is_included_before_registration_in_main():
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("main.py").read_text(encoding="utf-8")
    i_onsite = src.index("dp.include_router(onsite_reg.router)")
    i_reg = src.index("dp.include_router(registration.router)")
    assert i_onsite < i_reg


def test_walkin_logs_have_no_phone_or_name(tmp_path, caplog):
    import logging
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        state = _fresh_state(WALKER_ID)
        with caplog.at_level(logging.DEBUG):
            _walk_to_name(state)
            _run(onsite.onsite_name(_Msg(WALKER_ID, "Иванова Мария"), state))
            _run(onsite.onsite_phone_text(_Msg(WALKER_ID, "+79991234567"), state))
            _run(onsite.onsite_skip(_Cb(WALKER_ID, "onsite_skip"), state))
    # Только логи бота (aiosqlite на DEBUG печатает параметры SQL — это не наш лог).
    joined = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith(("handlers", "services"))
    )
    assert "79991234567" not in joined
    assert "Иванова" not in joined


# ══════════════════════════════════════════════════════════════════════════════════════════
# Task 2: экран менеджера «📝 Регистрация на месте» и строка хаба «🎪 Форум: функции»
# ══════════════════════════════════════════════════════════════════════════════════════════

from types import SimpleNamespace  # noqa: E402

from handlers import admin_forum_functions as aff  # noqa: E402
from handlers import admin_onsite_reg as aor  # noqa: E402
from handlers.admin_caps import required_capability  # noqa: E402
from tests.test_roles_phase8 import dispatch_callback  # noqa: E402


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class _AdminMsg:
    def __init__(self):
        self.answers = []  # (text, kwargs)
        self.edits = []
        self.photos = []  # (photo, kwargs)

    async def answer(self, text, **k):
        self.answers.append((text, k))

    async def edit_text(self, text, **k):
        self.edits.append((text, k))

    async def answer_photo(self, photo, **k):
        self.photos.append((photo, k))


class _AdminCb:
    def __init__(self, data, uid=ADMIN_ID):
        self.data = data
        self.from_user = _User(uid)
        self.message = _AdminMsg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append((text, show_alert))


class _MeBot:
    def __init__(self, username="yl_test_bot"):
        self.username = username

    async def me(self):
        return SimpleNamespace(username=self.username)


def test_hub_has_onsite_row_and_button(tmp_path):
    _ready(tmp_path)
    with _Cities():
        text, kb = _run(aff._render_hub(ADMIN_ID, "spb"))
    assert "📝 Регистрация на месте: ❌ Выкл" in text
    assert "onsitereg_cfg:spb" in _cbs(kb)


def test_hub_row_shows_on_after_enable(tmp_path):
    _ready(tmp_path)
    with _Cities():
        _enable("spb")
        text, _kb = _run(aff._render_hub(ADMIN_ID, "spb"))
    assert "📝 Регистрация на месте: ✅ Вкл" in text


def test_onsite_cfg_screen_shows_toggle_explanation_and_qr_button(tmp_path):
    _ready(tmp_path)
    with _Cities():
        cb = _AdminCb("onsitereg_cfg:spb")
        _run(aor.onsitereg_cfg_screen(cb))
    text, k = cb.message.answers[-1]
    assert "Регистрация на месте" in text
    assert "СПб" in text
    assert "волонт" in text.lower()
    assert "пакет" in text.lower()
    cbs = _cbs(k["reply_markup"])
    assert "onsitereg_toggle:spb" in cbs
    assert "onsitereg_qr:spb" in cbs
    assert "admin_forum_functions" in cbs
    # человеческие подписи, без ключей настроек
    assert "onsite_reg_enabled" not in text


def test_onsite_toggle_flips_only_own_city(tmp_path):
    from cities import get_setting_typed_for_city
    _ready(tmp_path)
    with _Cities():
        cb = _AdminCb("onsitereg_toggle:spb")
        _run(aor.onsitereg_toggle_go(cb))
        assert _run(get_setting_typed_for_city("onsite_reg_enabled", "spb")) == "on"
        assert _run(get_setting_typed_for_city("onsite_reg_enabled", "msk")) == "off"
        assert cb.alerts[-1] == ("✅ Вкл", True)
        assert cb.message.edits  # экран перерисован
        cb2 = _AdminCb("onsitereg_toggle:spb")
        _run(aor.onsitereg_toggle_go(cb2))
        assert _run(get_setting_typed_for_city("onsite_reg_enabled", "spb")) == "off"
        assert cb2.alerts[-1] == ("❌ Выкл", True)


def test_onsite_qr_sends_png_with_walkin_link(tmp_path):
    _ready(tmp_path)
    with _Cities():
        cb = _AdminCb("onsitereg_qr:spb")
        _run(aor.onsitereg_qr_send(cb, _MeBot()))
    assert cb.message.photos
    photo, k = cb.message.photos[-1]
    assert photo.data.startswith(b"\x89PNG")
    assert "https://t.me/yl_test_bot?start=walkin_spb" in k["caption"]
    assert "распечат" in k["caption"].lower()


def test_onsite_qr_without_bot_username_alerts(tmp_path):
    _ready(tmp_path)
    with _Cities():
        cb = _AdminCb("onsitereg_qr:spb")
        _run(aor.onsitereg_qr_send(cb, _MeBot(username=None)))
    assert not cb.message.photos
    assert cb.alerts and "имя бота" in (cb.alerts[-1][0] or "")


def test_onsite_screens_respect_manager_city_binding(tmp_path):
    from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT
    _ready(tmp_path)
    with _Cities():
        _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
        _run(db.set_staff_city(MANAGER_ID, "msk"))
        for data in ("onsitereg_cfg:spb", "onsitereg_toggle:spb", "onsitereg_qr:spb"):
            _result, event = dispatch_callback(data, MANAGER_ID)
            assert event.answers, data
            assert event.answers[0][0] == _CITY_FORBIDDEN_ALERT, data
        from cities import get_setting_typed_for_city
        assert _run(get_setting_typed_for_city("onsite_reg_enabled", "spb")) == "off"


def test_onsite_callbacks_need_moderate_reg():
    for data in ("onsitereg_cfg:spb", "onsitereg_toggle:spb", "onsitereg_qr:spb"):
        assert required_capability(callback_data=data) == "moderate_reg"
