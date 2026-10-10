"""Phase 33 (delegate-card admin actions, задача 1): «🧹 Сбросить зависшую анкету» — кнопка на
карточке `/find` (`handlers/applications/admin_reg_reset.py`) для делегата, застрявшего посреди анкеты.

Три места держат «зависание» одновременно (33-SEED.md, «Зависимости города» + рабочее
задание фазы):
  - FSM бота (`MemoryStorage`) — сбрасывается сам на каждом рестарте контейнера, но живёт
    между сообщениями внутри одного запуска;
  - `reg_drafts` (Phase 21, FORM-SYNC-02) — переживающий рестарт черновик ответов, общий для
    чата и Mini App;
  - `reg_started` (Phase 1) — bookkeeping «кто нажал /start», НЕ блокирует новый /start сам по
    себе, но несёт `last_step`/`started_at` для превью и COALESCE-подхватывает старый
    город/трек при повторном /start (`mark_reg_started`), если его не почистить.

`reset_stuck_registration` чистит ВСЕ ТРИ, но с оговоркой (координатор 25.09/33-SEED): для
`kind="edit"` (делегат УЖЕ подал анкету, `users`-строка есть, открыл правку) чистится ТОЛЬКО
`reg_drafts` — `users` и `reg_started` не трогаются (`reg_started` для такого делегата и так
пуст, `clear_reg_started` зовётся ровно один раз, на ПЕРВОЙ подаче, см. `services/
reg_finalize.py:648`). Для `kind="new"` (анкета никогда не была подана) чистится и
`reg_started` — иначе следующий /start подхватил бы старый город/трек через `mark_reg_started`
ON CONFLICT COALESCE, а делегат ждёт «начать анкету заново», не «продолжить с того же места».

FSM бота чистится ТОЛЬКО если текущее состояние делегата принадлежит группам анкеты
регистрации (`Registration`, `_CompositeChat`, `_LookupChat` — вложенные под-виджеты типов
`composite`/`lookup`, см. их докстринги) — состояние SOS/опроса/другого сценария не трогаем
(координатор 25.09): делегат мог зайти в анкету ПОСЛЕ того, как начал что-то другое, а мог и
уйти из анкеты в другой сценарий, не закрыв черновик, — второй случай не наш(его сброса) удел.

`storage`/`bot` — тот же приём, что `services/sos.py::close_delegate_collecting` (storage —
FSM-хранилище диспетчера, aiogram кладёт его в данные хендлера как `fsm_storage`; StorageKey
личного чата: `chat_id == user_id`)."""
from __future__ import annotations

import logging
from datetime import datetime

from database.db import (
    clear_reg_started,
    delete_reg_draft,
    get_reg_draft,
    get_reg_started_by_id,
    get_user,
    record_answer_history,
)
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

# Прикладные группы FSM, из которых состоит «анкета в процессе» в чате — см. докстринг модуля.
_REGISTRATION_STATE_GROUPS = frozenset({"Registration", "_CompositeChat", "_LookupChat"})

# Координатор 25.09: моложе этого порога активность считается «делегат может печатать прямо
# сейчас» — экран подтверждения обязан предупредить об этом ОТДЕЛЬНОЙ строкой (кнопка сброса
# при этом остаётся доступна, решение — за менеджером).
RECENT_ACTIVITY_MINUTES = 15


def _is_registration_state(state_str: str | None) -> bool:
    """Префикс до «:» — имя группы (`aiogram.fsm.state.State.state`, `f"{group}:{name}"`)."""
    if not state_str:
        return False
    return state_str.split(":", 1)[0] in _REGISTRATION_STATE_GROUPS


