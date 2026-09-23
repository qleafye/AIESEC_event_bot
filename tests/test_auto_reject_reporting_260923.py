"""Квик 260923 (AUTOREJ-REPORT): вкладка таблицы «🤖 Автоотказы», возврат из журнала обновляет
лист, экран «📊 Отчётность автоотказа».

Покрывает:
- database.db.auto_reject_sheet_rows — шапка + только живые автоотклонённые (status='rejected',
  не возвращены журналом), _csv_safe на строковых ячейках.
- services.scheduler.sync_auto_reject_sheet_job — пустое имя вкладки не трогает лист; заданное
  имя зовёт sync_named_worksheet с шапкой/строками.
- services.reject_journal.return_to_moderation — после успешного возврата обновляет статус в
  листе на «Новая» и пересобирает вкладку; сбой листа не откатывает возврат.
- handlers.admin_reject_reports — экран показывает текущие значения словами, кнопки ведут на
  settings_edit:<ключ>, «🔄 Обновить вкладку сейчас» — только когда имя задано.

pytest-asyncio недоступна — async через asyncio.run() (форма tests/test_reject_rules_journal.py);
БД — tmp_path.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from services import reject_journal as rj
from services import scheduler as sched

ADMIN_ID = 960001
DELEGATE_A = 960010
DELEGATE_B = 960011


def _ready(tmp_path, name="test_auto_reject_reporting.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, status="rejected", full_name=None, phone=None,
               email=None, university=None, course=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
        "phone": phone,
        "email": email,
        "university": university,
        "course": course,
    }))
    _run(_set_field(tid, "status", status))


async def _set_field(tid, field, value):
    async with db._connect() as conn:
        await conn.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, tid))
        await conn.commit()


def _seed_rule(**overrides):
    fields = {
        "name": None, "city": None, "tracks": "[]", "conditions": "{}",
        "action": "reject", "reject_text": None, "enabled": 1, "created_by": None,
    }
    fields.update(overrides)
    return _run(db.create_reject_rule(**fields))


# ══════════════════════════════════════════════════════════════════════════════════════════
# database.db.auto_reject_sheet_rows
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_auto_reject_sheet_rows_headers_and_live_row(tmp_path):
    _ready(tmp_path)
    rid = _seed_rule(name="Младше 16")
    _seed_user(DELEGATE_A, event_city="msk", full_name="Иванова", phone="+7999", email="a@b.c",
               university="ВУЗ", course="1")
    _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Слишком юн."]))

    headers, rows = _run(db.auto_reject_sheet_rows())

    assert headers == [
        "ФИО", "Ник", "Телефон", "Почта", "Город", "Вуз", "Курс", "Правила",
        "Текст отказа", "Первое срабатывание", "Последнее срабатывание", "Попыток",
        "Telegram ID",
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "Иванова"
    assert row[4] == "msk"
    assert row[7] == "Младше 16"  # человеческое имя правила, не id
    assert row[-1] == DELEGATE_A


def test_auto_reject_sheet_rows_excludes_returned_and_self_corrected(tmp_path):
    _ready(tmp_path)
    rid = _seed_rule(name="Младше 16")
    _seed_user(DELEGATE_A, status="rejected")
    entry_id = _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Текст."]))
    _run(rj.return_to_moderation(ADMIN_ID, entry_id))  # возвращена -> больше не живая

    _seed_user(DELEGATE_B, status="pending")  # сам поправил анкету
    _run(rj.record_auto_reject(DELEGATE_B, [rid], ["Текст."]))

    _headers, rows = _run(db.auto_reject_sheet_rows())
    assert rows == []


def test_auto_reject_sheet_rows_csv_safe_on_full_name(tmp_path):
    """T-en3-01 (CWE-1236): ФИО, начинающееся с «=», не должно уходить формулой."""
    _ready(tmp_path)
    rid = _seed_rule(name="Курс")
    _seed_user(DELEGATE_A, status="rejected", full_name="=HYPERLINK(\"http://evil\")")
    _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Текст."]))

    _headers, rows = _run(db.auto_reject_sheet_rows())
    assert rows[0][0].startswith("'=")


# ══════════════════════════════════════════════════════════════════════════════════════════
# services.scheduler.sync_auto_reject_sheet_job
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_sync_auto_reject_sheet_job_noop_when_tab_empty(tmp_path, monkeypatch):
    _ready(tmp_path)
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append((title, headers, rows))
        return len(rows)
    from services import sheets
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)

    result = _run(sched.sync_auto_reject_sheet_job())

    assert result == 0
    assert calls == []  # пустое имя -> до листа дело не доходит


def test_sync_auto_reject_sheet_job_calls_sync_named_worksheet_when_tab_set(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("auto_reject_sheet_tab", "🤖 Автоотказы"))
    rid = _seed_rule(name="Младше 16")
    _seed_user(DELEGATE_A, status="rejected")
    _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Текст."]))
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append((title, headers, rows))
        return len(rows)
    from services import sheets
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)

    result = _run(sched.sync_auto_reject_sheet_job())

    assert result == 1
    assert len(calls) == 1
    title, headers, rows = calls[0]
    assert title == "🤖 Автоотказы"
    assert len(rows) == 1


def test_sync_auto_reject_sheet_job_blank_whitespace_tab_is_treated_as_empty(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("auto_reject_sheet_tab", "   "))
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append(title)
        return 0
    from services import sheets
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)

    assert _run(sched.sync_auto_reject_sheet_job()) == 0
    assert calls == []


# ══════════════════════════════════════════════════════════════════════════════════════════
# services.reject_journal.return_to_moderation -> обновление листа (D-G)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_return_to_moderation_updates_sheet_status_and_resyncs_tab(tmp_path, monkeypatch):
    _ready(tmp_path)
    rid = _seed_rule(name="Курс")
    _seed_user(DELEGATE_A, status="rejected")
    entry_id = _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Текст."]))

    sheet_calls = []

    async def fake_update_status(tid, label):
        sheet_calls.append(("status", tid, label))
        return True
    resync_calls = []

    async def fake_resync():
        resync_calls.append(True)
        return 0

    from services import sheets
    monkeypatch.setattr(sheets, "update_status_in_sheet", fake_update_status)
    monkeypatch.setattr(sched, "sync_auto_reject_sheet_job", fake_resync)

    entry, error = _run(rj.return_to_moderation(ADMIN_ID, entry_id))

    assert error is None and entry is not None
    assert sheet_calls == [("status", DELEGATE_A, "Новая")]
    assert resync_calls == [True]


def test_return_to_moderation_sheet_failure_does_not_revert_db_change(tmp_path, monkeypatch):
    """Сбой листа — только предупреждение в лог, возврат в БД уже зафиксирован."""
    _ready(tmp_path)
    rid = _seed_rule(name="Курс")
    _seed_user(DELEGATE_A, status="rejected")
    entry_id = _run(rj.record_auto_reject(DELEGATE_A, [rid], ["Текст."]))

    async def boom(*a, **kw):
        raise RuntimeError("таблица недоступна")
    from services import sheets
    monkeypatch.setattr(sheets, "update_status_in_sheet", boom)

    entry, error = _run(rj.return_to_moderation(ADMIN_ID, entry_id))

    assert error is None and entry is not None  # возврат прошёл, несмотря на сбой листа
    status = _run(db.get_user(DELEGATE_A))["status"]
    assert status == "pending"


# ══════════════════════════════════════════════════════════════════════════════════════════
# handlers.admin_reject_reports — экран «📊 Отчётность автоотказа»
# ══════════════════════════════════════════════════════════════════════════════════════════

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text_edited = None
        self.edit_markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def test_render_reports_screen_shows_not_tracked_and_no_cap_by_default(tmp_path):
    _ready(tmp_path)
    from handlers.admin_reject_reports import render_reports_screen

    text, kb = _run(render_reports_screen(ADMIN_ID))

    assert "не ведётся" in text
    assert "без потолка" in text
    assert "settings_edit:auto_reject_sheet_tab" in _cbs(kb)
    assert "settings_edit:reg_submit_digest_max_minutes" in _cbs(kb)
    assert "arp_sync" not in _cbs(kb)  # без имени вкладки кнопки обновить нет


def test_render_reports_screen_shows_tab_name_and_cap_minutes_and_sync_button(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("auto_reject_sheet_tab", "🤖 Автоотказы"))
    _run(db.set_setting("reg_submit_digest_max_minutes", "30"))
    from handlers.admin_reject_reports import render_reports_screen

    text, kb = _run(render_reports_screen(ADMIN_ID))

    assert "🤖 Автоотказы" in text
    assert "не позже 30 мин" in text
    assert "arp_sync" in _cbs(kb)


def test_arp_sync_alerts_when_tab_not_set(tmp_path):
    _ready(tmp_path)
    from handlers.admin_reject_reports import arp_sync
    cb = _FakeCallback("arp_sync")

    _run(arp_sync(cb))

    assert cb.answers[0][1] is True  # show_alert
    assert "вкладк" in cb.answers[0][0].lower()


def test_arp_sync_reports_success_count(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("auto_reject_sheet_tab", "🤖 Автоотказы"))

    async def fake_job():
        return 3
    monkeypatch.setattr(sched, "sync_auto_reject_sheet_job", fake_job)

    from handlers.admin_reject_reports import arp_sync
    cb = _FakeCallback("arp_sync")
    _run(arp_sync(cb))

    assert cb.answers[0][1] is True
    assert "3" in cb.answers[0][0]


def test_arp_sync_reports_failure(tmp_path, monkeypatch):
    _ready(tmp_path)
    _run(db.set_setting("auto_reject_sheet_tab", "🤖 Автоотказы"))

    async def fake_job():
        return -1
    monkeypatch.setattr(sched, "sync_auto_reject_sheet_job", fake_job)

    from handlers.admin_reject_reports import arp_sync
    cb = _FakeCallback("arp_sync")
    _run(arp_sync(cb))

    assert cb.answers[0][1] is True
    assert "не получилось" in cb.answers[0][0].lower()


def test_reports_button_present_on_rules_screen(tmp_path):
    _ready(tmp_path)
    from handlers.admin_reject_rules import render_rules_screen

    _text, kb = _run(render_rules_screen(ADMIN_ID))
    assert "admin_reject_reports" in _cbs(kb)


def test_reports_screen_registered_under_settings_capability():
    from handlers.admin_caps import ADMIN_CAPS
    assert ADMIN_CAPS["admin_reject_reports"] == "settings"
    assert ADMIN_CAPS["arp_*"] == "settings"
