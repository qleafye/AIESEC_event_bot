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
    scoped = [btn.callback_data for row in _run(b.bulk_buttons(("msk", ()))) for btn in row]
    assert "ambc_decl" not in scoped and "ambc_add" in scoped


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


# ── назначить любого делегата ────────────────────────────────────────────────────────────

class _Origin:
    def __init__(self, uid):
        self.sender_user = type("U", (), {"id": uid})() if uid is not None else None


def _person_msg(text=None, forward_from=None, hidden_forward=False):
    from tests.test_amb_candidates_34 import FakeMessage
    msg = FakeMessage(text)
    msg.forward_origin = None
    if forward_from is not None or hidden_forward:
        msg.forward_origin = _Origin(forward_from)
    return msg


def _appoint_state():
    from handlers.states import AmbAppoint
    from tests.test_amb_candidates_34 import _new_state
    state = _new_state()
    _run(state.set_state(AmbAppoint.waiting_for_person))
    return state


def test_list_has_appoint_and_archive_buttons(tmp_path):
    from handlers import admin_amb_candidates as h
    _ready(tmp_path)
    cb = _cb("admin_amb_candidates")
    _run(h.show_candidates(cb))
    buttons = _buttons(_screen(cb)[1])
    assert ("➕ Назначить амбассадором", "ambc_add") in buttons
    assert ("📥 Прошлые сезоны (CSV)", "ambc_arch_csv") in buttons


def test_appoint_start_prompt_and_state(tmp_path):
    from handlers import admin_amb_bulk as b
    from tests.test_amb_candidates_34 import _new_state
    _ready(tmp_path)
    state = _new_state()
    cb = _cb("ambc_add")
    _run(b.appoint_start(cb, state))
    text, kb = cb.message.answers[-1]
    assert ("Пришлите @ник, ссылку t.me, Telegram ID или перешлите сообщение делегата. "
            "«❌ Отмена» — выйти.") in text
    assert ("❌ Отмена", "ambc_add_cancel") in _buttons(kb)
    assert _run(state.get_state()) == "AmbAppoint:waiting_for_person"


def test_appoint_by_username_confirm_then_take(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, status="pending", name="Иван Петров", username="ivan_p")
    state = _appoint_state()
    for query in ("@ivan_p", "https://t.me/ivan_p", "10"):
        msg = _person_msg(query)
        _run(b.appoint_person_step(msg, state))
        text, kb = msg.answers[-1]
        assert "Сделать амбассадором:</b> Иван Петров · ivan_p · заявка: на рассмотрении?" in text
        assert "Ему придёт сообщение «🎉 Ты в команде амбассадоров!" in text
        assert "{link}" not in text
        assert ("✅ Назначить", "ambc_add_go:10") in _buttons(kb)
    bot = FakeBot()
    cb = _cb("ambc_add_go:10", bot)
    _run(b.appoint_go(cb, state))
    assert _amb_row(10)[0] == "active"
    assert cb.answers[-1] == ("Взят без пакета: заявка на форум ещё не одобрена.", True)
    assert len(bot.sent) == 1 and "https://t.me/test_amb_bot?start=amb_10" in bot.sent[0][1]
    assert _run(state.get_state()) is None
    assert any(d.startswith("ambc_rm:10") for _, d in _buttons(_screen(cb)[1]))
    # повтор — второе сообщение не уходит
    again = _cb("ambc_add_go:10", bot)
    _run(b.appoint_go(again, state))
    assert again.answers[-1] == ("Он уже в команде.", True)
    assert len(bot.sent) == 1


def test_appoint_by_forward(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, name="Мария")
    state = _appoint_state()
    msg = _person_msg(forward_from=10)
    _run(b.appoint_person_step(msg, state))
    assert "Сделать амбассадором:</b> Мария" in msg.answers[-1][0]
    hidden = _person_msg(hidden_forward=True)
    _run(b.appoint_person_step(hidden, state))
    assert "скрыт аккаунт при пересылке" in hidden.answers[-1][0]


