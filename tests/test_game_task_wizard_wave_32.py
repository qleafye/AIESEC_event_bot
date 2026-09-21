"""Phase 32 План 12 (D-12/D-27/D-28) — визард задания получает два кнопочных шага («Волна»,
«Аудитория»), учится создавать задание без срока и сам ставит/снимает напоминание за сутки.

Задача 1 — шаги «Волна»/«Аудитория»: кнопочный выбор, фейл-софт на неизвестном id, «вне волн»
без тупика, обе новые строки на карточке подтверждения, оба поля в меню «✏️ Изменить».

Задача 2 — «Без срока»/«По умолчанию — конец волны» в пресетах дедлайна (визард + точечная
правка), метка `NO_DEADLINE_AT` никогда не печатается человеку буквой.

Задача 3 — напоминание ставится при создании/правке срока и снимается при архивации/удалении/
снятии срока; планировщик может быть не поднят — создание задания не имеет права упасть.

Handlers called DIRECTLY with Fake message/callback doubles (pytest-asyncio unavailable in this
env) — same convention as tests/test_game_ui16_manager_tasks_260820.py. Job scheduling tests
(задача 3) reuse tests/test_ambassador_wave_scheduling_32.py's real-AsyncIOScheduler-on-temp-
jobstore harness so a job's ID (not just the fact a call happened) is verified.
"""
from __future__ import annotations

import asyncio

import game_labels
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
import services.scheduler as sched
from handlers import admin_gamification
from handlers import admin_game_tasks
from handlers import game_task_wizard
from handlers.states import GameTaskCreate


ADMIN_ID = 321001


def _db_ready(tmp_path, name="wave_wizard.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = None


class FakeMessage:
    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers_sent = []
        self.answer_markups = []
        self.answer_photo_calls = []
        self.text_edited = None
        self.edit_markup = None
        self.edit_calls = 0

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)
        self.answer_markups.append(reply_markup)

    async def answer_photo(self, photo, caption=None, parse_mode=None, reply_markup=None):
        self.answer_photo_calls.append((photo, caption, parse_mode, reply_markup))

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text_edited = text
        self.edit_markup = reply_markup
        self.edit_calls += 1

    async def edit_reply_markup(self, reply_markup=None):
        pass


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID, message=None):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _flat_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _mk_wave(*, state="active", starts_at="2026-10-01 00:00:00", ends_at="2026-10-15 23:59:59"):
    wave_id = _run(db.create_wave(starts_at, ends_at, created_by=ADMIN_ID))
    if state != "draft":
        _run(db.set_wave_state(wave_id, state, expected_state="draft"))
    return wave_id


async def _a_drive_to_wave_step(state, *, title="Задание", text="Текст задания", coins="10"):
    """gtnew -> title -> text -> photo skip -> category -> coins -> proof done (module off) ->
    lands on the wave prompt. Returns the callback whose message got the wave prompt. Async
    core -- both the sync wrapper below and (later, task 3) an already-running-loop `body()`
    coroutine can share it (a nested `asyncio.run()` inside a running loop raises RuntimeError)."""
    await admin_gamification.game_task_new(FakeCallback("gtnew"), state)
    await admin_gamification.game_task_title_step(FakeMessage(text=title), state)
    await admin_gamification.game_task_text_step(FakeMessage(text=text), state)
    await admin_gamification.game_task_photo_skip(FakeCallback("gtphoto_skip"), state)
    await admin_gamification.game_task_category_step(FakeCallback("gtcat:Light"), state)
    await admin_gamification.game_task_coins_step(FakeMessage(text=coins), state)
    await admin_gamification.game_task_proof_step(FakeCallback("gtproof:text"), state)
    cb = FakeCallback("gtproof_done")
    await admin_gamification.game_task_proof_done(cb, state)
    assert await state.get_state() == GameTaskCreate.wave
    return cb