def _parse_msk(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


async def preview_stuck_reset(telegram_id: int) -> dict | None:
    """Только чтение — экран подтверждения. `None` — черновика нет, сбрасывать нечего (кнопка
    на карточке /find и так не должна была показаться, но вызывающий обязан быть готов к
    гонке — делегат мог сам закончить анкету между открытием карточки и тапом)."""
    draft = await get_reg_draft(telegram_id)
    if draft is None:
        return None

    started = await get_reg_started_by_id(telegram_id)
    user = await get_user(telegram_id)

    updated_at = draft.get("updated_at") or (started or {}).get("started_at")
    activity_dt = _parse_msk(updated_at)
    minutes_since = None
    if activity_dt is not None:
        minutes_since = (msk_now() - activity_dt).total_seconds() / 60

    step = draft.get("step") or (started or {}).get("last_step")

    return {
        "ok": True,
        "kind": draft.get("kind") or "new",
        "created_at": draft.get("created_at"),
        "updated_at": updated_at,
        "minutes_since_activity": minutes_since,
        "is_recent": minutes_since is not None and minutes_since < RECENT_ACTIVITY_MINUTES,
        "step": step,
        "has_submitted_application": user is not None,
    }


async def reset_stuck_registration(
    bot, storage, telegram_id: int, *, by_admin: int, notify: bool,
) -> dict:
    """Фактический сброс. `report["ok"]=False` + `report["error"]` — черновика уже нет (гонка,
    делегат сам успел закончить/бросить анкету между экраном подтверждения и тапом) — делегат
    НЕ трогается.

    `notify`/`bot` — делегатское сообщение «Анкета сброшена — начни заново: /start» (тумблер
    экрана подтверждения, дефолт «да» — 33-SEED); `bot=None` с `notify=True` тихо пропускает
    отправку (тот же fail-soft приём, что `services/revert_pending.py`)."""
    draft = await get_reg_draft(telegram_id)
    if draft is None:
        return {"ok": False, "error": "Черновика уже нет — возможно, делегат сам успел закончить или сбросить анкету."}

    kind = draft.get("kind") or "new"
    await delete_reg_draft(telegram_id)

    reg_started_cleared = False
    if kind != "edit":
        try:
            await clear_reg_started(telegram_id)
            reg_started_cleared = True
        except Exception as e:
            logger.warning(f"reset_stuck_registration: clear_reg_started({telegram_id}) failed: {e}")

    fsm_cleared = False
    if storage is not None:
        try:
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey

            ctx = FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id))
            current = await ctx.get_state()
            if _is_registration_state(current):
                await ctx.clear()
                fsm_cleared = True
        except Exception as e:
            logger.error(f"reset_stuck_registration: FSM делегата {telegram_id} не сброшен: {e}")

    try:
        await record_answer_history(
            telegram_id, [{"column": "reg_draft", "old": kind, "new": None}],
            source=f"admin:{by_admin}",
        )
    except Exception as e:
        logger.warning(f"reset_stuck_registration: record_answer_history({telegram_id}) failed: {e}")

    report: dict = {
        "ok": True, "error": None, "kind": kind,
        "reg_started_cleared": reg_started_cleared, "fsm_cleared": fsm_cleared,
        "notified": False,
    }

    if notify and bot is not None:
        try:
            from domain.cities import get_setting_typed_for_city
            from services import quiet_hours
            from services.i18n import context as _i18n_context, tr as _i18n_tr
            from services.scheduler import _now_moscow_naive
            from domain.settings.schema import SETTINGS_SCHEMA

            event_city = draft.get("event_city")
            template = await get_setting_typed_for_city("reg_reset_notify_text", event_city)
            if not template:
                template = SETTINGS_SCHEMA["reg_reset_notify_text"]["default"]
            lang, tr_map = await _i18n_context(telegram_id)
            text = _i18n_tr(template, lang, tr_map)
            sent_now = await quiet_hours.send_or_queue_text(
                _now_moscow_naive(), telegram_id, text,
                sender=lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
            )
            report["notified"] = True
            report["notified_now"] = sent_now
        except Exception as e:
            logger.warning(f"reset_stuck_registration: delegate notify({telegram_id}) failed: {e}")

    return report
