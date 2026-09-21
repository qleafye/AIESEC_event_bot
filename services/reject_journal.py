"""Phase 31 (31-05, D-18/D-19/D-24/D-29): журнал автоотказов как сервис.

Журнал (`database.db.auto_reject_log`) — отдельная сущность от `application_decisions`: у
решения человека есть окно отмены в 5 секунд и один финальный статус, у автоотказа его нет —
делегат может переподавать анкету сколько угодно раз (D-24: лимита попыток нет сознательно),
и менеджер может вернуть заявку на модерацию когда угодно, а не только в короткое окно после
срабатывания. Живая строка журнала (`returned_to_moderation_at IS NULL`) ровно одна на
делегата — это гарантирует частичный уникальный индекс на стороне БД (план 31-02).

Модуль aiogram-free: тот же разрез, что у `services/applications.py` против
`handlers/admin_moderation.py` — сообщение делегату о возврате на модерацию
(`reject_rules_return_text`) шлёт ВЫЗЫВАЮЩИЙ хендлер, не этот модуль.

Инцидент 06.09 (тихое массовое автоодобрение): в этом модуле сознательно НЕТ ни одной функции,
применяющей правила или возврат пакетом — каждая запись журнала пишется и возвращается по
ОДНОМУ `telegram_id`/`entry_id` (D-19). Не «оптимизировать» `return_to_moderation` в цикл по
списку id — это прямое нарушение урока инцидента.
"""
from __future__ import annotations

import json
import logging

from database.db import (
    claim_auto_reject_return,
    get_auto_reject_log_entry,
    get_user,
    revert_user_to_pending,
    update_user_answers,
    upsert_auto_reject_log,
)
from services.timeutil import msk_now
from settings_ops import per_city_visible_codes

logger = logging.getLogger(__name__)

# Сентинел `application_decisions.decided_by` для решения, принятого ПРАВИЛОМ, а не человеком
# (планы 31-06/31-09 импортируют константу отсюда — второй копии литерала в проекте быть не
# должно). Ровно `-1`, не `0` и не `NULL`: `handlers/admin_app_list.py::_decision_suffix` уже
# трактует любое ложное значение (`0`, `None`) как «решения нет / решение отменено» и печатает
# «автоматически» без указания менеджера — автоотказ слился бы с этой веткой и стал неотличим
# от отменённого решения человека (T-31-05-07). Отрицательное значение никогда не совпадёт с
# реальным `telegram_id` (Telegram id положительны). `database.db.resolve_decision_managers`
# уже пропускает неположительные `decided_by` (план 31-02) — резолвить «менеджера» с id `-1`
# не нужно.
AUTO_DECIDED_BY = -1


async def record_auto_reject(telegram_id: int, rule_ids: list[int], reject_texts: list[str]) -> int:
    """Пишет/обновляет живую строку журнала для `telegram_id` и возвращает её id.

    D-24: лимита попыток нет — это осознанное решение владельца. Каждое срабатывание, пока
    строка живая (`returned_to_moderation_at IS NULL`), растит `attempt_count` той же строки
    (`upsert_auto_reject_log`, план 31-02); после возврата на модерацию следующее срабатывание
    заводит НОВУЮ строку со счётчиком 1 — история предыдущего автоотказа не теряется, но не
    смешивается со следующим.

    Метка времени — `msk_now()` (вся семья `now()`-меток проекта пишется по Москве), не наивный
    `datetime.now()`.
    """
    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    rule_ids_json = json.dumps(rule_ids, ensure_ascii=False)
    reject_texts_json = json.dumps(reject_texts, ensure_ascii=False)
    return await upsert_auto_reject_log(telegram_id, rule_ids_json, reject_texts_json, now)


async def return_to_moderation(admin_id: int, entry_id: int) -> tuple[dict | None, str | None]:
    """Возврат ОДНОЙ заявки на модерацию. `(строка журнала, None)` при успехе;
    `(None, человеческая причина)` — запись недоступна, уже возвращена или вне города менеджера.

    Порядок шагов — фиксированный (T-31-05-01/T-31-05-02/T-31-05-03 threat register):
    1. запись существует;
    2. право на город делегата (город не задан у делегата -> доступен любому менеджеру);
    3. атомарный `claim_auto_reject_return` — второй тап по кнопке проигрывает;
    4. `revert_user_to_pending` — статус делегата назад в `pending`;
    5. узкий `update_user_answers` — признак автоотказа снят, сама строка делегата остаётся
       в базе (D-26: человек остаётся в базе ради охватов, не удаляется).

    Каждый шаг — по ОДНОМУ `telegram_id`/`entry_id`; в модуле нет ни одного массового `UPDATE`
    (D-19, урок инцидента 06.09) — не заводить здесь цикл по списку id."""
    entry = await get_auto_reject_log_entry(entry_id)
    if entry is None:
        return None, "Запись недоступна — обновите список"

    telegram_id = entry["telegram_id"]
    user = await get_user(telegram_id)
    delegate_city = (user or {}).get("event_city")
    if delegate_city:
        visible = await per_city_visible_codes(admin_id)
        if delegate_city not in visible:
            return None, "Эта заявка не из вашего города"

    now = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = await claim_auto_reject_return(entry_id, admin_id, now)
    if claimed is None:
        return None, "Эту заявку уже вернули на модерацию"

    reverted = await revert_user_to_pending(telegram_id, "rejected")
    if not reverted:
        # Гонка статусов (например, делегата параллельно тронул другой менеджер) — claim
        # журнала уже выигран и не откатывается: строка закрыта, это факт истории. Признак
        # автоотказа всё равно снимается ниже — предупреждение только в лог, не исключение,
        # тот же fail-soft приём, что у `services.applications.undo_decision`.
        logger.warning(
            "reject_journal.return_to_moderation: revert_user_to_pending(%s) вернул False "
            "(запись журнала %s уже закрыта)", telegram_id, entry_id,
        )

    await update_user_answers(
        telegram_id,
        {"auto_reject_rule_ids": None, "auto_rejected_at": None, "auto_rule_note": None},
        allowed_columns=["auto_reject_rule_ids", "auto_rejected_at", "auto_rule_note"],
    )
    return claimed, None
