"""После анкеты из нескольких вопросов (фаза 39, на проде с 28.10) — старые рассылки и опросы,
которыми менеджеры пользуются на форуме, сквозь настоящий `Dispatcher`: один вопрос (опрос
Telegram) по городу, отложенная рассылка по фильтру «город», мгновенная рассылка всем."""
from __future__ import annotations

from aiogram.methods import SendMessage, SendPoll
from aiogram.types import Poll, PollOption

from database import db
from tests._forum_day_world import (  # noqa: F401  (world_factory — фикстура)
    ADMIN_ID, MSK, RecordingBot, World, at, world_factory,
)


def _poll_aware(bot: RecordingBot) -> None:
    """SendPoll отвечает сообщением с опросом — из него рассылка берёт poll_id/message_id."""
    original = bot.__class__.__call__

    async def call(self, method, request_timeout=None):
        if isinstance(method, SendPoll):
            self.calls.append(method)
            return self._msg(method.chat_id, poll=Poll(
                id=f"p{self._mid}", question=method.question, total_voter_count=0, is_closed=False,
                is_anonymous=bool(method.is_anonymous), type="regular",
                allows_multiple_answers=bool(method.allows_multiple_answers), allows_revoting=False, members_only=False,
                options=[PollOption(persistent_id=str(i), text=o if isinstance(o, str) else o.text, voter_count=0)
                         for i, o in enumerate(method.options)],
            ))
        return await original(self, method, request_timeout)

    bot.__class__ = type("PollBot", (bot.__class__,), {"__call__": call})


def _jobs(w: World) -> dict:
    return {j.id: j for j in w.scheduler.get_jobs()}


async def _run_new_jobs(w: World, before: set) -> None:
    for jid, job in _jobs(w).items():
        if jid not in before:
            await job.func(*job.args, **job.kwargs)


def test_single_question_poll_scheduled_for_city(world_factory):
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        _poll_aware(w.bot)
        await w.tap(ADMIN_ID, "poll_new")
        fork = [b.callback_data for r in w.bot.last_markup(ADMIN_ID).inline_keyboard for b in r]
        await w.tap(ADMIN_ID, "poll_new_single")
        await w.send(ADMIN_ID, "Во сколько удобно начать?")
        await w.send(ADMIN_ID, "10:00\n11:00")
        await w.tap(ADMIN_ID, "poll_opts_done")
        await w.tap(ADMIN_ID, "poll_settings_next")
        await w.tap(ADMIN_ID, f"poll_aud:city:{MSK}")
        await w.tap(ADMIN_ID, "poll_schedule")
        before = set(_jobs(w))
        await w.send(ADMIN_ID, "29.10.2026 12:00")
        new = [j for j in _jobs(w) if j not in before]
        at(2026, 10, 29, 12, 0)
        await _run_new_jobs(w, before)
        polls = [m for m in w.bot.calls if isinstance(m, SendPoll) and int(m.chat_id) != ADMIN_ID]  # админу — превью
        return fork, new, polls, w.bot.texts_to(ADMIN_ID)

    fork, new, polls, texts = world_factory(scenario)
    assert fork[:2] == ["poll_new_single", "psv_new"]
    assert new, texts[-3:]
    assert len(polls) == 50, texts[-3:]
    assert polls[0].question == "Во сколько удобно начать?"


def test_scheduled_broadcast_by_city_filter(world_factory):
    at(2026, 10, 28, 15, 0)

    async def scenario(w: World, ids):
        async with db._connect() as conn:  # чужой город — не должен получить
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, season, event_city, registration_date) "
                "VALUES (799001, 'Петров Пётр', 'approved', 'YL''26', 'spb', '2026-10-01 10:00:00')")
            await conn.commit()
        await w.send(ADMIN_ID, "/broadcast")
        await w.tap(ADMIN_ID, "broadcast_filter")
        await w.tap(ADMIN_ID, "filter_f_event_city")
        await w.tap(ADMIN_ID, "filter_opt:0")
        await w.tap(ADMIN_ID, "filter_schedule")
        await w.send(ADMIN_ID, "30.10.2026 13:00")
        await w.send(ADMIN_ID, "Обед в 13:30 в зале А")
        before = set(_jobs(w))
        await w.tap(ADMIN_ID, "sched_go")
        new = [j for j in _jobs(w) if j not in before]
        at(2026, 10, 30, 13, 0)
        await _run_new_jobs(w, before)
        got = [int(m.chat_id) for m in w.bot.calls
               if isinstance(m, SendMessage) and m.text == "Обед в 13:30 в зале А" and int(m.chat_id) != ADMIN_ID]
        return new, got, w.bot.texts_to(ADMIN_ID)

    new, got, texts = world_factory(scenario)
    assert new, texts[-4:]
    assert len(got) == 50 and 799001 not in got, (len(got), texts[-4:])


def test_instant_broadcast_to_all(world_factory):
    at(2026, 10, 30, 13, 0)

    async def scenario(w: World, ids):
        await w.send(ADMIN_ID, "/broadcast")
        await w.tap(ADMIN_ID, "broadcast_all")
        await w.send(ADMIN_ID, "Начинаем через 10 минут")
        await w.tap(ADMIN_ID, "bc_go")
        import asyncio
        for _ in range(200):  # рассылка идёт фоном
            got = [m for m in w.bot.calls if isinstance(m, SendMessage)
                   and m.text == "Начинаем через 10 минут" and int(m.chat_id) != ADMIN_ID]
            if len(got) >= 50:
                break
            await asyncio.sleep(0.05)
        return len(got), w.bot.texts_to(ADMIN_ID)

    n, texts = world_factory(scenario)
    assert n == 50, texts[-4:]
