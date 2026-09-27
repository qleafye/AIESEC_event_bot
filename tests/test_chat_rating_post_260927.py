"""Еженедельный пост рейтинга в чат делегатов города (services/chat_rating_post.py +
handlers/admin_chat_rating_post.py).

Организатор СПб обещал делегатам «каждую неделю таблица самых богатых участников форума» —
бот публикует её сам. Расчёт общий с дашбордом (dashboard/chat_rating.py), команда исключена,
упоминания только @ником. Планирование — персистентная cron-джоба на город.

pytest-asyncio в проекте нет — `asyncio.run`; БД — `tmp_path` через `fast_init_db`;
планировщик — настоящий AsyncIOScheduler на временном jobstore (как test_forum_day_report).
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
import services.scheduler as sched
from config import config
from dashboard import chat_rating
from dashboard import db as dash_db
from database import db
from services import chat_rating_post as crp
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

ADMIN = 900927401
STAFF = 500
SPB_CHAT = -1006666666666
MSK_CHAT = -1007777777777
# Понедельник 28.09.2026, 12:00 МСК: последняя завершённая неделя — 21.09–27.09.
NOW = datetime(2026, 9, 28, 12, 0, 0)


def _run(coro):
    return asyncio.run(coro)


# ── БД ──────────────────────────────────────────────────────────────────────────────────

def _ready(tmp_path, *, city="spb", bind=True):
    path = str(tmp_path / "chat_post.db")
    config.DB_PATH = path
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]
    _run(db.set_setting("event_city_enabled", "on"))
    if city:
        assert _run(cities.set_admin_city(ADMIN, city))
    if bind:
        _run(db.set_setting("delegate_chat_id__city__spb", str(SPB_CHAT)))
        _run(db.set_setting("delegate_chat_title__city__spb", "Чат СПб"))
    _exec(path, "INSERT INTO staff (telegram_id, role, added_by, added_at) "
                "VALUES (?, 'reg_manager', 1, '2026-01-01 00:00:00')", (STAFF,))
    return path


def _exec(path, sql, *rows):
    conn = sqlite3.connect(path)
    try:
        for row in rows or [()]:
            conn.execute(sql, row)
        conn.commit()
    finally:
        conn.close()


def _msg(path, mid, author, ts, *, text_len=10, reply_mid=None, reply_author=None, chat_id=SPB_CHAT):
    _exec(
        path,
        "INSERT INTO chat_messages (chat_id, message_id, telegram_id, ts, kind, text_len, "
        "reply_to_message_id, reply_to_author_id, is_channel_post) VALUES (?, ?, ?, ?, 'text', ?, ?, ?, 0)",
        (chat_id, mid, author, ts, text_len, reply_mid, reply_author),
    )


def _nick(path, tid, nick):
    _exec(path, "INSERT INTO chat_usernames (telegram_id, username, updated_at) VALUES (?, ?, '')",
          (tid, nick))


def _rules_mode(path):
    _exec(path, "INSERT INTO bot_settings (key, value) VALUES ('chat_rating_mode__city__spb', 'rules')")
    _exec(path, "INSERT INTO bot_settings (key, value) VALUES ('chat_rules_currency__city__spb', 'LC')")


def _seed_comments(path):
    """Пост команды 22.09 и комментарии делегатов: 11 — два поста (20 LC), 12 — один (10 LC).
    13 комментирует, но без ника (в пост не попадает). 14 комментирует в ТЕКУЩЕЙ неделе
    (28.09) — в «прошлую неделю» не входит. 15 комментирует 20.09 — неделей раньше."""
    _msg(path, 1, STAFF, "2026-09-22 10:00:00")
    _msg(path, 2, STAFF, "2026-09-23 10:00:00")
    _msg(path, 3, 11, "2026-09-22 11:00:00", reply_mid=1, reply_author=STAFF)
    _msg(path, 4, 11, "2026-09-23 11:00:00", reply_mid=2, reply_author=STAFF)
    _msg(path, 5, 12, "2026-09-27 23:59:00", reply_mid=1, reply_author=STAFF)
    _msg(path, 6, 13, "2026-09-22 12:00:00", reply_mid=1, reply_author=STAFF)
    _msg(path, 7, 14, "2026-09-28 00:00:01", reply_mid=1, reply_author=STAFF)
    _msg(path, 8, STAFF, "2026-09-20 09:00:00")
    _msg(path, 9, 15, "2026-09-20 10:00:00", reply_mid=8, reply_author=STAFF)
    _msg(path, 10, STAFF, "2026-09-22 13:00:00", reply_mid=3, reply_author=11)  # ответ команды
    for tid, nick in ((11, "anna"), (12, "boris"), (14, "vera"), (15, "gleb"), (STAFF, "staffer")):
        _nick(path, tid, nick)
    # Анкеты делегатов города: в режиме «по формуле» пост, как и дашборд, по умолчанию берёт
    # только людей с анкетой сезона и города чата. 13 (без ника) анкеты тоже не имеет.
    for tid in (11, 12, 14, 15):
        _exec(path, "INSERT INTO users (telegram_id, full_name, status, event_city) "
                    "VALUES (?, 'Делегат', 'approved', 'spb')", (tid,))


# ── Реестр ──────────────────────────────────────────────────────────────────────────────

def test_registry_keys_per_city_and_disabled_by_default():
    for key in (crp.KEY_ENABLED, crp.KEY_WEEKDAY, crp.KEY_TIME, crp.KEY_TOP, crp.KEY_CUMULATIVE,
                crp.KEY_TITLE_RULES, crp.KEY_TITLE_FORMULA, crp.KEY_TOTAL_TITLE, crp.KEY_FOOTER):
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "chat", key
        assert entry.get("per_city") is True, key
        assert entry.get("label"), key
    assert SETTINGS_SCHEMA[crp.KEY_ENABLED]["default"] == "off"
    assert SETTINGS_SCHEMA[crp.KEY_CUMULATIVE]["default"] == "off"
    assert SETTINGS_SCHEMA[crp.KEY_TOP]["default"] == 10
    assert SETTINGS_SCHEMA[crp.KEY_TIME].get("format") == "time"
    assert SETTINGS_SCHEMA[crp.KEY_WEEKDAY]["options"] == list(crp.WEEKDAYS)
    assert "{week}" in SETTINGS_SCHEMA[crp.KEY_TITLE_RULES]["default"]
    assert "богат" in SETTINGS_SCHEMA[crp.KEY_TITLE_RULES]["default"]


def test_disabled_by_default_for_every_city(tmp_path):
    _ready(tmp_path)
    assert _run(crp.enabled_for("spb")) is False
    assert _run(crp.enabled_for("msk")) is False


def test_global_on_does_not_enable_city_when_cities_module_on(tmp_path):
    """Общий «вкл» не должен молча включить пост во всех городах — только своё значение."""
    _ready(tmp_path)
    _run(db.set_setting(crp.KEY_ENABLED, "on"))
    assert _run(crp.enabled_for("spb")) is False
    _run(db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    assert _run(crp.enabled_for("spb")) is True


def test_module_off_reads_global_toggle(tmp_path):
    _ready(tmp_path, city=None, bind=False)
    _run(db.set_setting("event_city_enabled", "off"))
    assert _run(crp.enabled_for(None)) is False
    _run(db.set_setting(crp.KEY_ENABLED, "on"))
    assert _run(crp.enabled_for(None)) is True


# ── Неделя по МСК ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("today, since, until", [
    (date(2026, 9, 28), date(2026, 9, 21), date(2026, 9, 27)),   # понедельник
    (date(2026, 10, 4), date(2026, 9, 21), date(2026, 9, 27)),   # воскресенье — неделя ещё идёт
    (date(2026, 10, 1), date(2026, 9, 21), date(2026, 9, 27)),   # четверг
])
def test_last_completed_week_is_previous_monday_to_sunday(today, since, until):
    assert crp.last_week(today) == (since, until)


def test_week_label():
    assert crp.week_label(date(2026, 9, 21), date(2026, 9, 27)) == "21.09–27.09"


def test_dashboard_bounds_override_limits_cumulative_to_end_of_week(tmp_path):
    """Общий расчёт с дашбордом: «с начала» — до конца завершённой недели, не до «сейчас»."""
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    with dash_db.read_conn(path) as conn:
        chat = {"chat_id": SPB_CHAT, "city": "spb"}
        res = chat_rating.rules_rating(conn, chat, period="all", admin_ids={ADMIN}, now=NOW,
                                       bounds=(None, date(2026, 9, 27)))
        ids = {r["telegram_id"] for r in res["rows"]}
        assert 14 not in ids and {11, 12, 15} <= ids
        res_all = chat_rating.rules_rating(conn, chat, period="all", admin_ids={ADMIN}, now=NOW)
        assert 14 in {r["telegram_id"] for r in res_all["rows"]}


# ── Текст поста (чистая функция) ────────────────────────────────────────────────────────

def _rows(*pairs):
    return [{"display_name": n, "value": v} for n, v in pairs]


def test_format_rules_mode_with_currency_medals_and_placeholders():
    text = crp.format_post(
        week_rows=_rows(("@anna", 20.0), ("@boris", 10.0), ("@vera", 2.5), ("@gleb", 1.0)),
        total_rows=None, top=10, currency="LC",
        title="🏆 Самые богатые участники недели {week}, {currency}", total_title="-",
        footer="Пишите больше!", week="21.09–27.09",
    )
    assert "🏆 Самые богатые участники недели 21.09–27.09, LC" in text
    assert "🥇 @anna — 20 LC" in text
    assert "🥈 @boris — 10 LC" in text
    assert "🥉 @vera — 2,5 LC" in text
    assert "4. @gleb — 1 LC" in text
    assert text.rstrip().endswith("Пишите больше!")


def test_format_top_n_skips_people_without_nick_and_zero():
    text = crp.format_post(
        week_rows=_rows(("@a", 50.0), ("123456", 40.0), ("@b", 30.0), ("@c", 20.0), ("@z", 0.0)),
        total_rows=None, top=2, currency="LC", title="T", total_title="-", footer="", week="w",
    )
    assert "@a" in text and "@b" in text
    assert "@c" not in text and "123456" not in text and "@z" not in text
    assert "2. " not in text or "🥈 @b" in text


def test_format_formula_mode_shows_score_without_currency():
    text = crp.format_post(
        week_rows=_rows(("@anna", 42.5)), total_rows=None, top=10, currency="",
        title="🏆 Самые активные {week}", total_title="-", footer="", week="21.09–27.09",
    )
    assert "🥇 @anna — 42,5" in text
    assert "42,5 " not in text.replace("42,5\n", "")


def test_format_cumulative_block_and_empty_week_returns_none():
    text = crp.format_post(
        week_rows=_rows(("@anna", 20.0)), total_rows=_rows(("@gleb", 50.0), ("@anna", 30.0)),
        top=10, currency="LC", title="Неделя {week}", total_title="С начала, {currency}",
        footer="", week="21.09–27.09",
    )
    assert text.index("Неделя 21.09–27.09") < text.index("С начала, LC") < text.index("🥇 @gleb — 50 LC")
    assert crp.format_post(week_rows=_rows(("123", 5.0)), total_rows=None, top=10, currency="LC",
                           title="T", total_title="-", footer="", week="w") is None


def test_format_escapes_manager_text():
    text = crp.format_post(week_rows=_rows(("@a", 1.0)), total_rows=None, top=10, currency="<b>",
                           title="<script> {currency}", total_title="-", footer="a & b", week="w")
    assert "<script>" not in text and "&lt;script&gt;" in text and "a &amp; b" in text


# ── Содержимое по данным: общий расчёт, команда исключена, границы недели ───────────────

def test_build_post_rules_mode_week_team_excluded_top(tmp_path):
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    text, chat = _run(crp.build_post("spb", now=NOW))
    assert chat["chat_id"] == SPB_CHAT
    assert "Самые богатые участники недели 21.09–27.09" in text
    assert "🥇 @anna — 20 LC" in text
    assert "🥈 @boris — 10 LC" in text        # 27.09 23:59 — последняя минута недели
    assert "@vera" not in text                # 28.09 — уже текущая неделя
    assert "@gleb" not in text                # 20.09 — неделей раньше
    assert "@staffer" not in text             # команда исключена
    _run(db.set_setting(f"{crp.KEY_TOP}__city__spb", "1"))
    text, _ = _run(crp.build_post("spb", now=NOW))
    assert "@anna" in text and "@boris" not in text


def test_build_post_cumulative_adds_since_start_block(tmp_path):
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    _run(db.set_setting(f"{crp.KEY_CUMULATIVE}__city__spb", "on"))
    text, _ = _run(crp.build_post("spb", now=NOW))
    total_part = text.split(crp_default_total_title())[1]
    assert "@gleb — 10 LC" in total_part      # прошлые недели входят в «с начала»
    assert "@vera" not in total_part          # текущая неделя — нет


def crp_default_total_title():
    return SETTINGS_SCHEMA[crp.KEY_TOTAL_TITLE]["default"].split("{")[0].strip()


def test_build_post_formula_mode_uses_score(tmp_path):
    path = _ready(tmp_path)
    _seed_comments(path)
    text, _ = _run(crp.build_post("spb", now=NOW))
    assert "Самые активные участники недели 21.09–27.09" in text
    assert "@anna — " in text and " LC" not in text
    assert "@staffer" not in text and "@vera" not in text


def test_build_post_nothing_to_post_and_unbound_chat(tmp_path):
    _ready(tmp_path)
    text, chat = _run(crp.build_post("spb", now=NOW))
    assert text is None and chat is not None
    text, chat = _run(crp.build_post("msk", now=NOW))
    assert chat is None


# ── Планирование ────────────────────────────────────────────────────────────────────────

def _build_scheduler(tmp_path):
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )


def _with_scheduler(tmp_path, monkeypatch, body):
    s = _build_scheduler(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            return await body(s)
        finally:
            s.shutdown(wait=False)

    return asyncio.run(go())


def _fields(job):
    return {f.name: str(f) for f in job.trigger.fields}


def test_schedule_disabled_no_job_enabled_cron_reschedule_and_remove(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        res = await crp.schedule_city_job("spb")
        assert res["scheduled"] is False and s.get_job(crp.job_id("spb")) is None
        await db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on")
        await crp.schedule_city_job("spb")
        job = s.get_job(crp.job_id("spb"))
        assert job is not None and job.args == ("spb",)
        f = _fields(job)
        assert (f["day_of_week"], f["hour"], f["minute"]) == ("mon", "12", "0")
        await db.set_setting(f"{crp.KEY_WEEKDAY}__city__spb", "fri")
        await db.set_setting(f"{crp.KEY_TIME}__city__spb", "19:30")
        await crp.schedule_city_job("spb")
        f = _fields(s.get_job(crp.job_id("spb")))
        assert (f["day_of_week"], f["hour"], f["minute"]) == ("fri", "19", "30")
        assert len([j for j in s.get_jobs() if j.id.startswith("chat_rating_post:")]) == 1
        await db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "off")
        await crp.schedule_city_job("spb")
        assert s.get_job(crp.job_id("spb")) is None

    _with_scheduler(tmp_path, monkeypatch, body)


def test_reconcile_schedules_enabled_cities_and_drops_stale(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        await db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on")
        s.add_job(crp.run_job, "cron", day_of_week="mon", hour=1, args=["zzz"],
                  id=crp.job_id("zzz"), replace_existing=True)
        await crp.reconcile()
        assert s.get_job(crp.job_id("spb")) is not None
        assert s.get_job(crp.job_id("msk")) is None
        assert s.get_job(crp.job_id("zzz")) is None

    _with_scheduler(tmp_path, monkeypatch, body)


def test_same_schedule_is_not_rewritten(tmp_path, monkeypatch):
    """Периодическая сверка не трогает джобу, если день и время не менялись."""
    _ready(tmp_path)

    async def body(s):
        await db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on")
        await crp.schedule_city_job("spb")
        first = s.get_job(crp.job_id("spb")).next_run_time
        res = await crp.schedule_city_job("spb")
        assert res.get("unchanged") is True
        assert s.get_job(crp.job_id("spb")).next_run_time == first

    _with_scheduler(tmp_path, monkeypatch, body)


class _Bot:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append((chat_id, text, kwargs))
        return type("Msg", (), {"message_id": 1})()


def test_run_job_rereads_settings_skips_when_off_or_unbound(tmp_path, monkeypatch):
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr(crp, "msk_now", lambda: NOW)

    _run(crp.run_job("spb"))                      # выключено — ничего
    assert bot.sent == []
    _run(db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    _run(crp.run_job("spb"))
    assert len(bot.sent) == 1 and bot.sent[0][0] == SPB_CHAT
    assert "@anna" in bot.sent[0][1] and bot.sent[0][2].get("parse_mode") == "HTML"

    _run(db.set_setting(f"{crp.KEY_ENABLED}__city__msk", "on"))  # чат Москвы не привязан
    _run(crp.run_job("msk"))
    assert len(bot.sent) == 1


def test_run_job_bot_cannot_post_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    _run(db.set_setting(f"{crp.KEY_ENABLED}__city__spb", "on"))
    monkeypatch.setattr(sched, "_bot", _Bot(fail=RuntimeError("Forbidden: bot was kicked")))
    monkeypatch.setattr(crp, "msk_now", lambda: NOW)
    _run(crp.run_job("spb"))
    assert "chat_rating_post" in caplog.text


# ── Экран админки ───────────────────────────────────────────────────────────────────────

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

    async def answer(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        self.markup = reply_markup

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
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


def test_rating_screen_has_publish_toggle_off_by_default(tmp_path):
    from handlers import admin_chat_rating as scr
    _ready(tmp_path)
    text, kb = _run(scr.render_chat_rating_screen(ADMIN))
    toggle = [t for t in _texts(kb) if "Публиковать рейтинг в чат" in t]
    assert toggle and toggle[0].startswith("⬜")
    assert "chpost:toggle:spb" in _cbs(kb)
    assert "chpost:open:spb" in _cbs(kb)


def test_toggle_writes_city_key_and_schedules(tmp_path, monkeypatch):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path)

    async def body(s):
        cb = _Callback("chpost:toggle:spb")
        await post.chpost_toggle(cb)
        assert await db.get_setting(f"{crp.KEY_ENABLED}__city__spb") == "on"
        assert await db.get_setting(crp.KEY_ENABLED) is None
        assert s.get_job(crp.job_id("spb")) is not None
        assert any("✅" in t and "Публиковать" in t for t in _texts(cb.message.markup))
        await post.chpost_toggle(_Callback("chpost:toggle:spb"))
        assert s.get_job(crp.job_id("spb")) is None

    _with_scheduler(tmp_path, monkeypatch, body)


def test_post_screen_day_buttons_and_human_text(tmp_path, monkeypatch):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path)

    async def body(s):
        cb = _Callback("chpost:open:spb")
        await post.chpost_open(cb, _state())
        shown = cb.message.edited + "\n".join(_texts(cb.message.markup))
        for code in ("chat_rating", "spb", "mon", "{week}"):
            assert code not in shown, code
        assert "Тихие часы" in cb.message.edited and "@" in cb.message.edited
        assert "✅ Пн" in _texts(cb.message.markup)
        await post.chpost_day(_Callback("chpost:day:spb:fri"))
        assert await db.get_setting(f"{crp.KEY_WEEKDAY}__city__spb") == "fri"

    _with_scheduler(tmp_path, monkeypatch, body)


def test_time_input_validated_with_example(tmp_path, monkeypatch):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path)

    async def body(s):
        state = _state()
        cb = _Callback("chpost:edit:spb:time")
        await post.chpost_edit(cb, state)
        assert "19:00" in cb.message.edited  # пример формата
        bad = _Message("7 вечера")
        await post.chpost_value(bad, state)
        assert "ЧЧ:ММ" in bad.answers[-1][0]
        good = _Message("9:05")
        await post.chpost_value(good, state)
        assert await db.get_setting(f"{crp.KEY_TIME}__city__spb") == "09:05"

    _with_scheduler(tmp_path, monkeypatch, body)


def test_top_input_bounds(tmp_path, monkeypatch):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path)

    async def body(s):
        state = _state()
        await post.chpost_edit(_Callback("chpost:edit:spb:top"), state)
        bad = _Message("500")
        await post.chpost_value(bad, state)
        assert "30" in bad.answers[-1][0]
        await post.chpost_value(_Message("5"), state)
        assert await db.get_setting(f"{crp.KEY_TOP}__city__spb") == "5"

    _with_scheduler(tmp_path, monkeypatch, body)


def test_preview_goes_to_admin_dm_then_confirm_then_publish(tmp_path):
    from handlers import admin_chat_rating_post as post
    path = _ready(tmp_path)
    _rules_mode(path)
    _seed_comments(path)
    bot = _Bot()

    cb = _Callback("chpost:preview:spb")
    _run(post.chpost_preview(cb, now=NOW))
    assert bot.sent == []                                     # в чат ничего не ушло
    preview_text, kb = cb.message.answers[-1]
    assert "@anna — 20 LC" in preview_text
    assert "chpost:pub:spb" in _cbs(kb)

    confirm = _Callback("chpost:pub:spb")
    _run(post.chpost_publish_ask(confirm))
    ask_text, ask_kb = confirm.message.answers[-1]
    assert "Чат СПб" in ask_text and "уведомление" in ask_text
    assert "chpost:go:spb" in _cbs(ask_kb)
    assert bot.sent == []

    go = _Callback("chpost:go:spb")
    _run(post.chpost_publish_go(go, bot, now=NOW))
    assert len(bot.sent) == 1 and bot.sent[0][0] == SPB_CHAT
    assert "Опубликовано" in go.message.edited


def test_preview_nothing_to_post_explains(tmp_path):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path)
    cb = _Callback("chpost:preview:spb")
    _run(post.chpost_preview(cb, now=NOW))
    assert cb.message.answers == []
    assert cb.answers and cb.answers[-1][1] is True and "никого" in cb.answers[-1][0]


def test_publish_to_unbound_chat_explains(tmp_path):
    from handlers import admin_chat_rating_post as post
    _ready(tmp_path, bind=False)
    go = _Callback("chpost:go:spb")
    bot = _Bot()
    _run(post.chpost_publish_go(go, bot, now=NOW))
    assert bot.sent == []
    assert "не привязан" in (go.message.edited or "")


def test_capabilities_registered():
    from handlers.admin_caps import required_capability
    assert required_capability(callback_data="chpost:toggle:spb") == "settings"
    assert required_capability(callback_data="chpost:go:spb") == "settings"
    assert required_capability(raw_state="ChatRatingPostEdit:waiting_for_value") == "settings"
