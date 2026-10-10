"""Делегатская сторона опросов: приём ответов (`poll_answer`) и итогов (`poll`).

Отдельный Router (не admin.router — там CapabilityMiddleware, а голосует делегат). aiogram
собирает `allowed_updates` для long polling из зарегистрированных observer'ов всех роутеров
(`Dispatcher.resolve_used_update_types`), поэтому достаточно подключить этот роутер в main.py —
без него Telegram вообще не присылал бы `poll_answer`/`poll` (закреплено тестом).

Ограничения Bot API, не бота:
- `poll_answer` приходит ТОЛЬКО по неанонимным опросам; у анонимных есть лишь `poll` со
  счётчиками (кто голосовал — неизвестно). Мастер предупреждает об этом при выборе «анонимный».
- `poll` приходит по каждому опросу, который отправил бот, при любом изменении голосов, — но
  у каждого делегата свой экземпляр опроса, поэтому счётчики хранятся по `telegram_poll_id`
  (poll_messages.totals_json) и суммируются в get_poll_results.
"""
import logging

from aiogram import Router
from aiogram.types import Poll, PollAnswer

from database.db import (
    get_poll_id_by_telegram_poll,
    upsert_poll_answer,
    set_poll_message_totals,
)

logger = logging.getLogger(__name__)

router = Router()


@router.poll_answer()
async def on_poll_answer(answer: PollAnswer):
    """Ответ делегата: перезаписывает прошлый; пустой option_ids = отозвал голос → строка
    удаляется. Чужие опросы (не из poll_messages) молча игнорируются."""
    poll_id = await get_poll_id_by_telegram_poll(answer.poll_id)
    if poll_id is None:
        return
    user = getattr(answer, "user", None)
    if user is None:
        return  # голос от имени канала/чата — для опросов в личке не бывает
    await upsert_poll_answer(poll_id, user.id, list(answer.option_ids or []))


@router.poll()
async def on_poll_update(poll: Poll):
    """Счётчики Telegram по одному экземпляру опроса — единственный источник итогов для
    анонимных опросов (и кросс-проверка для остальных)."""
    totals = {
        "total": int(poll.total_voter_count or 0),
        "options": [int(o.voter_count or 0) for o in (poll.options or [])],
    }
    await set_poll_message_totals(poll.id, totals)
