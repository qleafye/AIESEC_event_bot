"""Phase 33 — «🔍 Сверить с БД», обработка сбоя (handlers/admin_sheet_reconcile.py).

Хендлеры дописать/выправить и экраны отчёта/подтверждения оборачиваются в try/except (тот же
посыл, что у соседнего `sync_sheet`, handlers/admin_sheets.py): исключение из apply_*/
build_report превращается в человеческое сообщение, а не падает молча — иначе менеджер видит
вечное «Дописываю...»/«Читаю таблицу...» и не понимает, что случилось.

Два уровня: (1) исключение УЛЕТЕЛО мимо apply_* (сам сервис его не поймал — здесь моделируется
монки-патчем самого apply_* на raising-функцию) — хендлер обязан показать «ничего не записано»
(ничего и правда не успело). (2) apply_* сам поймал сбой и вернул `crashed=True` вместе с уже
накопленным `done`/`total` (services/sheet_reconcile.py's собственный try/except, см.
tests/test_sheet_reconcile_260926.py) — хендлер обязан показать «N из M (что успели)», а не
общее «ничего не записано» (это было бы неправдой — часть уже записана)."""
from __future__ import annotations

import asyncio

from config import config
import handlers.admin_sheet_reconcile as admin_sheet_reconcile
from tests._dbtpl import fast_init_db

ADMIN_ID = 260926601


def _run(coro):
    return asyncio.run(coro)


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_sheet_reconcile_handlers_260926.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.edits = []  # (text, reply_markup)
        self.answers = []  # (text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, reply_markup))


class _FakeCallback:
    def __init__(self, uid=ADMIN_ID):
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_append_go_exception_from_apply_shows_message_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_apply(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "apply_append_missing", raising_apply)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_append_go(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits, "хендлер обязан отредактировать сообщение, а не упасть молча"
    text, _kb = cb.message.edits[-1]
    assert "❌" in text
    assert "ничего не записано" in text


def test_append_go_partial_crash_shows_done_of_total(tmp_path, monkeypatch):
    """apply_append_missing сам поймал сбой (например, Sheets отвалилась на третьей строке) и
    вернул crashed=True вместе с уже накопленным done/total -- хендлер обязан показать «N из
    M», а не общее «ничего не записано» (это неправда -- часть УЖЕ записана)."""
    _db_ready(tmp_path)

    async def crashed_apply(*, city_scope=None):
        return {
            "ok": False, "crashed": True, "error": "таблица не ответила",
            "done": 2, "failed": [], "total": 5,
        }

    monkeypatch.setattr(admin_sheet_reconcile, "apply_append_missing", crashed_apply)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_append_go(cb)
        return cb

    cb = _run(scenario())
    text, _kb = cb.message.edits[-1]
    assert "2 из 5" in text
    assert "что успели" in text


def test_status_go_exception_from_apply_shows_message_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_apply(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "apply_fix_statuses", raising_apply)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_status_go(cb)
        return cb

    cb = _run(scenario())
    text, _kb = cb.message.edits[-1]
    assert "❌" in text
    assert "ничего не записано" in text


def test_status_go_partial_crash_shows_done_of_total(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def crashed_apply(*, city_scope=None):
        return {
            "ok": False, "crashed": True, "error": "таблица не ответила",
            "done": 1, "failed": [], "total": 3,
        }

    monkeypatch.setattr(admin_sheet_reconcile, "apply_fix_statuses", crashed_apply)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_status_go(cb)
        return cb

    cb = _run(scenario())
    text, _kb = cb.message.edits[-1]
    assert "1 из 3" in text


def test_open_report_screen_exception_shows_message_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_build_report(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "build_report", raising_build_report)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_open(cb)
        return cb

    cb = _run(scenario())
    text, _kb = cb.message.edits[-1]
    assert "❌" in text
    assert "ничего не записано" in text


def test_append_confirm_exception_shows_alert_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_build_report(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "build_report", raising_build_report)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_append_confirm(cb)
        return cb

    cb = _run(scenario())
    assert cb.answers, "хендлер обязан ответить алертом, а не упасть молча"
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert "ничего не записано" in text


def test_status_confirm_exception_shows_alert_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_build_report(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "build_report", raising_build_report)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_status_confirm(cb)
        return cb

    cb = _run(scenario())
    assert cb.answers
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert "ничего не записано" in text


def test_csv_exception_shows_alert_not_crash(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def raising_build_report(*, city_scope=None):
        raise RuntimeError("Sheets API упала (тест)")

    monkeypatch.setattr(admin_sheet_reconcile, "build_report", raising_build_report)

    async def scenario():
        cb = _FakeCallback()
        await admin_sheet_reconcile.sheet_reconcile_csv(cb)
        return cb

    cb = _run(scenario())
    assert cb.answers
    text, show_alert = cb.answers[-1]
    assert show_alert is True
    assert "ничего не записано" in text
