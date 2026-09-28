"""Кнопка «🔄 Сверить состав чата» (29.09).

- `chat_tracking.reconcile_all_now` сверяет ВСЕ привязанные чаты и при выключенном тумблере
  учёта (явное действие менеджера), отчёт по каждому чату с названием и городом;
- нет привязанных чатов -> пустой отчёт, ни одного get_chat_member; хендлер отвечает понятным
  текстом «Чат делегатов не подключён…»;
- сбой get_chat_member у части делегатов -> прогон доходит до конца, ошибки в отчёте;
- повторное нажатие во время сверки -> «Сверка уже идёт», второго прогона нет;
- после сверки в sheet_chat_queue лежат события всех одобренных города;
- кнопка стоит в разделе «🔧 Управление» и требует право settings."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from database import db
from handlers import admin_chat_cleanup
from handlers.admin_caps import required_capability
from handlers.admin_sections import section_rows
from services import chat_tracking
from tests.test_chat_refresh_admin_260914 import ADMIN_ID, FakeBot, _ready, _seed_approved

CHAT_ID = -1004489658418


def _run(coro):
    return asyncio.run(coro)


class _Bot(FakeBot):
    id = 1

    async def get_chat_administrators(self, chat_id):
        return []


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(chat_tracking, "REFRESH_PAUSE_SECONDS", 0)
    monkeypatch.setattr(chat_tracking, "_reconcile_running", False)


def test_reconcile_runs_with_tracking_off_and_reports(tmp_path):
    _ready(tmp_path)
    bot = _Bot(fail_ids={800002}, status_for={800001: "left"},
               error_text_for={800003: "Bad Request: PARTICIPANT_ID_INVALID"})

    async def go():
        await db.set_setting("chat_tracking_enabled", "off")
        await chat_tracking.bind_chat(None, CHAT_ID, "Форум «Юлид»", None)
        ids = await _seed_approved(5)
        reports = await chat_tracking.reconcile_all_now(bot)
        queued = {r["telegram_id"] for r in await db.list_due_sheet_chat("9999-12-31 00:00:00", 10_000)}
        return ids, reports, queued

    ids, reports, queued = _run(go())
    assert len(bot.calls) == 5
    assert len(reports) == 1
    rep = reports[0]
    assert rep["title"] == "Форум «Юлид»" and rep["city"] is None and rep["chat_id"] == CHAT_ID
    assert rep["in_chat"] == 2          # 800000, 800004
    assert rep["not_in_chat"] == 2      # 800001 left, 800003 не найден -> left
    assert rep["not_found"] == 1
    assert rep["errors"] == 1           # 800002
    assert queued == set(ids)


def test_reconcile_without_chats_makes_no_calls(tmp_path):
    _ready(tmp_path)
    bot = _Bot()

    async def go():
        await _seed_approved(3)
        return await chat_tracking.reconcile_all_now(bot)

    assert _run(go()) == []
    assert bot.calls == []


def test_reconcile_second_run_is_refused_while_running(tmp_path):
    _ready(tmp_path)
    assert chat_tracking.claim_reconcile() is True
    assert chat_tracking.claim_reconcile() is False
    bot = _Bot()

    async def go():
        await chat_tracking.bind_chat(None, CHAT_ID, "Чат", None)
        await _seed_approved(2)
        return await chat_tracking.reconcile_all_now(bot)

    assert _run(go()) is None
    assert bot.calls == []
    chat_tracking.release_reconcile()
    assert chat_tracking.claim_reconcile() is True
    chat_tracking.release_reconcile()


def test_report_text_human():
    text = chat_tracking.reconcile_report_text([
        {"title": "Форум", "city_label": "Тюмень", "in_chat": 135, "not_in_chat": 60,
         "not_found": 12, "errors": 3, "truncated": False},
    ])
    assert "Чат «Форум» (Тюмень): в чате — 135, не в чате — 60, аккаунт не найден — 12, " \
           "не удалось проверить — 3." in text
    assert "Колонка «В чате» в таблице обновится" in text
    assert "Нажмите кнопку ещё раз позже" in text


# ── хендлер ───────────────────────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _Cb:
    def __init__(self):
        self.from_user = SimpleNamespace(id=ADMIN_ID)
        self.message = _Msg()
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def test_handler_no_chats(tmp_path, monkeypatch):
    _ready(tmp_path)
    cb = _Cb()
    spawned = []
    monkeypatch.setattr(admin_chat_cleanup, "spawn", lambda coro: spawned.append(coro) or coro.close())
    _run(admin_chat_cleanup.chat_reconcile_now(cb, _Bot()))
    assert spawned == []
    assert any("Чат делегатов не подключён" in (a or "") for a in cb.alerts + cb.message.answers)


def test_handler_starts_background_and_refuses_second(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _Bot()
    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(admin_chat_cleanup, "spawn", fake_spawn)

    async def go():
        await chat_tracking.bind_chat(None, CHAT_ID, "Чат", None)
        cb1, cb2 = _Cb(), _Cb()
        await admin_chat_cleanup.chat_reconcile_now(cb1, bot)
        await admin_chat_cleanup.chat_reconcile_now(cb2, bot)
        return cb1, cb2

    cb1, cb2 = _run(go())
    assert len(spawned) == 1
    assert any("Сверяю состав чата" in t for t in cb1.message.answers)
    assert any("Сверка уже идёт" in (a or "") for a in cb2.alerts)
    chat_tracking.release_reconcile()


def test_background_sends_report_to_manager(tmp_path):
    _ready(tmp_path)
    bot = _Bot()

    async def go():
        await chat_tracking.bind_chat(None, CHAT_ID, "Чат", None)
        await _seed_approved(2)
        assert chat_tracking.claim_reconcile()
        await admin_chat_cleanup._reconcile_and_report(bot, ADMIN_ID)

    _run(go())
    assert bot.sent and bot.sent[-1][0] == ADMIN_ID
    assert "Чат «Чат»: в чате — 2" in bot.sent[-1][1]
    assert chat_tracking.claim_reconcile() is True  # флаг снят после прогона
    chat_tracking.release_reconcile()


def test_button_in_manage_section_with_settings_cap():
    rows = section_rows("manage")
    assert ("screen", "admin_chat_reconcile", "🔄 Сверить состав чата") in rows
    assert required_capability(callback_data="admin_chat_reconcile") == "settings"
