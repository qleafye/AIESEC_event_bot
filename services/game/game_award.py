"""Фаза 32 (фикс CR-02): «сколько начислить за сдачу с учётом просрочки» — единственная
функция на весь проект, и для бота, и для Mini App.

До этого фикса штраф считало только `handlers/game/admin_gamification.py::_award_for`, которое
Mini App не могло позвать (aiogram-модуль), поэтому `miniapp/routers/review.py::review_approve`
начисляло сдачу после дедлайна ПОЛНОЙ суммой — тот же делегат получал разные баллы за одну и
ту же просрочку в зависимости от того, кто из менеджеров и на какой поверхности одобрил сдачу
(бот или Mini App), что двигало места в рейтинге волны.

Сам модуль aiogram-free (`domain/game/labels.py`/`domain/settings/schema.py` — уже используются и Mini App, и
ботом), поэтому его можно звать из обоих процессов. Обе точки одобрения бота
(`grev_approve`/`grev_approve_amount_step` в `handlers/game/admin_gamification.py`) тоже зовут
эту функцию — формула считается в одном месте буквально."""
from __future__ import annotations

from domain.cities import get_setting_typed_for_city, is_per_city
from domain.game.labels import penalized_coins, task_has_deadline


async def _submitter_city(submission: dict) -> str | None:
    """event_city сдавшего. Пока ключ не городской — None, без лишнего чтения пользователя."""
    if not is_per_city("game_late_penalty_percent"):
        return None
    if "user_event_city" in submission:
        return submission.get("user_event_city")
    user_id = submission.get("user_id")
    if not user_id:
        return None
    from database.db import get_user
    user = await get_user(int(user_id))
    return (user or {}).get("event_city")


async def award_for(submission: dict, task: dict, base_coins: int) -> tuple[int, bool]:
    """Сколько начислить за сдачу с учётом просрочки, и была ли просрочка. Просрочка — та же
    строковая идиома, что и у карточки проверки (`submitted_at > deadline_at` как строки).
    Формула штрафа — единственная на проект, `game_labels.penalized_coins`."""
    late = task_has_deadline(task) and str(submission["submitted_at"]) > str(task["deadline_at"])
    if not late:
        return base_coins, False
    percent = await get_setting_typed_for_city("game_late_penalty_percent", await _submitter_city(submission))
    return penalized_coins(base_coins, percent), True
