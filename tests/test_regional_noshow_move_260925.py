"""Трек «региональные форумы → Москва» (25.09, forum-regions-msk): предложение переноса
неявившегося одобренного делегата регионального форума на московский форум —
`services/regional_noshow_move.py` + делегатская сторона `handlers/user_actions.py`
(rnm_accept/rnm_confirm/rnm_decline) + экран `handlers/admin_forum_functions.py`.

Стиль — `tests/test_forum_noshow_poll_260924.py` (реальный AsyncIOScheduler на временном
jobstore, `asyncio.run`, шаблонная БД `tests/_dbtpl.fast_init_db`), мок `move_user_city` —
`tests/test_city_move_260925.py` уже покрывает сам перенос (лист/БД/трек), этот файл мокает
границу и проверяет, что переносящий вызов получает правильные аргументы."""
from __future__ import annotations

import asyncio
from datetime import datetime

from config import config
from database import db
import services.scheduler as sched
import services.regional_noshow_move as rgnm
import services.city_move as city_move_mod
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback

ADMIN_ID = 925201
UID = 925210

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="regional_noshow_move.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _set(key, value):
    await db.set_setting(key, value)


async def _add_delegate(tid, *, city=None, status="approved", season=None, full_name=None):
    await db.add_user({
        "telegram_id": tid, "full_name": full_name or f"Делегат {tid}", "username": "d",
        "event_city": city, "registration_date": "2026-01-01 00:00:00",
    })
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET status = ?, season = ? WHERE telegram_id = ?", (status, season, tid),
        )
        await conn.commit()


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text, reply_markup)]

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"message_id": 1})()


def _with_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    return bot


def _build_scheduler(tmp_path):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )


