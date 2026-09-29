"""Форум-ночь п.6 (D-25, идея №14 бэклога чек-ина): шаблон рассылки «Не пришёл» — готовые
кнопки ответа делегата («Уже еду»/«Не смогу прийти»/«Я на месте»).

pytest-asyncio в этом окружении не установлен — каждый async-хелпер гоняется через
`asyncio.run()`, `config.DB_PATH` указывает на файл в `tmp_path`."""
from __future__ import annotations

import asyncio

from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
import services.scheduler as sched
import services.checkin_not_arrived as cna
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import FakeCallback, FakeMessage

ADMIN_ID = 900925


def _ready(tmp_path, name="checkin_not_arrived.db"):
    config.DB_PATH = str(tmp_path / name)
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


async def _add_user(tid, *, status="approved", city=None, season=None):
    await db.add_user({
        "telegram_id": tid, "full_name": f"Делегат {tid}",
        "registration_date": "2026-01-01 00:00:00", "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET status = ?, season = ? WHERE telegram_id = ?", (status, season, tid),
        )
        await conn.commit()


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text, reply_markup)]

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"message_id": 1})()


def _with_bot():
    bot = FakeBot()
    sched._bot = bot
    return bot


# ── БД: таблица + USER_PURGE_TABLES ──────────────────────────────────────────────────────────

def test_checkin_not_arrived_in_user_purge_tables():
    names = {t for t, _col, _grp in db.USER_PURGE_TABLES}
    assert "checkin_not_arrived" in names


def test_pending_mark_sent_idempotent_per_day(tmp_path):
    """`checkin_not_arrived_pending_ids` сверяет `day` с СЕГОДНЯ (МСК) — отметка ДОЛЖНА
    приходиться на реальный сегодняшний день, иначе дедуп не сработает (это и есть
    идемпотентность «в тот же день», а не вообще)."""
    from services.timeutil import msk_now
    _ready(tmp_path)
    _run(_add_user(1))
    assert _run(db.checkin_not_arrived_pending_ids()) == [1]

    today = msk_now().strftime("%Y-%m-%d 12:00:00")
    marked = _run(db.checkin_not_arrived_mark_sent(1, "msk", today))
    assert marked is True
    assert _run(db.checkin_not_arrived_pending_ids()) == []

    # тот же день -- повторная отметка не создаёт вторую строку
    marked2 = _run(db.checkin_not_arrived_mark_sent(1, "msk", msk_now().strftime("%Y-%m-%d 15:00:00")))
    assert marked2 is False


def test_pending_excludes_already_arrived(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    assert _run(db.checkin_not_arrived_pending_ids()) == []


def test_response_recorded_and_summary_counts(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    _run(_add_user(3))
    now = "2026-10-30 12:00:00"
    for tid in (1, 2, 3):
        _run(db.checkin_not_arrived_mark_sent(tid, "msk", now))

    assert _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_COMING, now)) is True
    assert _run(db.record_checkin_not_arrived_response(2, "2026-10-30", db.CNA_CANT, now)) is True
    # 3-й не отвечал вовсе

    summary = _run(db.checkin_not_arrived_summary(day="2026-10-30"))
    assert summary == {"coming": 1, "cant": 1, "here": 0, "no_response": 1, "total": 3}


def test_response_can_be_changed_by_repeated_tap(tmp_path):
    """Повторный тап другой кнопки перезаписывает ответ — делегат мог ошибиться."""
    _ready(tmp_path)
    _run(_add_user(1))
    now = "2026-10-30 12:00:00"
    _run(db.checkin_not_arrived_mark_sent(1, "msk", now))
    _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_COMING, now))
    _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_HERE, now))
    summary = _run(db.checkin_not_arrived_summary(day="2026-10-30"))
    assert summary["here"] == 1 and summary["coming"] == 0


def test_response_to_missing_row_is_false_not_a_crash(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    ok = _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_COMING, "2026-10-30 12:00:00"))
    assert ok is False


