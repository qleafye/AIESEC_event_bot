"""Фаза 32 (фикс CR-02): «сколько начислить за сдачу с учётом просрочки» — единственная
функция на весь проект, и для бота, и для Mini App.

До этого фикса штраф считало только `handlers/admin_gamification.py::_award_for`, которое
Mini App не могло позвать (aiogram-модуль), поэтому `miniapp/routers/review.py::review_approve`
начисляло сдачу после дедлайна ПОЛНОЙ суммой — тот же делегат получал разные баллы за одну и
ту же просрочку в зависимости от того, кто из менеджеров и на какой поверхности одобрил сдачу
(бот или Mini App), что двигало места в рейтинге волны.

Сам модуль aiogram-free (`game_labels.py`/`settings_schema.py` — уже используются и Mini App, и
ботом), поэтому его можно звать из обоих процессов. `handlers/admin_gamification.py::_award_for`
пока остаётся собственной копией — этот фикс не имеет права трогать `handlers/*`; при
следующей правке того файла стоит перевести обе точки одобрения бота
(`grev_approve`/`grev_approve_amount_step`) на эту функцию, чтобы формула считалась в одном
месте буквально, а не только «даёт тот же результат»."""
from __future__ import annotations

from game_labels import penalized_coins, task_has_deadline
from settings_schema import get_setting_typed


async def award_for(submission: dict, task: dict, base_coins: int) -> tuple[int, bool]:
    """Сколько начислить за сдачу с учётом просрочки, и была ли просрочка. Просрочка — та же
    строковая идиома, что и у карточки проверки (`submitted_at > deadline_at` как строки).
    Формула штрафа — единственная на проект, `game_labels.penalized_coins`."""
    late = task_has_deadline(task) and str(submission["submitted_at"]) > str(task["deadline_at"])
    if not late:
        return base_coins, False
    percent = await get_setting_typed("game_late_penalty_percent")
    return penalized_coins(base_coins, percent), True
