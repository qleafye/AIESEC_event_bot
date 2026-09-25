"""Phase 33 (delegate-card admin actions): «↩️ Вернуть в ожидание» — одобренную/отклонённую
заявку менеджер возвращает в статус ожидания модерации. Кнопка на карточке `/find`
(`handlers/admin_revert_pending.py`), сам перевод статуса — здесь.

Штатный путь возврата — `database.db.revert_user_to_pending` (тот же примитив, что
`services/reject_journal.py::return_to_moderation` использует для возврата из журнала
автоотказов): атомарный `UPDATE ... WHERE status = ?`, второй тап/гонка статусов проигрывает
молча, не откатывая уже применённое.

Побочные эффекты возврата — «как при обычном решении наоборот»:
  - `services.scheduler.cancel_payment_reminders` — снимает T-3/T-1 напоминания об оплате,
    ТОЛЬКО если делегат был одобрен (у отклонённого напоминаний и так нет, вызов для него
    безвреден, но незачем);
  - `services.sheets.update_status_in_sheet` — та же функция, что пишет решение модератора в
    лист (`services/application_effects.py::apply_decision_effects`), лейбл — тот же, что у
    НОВОЙ заявки (`reg_labels.STATUS_LABELS["pending"]` = «Новая»), делегат в листе снова
    выглядит как неразобранная заявка;
  - `services.reg_digest.notify_application(is_new=True)` — та же дверь очереди/дайджеста, что
    у обычной подачи (D: «очередь модерации — как при обычной подаче»), делегат попадает в тот
    же счётчик «Новые заявки: N» и в тот же дайджест-таймер, если менеджер держит режим
    `reg_submit_notify_mode=digest`.

QR чек-ина «перестаёт пускать» БЕЗ отдельного кода — `services.checkin.checkin_denial` читает
`users.status` вживую на каждом скане/загрузке CSV (см. её докстринг: «Статус... строго
'approved'»), возврат в `pending` сам закрывает пропуск. Монеты/реф-баллы за одобрение НЕ
трогаются нигде в этом модуле — только читаются для превью (см. `preview_revert_pending`).

Журнал: `record_answer_history(..., source=f"admin:{admin_id}")` — тот же маркер «кто и когда»,
что у `services/city_move.py` (координатор 25.09). Колонка снимка — `"status"`: экран «🕓
История» (`handlers/admin_moderation.py::appr_history`) пропускает записи с этой колонкой
намеренно (статус уже показан отдельной строкой карточки) — сама запись при этом остаётся в
`reg_answer_history`/листе «История правок» (оба читают `.get(source, source)`, незнакомый
префикс `admin:<id>` печатается как есть, экран не ломает).

Сообщение делегату — ОПЦИОНАЛЬНО (тумблер на экране подтверждения, дефолт — не сообщать,
в отличие от `city_move`, где сообщения нет вовсе ни при каком положении): решение владельца
33-SEED — возврат на модерацию обычно инициирован самим менеджером по договорённости с
делегатом, тревожить лишний раз не нужно, но иногда нужно.

aiogram-free по импортам; `bot` приходит параметром (as-is, тот же приём, что
`services/coins_notify.py`/`services/application_effects.py`) — нужен и для
`notify_application` (маршрутизация менеджерам), и для отправки текста делегату."""
from __future__ import annotations

import html as html_module
import logging

from database.db import get_balance, get_user, record_answer_history, revert_user_to_pending
from reg_labels import STATUS_LABELS
from services.scheduler import cancel_payment_reminders
from services.sheets import update_status_in_sheet

logger = logging.getLogger(__name__)

REVERTIBLE_STATUSES = ("approved", "rejected")


