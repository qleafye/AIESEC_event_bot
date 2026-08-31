"""Опросы (native Telegram polls, 260822): БД и итоги, доставка с чекпоинтом, закрытие
fail-soft, делегатские хендлеры poll_answer/poll, allowed_updates, мастер (happy path +
лимиты), права, выгрузка во вкладку «Опросы», ре-арм отложенного опроса после рестарта.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from services import polls as polls_svc
from services import scheduler as sched
from handlers import admin as admin_mod
from handlers import admin_core
from handlers import admin_poll_wizard as wiz
from handlers import polls as polls_handlers
from handlers.admin_caps import required_capability, ADMIN_CAPS
from tests.test_roles_phase8 import FakeUser, FakeMessage, _flat_callback_data

ADMIN_ID = 900901
STRANGER_ID = 900904


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "polls.db")
    config.ADMIN_IDS = [ADMIN_ID]
    asyncio.run(db.init_db())


async def _add_user(tid, name="Иван", username="@ivan", status="approved", city=None, track="full"):
    async with db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, event_city, participant_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, name, username, status, city, track),
        )
        await conn.commit()


class FakeBot:
    """send_poll возвращает объект с .poll.id и .message_id; stop_poll падает на fail_ids."""

    def __init__(self, fail_send=(), fail_stop=()):
        self.sent_polls = []
        self.sent_messages = []
        self.stopped = []
        self.fail_send = set(fail_send)
        self.fail_stop = set(fail_stop)
        self._n = 0

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent_messages.append((chat_id, text))

    async def send_poll(self, chat_id, question, options, is_anonymous=False,
                        allows_multiple_answers=False, is_closed=False, **kw):
        if chat_id in self.fail_send:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        self._n += 1
        self.sent_polls.append(dict(chat_id=chat_id, question=question, options=list(options),
                                    is_anonymous=is_anonymous, multi=allows_multiple_answers,
                                    is_closed=is_closed))
        return SimpleNamespace(message_id=1000 + self._n, poll=SimpleNamespace(id=f"tg{chat_id}"))

    async def stop_poll(self, chat_id, message_id):
        if chat_id in self.fail_stop:
            raise RuntimeError("chat not found")
        self.stopped.append((chat_id, message_id))


def _mk_poll(**kw):
    base = dict(question="Q?", options=["A", "B", "C"], is_anonymous=False, allows_multiple=False,
                created_by=ADMIN_ID, city=None, audience=[], scheduled_at="2026-01-01 10:00:00")
    base.update(kw)
    return db.create_poll(base.pop("question"), base.pop("options"), **base)


# ── БД + итоги ───────────────────────────────────────────────────────────────────────────────

def test_create_get_list_roundtrip(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll(audience=[{"field": "status", "value": "approved"}], city="spb")
        poll = await db.get_poll(pid)
        assert poll["options"] == ["A", "B", "C"]
        assert poll["audience"] == [{"field": "status", "value": "approved"}]
        assert poll["status"] == "scheduled" and poll["city"] == "spb"
        assert [p["id"] for p in await db.list_polls(statuses=("scheduled",))] == [pid]
        assert await db.list_polls(statuses=("open",)) == []
        # Городской скоуп: опрос города spb виден в spb и не виден в msk; общий (city NULL) — везде.
        pid_all = await _mk_poll()
        assert {p["id"] for p in await db.list_polls(city_scope=("spb", ()))} == {pid, pid_all}
        assert {p["id"] for p in await db.list_polls(city_scope=("msk", ()))} == {pid_all}
        await db.delete_poll(pid)
        assert await db.get_poll(pid) is None

    asyncio.run(go())


def test_answer_upsert_overwrite_and_retract(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll()
        await db.upsert_poll_answer(pid, 1, [0])
        await db.upsert_poll_answer(pid, 1, [2, 1])  # переголосовал — перезапись, не дубль
        answers = await db.list_poll_answers(pid)
        assert len(answers) == 1 and answers[0]["option_ids"] == [1, 2]
        await db.upsert_poll_answer(pid, 1, [])  # отозвал голос
        assert await db.list_poll_answers(pid) == []
        assert await db.count_poll_respondents(pid) == 0

    asyncio.run(go())


def test_results_non_anonymous_counts_from_answers(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1, "Аня", "@anya", city="spb")
        await _add_user(2, "Боря", "@borya")
        pid = await _mk_poll()
        await db.record_poll_message(pid, 1, "tg1", 11, True)
        await db.record_poll_message(pid, 2, "tg2", 12, True)
        await db.record_poll_message(pid, 3, None, None, False)
        await db.upsert_poll_answer(pid, 1, [0, 2])
        await db.upsert_poll_answer(pid, 2, [0])
        r = await db.get_poll_results(pid)
        assert r["counts"] == [2, 0, 1]
        assert r["respondents"] == 2 and r["delivered"] == 2 and r["failed"] == 1
        assert r["source"] == "answers"
        assert await db.get_poll_results(999) is None
        text = polls_svc.render_results_text(r)
        assert "█" in text and "— A" in text and "Ответили: <b>2</b> из 2" in text
        assert "не доставлено: 1" in text

    asyncio.run(go())


def test_results_anonymous_sum_totals_across_messages(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll(is_anonymous=True)
        await db.record_poll_message(pid, 1, "tg1", 11, True)
        await db.record_poll_message(pid, 2, "tg2", 12, True)
        assert await db.set_poll_message_totals("tg1", {"total": 1, "options": [1, 0, 0]})
        assert await db.set_poll_message_totals("tg2", {"total": 1, "options": [0, 0, 1]})
        assert not await db.set_poll_message_totals("foreign", {"total": 5, "options": [5]})
        r = await db.get_poll_results(pid)
        assert r["counts"] == [1, 0, 1] and r["respondents"] == 2 and r["source"] == "totals"
        assert "анонимный" in polls_svc.render_results_text(r)

    asyncio.run(go())


# ── доставка: клейм, чекпоинт, дошлёт ────────────────────────────────────────────────────────

def test_deliver_poll_checkpoints_and_resumes_after_crash(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        for i in range(1, 6):
            await _add_user(i)
        pid = await _mk_poll()
        bot1 = FakeBot()
        real = db.record_poll_message
        calls = {"n": 0}

        async def record_then_crash(*a, **kw):
            await real(*a, **kw)
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("process died")
        monkeypatch.setattr(polls_svc, "record_poll_message", record_then_crash)
        assert await polls_svc.deliver_poll(bot1, pid) is None
        assert [p["chat_id"] for p in bot1.sent_polls] == [1, 2]
        assert (await db.get_poll(pid))["status"] == "sending"  # застрял — реклейм на буте
        monkeypatch.setattr(polls_svc, "record_poll_message", real)

        # Свежий 'sending' не реклеймится; состаренный — да.
        assert await db.reclaim_stale_sending_polls(10) == []
        async with db._connect() as conn:
            await conn.execute("UPDATE polls SET sending_since = '2000-01-01 00:00:00' WHERE id = ?", (pid,))
            await conn.commit()
        assert await db.reclaim_stale_sending_polls(10) == [pid]

        bot2 = FakeBot()
        stats = await polls_svc.deliver_poll(bot2, pid)
        assert [p["chat_id"] for p in bot2.sent_polls] == [3, 4, 5]
        assert stats == {"sent": 3, "failed": 0, "skipped": 2, "total": 5}
        assert (await db.get_poll(pid))["status"] == "open"
        assert await db.get_poll_id_by_telegram_poll("tg4") == pid
        # Повторный вызов — клейм не проходит, ничего не шлётся.
        bot3 = FakeBot()
        assert await polls_svc.deliver_poll(bot3, pid) is None and bot3.sent_polls == []

    asyncio.run(go())


def test_deliver_records_failed_and_sends_intro_and_audience_filter(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def go():
        await _add_user(1, status="approved")
        await _add_user(2, status="pending")
        await _add_user(3, status="approved")
        await db.set_setting("poll_intro_text", "Привет, ответь 👇")
        pid = await _mk_poll(audience=[{"field": "status", "value": "approved"}], is_anonymous=True,
                             allows_multiple=True)
        bot = FakeBot(fail_send={3})
        stats = await polls_svc.deliver_poll(bot, pid)
        assert stats == {"sent": 1, "failed": 1, "skipped": 0, "total": 2}
        assert [c for c, _ in bot.sent_messages] == [1, 3]  # вступление ушло обоим
        assert bot.sent_polls[0]["is_anonymous"] is True and bot.sent_polls[0]["multi"] is True
        assert await db.count_poll_deliveries(pid) == (1, 1)
        assert await db.list_poll_sent_chat_ids(pid) == {1, 3}

    asyncio.run(go())


def test_close_poll_is_fail_soft_per_chat(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll()
        await db.set_poll_status(pid, "open")
        await db.record_poll_message(pid, 1, "tg1", 11, True)
        await db.record_poll_message(pid, 2, "tg2", 12, True)
        await db.record_poll_message(pid, 3, None, None, False)  # не доставлен — stop_poll не зовём
        bot = FakeBot(fail_stop={2})
        assert await polls_svc.close_poll(bot, pid) == (1, 1)
        assert bot.stopped == [(1, 11)]
        poll = await db.get_poll(pid)
        assert poll["status"] == "closed" and poll["closed_at"]

    asyncio.run(go())


# ── делегатская сторона: poll_answer / poll, allowed_updates ─────────────────────────────────

def test_poll_answer_handler_maps_and_retracts(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll()
        await db.record_poll_message(pid, 7, "tg7", 1, True)
        answer = SimpleNamespace(poll_id="tg7", user=SimpleNamespace(id=7), option_ids=[1])
        await polls_handlers.on_poll_answer(answer)
        assert (await db.list_poll_answers(pid))[0]["option_ids"] == [1]
        await polls_handlers.on_poll_answer(SimpleNamespace(poll_id="tg7", user=SimpleNamespace(id=7), option_ids=[]))
        assert await db.list_poll_answers(pid) == []
        # Чужой опрос — молча мимо.
        await polls_handlers.on_poll_answer(SimpleNamespace(poll_id="nope", user=SimpleNamespace(id=7), option_ids=[0]))
        assert await db.count_poll_respondents(pid) == 0

    asyncio.run(go())


def test_poll_update_handler_stores_totals(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await _mk_poll(is_anonymous=True)
        await db.record_poll_message(pid, 7, "tg7", 1, True)
        poll = SimpleNamespace(id="tg7", total_voter_count=1,
                               options=[SimpleNamespace(voter_count=0), SimpleNamespace(voter_count=1),
                                        SimpleNamespace(voter_count=0)])
        await polls_handlers.on_poll_update(poll)
        assert (await db.get_poll_results(pid))["counts"] == [0, 1, 0]

    asyncio.run(go())


def test_dispatcher_requests_poll_updates_only_with_polls_router():
    """aiogram собирает allowed_updates из observer'ов — без polls.router Telegram не прислал бы
    poll_answer/poll вовсе (main.py подключает его последним)."""
    from aiogram import Router
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(Router())
    assert "poll_answer" not in dp.resolve_used_update_types()
    dp2 = Dispatcher(storage=MemoryStorage())
    dp2.include_router(polls_handlers.router)
    used = dp2.resolve_used_update_types()
    assert "poll_answer" in used and "poll" in used


# ── права и меню ─────────────────────────────────────────────────────────────────────────────

def test_poll_callbacks_require_broadcast_capability_and_menu_row_exists(tmp_path):
    _ready(tmp_path)
    for key in ("admin_polls", "admin_polls_closed", "poll_new", "poll_opts_done", "poll_tg_anon",
                "poll_settings_next", "poll_aud:city:spb", "poll_send_now", "poll_schedule",
                "poll_cancel", "poll_card:1", "poll_close:1", "poll_export:1", "poll_del:1",
                "poll_del_go:1"):
        assert required_capability(callback_data=key) == "broadcast", key
    assert required_capability(raw_state="PollCreate:question") == "broadcast"
    assert ADMIN_CAPS["state:PollCreate:*"] == "broadcast"
    assert ("📊 Опросы", "admin_polls") in admin_core._ADMIN_MENU_ROWS
    rows = [cb for _, cb in admin_core._ADMIN_MENU_ROWS]
    assert rows.index("admin_polls") == rows.index("admin_broadcast") + 1
    # Phase 20 (20-03): на корне теперь разделы — «📊 Опросы» приходят на экране «📢 Общение»
    # (та же аудитория и то же право, что у рассылки, поэтому и раздел один).
    from handlers.admin_sections import build_section_keyboard

    root = _flat_callback_data(asyncio.run(admin_core.build_admin_keyboard(ADMIN_ID)))
    assert "admin_sec:comms" in root
    comms = _flat_callback_data(asyncio.run(build_section_keyboard("comms", ADMIN_ID)))
    assert "admin_polls" in comms


# ── мастер ───────────────────────────────────────────────────────────────────────────────────

def _state(user_id):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cb(data, state, bot, user_id=ADMIN_ID):
    raw = asyncio.run(state.get_state())
    event = FakeCallback(data, user_id)
    kwargs = dict(event_from_user=FakeUser(user_id), bot=bot, raw_state=raw, state=state, event_update=None)
    result = asyncio.run(admin_mod.router.propagate_event("callback_query", event, **kwargs))
    return result, event


def _msg(text, state, bot, user_id=ADMIN_ID):
    raw = asyncio.run(state.get_state())
    event = FakeMessage(text=text, user_id=user_id, chat_id=user_id)
    kwargs = dict(event_from_user=FakeUser(user_id), bot=bot, raw_state=raw, state=state, event_update=None)
    result = asyncio.run(admin_mod.router.propagate_event("message", event, **kwargs))
    return result, event


def test_split_options_accepts_lines_and_semicolons():
    assert wiz.split_options("10:00; 11:00 ;\n12:00\n\n") == ["10:00", "11:00", "12:00"]
    assert wiz.split_options("  ;  ") == []


def test_wizard_happy_path_send_now(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(_add_user(1))
    asyncio.run(_add_user(2))
    spawned = []
    monkeypatch.setattr(wiz, "_spawn", lambda coro: spawned.append(coro))
    state, bot = _state(ADMIN_ID), FakeBot()

    result, ev = _cb("admin_polls", state, bot)
    assert result is not UNHANDLED
    assert "poll_new" in _flat_callback_data(ev.message.markup)

    _cb("poll_new", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:question"
    _msg("Во сколько начинаем?", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:options"
    _msg("10:00; 11:00", state, bot)
    _msg("12:00", state, bot)
    assert asyncio.run(state.get_data())["options"] == ["10:00", "11:00", "12:00"]

    _cb("poll_opts_done", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:settings"
    _, ev = _cb("poll_tg_multi", state, bot)
    assert asyncio.run(state.get_data())["multi"] is True
    assert "⚠️" not in ev.message.text
    _, ev = _cb("poll_tg_anon", state, bot)
    assert "ответы по людям будут недоступны" in ev.message.text  # анонимный — честно предупреждаем
    _cb("poll_tg_anon", state, bot)  # назад в неанонимный

    _, ev = _cb("poll_settings_next", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:audience"
    assert "poll_aud:approved" in _flat_callback_data(ev.message.markup)

    _, ev = _cb("poll_aud:approved", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:confirm"
    assert bot.sent_polls[-1]["is_closed"] is True and bot.sent_polls[-1]["chat_id"] == ADMIN_ID
    assert bot.sent_polls[-1]["multi"] is True and bot.sent_polls[-1]["is_anonymous"] is False
    preview_text = ev.message.answers[-1][0]
    assert "только одобренные" in preview_text and "2 чел." in preview_text

    _cb("poll_send_now", state, bot)
    assert asyncio.run(state.get_state()) is None
    polls = asyncio.run(db.list_polls())
    assert len(polls) == 1 and polls[0]["audience"] == [{"field": "status", "value": "approved"}]
    assert polls[0]["allows_multiple"] == 1 and polls[0]["is_anonymous"] == 0
    assert len(spawned) == 1
    asyncio.run(spawned[0])  # доставка: 2 делегата + отчёт менеджеру
    delivered = [p["chat_id"] for p in bot.sent_polls if not p["is_closed"]]
    assert delivered == [1, 2]
    assert (asyncio.run(db.get_poll(polls[0]["id"])))["status"] == "open"
    assert any("Доставлено: 2" in t for c, t in bot.sent_messages if c == ADMIN_ID)


def test_wizard_validation_limits(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    _cb("poll_new", state, bot)
    _, ev = _msg("x" * 301, state, bot)
    assert "до 300" in ev.answers[-1][0]
    assert asyncio.run(state.get_state()) == "PollCreate:question"
    _msg("Q?", state, bot)
    _, ev = _msg("y" * 101, state, bot)
    assert "длиннее 100" in ev.answers[-1][0]
    _msg("A", state, bot)
    _, ev = _cb("poll_opts_done", state, bot)
    assert ev.answers[-1][1] is True and "хотя бы 2" in ev.answers[-1][0]
    assert asyncio.run(state.get_state()) == "PollCreate:options"
    _msg("; ".join(str(i) for i in range(9)), state, bot)  # итого 10 — потолок
    _, ev = _msg("ещё", state, bot)
    assert "максимум 10" in ev.answers[-1][0]
    assert len(asyncio.run(state.get_data())["options"]) == 10
    _, ev = _msg("Отмена", state, bot)
    assert asyncio.run(state.get_state()) is None


def test_wizard_schedule_path_creates_job(tmp_path, monkeypatch):
    _ready(tmp_path)
    jobs = []
    monkeypatch.setattr(wiz, "schedule_poll_job", lambda pid, when: jobs.append((pid, when)))
    state, bot = _state(ADMIN_ID), FakeBot()
    _cb("poll_new", state, bot)
    _msg("Q?", state, bot)
    _msg("A; B", state, bot)
    _cb("poll_opts_done", state, bot)
    _cb("poll_settings_next", state, bot)
    _cb("poll_aud:all", state, bot)
    _cb("poll_schedule", state, bot)
    assert asyncio.run(state.get_state()) == "PollCreate:schedule_when"
    _, ev = _msg("01.01.2020 10:00", state, bot)
    assert "уже прошло" in ev.answers[-1][0]
    when = sched._now_moscow_naive() + timedelta(days=1)
    _, ev = _msg(when.strftime("%d.%m.%Y %H:%M"), state, bot)
    assert "запланирован" in ev.answers[-1][0]
    polls = asyncio.run(db.list_polls(statuses=("scheduled",)))
    assert len(polls) == 1 and polls[0]["audience"] == []
    assert jobs and jobs[0][0] == polls[0]["id"]
    assert polls[0]["scheduled_at"] == sched._fmt_dt(when.replace(second=0, microsecond=0))


def test_wizard_audience_respects_header_city(tmp_path, monkeypatch):
    """Город в шапке: кнопок городов нет, любой выбор сужается до этого города."""
    _ready(tmp_path)

    async def scoped(_admin_id):
        return ("spb", ("msk",)), "Санкт-Петербург"
    monkeypatch.setattr(wiz, "_admin_city_view", scoped)
    spec, city = asyncio.run(wiz.build_audience_spec("approved", ADMIN_ID))
    assert city == "spb"
    assert spec[0] == {"field": "status", "value": "approved"}
    assert spec[1]["field"] == "event_city" and spec[1]["value"] == "spb"
    assert "spb" in polls_svc.audience_label(spec) or "Санкт" in polls_svc.audience_label(spec)
    text, kb = asyncio.run(wiz._audience_kb(ADMIN_ID))
    assert not any(cb.startswith("poll_aud:city:") for cb in _flat_callback_data(kb))
    assert "Город из шапки" in text


def test_stranger_cannot_open_polls(tmp_path):
    _ready(tmp_path)
    result, ev = _cb("admin_polls", _state(STRANGER_ID), FakeBot(), user_id=STRANGER_ID)
    assert result is UNHANDLED and ev.message.edit_calls == 0


# ── карточка: закрыть / удалить / экспорт ───────────────────────────────────────────────────

def test_card_close_and_delete_with_confirmation(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(_add_user(1))
    pid = asyncio.run(_mk_poll())
    asyncio.run(db.set_poll_status(pid, "open"))
    asyncio.run(db.record_poll_message(pid, 1, "tg1", 11, True))
    asyncio.run(db.upsert_poll_answer(pid, 1, [0]))
    state, bot = _state(ADMIN_ID), FakeBot()

    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert "Q?" in ev.message.text and f"poll_close:{pid}" in _flat_callback_data(ev.message.markup)
    assert "1 (100%) — A" in ev.message.text

    _, ev = _cb(f"poll_close:{pid}", state, bot)
    assert bot.stopped == [(1, 11)]
    assert "Опрос закрыт" in ev.message.text
    assert f"poll_close:{pid}" not in _flat_callback_data(ev.message.markup)
    assert asyncio.run(db.get_poll(pid))["status"] == "closed"

    _, ev = _cb(f"poll_del:{pid}", state, bot)
    assert "Пропадут все ответы (1 чел.)" in ev.message.text
    assert f"poll_del_go:{pid}" in _flat_callback_data(ev.message.markup)
    assert asyncio.run(db.get_poll(pid)) is not None  # подтверждение ещё не нажато
    _, ev = _cb(f"poll_del_go:{pid}", state, bot)
    assert asyncio.run(db.get_poll(pid)) is None
    assert asyncio.run(db.list_poll_answers(pid)) == []
    assert "Опросы" in ev.message.text


def test_delete_scheduled_cancels_job(tmp_path, monkeypatch):
    _ready(tmp_path)
    cancelled = []
    import handlers.admin_polls as ap
    monkeypatch.setattr(ap, "cancel_poll_job", lambda pid: cancelled.append(pid))
    pid = asyncio.run(_mk_poll())
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_del:{pid}", state, bot)
    assert "Отправка будет отменена" in ev.message.text
    _cb(f"poll_del_go:{pid}", state, bot)
    assert cancelled == [pid] and asyncio.run(db.get_poll(pid)) is None


def test_export_button_fail_soft_message(tmp_path, monkeypatch):
    _ready(tmp_path)
    pid = asyncio.run(_mk_poll())
    asyncio.run(db.set_poll_status(pid, "open"))
    import handlers.admin_polls as ap

    async def broken():
        return -1
    monkeypatch.setattr(ap, "export_polls_to_sheet", broken)
    _, ev = _cb(f"poll_export:{pid}", _state(ADMIN_ID), FakeBot())
    assert "Таблица сейчас недоступна" in ev.message.answers[-1][0]


# ── выгрузка в таблицу ───────────────────────────────────────────────────────────────────────

def test_sheet_rows_non_anonymous_and_anonymous(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1, "=Аня", "@anya", city="spb")
        pid = await _mk_poll(question="Когда?", options=["Утро", "Вечер"])
        await db.upsert_poll_answer(pid, 1, [0, 1])
        pid_a = await _mk_poll(question="Анон?", options=["Да", "Нет"], is_anonymous=True)
        await db.record_poll_message(pid_a, 1, "tg1", 1, True)
        await db.set_poll_message_totals("tg1", {"total": 1, "options": [0, 1]})
        rows = await polls_svc.build_polls_sheet_rows()
        assert len(rows) == 3
        assert rows[0][1] == "Когда?" and rows[0][2] == 1 and rows[0][3] == "'=Аня"  # CSV-инъекция
        assert rows[0][4] == "'@anya" and rows[0][6] == "Утро; Вечер"  # «@» — тоже CSV-триггер
        assert rows[1][1] == "Анон?" and rows[1][6] == "Да: 0" and rows[2][6] == "Нет: 1"
        assert rows[1][3] == "— анонимный опрос —"

    asyncio.run(go())


def test_export_uses_tab_setting_and_fails_soft(tmp_path, monkeypatch):
    _ready(tmp_path)
    import services.sheets as sheets
    calls = []

    async def fake_sync(title, headers, rows):
        calls.append((title, headers, rows))
        return len(rows)
    monkeypatch.setattr(sheets, "sync_named_worksheet", fake_sync)
    asyncio.run(_add_user(1))
    pid = asyncio.run(_mk_poll())
    asyncio.run(db.upsert_poll_answer(pid, 1, [0]))
    assert asyncio.run(polls_svc.export_polls_to_sheet()) == 1
    assert calls[0][0] == "Опросы" and calls[0][1] == polls_svc.POLLS_SHEET_HEADERS
    asyncio.run(db.set_setting("polls_sheet_tab", "Голосования"))
    asyncio.run(polls_svc.export_polls_to_sheet())
    assert calls[-1][0] == "Голосования"

    async def boom(title, headers, rows):
        raise RuntimeError("simulated gspread failure")
    monkeypatch.setattr(sheets, "sync_named_worksheet", boom)
    assert asyncio.run(polls_svc.export_polls_to_sheet()) == -1


# ── рестарт: ре-арм отложенного опроса ───────────────────────────────────────────────────────

def _isolate_sched(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "_JOBSTORE_URL", f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(sched, "_scheduler", None)


def _stop(s):
    s.pause()
    s.shutdown(wait=False)


def test_scheduled_poll_is_rearmed_after_restart(tmp_path, monkeypatch):
    _ready(tmp_path)
    _isolate_sched(tmp_path, monkeypatch)

    async def go():
        future = sched._now_moscow_naive() + timedelta(hours=2)
        pid = await _mk_poll(scheduled_at=sched._fmt_dt(future.replace(microsecond=0)))
        stale = await _mk_poll(scheduled_at=sched._fmt_dt(sched._now_moscow_naive() - timedelta(days=3)))
        # Крах посреди рассылки: 'sending' состарился → на буте вернётся в 'scheduled' и ре-армится.
        crashed = await _mk_poll(scheduled_at="2026-01-01 10:00:00")
        await db.claim_poll_sending(crashed)
        async with db._connect() as conn:
            await conn.execute("UPDATE polls SET sending_since = '2000-01-01 00:00:00' WHERE id = ?", (crashed,))
            await conn.commit()
        s = await sched.init_scheduler(bot=object())
        try:
            now = sched.datetime.now(sched.MOSCOW_TZ)
            job = s.get_job(f"poll_{pid}")
            assert job is not None and job.next_run_time == future.replace(microsecond=0, tzinfo=sched.MOSCOW_TZ)
            late = s.get_job(f"poll_{stale}")
            assert late is not None and now < late.next_run_time <= now + timedelta(minutes=10)
            assert s.get_job(f"poll_{crashed}") is not None
            assert (await db.get_poll(crashed))["status"] == "scheduled"
            assert (await db.get_poll(pid))["scheduled_at"] == sched._fmt_dt(future.replace(microsecond=0))
        finally:
            _stop(s)

    asyncio.run(go())


def test_send_scheduled_poll_job_delivers_via_module_bot(tmp_path, monkeypatch):
    _ready(tmp_path)
    asyncio.run(_add_user(1))
    pid = asyncio.run(_mk_poll())
    bot = FakeBot()
    monkeypatch.setattr(sched, "_bot", bot)
    asyncio.run(sched.send_scheduled_poll(pid))
    assert [p["chat_id"] for p in bot.sent_polls] == [1]
    assert asyncio.run(db.get_poll(pid))["status"] == "open"
