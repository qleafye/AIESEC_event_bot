"""Рассылки дня форума (форумы 03.10): тексты, догон, повторные попытки, отметки «отправлено».

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from config import config
from database import db
import services.checkin_broadcast as cb
import services.scheduler as sched
from services.timeutil import msk_now as real_msk_now
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="forum_bcast_fixes.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("event_season", "YL 26/2"))
    _run(db.set_setting("forum_date", "03.10.2026"))


def _seed(tid, city=None, name=None):
    _run(db.add_user({
        "telegram_id": tid, "full_name": name or f"D{tid}",
        "registration_date": "2026-09-01 00:00:00", "event_city": city,
    }))
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE users SET status='approved', season='YL 26/2' WHERE telegram_id=?", (tid,))
    conn.commit()
    conn.close()


class PhotoBot:
    def __init__(self):
        self.photos = []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append((chat_id, caption, reply_markup))
        return type("Msg", (), {"message_id": 1})()


# ── Утренний повтор: «Сегодня форум», не «Завтра форум» ──────────────────────────────────────

def test_morning_repeat_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    _run(cb.send_morning_repeat(None))
    caption = bot.photos[0][1]
    assert caption.startswith("Сегодня встречаемся!") and "Завтра" not in caption


def test_manual_send_on_forum_day_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 30))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Сегодня встречаемся!")


def test_manual_send_on_second_forum_day_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("sos_active_days", "2"))
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 4, 9, 30))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Сегодня встречаемся!")


def test_evening_send_keeps_tomorrow_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Завтра встречаемся!")


def test_morning_text_registered_like_neighbours():
    from handlers.admin_settings import SETTINGS_FIELDS
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from domain.settings.schema import SETTINGS_SCHEMA
    from domain.settings.synonyms import SETTINGS_SYNONYMS

    entry = SETTINGS_SCHEMA["checkin_qr_morning_text"]
    assert entry["group"] == "reg" and entry["per_city"] is True
    assert "завтра" not in entry["default"].lower()
    assert entry["default"] in FORM_DEFAULT_EN
    assert "checkin_qr_morning_text" in SETTINGS_SYNONYMS
    assert "checkin_qr_morning_text" in {k for k, _l, _p in SETTINGS_FIELDS}


# ── «Написать не пришедшим»: тихие часы не мешают в день форума ──────────────────────────────

class TextBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = dict(fail_for)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        exc = self.fail_for.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"message_id": 1})()


def _quiet_all_day():
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))


def test_not_arrived_ignores_quiet_hours_on_forum_day(tmp_path, monkeypatch):
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    _quiet_all_day()
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime(2026, 10, 3, 8, 30))
    res = _run(cna.send(city=None, city_scope=None))
    assert res["sent"] == 1 and res["quiet"] == 0


def test_not_arrived_keeps_quiet_hours_on_other_days(tmp_path, monkeypatch):
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    _quiet_all_day()
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cna, "msk_now", lambda: datetime(2026, 10, 6, 8, 30))
    res = _run(cna.send(city=None, city_scope=None))
    assert res["sent"] == 0 and res["quiet"] == 1 and bot.sent == []


def test_not_arrived_transient_failure_unmarks_for_retry(tmp_path, monkeypatch):
    """Сбой отправки не исключает делегата навсегда: повторное нажатие берёт его снова."""
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    monkeypatch.setattr(cna, "msk_now", real_msk_now)  # дата «сегодня» по МСК, как у db; datetime.now() в CI = UTC
    bot = TextBot(fail_for={1: RuntimeError("network down")})
    monkeypatch.setattr(sched, "_bot", bot)
    res = _run(cna.send(city=None, city_scope=None))
    assert res["failed"] == 1 and res["sent"] == 0
    assert _run(db.checkin_not_arrived_pending_ids()) == [1]
    bot.fail_for.clear()
    res2 = _run(cna.send(city=None, city_scope=None))
    assert res2["sent"] == 1


def test_not_arrived_blocked_user_stays_marked(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    import services.checkin_not_arrived as cna
    _ready(tmp_path)
    _seed(1)
    monkeypatch.setattr(cna, "msk_now", real_msk_now)  # дата «сегодня» по МСК, как у db; datetime.now() в CI = UTC
    bot = TextBot(fail_for={1: TelegramForbiddenError(method=None, message="bot was blocked")})
    monkeypatch.setattr(sched, "_bot", bot)
    res = _run(cna.send(city=None, city_scope=None))
    assert res["failed"] == 1
    assert _run(db.checkin_not_arrived_pending_ids()) == []


# ── Форумные тексты: разметка и длина подписи проверяются при сохранении ─────────────────────

def test_forum_texts_are_html_settings():
    from domain.settings.ops import HTML_SETTINGS
    from domain.settings.validation import FORUM_HTML_KEYS
    assert FORUM_HTML_KEYS <= HTML_SETTINGS


def test_stray_lt_rejected_with_human_error():
    from domain.settings.validation import validate_setting_value
    value, err = validate_setting_value("checkin_qr_broadcast_text", "Паспорт обязателен <3")
    assert value is None and "Telegram" in err
    value, err = validate_setting_value("checkin_not_arrived_text__city__spb", "<регистрация> закрыта")
    assert value is None and err


def test_unknown_and_unbalanced_tags_rejected():
    from domain.settings.validation import validate_setting_value
    assert validate_setting_value("forum_welcome_text", "Привет <div>x</div>")[0] is None
    assert validate_setting_value("forum_welcome_text", "Привет <b>x")[0] is None
    assert validate_setting_value("forum_welcome_text", "Привет x</b>")[0] is None


def test_valid_markup_and_escaped_text_pass():
    from domain.settings.validation import validate_setting_value
    ok = "Сегодня <b>форум</b> в {time}! Паспорт &lt;3 &amp; <a href=\"https://x.y\">карта</a>"
    assert validate_setting_value("forum_welcome_text", ok) == (ok, None)


def test_caption_longer_than_1024_rejected():
    from domain.settings.validation import validate_setting_value
    long_text = "а" * 1025
    value, err = validate_setting_value("checkin_qr_morning_text", long_text)
    assert value is None and "1024" in err
    # Теги не считаются: видимых символов ровно 1024.
    ok = "<b>" + "а" * 1024 + "</b>"
    assert validate_setting_value("checkin_qr_morning_text", ok) == (ok, None)
    # Обычное сообщение (не подпись) длинным быть может.
    assert validate_setting_value("checkin_not_arrived_text", long_text) == (long_text, None)


# ── Вечерняя рассылка QR не перевзводится каждые 10 минут после того, как отработала ─────────

class _Sched:
    def __init__(self):
        self.jobs = {}

    def get_job(self, jid):
        return self.jobs.get(jid)

    def add_job(self, fn, trigger, run_date=None, args=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = type("Job", (), {"func": fn, "next_run_time": run_date, "args": args})()

    def remove_job(self, jid):
        self.jobs.pop(jid, None)

    def get_jobs(self):
        return list(self.jobs.values())


def test_evening_not_rearmed_after_it_ran(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "_evening_done", {})
    calls = []

    async def _send(city):
        calls.append(city)
        return {"sent": 0, "failed": 1, "total": 1}

    monkeypatch.setattr(cb, "send_broadcast", _send)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    _run(cb._run_evening_job(None))
    assert calls == [None]
    fake.jobs.clear()  # date-джоба после срабатывания из хранилища уходит
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 10))
    res = _run(cb.schedule_city_jobs(None))
    assert res["evening_at"] is None
    assert cb.evening_job_id(None) not in fake.jobs


def test_evening_catches_up_when_it_never_ran(tmp_path, monkeypatch):
    _ready(tmp_path)
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "_evening_done", {})
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 10))
    res = _run(cb.schedule_city_jobs(None))
    assert res["evening_at"] == datetime(2026, 10, 2, 18, 11)


# ── Рестарт после 08:02: утренний повтор догоняется до полудня, без дублей ──────────────────

def test_morning_repeat_catches_up_after_late_restart(tmp_path, monkeypatch):
    _ready(tmp_path)
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 7, 0))
    _run(cb.schedule_city_jobs(None))
    assert cb.morning_job_id(None) in fake.jobs
    # Бот лежал с 07:59 до 09:40 — джоба осталась в хранилище несработавшей.
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 40))
    res = _run(cb.schedule_city_jobs(None))
    assert res["morning_at"] == datetime(2026, 10, 3, 9, 41)


def test_morning_repeat_not_repeated_after_it_fired(tmp_path, monkeypatch):
    _ready(tmp_path)
    fake = _Sched()  # джобы в хранилище нет — повтор уже отработал
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 40))
    assert _run(cb.schedule_city_jobs(None))["morning_at"] is None


def test_morning_repeat_not_caught_up_after_noon(tmp_path, monkeypatch):
    _ready(tmp_path)
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 7, 0))
    _run(cb.schedule_city_jobs(None))
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 12, 5))
    assert _run(cb.schedule_city_jobs(None))["morning_at"] is None


# ── Отчёт дня: отметка до отправки, сбой — пауза, а не отчёт каждую минуту ───────────────────

def test_day_report_auto_send_is_claimed_once(tmp_path, monkeypatch):
    import services.forum_day_report as fdr
    _ready(tmp_path)
    _run(db.set_setting("sos_chat_id", "-100500"))
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)
    first = _run(fdr.send_report(None, "2026-10-03", mark_sent=True))
    assert first["chat_delivered"] is True
    second = _run(fdr.send_report(None, "2026-10-03", mark_sent=True))
    assert second.get("already_sent") is True
    assert [cid for cid, _t, _k in bot.sent].count(-100500) == 1


def test_day_report_mark_failure_sends_nothing_and_backs_off(tmp_path, monkeypatch):
    import database.db as dbmod
    import services.forum_day_report as fdr
    _ready(tmp_path)
    _run(db.set_setting("sos_chat_id", "-100500"))
    _run(db.set_setting("forum_day_report_enabled", "on"))
    _run(db.set_setting("sos_active_days", "1"))
    bot = TextBot()
    monkeypatch.setattr(sched, "_bot", bot)

    async def _locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dbmod, "forum_day_report_mark_sent", _locked)
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 3, 21, 0))
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    _run(fdr._run_job(None))
    assert bot.sent == []  # отметка не встала — в чат ничего не ушло
    job = fake.jobs[fdr.job_id(None)]
    assert job.next_run_time == datetime(2026, 10, 3, 21, 30)  # пауза, а не «через минуту»
    # Сверка раз в 10 минут паузу не сокращает.
    monkeypatch.setattr(fdr, "msk_now", lambda: datetime(2026, 10, 3, 21, 10))
    _run(fdr.schedule_city_job(None))
    assert fake.jobs[fdr.job_id(None)].next_run_time == datetime(2026, 10, 3, 21, 30)


# ── Карточка «в цифрах»: имя экранируется, пустая подпись не уходит ──────────────────────────

def _stats_card_env(tmp_path, monkeypatch, name):
    import services.forum_stats_card as fsc
    from tests.test_forum_stats_card_260926 import FakeBot as CardBot, _fake_render
    _ready(tmp_path)
    _seed(9, name=name)
    _run(db.set_setting("forum_stats_card_enabled", "on"))
    bot = CardBot()
    monkeypatch.setattr(sched, "_bot", bot)
    _fake_render(monkeypatch, [])
    return fsc, bot


def test_stats_card_escapes_name_in_html_caption(tmp_path, monkeypatch):
    fsc, bot = _stats_card_env(tmp_path, monkeypatch, "Аня <Котик> & Ко")
    _run(db.set_setting("forum_stats_card_caption_text", "🎉 {name}, вот твой Юлид!"))
    res = _run(fsc.send_broadcast(None, only_arrived=False))
    assert res["sent"] == 1
    caption = bot.photos[0][1]
    assert "&lt;Котик&gt; &amp; Ко" in caption and "<Котик>" not in caption


def test_stats_card_empty_caption_sends_nothing(tmp_path, monkeypatch):
    fsc, bot = _stats_card_env(tmp_path, monkeypatch, "Аня")
    _run(db.set_setting("forum_stats_card_caption_text", "   "))
    res = _run(fsc.send_broadcast(None, only_arrived=False))
    assert res.get("empty_caption") is True and bot.photos == []


# ── «📤 Разослать QR сейчас» проверяет дату форума города ────────────────────────────────────

def test_manual_send_refused_without_city_date(tmp_path, monkeypatch):
    from handlers import admin_checkin
    from tests.test_roles_phase8 import FakeCallback
    config.ADMIN_IDS = [1]
    config.DB_PATH = str(tmp_path / "manual_send.db")
    fast_init_db()
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.set_setting("forum_date", "03.10.2026"))  # общая — Москве не в счёт
    _seed(5, "msk")
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 12, 0))
    q = FakeCallback("checkinqr_send:msk", user_id=1)
    _run(admin_checkin.checkinqr_send_confirm(q))
    assert q.answers and q.answers[0][1] is True and "не задана дата" in q.answers[0][0]


def test_manual_send_block_reasons(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 9, 25, 12, 0))
    assert "рано" in _run(cb.manual_send_block_reason(None))
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 1, 12, 0))
    assert "рано" in _run(cb.manual_send_block_reason(None))  # за 2 дня — ещё рано
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 12, 0))
    assert _run(cb.manual_send_block_reason(None)) is None
    _run(db.set_setting("sos_active_days", "1"))
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 4, 12, 0))
    assert "прошёл" in _run(cb.manual_send_block_reason(None))


# ── В день форума QR и приветствие приходят с главным меню (кнопка «🆘 SOS») ─────────────────

def _reply_texts(markup):
    from aiogram.types import ReplyKeyboardMarkup
    assert isinstance(markup, ReplyKeyboardMarkup), markup
    return [b.text for row in markup.keyboard for b in row]


def test_morning_repeat_carries_main_menu_with_sos(tmp_path, monkeypatch):
    import services.sos as sos_mod
    _ready(tmp_path)
    _run(db.set_setting("sos_chat_id", "-100500"))
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    monkeypatch.setattr(sos_mod, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    _run(cb.send_morning_repeat(None))
    texts = _reply_texts(bot.photos[0][2])
    assert any("SOS" in t for t in texts), texts


def test_evening_qr_keeps_confirm_button(tmp_path, monkeypatch):
    from aiogram.types import InlineKeyboardMarkup
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    _run(cb.send_broadcast(None))
    assert isinstance(bot.photos[0][2], InlineKeyboardMarkup)


def test_welcome_after_checkin_carries_main_menu(tmp_path, monkeypatch):
    import services.forum_welcome as fw
    _ready(tmp_path)
    _seed(7)
    _run(db.set_setting("forum_welcome_enabled", "on"))
    bot = TextBot()
    _run(fw._on_first_entry(bot, 7, None, "2026-10-03", source="miniapp",
                            scanned_at="2026-10-03 09:15:00"))
    assert bot.sent, "приветствие не ушло"
    _reply_texts(bot.sent[0][2])


# ── Фильтр рассылки «❌ Не пришли»: только города, где в этот день форум ───────────────────

def _cities_env(tmp_path):
    config.DB_PATH = str(tmp_path / "not_arrived_filter.db")
    fast_init_db()
    _run(db.set_setting("event_city_enabled", "on"))
    _run(db.set_setting("event_season", "YL 26/2"))
    _run(db.set_setting("forum_date__city__spb", "03.10.2026"))
    _run(db.set_setting("sos_active_days__city__spb", "1"))
    _run(db.set_setting("forum_date__city__msk", "30.10.2026"))
    _seed(1, "msk")
    _seed(2, "spb")
    _seed(3, None)  # без города = Москва


def test_not_arrived_today_filter_skips_city_without_forum_today(tmp_path, monkeypatch):
    _cities_env(tmp_path)
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 3, 11, 0))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": "no", "day": "today"}]))
    assert ids == [2]
    # «Не пришли ни разу» — только города, чей форум уже начался.
    ids_all = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": "no"}]))
    assert ids_all == [2]


def test_not_arrived_filter_moscow_on_its_forum_day(tmp_path, monkeypatch):
    _cities_env(tmp_path)
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 30, 11, 0))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": "no", "day": "today"}]))
    assert sorted(ids) == [1, 3]


def test_not_arrived_filter_unchanged_without_cities_module(tmp_path, monkeypatch):
    config.DB_PATH = str(tmp_path / "no_cities.db")
    fast_init_db()
    _run(db.set_setting("event_season", "YL 26/2"))
    _seed(1)
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 3, 11, 0))
    ids = _run(db.count_and_list_filtered([{"field": "checkin_entry", "value": "no", "day": "today"}]))
    assert ids == [1]


def test_not_arrived_confirm_note_lists_cities(tmp_path, monkeypatch):
    from services.forum_days import not_arrived_city_note
    _cities_env(tmp_path)
    note = _run(not_arrived_city_note([{"field": "checkin_entry", "value": "no"}], [2]))
    assert "По городам" in note and "— 1" in note and "идёт форум" in note
    assert _run(not_arrived_city_note([{"field": "status", "value": "approved"}], [2])) == ""


# ── Рассылка менеджера, привязанного к городу, — только его городу ───────────────────────────

def test_city_bound_manager_broadcast_limited_to_city(tmp_path):
    from services.broadcast_scope import restrict_to_sender_city, sender_city_note
    _cities_env(tmp_path)
    config.ADMIN_IDS = [1000]
    _run(db.add_staff(2000, "manager", 1000))
    _run(db.set_staff_city(2000, "spb"))
    assert _run(restrict_to_sender_city(2000, [1, 2, 3])) == [2]
    assert "Только делегатам вашего города" in _run(sender_city_note(2000))
    # Суперадмин и менеджер без города — без сужения.
    assert _run(restrict_to_sender_city(1000, [1, 2, 3])) == [1, 2, 3]
    _run(db.add_staff(3000, "manager", 1000))
    assert _run(restrict_to_sender_city(3000, [1, 2, 3])) == [1, 2, 3]
    assert _run(sender_city_note(1000)) == ""


def test_moscow_bound_manager_gets_cityless_delegates(tmp_path):
    from services.broadcast_scope import restrict_to_sender_city
    _cities_env(tmp_path)
    config.ADMIN_IDS = [1000]
    _run(db.add_staff(2001, "manager", 1000))
    _run(db.set_staff_city(2001, "msk"))
    assert sorted(_run(restrict_to_sender_city(2001, [1, 2, 3]))) == [1, 3]


def test_city_manager_reaches_own_unfinished_registrations(tmp_path):
    """Сегмент «📝 Не завершили регистрацию» живёт в reg_started, а не в users: менеджер города
    раньше рассылал ему никому. Теперь — его город по городу из начала анкеты."""
    from services.broadcast_scope import restrict_to_sender_city, split_by_sender_city
    _cities_env(tmp_path)
    config.ADMIN_IDS = [1000]
    _run(db.add_staff(2000, "manager", 1000))
    _run(db.set_staff_city(2000, "spb"))
    _run(db.mark_reg_started(200, "x", event_city="spb"))
    _run(db.mark_reg_started(201, "y", event_city="msk"))
    incomplete = _run(db.get_incomplete_user_ids())
    assert sorted(_run(restrict_to_sender_city(2000, incomplete))) == [200]
    assert sorted(_run(restrict_to_sender_city(1000, incomplete))) == [200, 201]
    # Список из файла: чужой город и незнакомый id отсеяны и посчитаны.
    kept, dropped = _run(split_by_sender_city(2000, [2, 1, 999, 200]))
    assert sorted(kept) == [2, 200] and dropped == 2


def test_moscow_manager_does_not_reach_unfinished_without_city(tmp_path):
    """Нажал /start, до вопроса о городе не дошёл — город неизвестен: менеджеру Москвы (город
    по умолчанию) такой человек не уходит, он посчитан в «отсеяно»; суперадмину — уходит."""
    from services.broadcast_scope import split_by_sender_city
    _cities_env(tmp_path)
    config.ADMIN_IDS = [1000]
    _run(db.add_staff(2001, "manager", 1000))
    _run(db.set_staff_city(2001, "msk"))
    _run(db.mark_reg_started(210, "x", event_city="msk"))
    _run(db.mark_reg_started(211, "y"))
    incomplete = _run(db.get_incomplete_user_ids())
    kept, dropped = _run(split_by_sender_city(2001, incomplete))
    assert sorted(kept) == [210] and dropped == 1
    assert sorted(_run(split_by_sender_city(1000, incomplete))[0]) == [210, 211]


def test_city_manager_confirm_note_counts_dropped(tmp_path):
    from services.broadcast_scope import sender_city_note
    _cities_env(tmp_path)
    config.ADMIN_IDS = [1000]
    _run(db.add_staff(2000, "manager", 1000))
    _run(db.set_staff_city(2000, "spb"))
    note = _run(sender_city_note(2000, 3))
    assert "3 из выбранных" in note and "не уйдёт" in note
    assert "из выбранных" not in _run(sender_city_note(2000))


def test_stats_card_empty_caption_explained_to_manager(tmp_path, monkeypatch):
    """Пустая подпись — не «✅ Отправлено 0 из 0», а объяснение, что заполнить."""
    from handlers import admin_forum_stats_card as afsc
    from handlers.admin_checkin import _NO_CITY
    import services.forum_stats_card as fsc
    from tests.test_roles_phase8 import FakeCallback
    _ready(tmp_path)
    config.ADMIN_IDS = [1]

    async def _empty(city, only_arrived=False):
        return {"sent": 0, "failed": 0, "quiet": 0, "muted": 0, "total": 0, "empty_caption": True}

    monkeypatch.setattr(fsc, "send_broadcast", _empty)
    q = FakeCallback(f"forumstats_send_go:{_NO_CITY}:all", user_id=1)
    _run(afsc.forumstats_send_go(q))
    said = " ".join(a[0] for a in q.message.answers)
    assert "подпись к карточке пуста" in said and "Отправлено 0" not in said


def test_forum_day_qr_keeps_sos_keyboard_of_collecting_delegate(tmp_path, monkeypatch):
    """Делегат дописывает SOS — QR в день форума не заменяет его «✅ Готово»/«📍» главным
    меню; остальным меню приходит как раньше."""
    import services.sos as sos_mod
    _ready(tmp_path)
    _seed(7)
    _seed(8)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    monkeypatch.setattr(sos_mod, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))

    async def _collecting(tid):
        return tid == 7

    monkeypatch.setattr(sos_mod, "may_be_collecting", _collecting, raising=False)
    _run(cb.send_morning_repeat(None))
    by_id = {p[0]: p[2] for p in bot.photos}
    assert by_id[7] is None
    assert any("SOS" in t for t in _reply_texts(by_id[8]))


def test_forum_day_qr_without_collecting_check_still_sends_menu(tmp_path, monkeypatch):
    import services.sos as sos_mod
    monkeypatch.delattr(sos_mod, "may_be_collecting", raising=False)
    assert _run(cb._may_be_collecting_sos(7)) is False


def test_checkin_day_filter_label_names_forum_city(tmp_path, monkeypatch):
    """«не пришли 25.09» при форумах в разные дни не читается — к дню приписан город."""
    import services.timeutil as tu
    from services.forum_days import day_cities_suffix
    _cities_env(tmp_path)
    monkeypatch.setattr(tu, "msk_now", lambda: datetime(2026, 10, 3, 11, 0))
    spb = _run(day_cities_suffix("2026-10-03"))
    assert spb.startswith(" — ") and "Москва" not in spb
    assert _run(day_cities_suffix("today")) == spb
    assert "Москва" in _run(day_cities_suffix("2026-10-31"))  # второй день Москвы
    assert _run(day_cities_suffix("2026-10-10")) == ""  # форума ни у кого


# ── Утренний повтор: блокировка города и догон после рестарта посреди цикла ─────────────────

def test_morning_repeat_waits_for_manual_send_no_duplicates(tmp_path, monkeypatch):
    """Ручная «📤 Разослать QR сейчас» и утренний повтор одновременно: повтор ждёт блокировку
    города и дошлёт только тем, кто сегодня QR ещё не получил, — по одному QR на человека."""
    _ready(tmp_path)
    for tid in (31, 32, 33):
        _seed(tid)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    sent: list[int] = []

    async def _slow_send(tid, png, caption, kb, on_permanent_failure=None):
        await asyncio.sleep(0.02)
        sent.append(tid)
        return True

    monkeypatch.setattr(cb, "_send_one", _slow_send)
    monkeypatch.setattr(cb, "_city_locks", {})

    async def _both():
        return await asyncio.gather(cb.send_broadcast(None), cb.send_morning_repeat(None))

    manual, morning = _run(_both())
    assert sorted(sent) == [31, 32, 33]
    assert manual["sent"] == 3 and morning["total"] == 0


def test_morning_repeat_interrupted_is_caught_up_after_restart(tmp_path, monkeypatch):
    """Рестарт посреди утреннего повтора: страховочная джоба осталась в хранилище, сверка
    после рестарта догоняет повтор; дошедший до конца повтор её снимает."""
    _ready(tmp_path)
    fake = _Sched()
    monkeypatch.setattr(sched, "get_scheduler", lambda: fake)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 8, 0))
    jid = cb.morning_job_id(None)
    seen = {}

    async def _crash(city):
        seen["guard"] = fake.get_job(jid)
        # Сверка посреди цикла джобу не перевзводит.
        seen["recon"] = (await cb.schedule_city_jobs(city))["morning_at"]
        raise RuntimeError("рестарт посреди цикла")

    monkeypatch.setattr(cb, "send_morning_repeat", _crash)
    try:
        _run(cb._run_morning_job(None))
    except RuntimeError:
        pass
    assert seen["guard"] is not None
    assert seen["recon"] == datetime(2026, 10, 3, 8, 30)
    assert jid in fake.jobs and None not in cb._morning_in_progress
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 0))
    assert _run(cb.schedule_city_jobs(None))["morning_at"] == datetime(2026, 10, 3, 9, 1)

    async def _ok(city):
        return {"sent": 0, "failed": 0, "total": 0}

    monkeypatch.setattr(cb, "send_morning_repeat", _ok)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 1))
    _run(cb._run_morning_job(None))
    assert jid not in fake.jobs
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 20))
    assert _run(cb.schedule_city_jobs(None))["morning_at"] is None