async def preview_revert_pending(telegram_id: int) -> dict:
    """Только чтение — для экрана подтверждения. `coins_balance`/`referrer` — «монеты/реф-баллы
    НЕ трогаются, но видно, что останутся» (33-SEED): реферер читается по `users.referrer_id`,
    `None`, если делегат пришёл не по ссылке.

    Ревью 25.09: `payment_status`/`payment_option` — та же логика для оплаты, что у монет/
    реф-баллов выше: `revert_to_pending` НИГДЕ не трогает `payment_status` (см. докстринг
    модуля), экран подтверждения обязан явно предупредить об этом, когда оплата уже
    подтверждена (`payment_status == "paid"`), а не просто молчать."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден"}
    status = user.get("status")
    report: dict = {
        "ok": status in REVERTIBLE_STATUSES,
        "error": None if status in REVERTIBLE_STATUSES else "Заявка не одобрена и не отклонена — возвращать не с чего",
        "status": status,
        "coins_balance": await get_balance(telegram_id),
        "referrer": None,
        "payment_status": user.get("payment_status"),
        "payment_option": user.get("payment_option"),
    }
    referrer_id = user.get("referrer_id")
    if referrer_id:
        referrer = await get_user(referrer_id)
        report["referrer"] = {
            "telegram_id": referrer_id,
            "full_name": (referrer or {}).get("full_name") or str(referrer_id),
            "coins_balance": await get_balance(referrer_id),
        }
    return report


async def revert_to_pending(
    telegram_id: int, *, by_admin: int, notify: bool, bot=None, history_source: str = "admin",
) -> dict:
    """Возврат ОДНОГО делегата на модерацию. `report["ok"]=False` + `report["error"]` — отказ
    (не найден / уже на модерации / статус успели поменять параллельно), делегат НЕ трогается.

    `notify`/`bot` — делегатское сообщение (тумблер экрана подтверждения); `bot=None` с
    `notify=True` — сообщение тихо пропускается (fail-soft, тот же приём, что остальные
    уведомители при отсутствии живого бота, например фоновые скрипты)."""
    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден"}

    old_status = user.get("status")
    if old_status not in REVERTIBLE_STATUSES:
        return {"ok": False, "error": "Заявка не одобрена и не отклонена — возвращать не с чего"}

    reverted = await revert_user_to_pending(telegram_id, old_status)
    if not reverted:
        return {"ok": False, "error": "Статус успели изменить параллельно — возврат отменён"}

    report: dict = {
        "ok": True, "error": None,
        "before_status": old_status, "after_status": "pending",
        "sheet_updated": False, "notified": False,
    }

    if old_status == "approved":
        try:
            cancel_payment_reminders(telegram_id)
        except Exception as e:
            logger.warning(f"revert_to_pending: cancel_payment_reminders({telegram_id}) failed: {e}")

    await record_answer_history(
        telegram_id, [{"column": "status", "old": old_status, "new": "pending"}],
        source=f"admin:{by_admin}" if history_source == "admin" else history_source,
    )

    try:
        report["sheet_updated"] = await update_status_in_sheet(telegram_id, STATUS_LABELS["pending"])
    except Exception as e:
        logger.warning(f"revert_to_pending: update_status_in_sheet({telegram_id}) failed: {e}")

    if bot is not None:
        try:
            from services.reg_digest import notify_application
            name = html_module.escape(str(user.get("full_name") or "-"))
            username = html_module.escape(str(user.get("username") or "-"))
            admin_text = f"↩️ <b>Возвращена на модерацию</b>\n👤 {name} ({username})"
            await notify_application(
                bot, telegram_id=telegram_id, admin_text=admin_text,
                city_raw=user.get("event_city"), is_new=True, auto_rejected=False,
            )
        except Exception as e:
            logger.warning(f"revert_to_pending: notify_application({telegram_id}) failed: {e}")

    if notify and bot is not None:
        try:
            from cities import get_setting_typed_for_city
            from services import quiet_hours
            from services.i18n import context as _i18n_context, tr as _i18n_tr
            from services.scheduler import _now_moscow_naive
            from settings_schema import SETTINGS_SCHEMA

            template = await get_setting_typed_for_city("revert_pending_notify_text", user.get("event_city"))
            if not template:
                template = SETTINGS_SCHEMA["revert_pending_notify_text"]["default"]
            lang, tr_map = await _i18n_context(telegram_id)
            text = _i18n_tr(template, lang, tr_map)
            sent_now = await quiet_hours.send_or_queue_text(
                _now_moscow_naive(), telegram_id, text,
                sender=lambda: bot.send_message(telegram_id, text, parse_mode="HTML"),
            )
            report["notified"] = True
            report["notified_now"] = sent_now
        except Exception as e:
            logger.warning(f"revert_to_pending: delegate notify({telegram_id}) failed: {e}")

    return report
