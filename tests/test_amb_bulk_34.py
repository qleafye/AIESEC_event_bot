"""Раздел «🤝 Амбассадоры» → массовые действия (handlers/admin_amb_bulk.py).

- «🙅 Вежливо отказать всем оставшимся»: подтверждение с числом и текстом реестра, сверка
  числа, фоновая рассылка через тихие часы с отметкой до отправки, отчёт менеджеру;
- «➕ Назначить амбассадором» любого делегата с анкетой;
- «🔄 Новый сезон» сбрасывает статусы амбассадоров, прошлый сезон — в архивной выгрузке.

pytest-asyncio нет — async через `asyncio.run()`; фоновая задача рассылки дожидается внутри
того же цикла (`_go_and_wait`).
"""
from __future__ import annotations

import asyncio

from database import db
from services import background
from settings_schema import SETTINGS_SCHEMA
from tests.test_amb_candidates_34 import (
    ADMIN_ID,
    FakeBot,
    _amb_row,
    _buttons,
    _cb,
    _ready,
    _run,
    _screen,
    _seed,
    _sql,
)

DECLINE_DEFAULT = SETTINGS_SCHEMA["amb_decline_all_text"]["default"]


def _fast(monkeypatch):
    from handlers import admin_amb_bulk
    monkeypatch.setattr(admin_amb_bulk, "_PAUSE", 0)


async def _drain():
    while background.pending_count():
        await asyncio.gather(*[t for t in list(background._background_tasks) if not t.done()])


def _go_and_wait(cb):
    from handlers import admin_amb_bulk as b

    async def run():
        await b.decline_all_go(cb)
        await _drain()
    _run(run())


def _admin_messages(bot):
    return [text for chat, text in bot.sent if chat == ADMIN_ID]


def _delegate_messages(bot):
    return [(chat, text) for chat, text in bot.sent if chat != ADMIN_ID]


# ── вежливый отказ всем оставшимся ───────────────────────────────────────────────────────