async def _a_drive_to_deadline(state, *, wave_cb="gtwave:none", audience="all", **kw):
    """`_a_drive_to_wave_step` + a wave pick + an audience pick -> lands on the deadline prompt."""
    await _a_drive_to_wave_step(state, **kw)
    await admin_game_tasks.game_task_wave_step(FakeCallback(wave_cb), state)
    assert await state.get_state() == GameTaskCreate.audience
    cb = FakeCallback(f"gtaud:{audience}")
    await admin_game_tasks.game_task_audience_step(cb, state)
    assert await state.get_state() == GameTaskCreate.deadline
    return cb


def _drive_to_wave_step(state, **kw):
    return asyncio.run(_a_drive_to_wave_step(state, **kw))


def _drive_to_deadline(state, **kw):
    return asyncio.run(_a_drive_to_deadline(state, **kw))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: шаги «Волна» / «Аудитория»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_step_no_waves_offers_only_out_of_wave_with_explainer(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    cb = _drive_to_wave_step(state)
    kb = cb.message.answer_markups[-1]
    assert _flat_callback_data(kb) == ["gtwave:none"]
    assert "🚫 Вне волн" in _flat_texts(kb)
    assert "Волн пока нет" in cb.message.answers_sent[-1]
    assert "Волны" in cb.message.answers_sent[-1]  # где завести — подсказка не молчит


def test_wave_step_lists_only_draft_and_active_waves(tmp_path):
    _db_ready(tmp_path)
    draft_id = _mk_wave(state="draft")
    active_id = _mk_wave(state="active")
    closing_id = _mk_wave(state="closing")
    state = _new_state()
    cb = _drive_to_wave_step(state)
    kb = cb.message.answer_markups[-1]
    data = _flat_callback_data(kb)
    assert data[0] == "gtwave:none"
    assert f"gtwave:{draft_id}" in data
    assert f"gtwave:{active_id}" in data
    assert f"gtwave:{closing_id}" not in data
    texts = _flat_texts(kb)
    assert any(t.startswith("Волна ") for t in texts)


def test_wave_step_unknown_id_alerts_without_state_change(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_wave_step(state)
    cb = FakeCallback("gtwave:99999")
    asyncio.run(admin_game_tasks.game_task_wave_step(cb, state))
    assert cb.answers == [("Неизвестная волна", True)]
    assert asyncio.run(state.get_state()) == GameTaskCreate.wave


def test_wave_step_selecting_wave_advances_to_audience_with_data(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave(ends_at="2026-10-20 23:59:59")
    state = _new_state()
    _drive_to_wave_step(state)
    cb = FakeCallback(f"gtwave:{wave_id}")
    asyncio.run(admin_game_tasks.game_task_wave_step(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.audience
    data = asyncio.run(state.get_data())
    assert data["gt_wave_id"] == wave_id
    assert data["gt_wave_label"].startswith("Волна ")
    assert data["gt_wave_ends_at"] == "2026-10-20 23:59:59"


def test_audience_step_unknown_value_alerts_without_state_change(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_wave_step(state)
    asyncio.run(admin_game_tasks.game_task_wave_step(FakeCallback("gtwave:none"), state))
    cb = FakeCallback("gtaud:bogus")
    asyncio.run(admin_game_tasks.game_task_audience_step(cb, state))
    assert cb.answers == [("Неизвестный вариант", True)]
    assert asyncio.run(state.get_state()) == GameTaskCreate.audience


def test_audience_step_with_wave_deadline_prompt_offers_wave_end_hint(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave()
    state = _new_state()
    cb = _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}", audience="ambassadors")
    prompt = cb.message.answers_sent[-1]
    assert "По умолчанию — конец" in prompt
    kb = cb.message.answer_markups[-1]
    assert "gtdeadline_preset:wave_end" in _flat_callback_data(kb)


def test_audience_step_without_wave_deadline_prompt_has_no_hint(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    cb = _drive_to_deadline(state)  # gtwave:none by default
    prompt = cb.message.answers_sent[-1]
    assert "По умолчанию" not in prompt
    kb = cb.message.answer_markups[-1]
    assert "gtdeadline_preset:wave_end" not in _flat_callback_data(kb)


def test_confirm_card_has_wave_and_audience_lines_no_codes(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave()
    state = _new_state()
    cb = _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}", audience="ambassadors")
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    card = msg.answers_sent[-1]
    assert "Волна: Волна " in card
    assert "Аудитория: Только амбассадорам" in card
    assert "ambassadors" not in card
    assert "wave_id" not in card


def test_confirm_card_out_of_wave_all_audience_defaults(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    card = msg.answers_sent[-1]
    assert "Волна: Вне волн" in card
    assert "Аудитория: Всем делегатам" in card


def test_full_wizard_creates_task_with_wave_and_audience(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave()
    state = _new_state()
    _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}", audience="ambassadors")
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    asyncio.run(admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state))
    tasks = asyncio.run(db.list_all_tasks())
    assert len(tasks) == 1
    assert tasks[0]["wave_id"] == wave_id
    assert tasks[0]["audience"] == "ambassadors"


def test_old_path_module_off_no_waves_creates_out_of_wave_all_audience(tmp_path):
    """Задание, созданное старым путём (модуль городов выключен, волн нет), получает «вне
    волн» и «всем» (D-12/D-28 defaults)."""
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    asyncio.run(admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state))
    tasks = asyncio.run(db.list_all_tasks())
    assert tasks[0]["wave_id"] is None
    assert tasks[0]["audience"] == "all"


def test_wizard_edit_fields_has_exactly_eight_including_wave_and_audience(tmp_path):
    fields = {f for f, _ in admin_game_tasks._WIZARD_EDIT_FIELDS}
    assert len(admin_game_tasks._WIZARD_EDIT_FIELDS) == 8
    assert "wave" in fields and "audience" in fields


def test_edit_wave_field_from_preview_returns_to_updated_preview(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave()
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm

    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:wave"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.wave
    cb = FakeCallback(f"gtwave:{wave_id}")
    asyncio.run(admin_game_tasks.game_task_wave_step(cb, state))
    # выбор волны из «✏️ Изменить» возвращает СРАЗУ в превью, не на шаг аудитории
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert "Волна: Волна " in cb.message.answers_sent[-1]


def test_edit_audience_field_from_preview_returns_to_updated_preview(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))

    asyncio.run(admin_game_tasks.game_task_wizard_edit_field(FakeCallback("gtwiz_edit:audience"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.audience
    cb = FakeCallback("gtaud:ambassadors")
    asyncio.run(admin_game_tasks.game_task_audience_step(cb, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    assert "Аудитория: Только амбассадорам" in cb.message.answers_sent[-1]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: «Без срока» / «По умолчанию — конец волны»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wizard_none_preset_creates_task_without_deadline(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    asyncio.run(admin_game_tasks.game_task_deadline_preset(FakeCallback("gtdeadline_preset:none"), state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    asyncio.run(admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state))
    tasks = asyncio.run(db.list_all_tasks())
    assert game_labels.task_has_deadline(tasks[0]) is False
    assert tasks[0]["deadline_at"] == db.NO_DEADLINE_AT


def test_wizard_none_preset_confirm_card_prints_words_not_9999(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    preset_cb = FakeCallback("gtdeadline_preset:none")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(preset_cb, state))
    card = preset_cb.message.answers_sent[-1]
    assert "9999" not in card
    assert "без срока" in card


def test_wizard_wave_end_preset_sets_deadline_to_wave_end(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave(ends_at="2026-11-30 23:59:59")
    state = _new_state()
    _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(FakeCallback("gtdeadline_preset:wave_end"), state))
    data = asyncio.run(state.get_data())
    assert data["gt_deadline"] == "2026-11-30 23:59:59"


def test_wizard_wave_end_preset_unavailable_without_wave_is_unknown(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)  # вне волн
    cb = FakeCallback("gtdeadline_preset:wave_end")
    asyncio.run(admin_game_tasks.game_task_deadline_preset(cb, state))
    assert cb.answers == [("Неизвестный вариант", True)]


def test_point_edit_none_preset_removes_deadline_and_prints_words(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task(
        "т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID,
    ))
    state = _new_state()
    asyncio.run(admin_game_tasks.game_task_editdeadline_start(FakeCallback(f"gteditdeadline:{task_id}"), state))
    asyncio.run(admin_game_tasks.game_task_editdeadline_preset(
        FakeCallback("gteditdeadline_preset:none"), state,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["deadline_at"] == db.NO_DEADLINE_AT
    assert game_labels.task_has_deadline(task) is False
    admin_text = game_labels.task_deadline_admin(task)
    assert "9999" not in admin_text
    assert admin_text == "без срока"


def test_point_edit_wave_end_preset_available_only_for_wave_task(tmp_path):
    _db_ready(tmp_path)
    wave_id = _mk_wave(ends_at="2026-12-24 23:59:59")
    task_id = asyncio.run(db.create_task(
        "т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID, wave_id=wave_id,
    ))
    state = _new_state()
    cb = FakeCallback(f"gteditdeadline:{task_id}")
    asyncio.run(admin_game_tasks.game_task_editdeadline_start(cb, state))
    assert "gteditdeadline_preset:wave_end" in _flat_callback_data(cb.message.edit_markup)
    asyncio.run(admin_game_tasks.game_task_editdeadline_preset(
        FakeCallback("gteditdeadline_preset:wave_end"), state,
    ))
    task = asyncio.run(db.get_task(task_id))
    assert task["deadline_at"] == "2026-12-24 23:59:59"


def test_existing_presets_still_work_plus3(tmp_path):
    _db_ready(tmp_path)
    state = _new_state()
    _drive_to_deadline(state)
    asyncio.run(admin_game_tasks.game_task_deadline_preset(FakeCallback("gtdeadline_preset:plus3"), state))
    data = asyncio.run(state.get_data())
    expected = game_task_wizard._resolve_deadline_preset("plus3")
    assert data["gt_deadline"] == expected.strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: автоматические напоминания
# ══════════════════════════════════════════════════════════════════════════════════════════

def _build_scheduler(tmp_path):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.sqlite'}")},
        timezone=sched.MOSCOW_TZ,
    )


def _run_scheduled(tmp_path, monkeypatch, body):
    """Реальный AsyncIOScheduler на временном jobstore (тот же приём, что
    tests/test_ambassador_wave_scheduling_32.py::_run_scheduled) — job-id проверяется по-
    настоящему, не только факт вызова add_job. `body` — асинхронная функция, принимающая
    планировщик; внутренние вызовы визарда идут через `_a_drive_to_*` (await напрямую), т.к.
    вложенный `asyncio.run()` внутри уже запущенного цикла роняет RuntimeError."""
    s = _build_scheduler(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", s)

    async def go():
        s.start(paused=True)
        try:
            return await body(s)
        finally:
            s.shutdown(wait=False)

    return asyncio.run(go())


def test_confirm_with_deadline_schedules_reminder_with_expected_job_id(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        state = _new_state()
        await _a_drive_to_deadline(state)
        msg = FakeMessage(text="01.01.2099 00:00")
        await admin_gamification.game_task_deadline_step(msg, state)
        await admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state)
        tasks = await db.list_all_tasks()
        task_id = tasks[0]["id"]
        return s.get_job(f"task_deadline_reminder_{task_id}")

    job = _run_scheduled(tmp_path, monkeypatch, body)
    assert job is not None


def test_confirm_without_deadline_does_not_schedule(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        state = _new_state()
        await _a_drive_to_deadline(state)
        await admin_game_tasks.game_task_deadline_preset(FakeCallback("gtdeadline_preset:none"), state)
        await admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state)
        return len(s.get_jobs())

    assert _run_scheduled(tmp_path, monkeypatch, body) == 0


def test_point_edit_deadline_change_reschedules_job(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        task_id = await db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID)
        sched.schedule_task_deadline_reminder(task_id, sched._parse_schedule_dt("01.01.2099 00:00"))
        first = s.get_job(f"task_deadline_reminder_{task_id}").next_run_time

        state = _new_state()
        await admin_game_tasks.game_task_editdeadline_start(FakeCallback(f"gteditdeadline:{task_id}"), state)
        await admin_game_tasks.game_task_editdeadline_preset(
            FakeCallback("gteditdeadline_preset:plus7"), state,
        )
        job = s.get_job(f"task_deadline_reminder_{task_id}")
        return first, job

    first, job = _run_scheduled(tmp_path, monkeypatch, body)
    assert job is not None
    assert job.next_run_time != first


def test_point_edit_none_preset_cancels_job(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        task_id = await db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID)
        sched.schedule_task_deadline_reminder(task_id, sched._parse_schedule_dt("01.01.2099 00:00"))
        assert s.get_job(f"task_deadline_reminder_{task_id}") is not None

        state = _new_state()
        await admin_game_tasks.game_task_editdeadline_start(FakeCallback(f"gteditdeadline:{task_id}"), state)
        await admin_game_tasks.game_task_editdeadline_preset(
            FakeCallback("gteditdeadline_preset:none"), state,
        )
        return s.get_job(f"task_deadline_reminder_{task_id}")

    assert _run_scheduled(tmp_path, monkeypatch, body) is None


def test_archive_cancels_reminder_job(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        task_id = await db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID)
        sched.schedule_task_deadline_reminder(task_id, sched._parse_schedule_dt("01.01.2099 00:00"))
        await admin_gamification.game_task_archive_go(FakeCallback(f"gtarchive_go:{task_id}"))
        return s.get_job(f"task_deadline_reminder_{task_id}")

    assert _run_scheduled(tmp_path, monkeypatch, body) is None


def test_delete_cancels_reminder_job(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def body(s):
        task_id = await db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID)
        sched.schedule_task_deadline_reminder(task_id, sched._parse_schedule_dt("01.01.2099 00:00"))
        await admin_gamification.game_task_delete_go(FakeCallback(f"gtdelete_go:{task_id}"))
        return s.get_job(f"task_deadline_reminder_{task_id}")

    assert _run_scheduled(tmp_path, monkeypatch, body) is None


def test_unarchive_rearms_reminder_job(tmp_path, monkeypatch):
    """[Rule 1] Симметрично архивации: возврат из архива задания с будущим сроком снова
    ставит напоминание -- иначе снятое при архивации напоминание осиротело бы навсегда."""
    _db_ready(tmp_path)

    async def body(s):
        task_id = await db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID)
        await admin_gamification.game_task_archive_go(FakeCallback(f"gtarchive_go:{task_id}"))
        assert s.get_job(f"task_deadline_reminder_{task_id}") is None
        await admin_gamification.game_task_unarchive(FakeCallback(f"gtunarchive:{task_id}"))
        return s.get_job(f"task_deadline_reminder_{task_id}")

    assert _run_scheduled(tmp_path, monkeypatch, body) is not None


def test_scheduler_unavailable_does_not_block_task_creation(tmp_path, monkeypatch):
    """T-32-12-04: планировщик не поднят в тестовой среде -- create_task не имеет права упасть."""
    _db_ready(tmp_path)
    monkeypatch.setattr(sched, "_scheduler", None)
    state = _new_state()
    _drive_to_deadline(state)
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    asyncio.run(admin_gamification.game_task_confirm(FakeCallback("gtconfirm"), state))
    tasks = asyncio.run(db.list_all_tasks())
    assert len(tasks) == 1  # задание всё равно создано


def test_scheduler_unavailable_does_not_block_archive(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("т", "Light", 10, "text", "2099-01-01 00:00:00", ADMIN_ID))
    monkeypatch.setattr(sched, "_scheduler", None)
    asyncio.run(admin_gamification.game_task_archive_go(FakeCallback(f"gtarchive_go:{task_id}")))
    task = asyncio.run(db.get_task(task_id))
    assert task["archived_at"] is not None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ревизия 32-FIX (WR-07): право на волну и открытый состав перепроверяются заново — на
# шаге «Волна», в списке волн этого шага и повторно при «✅ Опубликовать».
# ══════════════════════════════════════════════════════════════════════════════════════════

def _bind_manager(manager_id, city):
    asyncio.run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(manager_id, city))


def test_wave_step_rejects_wave_locked_after_start_notified(tmp_path):
    """Стартовая рассылка ушла между показом кнопки и тапом — состав волны заперт, задание в
    неё больше не добавляется."""
    _db_ready(tmp_path)
    wave_id = _mk_wave(state="active")
    asyncio.run(db.mark_wave_started(wave_id, "2026-09-30 09:00:00"))
    state = _new_state()
    _drive_to_wave_step(state)
    cb = FakeCallback(f"gtwave:{wave_id}")
    asyncio.run(admin_game_tasks.game_task_wave_step(cb, state))
    assert cb.answers and cb.answers[-1][1] is True
    data = asyncio.run(state.get_data())
    assert data.get("gt_wave_id") is None
    assert asyncio.run(state.get_state()) == GameTaskCreate.wave


def test_wave_step_rejects_wave_of_other_city_for_bound_manager(tmp_path):
    """Городской менеджер не может привязать задание к волне «все города», которую по
    `can_edit_wave` он не вправе трогать, хотя кнопка волны в его списке есть."""
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    codes = cities.city_codes()
    city_a, city_b = codes[0], codes[1]
    wave_id = asyncio.run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-15 23:59:59", event_city=city_b, created_by=ADMIN_ID,
    ))
    manager_id = 921777
    _bind_manager(manager_id, city_a)
    state = _new_state(manager_id)
    cb = FakeCallback(f"gtwave:{wave_id}", user_id=manager_id)
    asyncio.run(admin_game_tasks.game_task_wave_step(cb, state))
    assert cb.answers and cb.answers[-1][1] is True
    data = asyncio.run(state.get_data())
    assert data.get("gt_wave_id") is None


def test_wave_prompt_excludes_locked_wave_from_button_list(tmp_path):
    """WR-07: на шаге выбора показываем только волны, куда добавить задание сейчас можно."""
    _db_ready(tmp_path)
    open_id = _mk_wave(state="draft")
    locked_id = _mk_wave(state="active")
    asyncio.run(db.mark_wave_started(locked_id, "2026-09-30 09:00:00"))
    state = _new_state()
    target = FakeMessage()
    asyncio.run(game_task_wizard._game_task_wave_prompt(target, state, ADMIN_ID))
    kb = target.answer_markups[-1]
    data = _flat_callback_data(kb)
    assert f"gtwave:{open_id}" in data
    assert f"gtwave:{locked_id}" not in data


def test_gtconfirm_rechecks_wave_deleted_mid_wizard(tmp_path):
    """Волну удалили, пока менеджер шёл по визарду до подтверждения — задание не создаётся с
    висячим `wave_id`, менеджер возвращается на шаг выбора волны."""
    _db_ready(tmp_path)
    wave_id = _mk_wave()
    state = _new_state()
    _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}")
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    assert asyncio.run(state.get_state()) == GameTaskCreate.confirm
    asyncio.run(db.delete_wave(wave_id))
    cb = FakeCallback("gtconfirm")
    asyncio.run(admin_gamification.game_task_confirm(cb, state))
    assert asyncio.run(db.list_all_tasks()) == []
    assert asyncio.run(state.get_state()) == GameTaskCreate.wave
    assert cb.answers and cb.answers[-1][1] is True


def test_gtconfirm_rechecks_wave_locked_after_start_notified(tmp_path):
    """Та же перепроверка, если волна не удалена, а её состав заперла стартовая рассылка,
    ушедшая уже ПОСЛЕ выбора волны в визарде."""
    _db_ready(tmp_path)
    wave_id = _mk_wave(state="active")
    state = _new_state()
    _drive_to_deadline(state, wave_cb=f"gtwave:{wave_id}")
    msg = FakeMessage(text="01.01.2099 00:00")
    asyncio.run(admin_gamification.game_task_deadline_step(msg, state))
    asyncio.run(db.mark_wave_started(wave_id, "2026-09-30 09:00:00"))
    cb = FakeCallback("gtconfirm")
    asyncio.run(admin_gamification.game_task_confirm(cb, state))
    assert asyncio.run(db.list_all_tasks()) == []
    assert asyncio.run(state.get_state()) == GameTaskCreate.wave