def _run_scheduled(tmp_path, monkeypatch, body):
    s = _build_scheduler(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            return await body(s)
        finally:
            s.shutdown(wait=False)

    return asyncio.run(go())


def _cities_fixture(monkeypatch):
    import cities
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    return saved


def _restore_cities(saved):
    import cities
    cities.set_cities_for_test(saved)


# ══════════════════════════════════════════════════════════════════════════════════════════
# БД: USER_PURGE_TABLES + аудитория
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_regional_noshow_move_in_user_purge_tables():
    names = {t for t, _col, _grp in db.USER_PURGE_TABLES}
    assert "regional_noshow_move" in names


def test_pending_ids_includes_approved_without_entry(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    assert _run(db.regional_noshow_move_pending_ids()) == [1]


def test_pending_ids_excludes_already_checked_in(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    assert _run(db.regional_noshow_move_pending_ids()) == []


def test_pending_ids_excludes_not_approved(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1, status="pending"))
    assert _run(db.regional_noshow_move_pending_ids()) == []


def test_pending_ids_excludes_past_season(tmp_path):
    _ready(tmp_path)
    _run(_set("event_season", "YL 26/2"))
    _run(_add_delegate(1, season="YL 26/1"))
    assert _run(db.regional_noshow_move_pending_ids()) == []


def test_pending_ids_excludes_changed_mind_poll_answer(tmp_path):
    """Ядро задачи: делегат, ответивший в опросе неявившихся «Передумал(а)» ЭТОГО сезона,
    предложение переноса не получает."""
    _ready(tmp_path)
    _run(_set("event_season", "YL 26/2"))
    _run(_add_delegate(1, season="YL 26/2"))
    _run(_add_delegate(2, season="YL 26/2"))
    _run(db.forum_noshow_poll_mark_sent(1, None, "YL 26/2", "2026-10-04 12:00:00"))
    _run(db.record_forum_noshow_poll_response(1, "YL 26/2", "changed_mind", None, "2026-10-04 12:05:00"))
    assert _run(db.regional_noshow_move_pending_ids()) == [2]


def test_pending_ids_other_poll_reason_still_offered(tmp_path):
    """Причина, отличная от «Передумал(а)» (например «Забыл(а)»), не исключает делегата —
    только явное «не интересно» фильтруется."""
    _ready(tmp_path)
    _run(_set("event_season", "YL 26/2"))
    _run(_add_delegate(1, season="YL 26/2"))
    _run(db.forum_noshow_poll_mark_sent(1, None, "YL 26/2", "2026-10-04 12:00:00"))
    _run(db.record_forum_noshow_poll_response(1, "YL 26/2", "forgot", None, "2026-10-04 12:05:00"))
    assert _run(db.regional_noshow_move_pending_ids()) == [1]


def test_pending_ids_scoped_by_city(tmp_path):
    import cities as cities_mod
    _ready(tmp_path)
    _run(_add_delegate(1, city="msk"))
    _run(_add_delegate(2, city="spb"))
    scope_spb = cities_mod.city_scope("spb")
    assert _run(db.regional_noshow_move_pending_ids(city_scope=scope_spb)) == [2]


def test_mark_sent_idempotent_per_season(tmp_path):
    _ready(tmp_path)
    marked = _run(db.regional_noshow_move_mark_sent(1, "spb", "YL 26/2", "2026-10-04 12:00:00"))
    assert marked is True
    assert _run(db.regional_noshow_move_sent_ids("YL 26/2")) == {1}
    marked2 = _run(db.regional_noshow_move_mark_sent(1, "spb", "YL 26/2", "2026-10-04 12:05:00"))
    assert marked2 is False


def test_pending_ids_excludes_already_sent_this_season(tmp_path):
    _ready(tmp_path)
    _run(_add_delegate(1))
    _run(db.regional_noshow_move_mark_sent(1, None, "", "2026-10-04 12:00:00"))
    assert _run(db.regional_noshow_move_pending_ids()) == []


def test_summary_counts_offered_moved_declined(tmp_path):
    _ready(tmp_path)
    for tid in (1, 2, 3):
        _run(db.regional_noshow_move_mark_sent(tid, "spb", "", "2026-10-04 12:00:00"))
    _run(db.record_regional_noshow_move_response(1, "", db.RNM_MOVED, "msk", "2026-10-04 13:00:00"))
    _run(db.record_regional_noshow_move_response(2, "", db.RNM_DECLINED, None, "2026-10-04 13:00:00"))
    summary = _run(db.regional_noshow_move_summary(""))
    assert summary == {"offered": 3, "moved": 1, "declined": 1}


# ══════════════════════════════════════════════════════════════════════════════════════════
# schedule_city_job: гейты + порядок с опросом неявившихся
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_schedule_disabled_by_default(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def body(s):
        return await rgnm.schedule_city_job(None)

    assert _run_scheduled(tmp_path, monkeypatch, body) == {"scheduled": False, "reason": "disabled"}


def test_schedule_no_date(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("regional_noshow_offer_enabled", "on"))

    async def body(s):
        return await rgnm.schedule_city_job(None)

    assert _run_scheduled(tmp_path, monkeypatch, body) == {"scheduled": False, "reason": "no_date"}


def test_schedule_targets_day_after_last_forum_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("regional_noshow_offer_enabled", "on"))
    _run(_set("forum_date", "03.10.2026"))
    _run(_set("sos_active_days", "1"))
    monkeypatch.setattr(rgnm, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await rgnm.schedule_city_job(None)
        assert result["scheduled"] is True
        assert result["run_at"] == datetime(2026, 10, 4, 12, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_delayed_past_poll_time_when_poll_enabled(tmp_path, monkeypatch):
    """Ядро задачи: если опрос неявившихся тоже включён — предложение уходит не раньше, чем
    через 3 часа после расчётного времени опроса, даже если своё время настроено раньше."""
    _ready(tmp_path)
    _run(_set("regional_noshow_offer_enabled", "on"))
    _run(_set("regional_noshow_offer_time", "12:00"))
    _run(_set("forum_noshow_poll_enabled", "on"))
    _run(_set("forum_noshow_poll_time", "12:00"))
    _run(_set("forum_date", "03.10.2026"))
    _run(_set("sos_active_days", "1"))
    monkeypatch.setattr(rgnm, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await rgnm.schedule_city_job(None)
        assert result["run_at"] == datetime(2026, 10, 4, 15, 0)  # 12:00 + 3ч

    _run_scheduled(tmp_path, monkeypatch, body)


def test_schedule_not_delayed_when_poll_disabled(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_set("regional_noshow_offer_enabled", "on"))
    _run(_set("regional_noshow_offer_time", "12:00"))
    _run(_set("forum_noshow_poll_enabled", "off"))
    _run(_set("forum_date", "03.10.2026"))
    _run(_set("sos_active_days", "1"))
    monkeypatch.setattr(rgnm, "msk_now", lambda: datetime(2026, 9, 1, 10, 0))

    async def body(s):
        result = await rgnm.schedule_city_job(None)
        assert result["run_at"] == datetime(2026, 10, 4, 12, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


# ══════════════════════════════════════════════════════════════════════════════════════════
# send_offers: доставка, идемпотентность, мут, город назначения по умолчанию
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_send_offers_delivers_with_two_buttons(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1, city="spb"))
    bot = _with_bot(monkeypatch)
    result = _run(rgnm.send_offers("spb"))
    assert result["sent"] == 1
    assert len(bot.sent) == 1
    chat_id, text, kb = bot.sent[0]
    assert chat_id == 1
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks == ["rnm_accept", "rnm_decline"]


def test_send_offers_idempotent_same_season(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1, city="spb"))
    bot = _with_bot(monkeypatch)
    _run(rgnm.send_offers("spb"))
    result2 = _run(rgnm.send_offers("spb"))
    assert result2["sent"] == 0
    assert len(bot.sent) == 1


def test_send_offers_respects_mute(tmp_path, monkeypatch):
    from services.timeutil import msk_now
    _ready(tmp_path)
    _run(_add_delegate(1, city="spb"))
    today = msk_now().strftime("%Y-%m-%d")
    _run(db.set_broadcast_mute(1, today))
    bot = _with_bot(monkeypatch)
    result = _run(rgnm.send_offers("spb"))
    assert result == {"sent": 0, "queued": 0, "muted": 1, "failed": 0, "total": 1}
    assert bot.sent == []


def test_send_offers_no_bot_returns_zeroes(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(_add_delegate(1, city="spb"))
    monkeypatch.setattr(sched, "_bot", None)
    result = _run(rgnm.send_offers("spb"))
    assert result == {"sent": 0, "queued": 0, "muted": 0, "failed": 0, "total": 0}


def test_target_city_defaults_to_default_city_code(tmp_path):
    """«Не хардкодить msk» — дефолт берётся из `cities.default_city_code()`, не литерала."""
    import cities
    _ready(tmp_path)
    assert _run(rgnm.target_city_for("spb")) == cities.default_city_code()


def test_target_city_uses_per_city_override(tmp_path, monkeypatch):
    saved = _cities_fixture(monkeypatch)
    try:
        _ready(tmp_path)
        _run(db.set_setting("event_city_enabled", "on"))
        import cities
        key = cities.per_city_key("regional_noshow_target_city", "spb")
        _run(db.set_setting(key, "msk"))
        assert _run(rgnm.target_city_for("spb")) == "msk"
    finally:
        _restore_cities(saved)


def test_move_status_defaults_to_keep(tmp_path):
    _ready(tmp_path)
    assert _run(rgnm.move_status_for("spb")) == rgnm.STATUS_MODE_KEEP


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers/user_actions.py: rnm_accept/rnm_confirm/rnm_decline — мини-флоу делегата
# ══════════════════════════════════════════════════════════════════════════════════════════

def _fake_move_user_city(monkeypatch, *, ok=True):
    calls = []

    async def fake(telegram_id, new_city, *, status_mode, by_admin, dry_run=False):
        calls.append({
            "telegram_id": telegram_id, "new_city": new_city,
            "status_mode": status_mode, "by_admin": by_admin,
        })
        if not ok:
            return {"ok": False, "error": "boom"}
        return {
            "ok": True, "status_changed": status_mode == rgnm.STATUS_MODE_TO_MODERATION,
            "db_changes": ["users"], "sheet": {"moved": True},
        }

    monkeypatch.setattr(city_move_mod, "move_user_city", fake)
    return calls


def test_rnm_accept_shows_confirmation(tmp_path):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    cb = FakeCallback("rnm_accept", UID)
    _run(ua.regional_noshow_move_accept(cb))
    assert "Перенести заявку в Москву?" in cb.message.text
    callbacks = [b.callback_data for row in cb.message.markup.inline_keyboard for b in row]
    assert callbacks == ["rnm_confirm", "rnm_decline"]


def test_rnm_accept_stale_row_is_noop(tmp_path):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    cb = FakeCallback("rnm_accept", UID)  # никогда не получал предложение — строки нет
    _run(ua.regional_noshow_move_accept(cb))
    assert cb.message.edit_calls == 0
    assert cb.answers == [(None, False)]


def test_rnm_confirm_calls_move_user_city_with_target_and_status(tmp_path, monkeypatch):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.set_setting("regional_noshow_move_status", "to_moderation"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    calls = _fake_move_user_city(monkeypatch)

    cb = FakeCallback("rnm_confirm", UID)
    _run(ua.regional_noshow_move_confirm(cb))

    assert len(calls) == 1
    call = calls[0]
    assert call["telegram_id"] == UID
    import cities
    assert call["new_city"] == cities.default_city_code()
    assert call["status_mode"] == "to_moderation"
    assert "теперь в Москве" in cb.message.text
    assert "посмотрят ещё раз" in cb.message.text


def test_rnm_confirm_default_status_keeps_approval(tmp_path, monkeypatch):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    calls = _fake_move_user_city(monkeypatch)

    cb = FakeCallback("rnm_confirm", UID)
    _run(ua.regional_noshow_move_confirm(cb))

    assert calls[0]["status_mode"] == "keep"
    assert "посмотрят ещё раз" not in cb.message.text


def test_rnm_confirm_records_moved_response(tmp_path, monkeypatch):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    _fake_move_user_city(monkeypatch)

    _run(ua.regional_noshow_move_confirm(FakeCallback("rnm_confirm", UID)))

    summary = _run(db.regional_noshow_move_summary(""))
    assert summary["moved"] == 1


def test_rnm_confirm_twice_is_idempotent(tmp_path, monkeypatch):
    """Повторный тап «Уже перенесено» — второй вызов move_user_city не происходит."""
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    calls = _fake_move_user_city(monkeypatch)

    _run(ua.regional_noshow_move_confirm(FakeCallback("rnm_confirm", UID)))
    cb2 = FakeCallback("rnm_confirm", UID)
    _run(ua.regional_noshow_move_confirm(cb2))

    assert len(calls) == 1  # второй вызов не дошёл до move_user_city
    assert cb2.answers[-1] == ("Уже перенесено.", True)


def test_rnm_decline_records_and_acks(tmp_path):
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))

    cb = FakeCallback("rnm_decline", UID)
    _run(ua.regional_noshow_move_decline(cb))

    assert "до встречи в следующий раз" in cb.message.text
    summary = _run(db.regional_noshow_move_summary(""))
    assert summary["declined"] == 1


def test_rnm_accept_already_moved_to_other_city_by_admin(tmp_path):
    """Делегата перевели вручную в ДРУГОЙ город между предложением и тапом — «Заявка уже не
    в {city}», move_user_city не вызывается вовсе (проверяется отсутствием падения/изменений)."""
    from handlers import user_actions as ua

    _ready(tmp_path)
    _run(_add_delegate(UID, city="spb"))
    _run(db.regional_noshow_move_mark_sent(UID, "spb", "", "2026-10-04 12:00:00"))
    # Админ вручную перевёл делегата в другой город (msk) до того, как он ответил на предложение.
    async def _move_manually():
        await db.update_user_answers(UID, {"event_city": "msk"}, allowed_columns=["event_city"])
    _run(_move_manually())

    cb = FakeCallback("rnm_accept", UID)
    _run(ua.regional_noshow_move_accept(cb))
    assert cb.message.edit_calls == 0
    assert "уже не в" in (cb.answers[-1][0] or "")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Сводка менеджеру города назначения
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_notify_managers_job_sends_aggregated_summary(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    saved = _cities_fixture(monkeypatch)
    try:
        bot = _with_bot(monkeypatch)
        _run(db.regional_noshow_move_mark_sent(1, "spb", "", "2026-10-04 12:00:00"))
        _run(db.record_regional_noshow_move_response(1, "", db.RNM_MOVED, "msk", "2026-10-04 13:00:00"))
        _run(db.regional_noshow_move_mark_sent(2, "spb", "", "2026-10-04 12:00:00"))
        _run(db.record_regional_noshow_move_response(2, "", db.RNM_MOVED, "msk", "2026-10-04 13:05:00"))

        _run(rgnm._notify_managers_job())

        assert len(bot.sent) == 1
        chat_id, text, _kb = bot.sent[0]
        assert chat_id == ADMIN_ID  # суперадмин держит moderate_reg по умолчанию
        assert "2" in text
        assert "Санкт-Петербург" in text

        unnotified = _run(db.regional_noshow_move_unnotified_moved())
        assert unnotified == []  # обе строки помечены notified_at
    finally:
        _restore_cities(saved)


def test_notify_managers_job_no_pending_rows_sends_nothing(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _with_bot(monkeypatch)
    _run(rgnm._notify_managers_job())
    assert bot.sent == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# Реестр: дефолты/формат/TOGGLE_SECTION/per_city
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_registry_defaults_and_format():
    from settings_schema import SETTINGS_SCHEMA
    import settings_ops

    enabled = SETTINGS_SCHEMA["regional_noshow_offer_enabled"]
    assert enabled["default"] == "off"
    assert enabled["per_city"] is True
    assert enabled["type"] == "enum"

    t = SETTINGS_SCHEMA["regional_noshow_offer_time"]
    assert t["type"] == "text"
    assert t["format"] == "time"
    assert t["default"] == "12:00"
    assert t["per_city"] is True

    target = SETTINGS_SCHEMA["regional_noshow_target_city"]
    assert target["type"] == "text"
    assert target["default"] is None
    assert target["per_city"] is True

    status = SETTINGS_SCHEMA["regional_noshow_move_status"]
    assert status["type"] == "enum"
    assert status["options"] == ["keep", "to_moderation"]
    assert status["default"] == "keep"
    assert status["per_city"] is True

    text = SETTINGS_SCHEMA["regional_noshow_offer_text"]
    assert text["type"] == "text"
    assert text["group"] == "reg"
    assert text["per_city"] is True

    assert settings_ops.TOGGLE_SECTION["regional_noshow_offer_enabled"] == "apps"


def test_registry_default_text_has_manual_en_translation():
    from settings_schema import SETTINGS_SCHEMA
    from services.i18n_form_manual import FORM_DEFAULT_EN

    default = SETTINGS_SCHEMA["regional_noshow_offer_text"]["default"]
    assert default in FORM_DEFAULT_EN


# ══════════════════════════════════════════════════════════════════════════════════════════
# Хаб «🎪 Форум: функции» + экран
# ══════════════════════════════════════════════════════════════════════════════════════════

def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_hub_shows_regional_noshow_move_row_and_button(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert "Перенос неявившихся на форум в Москве" in text
    assert "rgnm_cfg:msk" in _cbs(kb)


def test_cfg_screen_toggle_flips_global_setting(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _run(aff.rgnm_toggle_go(FakeCallback("rgnm_toggle:_all", ADMIN_ID)))
    assert _run(db.get_setting("regional_noshow_offer_enabled")) == "on"
    _run(aff.rgnm_toggle_go(FakeCallback("rgnm_toggle:_all", ADMIN_ID)))
    assert _run(db.get_setting("regional_noshow_offer_enabled")) == "off"


def test_cfg_screen_shows_summary_line(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _run(db.regional_noshow_move_mark_sent(1, None, "", "2026-10-04 12:00:00"))
    _run(db.record_regional_noshow_move_response(1, "", db.RNM_MOVED, "msk", "2026-10-04 13:00:00"))
    text, _kb = _run(aff._regional_noshow_cfg_text_kb(None))
    assert "Предложено 1, перенеслись 1, отказались 0" in text


def test_cfg_screen_status_toggle_cycles(tmp_path):
    from handlers import admin_forum_functions as aff

    _ready(tmp_path)
    _run(aff.rgnm_status_toggle_go(FakeCallback("rgnm_status_toggle:_all", ADMIN_ID)))
    assert _run(db.get_setting("regional_noshow_move_status")) == "to_moderation"
    _run(aff.rgnm_status_toggle_go(FakeCallback("rgnm_status_toggle:_all", ADMIN_ID)))
    assert _run(db.get_setting("regional_noshow_move_status")) == "keep"


def test_cfg_screen_target_pick_sets_city(tmp_path, monkeypatch):
    from handlers import admin_forum_functions as aff

    saved = _cities_fixture(monkeypatch)
    try:
        _ready(tmp_path)
        _run(db.set_setting("event_city_enabled", "on"))
        cb = FakeCallback("rgnm_target_pick:_all:msk", ADMIN_ID)
        _run(aff.rgnm_target_pick(cb))
        assert _run(db.get_setting("regional_noshow_target_city")) == "msk"
    finally:
        _restore_cities(saved)


# ══════════════════════════════════════════════════════════════════════════════════════════
# EN: перевод текста предложения и ответов делегату
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_offer_text_and_replies_translated_for_en_delegate(tmp_path, monkeypatch):
    from handlers import user_actions as ua
    from services.i18n_form_manual import seed

    _ready(tmp_path)
    _run(_set("delegate_lang_enabled", "on"))
    _run(seed("en"))
    _run(_add_delegate(UID, city="spb"))
    _run(db.set_user_lang(UID, "en"))

    bot = _with_bot(monkeypatch)
    _run(rgnm.send_offers("spb"))
    assert bot.sent
    _chat_id, text, kb = bot.sent[0]
    assert "Couldn't make it to the forum" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ Move my application to Moscow" in labels
    assert "No, thanks" in labels

    cb = FakeCallback("rnm_accept", UID)
    _run(ua.regional_noshow_move_accept(cb))
    assert "Move your application to Moscow?" in cb.message.text
