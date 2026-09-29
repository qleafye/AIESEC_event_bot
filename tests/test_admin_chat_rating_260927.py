"""Экран «🏆 Рейтинг чата» (handlers/admin_chat_rating.py): режим по городу, суммы правил,
галочки заданий для «упоминаний в соцсетях».

Бот для людей: режим и задания — кнопками, суммы — числом с примером; коды ключей, городов и
режимов менеджеру не показываются. pytest-asyncio недоступна — `asyncio.run()`; БД —
`tmp_path` через `fast_init_db`.
"""
from __future__ import annotations

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from core import cities
from config import config
from database import db
from handlers import admin_chat_rating as scr
from handlers.admin_caps import required_capability
from handlers.states import ChatRatingEdit
from tests._dbtpl import fast_init_db

ADMIN = 900927301


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, city="spb"):
    config.DB_PATH = str(tmp_path / "chat_rating.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]
    if city:
        _run(db.set_setting("event_city_enabled", "on"))
        assert _run(cities.set_admin_city(ADMIN, city))


class _User:
    def __init__(self, uid=ADMIN):
        self.id = uid


class _Message:
    def __init__(self, text=None):
        self.text = text
        self.html_text = text
        self.from_user = _User()
        self.answers = []
        self.edited = None
        self.markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)
        self.markup = reply_markup

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text
        self.markup = reply_markup


class _Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = _User()
        self.message = _Message()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _find(kb, prefix):
    return [cd for cd in _cbs(kb) if cd and cd.startswith(prefix)]


def test_screen_for_city_shows_city_and_selected_mode_without_codes(tmp_path):
    _ready(tmp_path)
    text, kb = _run(scr.render_chat_rating_screen(ADMIN))
    assert _run(cities.city_label("spb")) in text
    assert "✅ 📈 По формуле активности" in _texts(kb)
    assert "🪙 По правилам города" in _texts(kb)
    shown = text + "\n".join(_texts(kb))
    for code in ("chat_rules", "chat_rating", "formula", "rules", "spb"):
        assert code not in shown, code
    assert "settings_group:chat" in _cbs(kb)  # веса формулы — в группе «💬 Чат делегатов»
    assert "admin_sec:manage" in _cbs(kb)


def test_tap_rules_and_formula_write_city_mode(tmp_path):
    _ready(tmp_path)
    _, kb = _run(scr.render_chat_rating_screen(ADMIN))
    rules_cb = [cd for cd, t in zip(_cbs(kb), _texts(kb)) if "По правилам города" in t][0]
    cb = _Callback(rules_cb)
    _run(scr.chrate_mode(cb))
    assert _run(db.get_setting("chat_rating_mode__city__spb")) == "rules"
    assert _run(db.get_setting("chat_rating_mode")) is None
    assert "✅ 🪙 По правилам города" in _texts(cb.message.markup)
    rule_texts = [t for t in _texts(cb.message.markup) if t.startswith("🪙 Комментарий")]
    assert rule_texts and "10 баллы" in rule_texts[0]

    formula_cb = [cd for cd, t in zip(_cbs(cb.message.markup), _texts(cb.message.markup))
                  if "По формуле" in t][0]
    _run(scr.chrate_mode(_Callback(formula_cb)))
    assert _run(db.get_setting("chat_rating_mode__city__spb")) == "formula"


def test_rules_screen_shows_currency(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("chat_rating_mode__city__spb", "rules"))
    _run(db.set_setting("chat_rules_currency__city__spb", "коины"))
    text, kb = _run(scr.render_chat_rating_screen(ADMIN))
    assert "10 коины" in text
    assert any("коины" in t for t in _texts(kb))


def _edit(name):
    _, kb = _run(scr.render_chat_rating_screen(ADMIN))
    return [cd for cd in _find(kb, "chrate:edit:") if cd.endswith(f":{name}")][0]


def test_edit_rule_amount_saves_number_under_city_key(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("chat_rating_mode__city__spb", "rules"))
    state = _state()
    cb = _Callback(_edit("comment_points"))
    _run(scr.chrate_edit(cb, state))
    assert _run(state.get_state()) == ChatRatingEdit.waiting_for_value.state
    assert "Сколько баллов за ответ делегата на пост команды" in cb.message.edited

    bad = _Message("abc")
    _run(scr.chrate_value(bad, state))
    assert "<code>" in bad.answers[-1]
    assert _run(db.get_setting("chat_rules_comment_points__city__spb")) is None
    assert _run(state.get_state()) == ChatRatingEdit.waiting_for_value.state

    good = _Message("0,5")
    _run(scr.chrate_value(good, state))
    assert _run(db.get_setting("chat_rules_comment_points__city__spb")) == "0.5"
    assert _run(state.get_state()) is None
    assert any("0,5 баллы" in t for t in _texts(good.markup))


def test_reset_rule_deletes_city_override(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("chat_rating_mode__city__spb", "rules"))
    _run(db.set_setting("chat_rules_comment_points__city__spb", "12"))
    state = _state()
    cb = _Callback(_edit("comment_points"))
    _run(scr.chrate_edit(cb, state))
    reset = _find(cb.message.markup, "chrate:rst:")
    assert reset, "у города своё значение — должна быть кнопка сброса"
    assert "↩️ Как в остальных городах" in _texts(cb.message.markup)
    cb2 = _Callback(reset[0])
    _run(scr.chrate_reset(cb2, state))
    assert _run(db.get_setting("chat_rules_comment_points__city__spb")) is None
    assert cb2.answers and cb2.answers[0][0]
    assert _run(state.get_state()) is None