def test_summary_scoped_by_city_snapshot(tmp_path):
    """Сводка города — по СНИМКУ `event_city` на момент отправки, не по текущему полю users."""
    from core import cities as cities_mod
    _ready(tmp_path)
    _run(_add_user(1, city="msk"))
    now = "2026-10-30 12:00:00"
    _run(db.checkin_not_arrived_mark_sent(1, "msk", now))
    _run(db.record_checkin_not_arrived_response(1, "2026-10-30", db.CNA_COMING, now))
    scope_msk = cities_mod.city_scope("msk")
    scope_spb = cities_mod.city_scope("spb")
    assert _run(db.checkin_not_arrived_summary(city_scope=scope_msk, day="2026-10-30"))["coming"] == 1
    assert _run(db.checkin_not_arrived_summary(city_scope=scope_spb, day="2026-10-30"))["coming"] == 0


# ── services/checkin_not_arrived.py: send() ──────────────────────────────────────────────────

def test_send_marks_and_delivers_with_three_buttons(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    bot = _with_bot()

    result = _run(cna.send(city=None, city_scope=None))
    assert result == {"sent": 1, "quiet": 0, "failed": 0, "total": 1}
    assert len(bot.sent) == 1
    chat_id, text, kb = bot.sent[0]
    assert chat_id == 1
    assert "не видим" in text
    assert isinstance(kb, InlineKeyboardMarkup)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(callbacks) == 3
    assert all(c.startswith(f"cna:") for c in callbacks)
    assert {c.split(":")[1] for c in callbacks} == {db.CNA_COMING, db.CNA_CANT, db.CNA_HERE}


def test_send_is_idempotent_same_day(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    bot = _with_bot()
    _run(cna.send(city=None, city_scope=None))
    result2 = _run(cna.send(city=None, city_scope=None))
    assert result2 == {"sent": 0, "quiet": 0, "failed": 0, "total": 0}
    assert len(bot.sent) == 1  # второй прогон никому ничего не отправил


def test_send_skips_quiet_hours_delegates_without_marking(tmp_path):
    """Полное окно тихих часов (00:00-23:59, тот же приём, что test_quiet_hours_kinds_260916.py)
    — делегата в тихих часах ПРОПУСКАЕМ, не ставим в очередь `quiet_hours` (отложенная доставка
    `flush_due` не перепроверяет отметку «Вход» на момент доставки — пришедший ночью получил бы
    «мы тебя не видим» уже после того, как отметился). Идемпотентность «раз в день» НЕ
    срабатывает для пропущенных — `checkin_not_arrived_mark_sent` не вызывается, повторный
    запуск позже (после тихих часов) обязан взять того же делегата снова."""
    _ready(tmp_path)
    _run(_add_user(1))
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))
    bot = _with_bot()

    result = _run(cna.send(city=None, city_scope=None))
    assert result == {"sent": 0, "quiet": 1, "failed": 0, "total": 1}
    assert bot.sent == []  # ничего не ушло немедленно

    # НЕ отмечен отправленным -- повторное нажатие "в процессе" позже возьмёт его снова
    assert _run(db.checkin_not_arrived_pending_ids()) == [1]


def test_send_retry_after_quiet_hours_delivers(tmp_path):
    """Повторный запуск после того, как тихие часы кончились (тумблер выключен вручную —
    имитирует «менеджер нажал ещё раз позже»), доставляет тому же делегату — идемпотентность
    по (делегат, день) это позволяет, т.к. ему ничего не отправлялось в первый раз."""
    _ready(tmp_path)
    _run(_add_user(1))
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))
    bot = _with_bot()
    first = _run(cna.send(city=None, city_scope=None))
    assert first == {"sent": 0, "quiet": 1, "failed": 0, "total": 1}

    _run(db.set_setting("quiet_hours_enabled", "off"))
    second = _run(cna.send(city=None, city_scope=None))
    assert second == {"sent": 1, "quiet": 0, "failed": 0, "total": 1}
    assert len(bot.sent) == 1


def test_send_scoped_to_city(tmp_path):
    from core import cities as cities_mod
    _ready(tmp_path)
    _run(_add_user(1, city="msk"))
    _run(_add_user(2, city="spb"))
    bot = _with_bot()
    result = _run(cna.send(city="msk", city_scope=cities_mod.city_scope("msk")))
    assert result["total"] == 1
    assert bot.sent[0][0] == 1


