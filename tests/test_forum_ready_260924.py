"""Бэклог чек-ина №25: «🚦 Готовность к форуму» (handlers/admin_forum_ready.py) — светофор
по городу из хаба «🎪 Форум: функции». Экран только читает: планировщик — через `get_job`,
без `schedule_city_jobs` (тот переставляет джобы).

async через `asyncio.run()` (конвенция проекта), БД — `tests/_dbtpl.py::fast_init_db`."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from config import config
from database import db
from handlers import admin_forum_functions as aff
from handlers import admin_forum_ready as afr
from handlers.admin_caps import role_caps_key
from services import sheets
from tests._dbtpl import fast_init_db

ADMIN_ID = 924200


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum_ready_260924.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


class _Bot:
    id = 42

    def __init__(self, member=True):
        self.member = member

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member" if self.member else "left")


class _FakeSched:
    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.added = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, *a, **k):  # экран не имеет права ставить джобы
        self.added.append((a, k))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _patch_sched(monkeypatch, sched):
    from services import scheduler
    monkeypatch.setattr(scheduler, "get_scheduler", lambda: sched)


def test_empty_setup_is_red_with_fix_buttons(tmp_path, monkeypatch):
    _ready(tmp_path)
    sched = _FakeSched()
    _patch_sched(monkeypatch, sched)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴 Дата форума не задана" in text
    assert "🔴 Вход по QR выключен" in text
    assert "🔴 Никому, кроме суперадминов, не выдано право отметки" in text
    assert "🔴 Программа не заведена" in text
    assert "⚪ Таблица не подключена" in text
    assert "🟡 Чат делегатов не привязан" in text
    cbs = _cbs(kb)
    for cb in ("settings_edit:forum_date", "admin_forum_functions", "admin_roles", "admin_program", "admin_sos"):
        assert cb in cbs
    assert cbs[-2:] == ["forum_ready_re:msk", "admin_forum_functions"]
    assert sched.added == []


def test_ready_city_is_green(tmp_path, monkeypatch):
    _ready(tmp_path)
    from datetime import datetime
    run_at = datetime(2026, 10, 2, 18, 0)
    monkeypatch.setattr(afr, "msk_now", lambda: datetime(2026, 9, 25, 12, 0))
    _patch_sched(monkeypatch, _FakeSched({"checkin_qr_evening:all": SimpleNamespace(next_run_time=run_at)}))
    _run(db.set_setting("forum_date", "03.10.2026"))
    _run(db.set_setting("sos_active_days", "1"))  # однодневный: двухдневный подсвечен жёлтым
    _run(db.set_setting("checkin_qr_enabled", "on"))
    _run(db.add_staff(924201, "stats_manager", ADMIN_ID))
    _run(db.set_setting(role_caps_key("stats_manager"), "checkin"))
    _run(db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting("sos_chat_id", "-1001"))
    _run(db.set_setting("sos_chat_title", "Орги"))
    _run(db.set_setting("delegate_chat_id", "-1002"))
    _run(db.set_setting("delegate_chat_title", "Делегаты"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    monkeypatch.setattr(sheets, "_write_state", {"ok": time.time() - 120, "fail": None})

    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴" not in text and "🟡" not in text, text
    assert "рассылка QR запланирована на 02.10 18:00" in text
    assert "С правом отметки на входе: 1 чел." in text
    assert "сессий 1" in text
    assert "Чат SOS «Орги», бот в чате" in text
    assert "последняя запись 2 мин назад" in text
    assert "Всё готово." in text
    # Зелёная строка даты несёт только «Изменить» (дата/длина форума), проблемных кнопок нет.
    assert _cbs(kb) == [
        "settings_edit:forum_date", "settings_edit:sos_active_days",
        "forum_ready_re:msk", "admin_forum_functions",
    ]


def test_bot_kicked_from_sos_chat_and_sheet_failure_are_red(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("sos_chat_id", "-1001"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    now = time.time()
    monkeypatch.setattr(sheets, "_write_state", {"ok": now - 600, "fail": now - 60})
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot(member=False)))
    assert "🔴 Бота нет в чате SOS" in text
    assert "🔴 Последняя запись в таблицу не прошла" in text
    assert "admin_sync_sheet" in _cbs(kb)


def test_qr_on_without_jobs_or_date_explains(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("checkin_qr_enabled", "on"))
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🔴 Вход по QR включён, но рассылка не поставлена — нет даты форума" in text
    assert "checkinqr_cfg:msk" in _cbs(kb)


def test_scheduler_down_does_not_break_screen(tmp_path, monkeypatch):
    _ready(tmp_path)
    from services import scheduler

    def _boom():
        raise RuntimeError("no scheduler")

    monkeypatch.setattr(scheduler, "get_scheduler", _boom)
    _run(db.set_setting("checkin_qr_enabled", "on"))
    text, _kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "расписание рассылки прочитать не удалось" in text


def test_note_write_records_success_and_failure(monkeypatch):
    monkeypatch.setattr(sheets, "_write_state", {"ok": None, "fail": None})
    sheets._note_write(True)
    sheets._note_write(False)
    state = sheets.last_write_state()
    assert state["ok"] and state["fail"] and state["fail"] >= state["ok"]


def test_hub_has_ready_button(tmp_path):
    _ready(tmp_path)
    _text, kb = _run(aff._render_hub(ADMIN_ID, "msk"))
    assert _cbs(kb)[0] == "forum_ready:msk"


def test_one_failing_row_turns_gray_and_screen_still_renders(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")

    async def _boom(code):
        raise RuntimeError("db locked")

    monkeypatch.setattr(afr, "_row_program", _boom)
    text, kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "⚪ Программа: не удалось проверить" in text
    assert "🔴 Дата форума не задана" in text
    assert "🟡 Чат делегатов не привязан" in text
    assert text.count("\n🔴 ") + text.count("\n🟡 ") + text.count("\n🟢 ") + text.count("\n⚪ ") == 7
    assert "forum_ready_re:msk" in _cbs(kb)


def test_qr_send_counts_failure_is_contained(tmp_path, monkeypatch):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    _run(db.set_setting("checkin_qr_enabled", "on"))

    async def _boom(**k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(afr, "checkin_qr_send_counts", _boom)
    text, _kb = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "⚪ Вход по QR: не удалось проверить" in text
    assert "Дата форума" in text


def test_count_program_sessions_none_counts_all_cities(tmp_path):
    from services.checkin_arrival import count_program_sessions
    _ready(tmp_path)
    _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "А"))
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Б"))
    assert _run(count_program_sessions("spb")) == 1
    assert _run(count_program_sessions(None)) == 2


def _date_row(tmp_path, monkeypatch, forum_date, now, days=None):
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(afr, "msk_now", lambda: now)
    _run(db.set_setting("forum_date", forum_date))
    if days is not None:
        _run(db.set_setting("sos_active_days", str(days)))
    return _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))


def test_past_forum_date_is_yellow_with_fix(tmp_path, monkeypatch):
    """Форум закончился (последний день раньше сегодняшнего по МСК) — жёлтая строка с кнопкой
    правки."""
    from datetime import datetime
    text, kb = _date_row(tmp_path, monkeypatch, "03.10.2026", datetime(2026, 10, 5, 0, 5))
    assert "🟡 Дата форума прошла (03.10–04.10) — это прошлый форум? Обновите дату" in text
    assert "settings_edit:forum_date" in _cbs(kb)


def test_second_forum_day_is_not_past(tmp_path, monkeypatch):
    """Второй день двухдневного форума — форум ещё идёт, не «дата прошла»."""
    from datetime import datetime
    text, _kb = _date_row(tmp_path, monkeypatch, "30.10.2026", datetime(2026, 10, 31, 9, 0), days=2)
    assert "🟡 Форум: 30.10–31.10 (2 дня) — идёт сегодня — ⚠️ дольше одного дня" in text


def test_forum_today_is_green_today(tmp_path, monkeypatch):
    from datetime import datetime
    text, kb = _date_row(tmp_path, monkeypatch, "03.10.2026", datetime(2026, 10, 3, 23, 50), days=1)
    assert "🟢 Форум: 03.10 (1 день) — идёт сегодня" in text.splitlines()


def test_future_forum_date_is_plain_green(tmp_path, monkeypatch):
    from datetime import datetime
    text, _kb = _date_row(tmp_path, monkeypatch, "03.10.2026", datetime(2026, 10, 2, 23, 59), days=1)
    assert "🟢 Форум: 03.10 (1 день)" in text.splitlines()


def test_two_day_forum_is_yellow_with_edit_buttons(tmp_path, monkeypatch):
    """Длина > 1 дня — жёлтая с ⚠️ (не ошибка, у Москвы два дня законно, но однодневный
    региональный форум с длиной 2 получает лишний «день форума»): явный диапазон и кнопки
    правки даты и длины."""
    from datetime import datetime
    text, kb = _date_row(tmp_path, monkeypatch, "30.10.2026", datetime(2026, 10, 20, 12, 0), days=2)
    assert "🟡 Форум: 30.10–31.10 (2 дня) — ⚠️ дольше одного дня" in text
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    assert ["settings_edit:forum_date", "settings_edit:sos_active_days"] in rows
    edit_idx = rows.index(["settings_edit:forum_date", "settings_edit:sos_active_days"])
    labels = [b.text for b in kb.inline_keyboard[edit_idx]]
    assert labels == ["🟡 🗓 Изменить дату", "🟡 🗓 Сколько дней идёт"]


def test_days_word():
    assert [afr._days_word(n) for n in (1, 2, 5, 11, 21, 22)] == [
        "1 день", "2 дня", "5 дней", "11 дней", "21 день", "22 дня",
    ]


def test_stale_arrival_queue_is_yellow(tmp_path, monkeypatch):
    """Нагрузочный прогон 25.09: «Пришёл» пишется в лист из очереди — застрявшая больше 5 мин
    очередь = жёлтая строка «Таблица» с числом и возрастом, свежая — не мешает зелёной."""
    from datetime import datetime
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    monkeypatch.setattr(sheets, "_write_state", {"ok": time.time() - 120, "fail": None})
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 3, 9, 0))
    _run(db.enqueue_sheet_arrival(1, db.SHEET_ARRIVAL_SET))
    _run(db.enqueue_sheet_arrival(2, db.SHEET_ARRIVAL_SET))

    monkeypatch.setattr(afr, "msk_now", lambda: datetime(2026, 10, 3, 9, 3))
    text, _ = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🟢 Таблица пишется" in text

    monkeypatch.setattr(afr, "msk_now", lambda: datetime(2026, 10, 3, 9, 12))
    text, _ = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🟡 Отметки «Пришёл» копятся: в очереди 2, старейшая 12 мин" in text


def test_waiting_for_sheet_row_is_not_a_write_jam(tmp_path, monkeypatch):
    """Делегата ещё нет в листе — событие ждёт строку до 7 дней. Это не «таблица не принимает
    запись»: строка «Таблица» остаётся зелёной, ждущие — отдельной строкой."""
    from datetime import datetime
    from services.sheet_arrival_sync import MISSING_ERROR
    _ready(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "creds.json")
    monkeypatch.setattr(sheets, "_write_state", {"ok": time.time() - 120, "fail": None})
    monkeypatch.setattr(db, "msk_now", lambda: datetime(2026, 10, 3, 9, 0))
    _run(db.enqueue_sheet_arrival(1, db.SHEET_ARRIVAL_SET))
    _run(db.fail_sheet_arrivals({1: 10**9}, MISSING_ERROR, "2026-10-03 10:00:00"))
    monkeypatch.setattr(afr, "msk_now", lambda: datetime(2026, 10, 3, 9, 30))
    text, _ = _run(afr.render_ready(ADMIN_ID, "msk", _Bot()))
    assert "🟢 Таблица пишется" in text and "копятся" not in text
    assert "\n⏳ «Пришёл» ждут своей строки в листе: 1" in text


def _cities_on(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))


def test_sos_button_carries_traffic_light_city(tmp_path, monkeypatch):
    _cities_on(tmp_path)
    _patch_sched(monkeypatch, _FakeSched())
    row = _run(afr._row_sos("tyumen", _Bot()))
    assert row["fix"] == ("🆘 Настройки SOS", "asos_city:tyumen")


def test_program_row_honest_about_shared_photo(tmp_path):
    _cities_on(tmp_path)
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _run(db.set_setting("program_photo_file_id", "shared"))
    row = _run(afr._row_program("spb"))
    assert "есть фото" not in row["text"] and "загрузите фото для города" in row["text"]
    _run(db.set_setting("program_photo_file_id__city__spb", "own"))
    assert "есть фото" in _run(afr._row_program("spb"))["text"]