def test_appoint_already_active_and_not_found(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, amb_status="active", username="ivan_p")
    state = _appoint_state()
    msg = _person_msg("@ivan_p")
    _run(b.appoint_person_step(msg, state))
    assert msg.answers[-1][0] == "Он уже в команде."
    missing = _person_msg("@nobody_here")
    _run(b.appoint_person_step(missing, state))
    assert missing.answers[-1][0] == (
        "Не нашёл такого делегата. Проверьте ник или перешлите его сообщение. "
        "Назначить можно только того, кто подал анкету."
    )
    # только нажал /start, анкету не подал — не находится
    _sql("INSERT INTO reg_started (telegram_id, username, started_at) VALUES (?, ?, ?)",
         (55, "started_only", "2026-09-01 00:00:00"))
    started = _person_msg("@started_only")
    _run(b.appoint_person_step(started, state))
    assert started.answers[-1][0].startswith("Не нашёл такого делегата.")
    assert _run(state.get_state()) == "AmbAppoint:waiting_for_person"


def test_appoint_several_matches_pick(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    for tid in (10, 11, 12):
        _seed(tid, name=f"Анна Смирнова {tid}")
    state = _appoint_state()
    msg = _person_msg("Смирнова")
    _run(b.appoint_person_step(msg, state))
    text, kb = msg.answers[-1]
    assert text.startswith("Нашлось несколько делегатов")
    picks = [d for _, d in _buttons(kb) if d.startswith("ambc_add_pick:")]
    assert sorted(picks) == ["ambc_add_pick:10", "ambc_add_pick:11", "ambc_add_pick:12"]
    cb = _cb("ambc_add_pick:11")
    _run(b.appoint_pick(cb))
    assert "Анна Смирнова 11" in _screen(cb)[0]
    assert ("✅ Назначить", "ambc_add_go:11") in _buttons(_screen(cb)[1])


def test_appoint_command_exits_fsm(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    state = _appoint_state()
    msg = _person_msg("/admin")
    _run(b.appoint_person_step(msg, state))
    assert msg.answers[-1][0] == "Отменено — никого не назначили."
    assert _run(state.get_state()) is None


def test_appoint_cancel_button(tmp_path):
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    state = _appoint_state()
    cb = _cb("ambc_add_cancel")
    _run(b.appoint_cancel(cb, state))
    assert cb.answers[-1][0] == "Отменено — никого не назначили."
    assert _run(state.get_state()) is None


def test_appoint_go_out_of_city_scope_refused(tmp_path, monkeypatch):
    from handlers import admin_core
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    _seed(10, city="spb")

    async def city_view(uid):
        return ("msk", ()), "Москва"

    monkeypatch.setattr(admin_core, "_admin_city_view", city_view)
    bot = FakeBot()
    cb = _cb("ambc_add_go:10", bot)
    _run(b.appoint_go(cb, _appoint_state()))
    assert cb.answers[-1][0].startswith("Не нашёл такого делегата.")
    assert _amb_row(10)[0] is None and not bot.sent


# ── новый сезон и архив ──────────────────────────────────────────────────────────────────

SEASON_OLD = "RT26"


def _season_wizard(tmp_path, n_amb=17):
    from handlers.states import SeasonReset
    from tests.test_amb_candidates_34 import _new_state
    _ready(tmp_path)
    for tid in range(100, 100 + n_amb):
        _seed(tid, amb_status="active" if tid % 2 else "candidate", slot=bool(tid % 2),
              username=f"amb_user{tid}")
    _seed(500)  # без статуса
    state = _new_state()
    _run(state.set_state(SeasonReset.naming))
    _run(state.update_data(season_old=SEASON_OLD))
    return state


def _msg(text):
    from tests.test_amb_candidates_34 import FakeMessage
    return FakeMessage(text)


def test_season_screen_states_amb_reset_count(tmp_path):
    from handlers import admin_cities
    state = _season_wizard(tmp_path)
    msg = _msg("RT27")
    _run(admin_cities.season_reset_name_step(msg, state))
    text = msg.answers[-1][0]
    assert ("Сбросятся статусы амбассадоров: 17 (история останется в выгрузке "
            "«Прошлые сезоны»).") in text


def test_season_screen_without_ambassadors_has_no_line(tmp_path):
    from handlers import admin_cities
    state = _season_wizard(tmp_path, n_amb=0)
    msg = _msg("RT27")
    _run(admin_cities.season_reset_name_step(msg, state))
    assert "амбассадоров" not in msg.answers[-1][0]


def test_season_reset_archives_and_clears(tmp_path):
    from handlers import admin_cities
    from handlers import admin_amb_bulk as b
    from handlers.states import SeasonReset
    state = _season_wizard(tmp_path)
    _run(state.set_state(SeasonReset.passphrase))
    _run(state.update_data(season_new="RT27", season_phrase=SEASON_OLD))
    msg = _msg(SEASON_OLD)
    _run(admin_cities.season_reset_passphrase_step(msg, state))
    assert "Статусы амбассадоров сброшены: 17." in msg.answers[-1][0]
    assert _run(db.get_setting("event_season")) == "RT27"
    assert _sql("SELECT COUNT(*) FROM users WHERE ambassador_status IS NOT NULL "
                "OR is_ambassador = 1 OR ambassador_slot_at IS NOT NULL")[0][0] == 0
    rows = _sql("SELECT season, COUNT(*) FROM ambassador_season_archive GROUP BY season")
    assert rows == [(SEASON_OLD, 17)]

    cb = _cb("ambc_arch_csv")
    _run(b.archive_csv(cb))
    document, caption = cb.message.documents[-1]
    assert caption == "Амбассадоры прошлых сезонов: 17 записей"
    raw = document.data
    assert raw.startswith("﻿".encode("utf-8"))
    body = raw.decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0] == "Сезон;Имя;username;Город;Статус;Вступил;Было место;Пакет выдан;Telegram ID"
    assert len(lines) == 18
    assert "@" not in body
    assert any(";в команде;" in ln and ";да;" in ln for ln in lines[1:])
    assert any(";кандидат;" in ln and ";нет;" in ln for ln in lines[1:])


def test_season_reset_survives_amb_failure(tmp_path, monkeypatch):
    from database import amb_status_db
    from handlers import admin_cities
    from handlers.states import SeasonReset
    state = _season_wizard(tmp_path, n_amb=2)

    async def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(amb_status_db, "archive_and_reset_season", boom)
    _run(state.set_state(SeasonReset.passphrase))
    _run(state.update_data(season_new="RT27", season_phrase=SEASON_OLD))
    msg = _msg(SEASON_OLD)
    _run(admin_cities.season_reset_passphrase_step(msg, state))
    assert _run(db.get_setting("event_season")) == "RT27"
    assert "Статусы амбассадоров сбросить не удалось" in msg.answers[-1][0]


def test_archive_csv_empty_and_formula_safe(tmp_path):
    from database import amb_status_db
    from handlers import admin_amb_bulk as b
    _ready(tmp_path)
    cb = _cb("ambc_arch_csv")
    _run(b.archive_csv(cb))
    assert cb.answers[-1] == ("Прошлых сезонов пока нет.", True)
    assert not cb.message.documents
    _seed(10, amb_status="active", name="=1+1", username="@evil")
    _run(amb_status_db.archive_and_reset_season("=HYPERLINK()", at="2026-09-30 10:00:00"))
    data, count = _run(b.export_archive_csv())
    body = data.decode("utf-8-sig")
    assert count == 1
    assert "'=1+1" in body and "'=HYPERLINK()" in body and "@evil" not in body
    assert ";evil;" in body


# ── права и порядок регистрации ──────────────────────────────────────────────────────────

def test_every_bulk_callback_resolves_to_moderate_game():
    from handlers.admin_caps import required_capability
    for data in ("ambc_decl", "ambc_decl_go:12", "ambc_decl_go:0", "ambc_decl_no", "ambc_add",
                 "ambc_add_pick:10", "ambc_add_go:10", "ambc_add_cancel", "ambc_arch_csv"):
        assert required_capability(callback_data=data) == "moderate_game", data
    assert required_capability(raw_state="AmbAppoint:waiting_for_person") == "moderate_game"


def test_bulk_seam_registered_after_candidates():
    import handlers.admin_onsite_reg  # noqa: F401
    from handlers.admin import router
    names = [h.callback.__name__ for h in router.callback_query.handlers]
    expected = ["decline_all_confirm", "decline_all_cancel", "decline_all_go", "appoint_start",
                "appoint_cancel", "appoint_pick", "appoint_go", "archive_csv"]
    start = names.index("show_form_card") + 1
    assert names[start:start + len(expected)] == expected
    messages = [h.callback.__name__ for h in router.message.handlers]
    assert "appoint_person_step" in messages