def test_pending_count_matches_pending_ids(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    assert _run(cna.pending_count()) == 2
    _run(db.record_checkin(1, db.CHECKIN_ENTRY_POINT, source="miniapp"))
    assert _run(cna.pending_count()) == 1


# ── handlers/admin_checkin.py: экран подтверждения + отправка ───────────────────────────────

def test_admin_confirm_shows_count_and_two_buttons(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    cb = FakeCallback("cna_send:_all", ADMIN_ID)
    _run(ac.cna_send_confirm(cb))
    assert cb.message.answers, "confirm screen not shown"
    text, _pm, kb = cb.message.answers[-1]
    assert "2 делегатам" in text
    assert "CSV-режим" in text or "по ошибке" in text
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c.startswith("cna_send_go:") for c in callbacks)
    assert "cna_send_no" in callbacks


def test_admin_confirm_alerts_when_nobody_pending(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    cb = FakeCallback("cna_send:_all", ADMIN_ID)
    _run(ac.cna_send_confirm(cb))
    assert cb.answers and cb.answers[0][1] is True


def test_admin_send_go_executes_and_reports(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _with_bot()
    cb = FakeCallback("cna_send_go:_all", ADMIN_ID)
    _run(ac.cna_send_go(cb))
    assert cb.message.edit_calls >= 1
    assert cb.message.answers, "no report after send"
    report = cb.message.answers[-1][0]
    assert "Отправлено 1" in report


def test_admin_send_go_reports_quiet_hours_separately(tmp_path):
    """Экран после отправки обязан назвать делегатов, попавших в тихие часы, отдельной строкой
    — менеджер должен понимать, что часть аудитории не тронута и её нужно дожать позже."""
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _run(db.set_setting("quiet_hours_enabled", "on"))
    _run(db.set_setting("quiet_hours_start", "00:00"))
    _run(db.set_setting("quiet_hours_end", "23:59"))
    _with_bot()
    cb = FakeCallback("cna_send_go:_all", ADMIN_ID)
    _run(ac.cna_send_go(cb))
    report = cb.message.answers[-1][0]
    assert "Отправлено 0" in report
    assert "1 делегатов сейчас в тихих часах" in report
    assert "повторите позже" in report


def test_admin_send_cancel_does_not_send(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _with_bot()
    cb = FakeCallback("cna_send_no", ADMIN_ID)
    _run(ac.cna_send_cancel(cb))
    assert "Отменено" in cb.message.text
    assert _run(db.checkin_not_arrived_pending_ids()) == [1]  # никого не тронуло


# ── capability (deny-by-default, D-02) ───────────────────────────────────────────────────────

def test_required_capability_cna_send_is_moderate_reg():
    from handlers import admin_caps
    assert admin_caps.required_capability(callback_data="cna_send:_all") == "moderate_reg"
    assert admin_caps.required_capability(callback_data="cna_send_go:_all") == "moderate_reg"
    assert admin_caps.required_capability(callback_data="cna_send_no") == "moderate_reg"


# ── handlers/user_actions.py: ответ делегата ────────────────────────────────────────────────

class FakePhotoMessage(FakeMessage):
    async def answer_photo(self, photo, caption=None):
        self.answers.append((caption, "photo", None))


def test_delegate_response_coming_records_and_acks(tmp_path):
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(777))
    _run(db.checkin_not_arrived_mark_sent(777, "msk", "2026-10-30 12:00:00"))
    cb = FakeCallback(f"cna:{db.CNA_COMING}:2026-10-30", 777)
    _run(ua.checkin_not_arrived_respond(cb))
    assert cb.answers and cb.answers[-1][1] is True
    summary = _run(db.checkin_not_arrived_summary(day="2026-10-30"))
    assert summary["coming"] == 1


def test_delegate_response_here_offers_qr_button(tmp_path):
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(778))
    _run(db.checkin_not_arrived_mark_sent(778, "msk", "2026-10-30 12:00:00"))
    cb = FakeCallback(f"cna:{db.CNA_HERE}:2026-10-30", 778)
    cb.message = FakePhotoMessage()
    _run(ua.checkin_not_arrived_respond(cb))
    assert cb.message.answers, "no follow-up message with QR button"
    text, _pm, kb = cb.message.answers[-1]
    assert "волонтёру" in text
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "cna_qr" in callbacks


def test_delegate_response_unknown_value_is_noop(tmp_path):
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(779))
    cb = FakeCallback("cna:garbage:2026-10-30", 779)
    _run(ua.checkin_not_arrived_respond(cb))
    assert cb.answers == [(None, False)]


def test_delegate_response_stale_row_is_noop_not_a_crash(tmp_path):
    """Строки нет вовсе (например, сообщение переслано другому человеку) — fail-soft (D-04)."""
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(780))
    cb = FakeCallback(f"cna:{db.CNA_COMING}:2026-10-30", 780)
    _run(ua.checkin_not_arrived_respond(cb))  # никогда не получал шаблон -- строки нет
    assert cb.answers == [(None, False)]