def test_decline_no_candidates_alerts(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    cb = _cb("ambc_decl")
    _run(b.decline_all_confirm(cb))
    assert cb.answers[-1] == ("Кандидатов нет — отказывать некому.", True)


def test_list_shows_decline_button_only_with_candidates(tmp_path):
    from handlers import admin_amb_candidates as h
    _ready(tmp_path)
    cb = _cb("ambc:candidates:0")
    _run(h.candidates_page(cb))
    assert not any(d == "ambc_decl" for _, d in _buttons(_screen(cb)[1]))
    for tid in range(10, 13):
        _seed(tid, amb_status="candidate")
    cb2 = _cb("ambc:team:0")
    _run(h.candidates_page(cb2))
    assert ("🙅 Вежливо отказать всем оставшимся (3)", "ambc_decl") in _buttons(_screen(cb2)[1])


def test_decline_confirm_states_count_text_and_consequences(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    for tid in range(100, 112):
        _seed(tid, amb_status="candidate", reserve=tid % 2 == 0)
    cb = _cb("ambc_decl")
    _run(b.decline_all_confirm(cb))
    text, kb = _screen(cb)
    assert "Уйдёт 12 кандидатам (включая отложенных «в запасе»)." in text
    assert f"Текст: «{DECLINE_DEFAULT}»" in text
    assert "После этого кнопки «Хочу стать амбассадором» у них не будет в этом сезоне." in text
    assert "«🙋 Кандидаты и команда» → «Отказано» → «Взять»" in text
    buttons = _buttons(kb)
    assert ("✅ Отправить 12", "ambc_decl_go:12") in buttons
    assert ("✏️ Изменить текст", "settings_edit:amb_decline_all_text") in buttons
    assert ("❌ Отмена", "ambc_decl_no") in buttons
    # ничего не изменилось до подтверждения
    assert _sql("SELECT COUNT(*) FROM users WHERE ambassador_status = 'candidate'")[0][0] == 12


def test_decline_confirm_hides_edit_without_settings_right(tmp_path, monkeypatch):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, amb_status="candidate")

    async def no_settings(uid, cap):
        return cap != "settings"

    monkeypatch.setattr(b, "has_capability", no_settings)
    cb = _cb("ambc_decl")
    _run(b.decline_all_confirm(cb))
    assert not any(d.startswith("settings_edit:") for _, d in _buttons(_screen(cb)[1]))


def test_decline_text_escaped_html(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    _run(db.set_setting("amb_decline_all_text", "Спасибо <3 & до встречи"))
    cb = _cb("ambc_decl")
    _run(b.decline_all_confirm(cb))
    assert "Спасибо &lt;3 &amp; до встречи" in _screen(cb)[0]


def test_decline_go_sends_to_all_and_reports(tmp_path, monkeypatch):
    _fast(monkeypatch)
    _ready(tmp_path)
    for tid in range(100, 112):
        _seed(tid, amb_status="candidate")
    _seed(200, amb_status="active", slot=True)
    bot = FakeBot()
    cb = _cb("ambc_decl_go:12", bot)
    _go_and_wait(cb)
    assert _sql("SELECT COUNT(*) FROM users WHERE ambassador_status = 'declined'")[0][0] == 12
    assert _amb_row(200)[0] == "active"
    delegates = _delegate_messages(bot)
    assert sorted(c for c, _ in delegates) == list(range(100, 112))
    assert all(t == DECLINE_DEFAULT for _, t in delegates)
    assert _admin_messages(bot) == [
        "🙅 Отказ кандидатам. Готово: отправлено 12, отложено до утра 0, не доставлено 0."
    ]
    assert _sql("SELECT COUNT(*) FROM users WHERE ambassador_declined_notified_at IS NOT NULL")[0][0] == 12
    assert "Отказано кандидатам: 12" in _screen(cb)[0]


def test_decline_go_stale_count_sends_nothing(tmp_path, monkeypatch):
    _fast(monkeypatch)
    _ready(tmp_path)
    for tid in range(100, 113):
        _seed(tid, amb_status="candidate")
    bot = FakeBot()
    cb = _cb("ambc_decl_go:12", bot)
    _go_and_wait(cb)
    assert not bot.sent
    assert _sql("SELECT COUNT(*) FROM users WHERE ambassador_status = 'candidate'")[0][0] == 13
    text, kb = _screen(cb)
    assert "Число кандидатов изменилось: теперь 13. Проверьте и подтвердите ещё раз." in text
    assert ("✅ Отправить 13", "ambc_decl_go:13") in _buttons(kb)


def test_decline_repeat_after_crash_sends_only_tail(tmp_path, monkeypatch):
    from database import amb_status_db
    from handlers import admin_amb_bulk as b
    _fast(monkeypatch)
    _ready(tmp_path)
    for tid in range(100, 105):
        _seed(tid, amb_status="candidate")
    # «сбой на середине»: всех отказали, двоим письмо уже ушло
    _run(amb_status_db.decline_remaining(at="2026-09-30 10:00:00", by=ADMIN_ID))
    for tid in (100, 101):
        assert _run(amb_status_db.claim_decline_notice(tid, at="2026-09-30 10:00:01"))
    # список предлагает дослать
    cb_confirm = _cb("ambc_decl")
    _run(b.decline_all_confirm(cb_confirm))
    text, kb = _screen(cb_confirm)
    assert "3 отказанным сообщение ещё не ушло" in text
    assert ("✅ Дослать 3", "ambc_decl_go:0") in _buttons(kb)

    bot = FakeBot()
    _go_and_wait(_cb("ambc_decl_go:0", bot))
    assert sorted(c for c, _ in _delegate_messages(bot)) == [102, 103, 104]
    assert "отправлено 3" in _admin_messages(bot)[0]

    # третий раз — некому
    bot2 = FakeBot()
    cb3 = _cb("ambc_decl_go:0", bot2)
    _go_and_wait(cb3)
    assert not bot2.sent
    assert cb3.answers[-1] == ("Кандидатов нет — отказывать некому.", True)


def test_decline_failure_counted_and_does_not_stop(tmp_path, monkeypatch):
    _fast(monkeypatch)
    _ready(tmp_path)
    for tid in (100, 101, 102):
        _seed(tid, amb_status="candidate")

    class BlockingBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None, **kw):
            if chat_id == 101:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            await super().send_message(chat_id, text, parse_mode=parse_mode, **kw)

    bot = BlockingBot()
    _go_and_wait(_cb("ambc_decl_go:3", bot))
    assert sorted(c for c, _ in _delegate_messages(bot)) == [100, 102]
    report = _admin_messages(bot)[0]
    assert "Готово: отправлено 2, отложено до утра 0, не доставлено 1." in report
    assert "заблокировали бота" in report


def test_decline_quiet_hours_counted_as_deferred(tmp_path, monkeypatch):
    from services import quiet_hours
    _fast(monkeypatch)
    _ready(tmp_path)
    for tid in (100, 101):
        _seed(tid, amb_status="candidate")

    async def fake_queue(now, user_id, text, *, sender, **kw):
        return False

    monkeypatch.setattr(quiet_hours, "send_or_queue_text", fake_queue)
    bot = FakeBot()
    _go_and_wait(_cb("ambc_decl_go:2", bot))
    assert not _delegate_messages(bot)
    assert "отправлено 0, отложено до утра 2, не доставлено 0" in _admin_messages(bot)[0]


def test_decline_city_scoped_admin_refused(tmp_path, monkeypatch):
    from handlers import admin_core
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, amb_status="candidate")

    async def city_view(uid):
        return ("msk", ()), "Москва"

    monkeypatch.setattr(admin_core, "_admin_city_view", city_view)
    for handler, data in ((b.decline_all_confirm, "ambc_decl"), (b.decline_all_go, "ambc_decl_go:1")):
        cb = _cb(data)
        _run(handler(cb))
        alert, show = cb.answers[-1]
        assert show and "всех городов" in alert
    assert _amb_row(10)[0] == "candidate"
    assert _run(b.bulk_buttons(("msk", ()))) == []


def test_declined_cannot_rejoin_but_manager_can_take(tmp_path, monkeypatch):
    from handlers import admin_amb_candidates as h
    from services import amb_status
    _fast(monkeypatch)
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    _go_and_wait(_cb("ambc_decl_go:1", FakeBot()))
    assert _run(amb_status.request_join(10, source="button_bot")).outcome == "declined"
    assert _run(amb_status.offer_open(10)) is False
    cb = _cb("ambc:declined:0")
    _run(h.candidates_page(cb))
    assert any(d.startswith("ambp:10:") for _, d in _buttons(_screen(cb)[1]))
    bot = FakeBot()
    _run(h.take_person(_cb("ambc_take:10:declined:0", bot)))
    assert _amb_row(10)[0] == "active" and len(bot.sent) == 1


def test_decline_cancel_returns_to_list(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, amb_status="candidate")
    cb = _cb("ambc_decl_no")
    _run(b.decline_all_cancel(cb))
    assert "Кандидаты и команда" in _screen(cb)[0]
    assert cb.answers[-1][0] == "Отменено — никому ничего не ушло."
    assert _amb_row(10)[0] == "candidate"
