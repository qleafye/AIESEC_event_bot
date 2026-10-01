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
    assert caption.startswith("Сегодня форум!") and "Завтра" not in caption


def test_manual_send_on_forum_day_uses_today_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 3, 9, 30))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Сегодня форум!")


def test_evening_send_keeps_tomorrow_text(tmp_path, monkeypatch):
    _ready(tmp_path)
    _seed(7)
    bot = PhotoBot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2026, 10, 2, 18, 0))
    _run(cb.send_broadcast(None))
    assert bot.photos[0][1].startswith("Завтра форум!")


def test_morning_text_registered_like_neighbours():
    from handlers.admin_settings import SETTINGS_FIELDS
    from services.i18n_form_manual import FORM_DEFAULT_EN
    from settings_schema import SETTINGS_SCHEMA
    from settings_synonyms import SETTINGS_SYNONYMS

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
    monkeypatch.setattr(cna, "msk_now", lambda: datetime.now())
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
    monkeypatch.setattr(cna, "msk_now", lambda: datetime.now())
    bot = TextBot(fail_for={1: TelegramForbiddenError(method=None, message="bot was blocked")})
    monkeypatch.setattr(sched, "_bot", bot)
    res = _run(cna.send(city=None, city_scope=None))
    assert res["failed"] == 1
    assert _run(db.checkin_not_arrived_pending_ids()) == []


# ── Форумные тексты: разметка и длина подписи проверяются при сохранении ─────────────────────

def test_forum_texts_are_html_settings():
    from settings_ops import HTML_SETTINGS
    from settings_validation import FORUM_HTML_KEYS
    assert FORUM_HTML_KEYS <= HTML_SETTINGS


def test_stray_lt_rejected_with_human_error():
    from settings_validation import validate_setting_value
    value, err = validate_setting_value("checkin_qr_broadcast_text", "Паспорт обязателен <3")
    assert value is None and "Telegram" in err
    value, err = validate_setting_value("checkin_not_arrived_text__city__spb", "<регистрация> закрыта")
    assert value is None and err


def test_unknown_and_unbalanced_tags_rejected():
    from settings_validation import validate_setting_value
    assert validate_setting_value("forum_welcome_text", "Привет <div>x</div>")[0] is None
    assert validate_setting_value("forum_welcome_text", "Привет <b>x")[0] is None
    assert validate_setting_value("forum_welcome_text", "Привет x</b>")[0] is None


def test_valid_markup_and_escaped_text_pass():
    from settings_validation import validate_setting_value
    ok = "Сегодня <b>форум</b> в {time}! Паспорт &lt;3 &amp; <a href=\"https://x.y\">карта</a>"
    assert validate_setting_value("forum_welcome_text", ok) == (ok, None)


def test_caption_longer_than_1024_rejected():
    from settings_validation import validate_setting_value
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