def test_task_picker_lists_city_tasks_and_toggles(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("chat_rating_mode__city__spb", "rules"))
    spb = _run(db.create_task("Пост в сторис", "social", 10, "photo", "2026-10-01 00:00", ADMIN,
                              event_city="spb", title="Сторис про форум"))
    common = _run(db.create_task("Репост", "social", 5, "photo", "2026-10-01 00:00", ADMIN,
                                 title="Репост анонса"))
    msk = _run(db.create_task("Москва", "social", 5, "photo", "2026-10-01 00:00", ADMIN,
                              event_city="msk", title="Только Москва"))
    old = _run(db.create_task("Старое", "social", 5, "photo", "2026-10-01 00:00", ADMIN,
                              event_city="spb", title="Старое задание"))
    _run(db.archive_task(old))

    _, kb = _run(scr.render_chat_rating_screen(ADMIN))
    cb = _Callback(_find(kb, "chrate:tasks:")[0])
    _run(scr.chrate_tasks(cb))
    texts = _texts(cb.message.markup)
    assert any("Сторис про форум" in t for t in texts)
    assert any("Репост анонса" in t for t in texts)
    assert any("Старое задание" in t and "(архив)" in t for t in texts)
    assert not any("Только Москва" in t for t in texts)
    assert not any(str(spb) == t for t in texts)

    toggle = [cd for cd in _find(cb.message.markup, "chrate:task:") if cd.endswith(f":{spb}")][0]
    cb2 = _Callback(toggle)
    _run(scr.chrate_task_toggle(cb2))
    assert _run(db.get_setting("chat_rules_social_tasks__city__spb")) == str(spb)
    assert any(t.startswith("✅") and "Сторис про форум" in t for t in _texts(cb2.message.markup))

    toggle2 = [cd for cd in _find(cb2.message.markup, "chrate:task:") if cd.endswith(f":{common}")][0]
    _run(scr.chrate_task_toggle(_Callback(toggle2)))
    assert _run(db.get_setting("chat_rules_social_tasks__city__spb")) == f"{spb}\n{common}"

    _run(scr.chrate_task_toggle(_Callback(toggle)))
    assert _run(db.get_setting("chat_rules_social_tasks__city__spb")) == str(common)

    # задание другого города по подделанной кнопке — не пишется
    cb3 = _Callback(f"chrate:task:spb:{msk}")
    _run(scr.chrate_task_toggle(cb3))
    assert _run(db.get_setting("chat_rules_social_tasks__city__spb")) == str(common)
    assert cb3.answers and cb3.answers[0][1]


def test_module_off_edits_global_keys(tmp_path):
    _ready(tmp_path, city=None)
    text, kb = _run(scr.render_chat_rating_screen(ADMIN))
    assert "admin_city_switch:manage" not in _cbs(kb)
    rules_cb = [cd for cd, t in zip(_cbs(kb), _texts(kb)) if "По правилам города" in t][0]
    _run(scr.chrate_mode(_Callback(rules_cb)))
    assert _run(db.get_setting("chat_rating_mode")) == "rules"
    state = _state()
    _run(scr.chrate_edit(_Callback(_edit("referral_points")), state))
    _run(scr.chrate_value(_Message("7"), state))
    assert _run(db.get_setting("chat_rules_referral_points")) == "7"


def test_stale_button_of_other_city_writes_nothing(tmp_path):
    _ready(tmp_path)
    cb = _Callback("chrate:mode:msk:rules")
    _run(scr.chrate_mode(cb))
    assert _run(db.get_setting("chat_rating_mode__city__msk")) is None
    assert _run(db.get_setting("chat_rating_mode__city__spb")) is None
    assert cb.answers and cb.answers[0][1]
    forged = _Callback("chrate:mode:zzz:rules")
    _run(scr.chrate_mode(forged))
    assert _run(db.get_setting("chat_rating_mode__city__zzz")) is None


def test_capabilities_are_settings():
    assert required_capability(callback_data="admin_chat_rating") == "settings"
    assert required_capability(callback_data="chrate:mode:spb:rules") == "settings"
    assert required_capability(callback_data="chrate:task:spb:12") == "settings"
    assert required_capability(raw_state="ChatRatingEdit:waiting_for_value") == "settings"


def test_typed_value_reaches_handler_through_real_dispatcher(tmp_path):
    """Ввод числа в состоянии ChatRatingEdit не перехватывается ни одним хендлером, стоящим
    раньше в admin.router (наш — последним message-хендлером роутера)."""
    from aiogram import Bot
    from aiogram.dispatcher.event.bases import UNHANDLED

    import tests.test_refac_snapshot_260816 as snap
    from handlers import admin as admin_mod

    snap._roles_ready(tmp_path)
    dp = snap._full_dispatcher()
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        fsm = dp.fsm.resolve_context(bot, chat_id=snap.ADMIN_ID, user_id=snap.ADMIN_ID)
        _run(fsm.set_state(ChatRatingEdit.waiting_for_value))
        with snap._spied(admin_mod, "message", "chrate_value") as calls:
            result = _run(dp.feed_update(bot, snap._make_message_update(1, "12,5", snap.ADMIN_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1
    finally:
        _run(bot.session.close())
