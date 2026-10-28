"""Тексты геймы и волн по городам и `{chat}` — ссылка на чат города делегата.

Проверяется: реестр (29 городских текстов + «💬 Ссылка на чат города»), проверка ссылки при
сохранении, подстановка `{chat}` в проверке подстановок, перевод, резолвер
`services/game/city_texts.py`, джобы и фоновые сообщения (город ПОЛУЧАТЕЛЯ), экраны бота и
Mini App, сторож мест чтения. Без городских значений — общий текст байт-в-байт как раньше.

asyncio.run + временная БД в tmp_path (pytest-asyncio в окружении нет), без сети.
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import db
import domain.cities as cities
from domain.settings.city_chat import CHAT_LINK_KEY, CITY_CHAT_TEXT_KEYS, normalize_chat_link
from domain.settings.schema import SETTINGS_SCHEMA
from services.game import city_texts
from tests._dbtpl import fast_init_db

A = cities.CITIES[0]["code"]
B = cities.CITIES[1]["code"]
LINK_A = "https://t.me/+chatA_1"
LINK_B = "https://t.me/+chatB_2"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="city_game_texts.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _enable_cities():
    _run(db.set_setting("event_city_enabled", "on"))


def _set(key, value):
    _run(db.set_setting(key, value))


def _set_city(key, code, value):
    _run(db.set_setting(cities.per_city_key(key, code), value))


def _seed_user(tid, *, event_city=None, lang=None):
    _run(db.add_user({
        "telegram_id": tid, "full_name": f"Delegate {tid}",
        "registration_date": "2026-01-01 00:00:00", "event_city": event_city,
    }))


# ── реестр ─────────────────────────────────────────────────────────────────────────────────

def test_registry_exactly_29_city_texts_plus_chat_link():
    assert len(CITY_CHAT_TEXT_KEYS) == 29
    per_city_game = {
        k for k, v in SETTINGS_SCHEMA.items()
        if v.get("per_city") and v.get("group") in ("amb", "game")
    }
    assert per_city_game == CITY_CHAT_TEXT_KEYS | {CHAT_LINK_KEY}


def test_chat_link_entry_and_screen_position():
    from domain.settings.amb_fields import AMB_FIELD_ORDER

    e = SETTINGS_SCHEMA[CHAT_LINK_KEY]
    assert e["group"] == "amb" and e["per_city"] is True
    assert e["format"] == "tg_link" and e["default"] == ""
    assert e["label"] == "💬 Ссылка на чат города"
    order = AMB_FIELD_ORDER
    assert order.index(CHAT_LINK_KEY) < order.index("wave_start_message_text")


# ── проверка ссылки ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("t.me/+AbC_d-1", "https://t.me/+AbC_d-1"),
    ("@yulid_msk", "https://t.me/yulid_msk"),
    ("https://telegram.me/yulid", "https://t.me/yulid"),
    ("http://t.me/yulid", "https://t.me/yulid"),
    ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"),
    ("нет", ""),
])
def test_validate_chat_link_accepts_and_normalizes(raw, expected):
    from domain.settings.validation import validate_setting_value

    assert validate_setting_value(CHAT_LINK_KEY, raw) == (expected, None)
    assert validate_setting_value(cities.per_city_key(CHAT_LINK_KEY, A), raw) == (expected, None)


@pytest.mark.parametrize("raw", ["просто текст", "https://evil.com/x", "https://t.me/a{b}",
                                 "https://t.me/<b>", "@abc"])
def test_validate_chat_link_rejects_with_example(raw):
    from domain.settings.validation import validate_setting_value

    for key in (CHAT_LINK_KEY, cities.per_city_key(CHAT_LINK_KEY, A)):
        value, err = validate_setting_value(key, raw)
        assert value is None
        assert "https://t.me/+AbCdEf123" in err and "@yulid_msk" in err


def test_normalize_chat_link_none_on_garbage():
    assert normalize_chat_link("") is None
    assert normalize_chat_link("javascript:alert(1)") is None


# ── подстановки и перевод ──────────────────────────────────────────────────────────────────

def test_placeholders_know_chat_for_city_texts_only():
    import domain.settings.placeholders as sp

    prev = SETTINGS_SCHEMA["wave_deadline_reminder_text"]["default"]
    assert sp.check("wave_deadline_reminder_text", prev + " {chat}", prev).ok
    composed = cities.per_city_key("wave_deadline_reminder_text", A)
    assert sp.check(composed, prev + " {chat}", prev).ok
    other = sp.check("game_proof_done_hint", "жми {chat}", "жми")
    assert not other.ok and other.unknown[0][0] == "chat"
    assert "Ссылка на чат города" in sp.PLACEHOLDER_LABELS["chat"]
    assert "{chat}" in sp.hint("amb_taken_text")


def test_i18n_sources_skip_link_keep_city_texts():
    from services.i18n.i18n_sources import is_delegate_dynamic_key

    assert is_delegate_dynamic_key(CHAT_LINK_KEY) is False
    assert is_delegate_dynamic_key(cities.per_city_key("wave_deadline_reminder_text", A)) is True


# ── резолвер ───────────────────────────────────────────────────────────────────────────────

def test_city_chat_link_module_off_reads_common(tmp_path):
    _ready(tmp_path)
    _set(CHAT_LINK_KEY, LINK_A)
    assert _run(city_texts.city_chat_link(A)) == LINK_A
    assert _run(city_texts.city_chat_link(None)) == LINK_A


def test_city_chat_link_module_on_no_fallback(tmp_path):
    _ready(tmp_path)
    _enable_cities()
    _set(CHAT_LINK_KEY, LINK_A)
    assert _run(city_texts.city_chat_link(A)) == ""  # только общий — не протекает
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    assert _run(city_texts.city_chat_link(A)) == LINK_A
    assert _run(city_texts.city_chat_link(B)) == ""
    assert _run(city_texts.city_chat_link(None)) == ""
    assert _run(city_texts.city_chat_link("*")) == ""


def test_delegate_city_dict_int_and_module_off(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed_user(501, event_city=B)
    assert _run(city_texts.delegate_city(501)) is None  # модуль выключен
    _enable_cities()
    assert _run(city_texts.delegate_city(501)) == B

    async def boom(*a, **k):
        raise AssertionError("dict не должен ходить в БД")
    monkeypatch.setattr(db, "get_user", boom)
    assert _run(city_texts.delegate_city({"event_city": A})) == A
    assert _run(city_texts.delegate_city(None)) is None


def test_city_text_override_other_city_and_default(tmp_path):
    _ready(tmp_path)
    _enable_cities()
    _seed_user(601, event_city=A)
    _seed_user(602, event_city=B)
    key = "game_submit_accepted_text"
    _set_city(key, A, "Принято! Обсуждение — {chat}")
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    assert _run(city_texts.city_text(key, 601)) == f"Принято! Обсуждение — {LINK_A}"
    assert _run(city_texts.city_text(key, 602)) == SETTINGS_SCHEMA[key]["default"]


def test_city_text_without_link_drops_token(tmp_path):
    _ready(tmp_path)
    _enable_cities()
    _seed_user(611, event_city=A)
    _set("game_submit_accepted_text", "Принято! {chat}")
    assert _run(city_texts.city_text("game_submit_accepted_text", 611)) == "Принято! "


def test_city_text_translates_source_with_chat(tmp_path):
    from services.i18n.i18n import src_hash

    _ready(tmp_path)
    _enable_cities()
    _seed_user(621, event_city=A)
    src = "Принято! Чат: {chat}"
    _set_city("game_submit_accepted_text", A, src)
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    tr_map = {src_hash(src): "Accepted! Chat: {chat}"}
    out = _run(city_texts.city_text("game_submit_accepted_text", 621, lang="en", tr_map=tr_map))
    assert out == f"Accepted! Chat: {LINK_A}"


# ── джобы и фоновые сообщения: город ПОЛУЧАТЕЛЯ ───────────────────────────────────────────

from datetime import datetime  # noqa: E402

import services.scheduler as sched  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text)]

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **_kw):
        self.sent.append((chat_id, text))

    async def get_me(self):
        class _Me:
            username = "yulid_bot"
        return _Me()

    def texts(self, chat_id):
        return [t for c, t in self.sent if c == chat_id]


def _make_ambassador(tid, city):
    _seed_user(tid, event_city=city)
    _run(db.set_ambassador_flag(tid, active=True, at="2025-01-01 00:00:00"))


def _two_cities_setup(tmp_path, monkeypatch, key, override):
    """Модуль городов включён, амбассадоры 1 (город A) и 2 (город B); у A свой текст и ссылка."""
    _ready(tmp_path)
    _enable_cities()
    _make_ambassador(1, A)
    _make_ambassador(2, B)
    _set_city(key, A, override)
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    _set_city(CHAT_LINK_KEY, B, LINK_B)  # у B ссылка есть, но текст общий — без {chat}
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def test_deadline_reminder_city_text_for_a_default_for_b(tmp_path, monkeypatch):
    key = "wave_deadline_reminder_text"
    bot = _two_cities_setup(tmp_path, monkeypatch, key, "До {deadline}: «{task}» ({coins}). Чат: {chat}")
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    task_id = _run(db.create_task("Пост", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id))
    monkeypatch.setattr(sched, "_now_moscow_naive", lambda: datetime(2026, 10, 4, 12, 0, 0))

    _run(sched.send_task_deadline_reminder(task_id))
    [a] = bot.texts(1)
    assert a.startswith("До ") and "«Пост» (10)" in a and a.endswith(f"Чат: {LINK_A}")
    assert "{" not in a
    [b] = bot.texts(2)
    default = SETTINGS_SCHEMA[key]["default"]
    assert b.startswith(default.split("(")[0]) and "Пост" in b and LINK_B not in b


def test_wave_start_dm_city_text(tmp_path, monkeypatch):
    key = "wave_start_message_text"
    bot = _two_cities_setup(tmp_path, monkeypatch, key, "Волна {wave} до {ends}. {tasks}\nЧат: {chat}")
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.create_task("Пост", "Light", 10, "photo", "2026-10-05 12:00:00", None, wave_id=wave_id))

    _run(sched.send_wave_start_dm(wave_id, 1))
    _run(sched.send_wave_start_dm(wave_id, 2))
    [a] = bot.texts(1)
    assert a.startswith("Волна ") and a.endswith(f"Чат: {LINK_A}") and "Пост" in a
    [b] = bot.texts(2)
    assert b.startswith("Волна ") and "началась" in b and "Чат:" not in b


def test_wave_results_city_texts_for_winner(tmp_path, monkeypatch):
    from services.amb import ambassador_waves as aw

    bot = _two_cities_setup(tmp_path, monkeypatch, "wave_results_announce_text",
                            "Итоги волны {wave}: место {place}. Чат: {chat}")
    _set_city("wave_results_winner_text", A, "Ты призёр ({place})! {prize} Чат: {chat}")
    _set_city("wave_results_prize_text", A, "Приз — в чате {chat}.")
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 23:59:59", prize_places=1))
    task_id = _run(db.create_task("Задание", "Light", 100, "text", "2026-10-08 23:59:59", None, wave_id=wave_id))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_wave_state(wave_id, "closing", expected_state="active"))
    _run(db.add_coins(1, 50, source="task", task_id=task_id))
    _run(db.add_coins(2, 10, source="task", task_id=task_id))
    _run(aw.announce_results(wave_id))

    _run(sched.send_wave_results(wave_id))
    a = bot.texts(1)
    assert len(a) == 2
    assert a[0].startswith("Итоги волны") and a[0].endswith(f"Чат: {LINK_A}")
    assert a[1] == f"Ты призёр (1)! Приз — в чате {LINK_A}. Чат: {LINK_A}"
    [b] = bot.texts(2)
    assert "Чат:" not in b and LINK_A not in b


def _direct_quiet_hours(monkeypatch):
    from services.comms import quiet_hours

    async def direct(now, user_id, text, *, sender, **_kw):
        await sender()
        return True
    monkeypatch.setattr(quiet_hours, "send_or_queue_text", direct)


def test_tier_notification_city_text_with_chat_and_left(tmp_path, monkeypatch):
    from database import amb_tiers_db
    from services.amb import amb_tiers_notify

    _ready(tmp_path)
    _enable_cities()
    _seed_user(100, event_city=A)
    _set_city("amb_tier1_text", A, "Ступень 1! Ещё {left}. Чат: {chat}")
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    _direct_quiet_hours(monkeypatch)

    async def claim(tid, tier, at):
        return {"telegram_id": tid, "tier": tier, "o2o_status": None}
    monkeypatch.setattr(amb_tiers_db, "claim_tier_notification", claim)
    bot = FakeBot()
    assert _run(amb_tiers_notify.deliver_tier_notification(bot, 100, 1, 3)) is True
    assert bot.texts(100) == [f"Ступень 1! Ещё 3. Чат: {LINK_A}"]


def test_amb_notify_city_text_with_link_and_chat(tmp_path, monkeypatch):
    from handlers.amb import admin_amb_candidates as cand

    _ready(tmp_path)
    _enable_cities()
    _seed_user(200, event_city=A)
    _seed_user(201, event_city=B)
    _set_city("amb_taken_text", A, "Ты в команде! Ссылка: {link}. Чат: {chat}")
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    _direct_quiet_hours(monkeypatch)
    bot = FakeBot()
    _run(cand._notify(bot, 200, "amb_taken_text"))
    _run(cand._notify(bot, 201, "amb_taken_text"))
    [a] = bot.texts(200)
    assert a.startswith("Ты в команде! Ссылка: https://t.me/yulid_bot?start=") and a.endswith(f"Чат: {LINK_A}")
    [b] = bot.texts(201)
    assert "{chat}" not in b and LINK_A not in b


def test_manual_coins_city_text(tmp_path, monkeypatch):
    from services.game.coins_notify import notify_manual_coins

    _ready(tmp_path)
    _enable_cities()
    _seed_user(300, event_city=A)
    _seed_user(301, event_city=B)
    _set_city("coins_manual_notify_text", A, "{delta}: {reason}, всего {balance}. Чат: {chat}")
    _set_city(CHAT_LINK_KEY, A, LINK_A)
    _direct_quiet_hours(monkeypatch)
    bot = FakeBot()
    assert _run(notify_manual_coins(bot, 300, 5, "<b>за пост</b>", 15)) is True
    assert _run(notify_manual_coins(bot, 301, 5, "за пост", 15)) is True
    [a] = bot.texts(300)
    assert a == f"+5: &lt;b&gt;за пост&lt;/b&gt;, всего 15. Чат: {LINK_A}"
    [b] = bot.texts(301)
    assert "Чат:" not in b and "+5" in b