def test_cna_qr_sends_photo_for_approved_delegate(tmp_path):
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(781))
    cb = FakeCallback("cna_qr", 781)
    cb.message = FakePhotoMessage()
    _run(ua.checkin_not_arrived_show_qr(cb))
    assert cb.message.answers and cb.message.answers[-1][1] == "photo"


def test_cna_qr_denies_not_approved_delegate(tmp_path):
    from handlers import user_actions as ua
    _ready(tmp_path)
    _run(_add_user(782, status="pending"))
    cb = FakeCallback("cna_qr", 782)
    cb.message = FakePhotoMessage()
    _run(ua.checkin_not_arrived_show_qr(cb))
    assert not cb.message.answers  # фото не ушло
    assert cb.answers and cb.answers[0][1] is True  # show_alert с текстом ожидания


def test_admin_confirm_names_city_and_today(tmp_path):
    """Подтверждение массовой отправки называет число, город и «сегодня» — менеджер видит, кому
    уйдёт, до нажатия."""
    from core.cities import city_label
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    _run(_add_user(1, city="spb"))
    _run(_add_user(2, city="spb"))
    _run(_add_user(3, city="msk"))
    cb = FakeCallback("cna_send:spb", ADMIN_ID)
    _run(ac.cna_send_confirm(cb))
    text = cb.message.answers[-1][0]
    assert f"Уйдёт 2 делегатам города {_run(city_label('spb'))}" in text
    assert "не пришли сегодня" in text


# ── Итог после отправки: ушло меньше, чем обещал экран подтверждения ─────────────────────────

def test_confirm_button_carries_count(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    cb = FakeCallback("cna_send:_all", ADMIN_ID)
    _run(ac.cna_send_confirm(cb))
    kb = cb.message.answers[-1][2]
    assert "cna_send_go:_all:2" in [b.callback_data for row in kb.inline_keyboard for b in row]


def test_send_go_reports_fewer_than_confirmed(tmp_path):
    """На подтверждении было 3, за это время один отметился на входе — «Ушло 2 из 3»."""
    from handlers import admin_checkin as ac
    from handlers.admin_caps import required_capability
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    _with_bot()
    cb = FakeCallback("cna_send_go:_all:3", ADMIN_ID)
    assert required_capability(callback_data=cb.data) == "moderate_reg"
    _run(ac.cna_send_go(cb))
    report = cb.message.answers[-1][0]
    assert "Ушло 2 из 3" in report
    assert "1 за это время отметились на входе" in report
    assert "ошибка доставки" not in report


def test_send_go_reports_delivery_failure_separately(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _run(_add_user(2))
    bot = _with_bot()

    async def flaky(chat_id, text, reply_markup=None):
        if chat_id == 2:
            raise RuntimeError("chat not found")
        bot.sent.append((chat_id, text, reply_markup))

    bot.send_message = flaky
    cb = FakeCallback("cna_send_go:_all:2", ADMIN_ID)
    _run(ac.cna_send_go(cb))
    report = cb.message.answers[-1][0]
    assert "Ушло 1 из 2" in report
    assert "1 не получили сообщение (ошибка доставки)" in report
    assert "отметились" not in report


def test_send_go_all_sent_keeps_plain_report(tmp_path):
    from handlers import admin_checkin as ac
    _ready(tmp_path)
    _run(_add_user(1))
    _with_bot()
    cb = FakeCallback("cna_send_go:_all:1", ADMIN_ID)
    _run(ac.cna_send_go(cb))
    report = cb.message.answers[-1][0]
    assert "Отправлено 1 делегатам из 1" in report
    assert "Ушло" not in report
