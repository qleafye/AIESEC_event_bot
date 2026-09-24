"""Phase 33 (delegate-card admin actions): перевод делегата между городами мероприятия — общий
примитив (33-SEED.md, прецедент ручного переноса 23.09: Анна Потаенко spb/short/approved ->
msk «как есть, одобренной»). Единая точка правды, вызываемая и карточкой `/find`
(`handlers/admin_city_move.py`), и разовым dry-run скриптом для стенда
(`scripts/move_city_dry_run.py`).

Объём (SEED «Зависимости города»):
  - `users.event_city` + трек (`participant_type`, short/full — party НЕ трогаем никогда) через
    `update_user_answers` + `record_answer_history(source="admin")` (`users.city` — родной город
    делегата из анкеты, НЕ трогаем, инвариант зафиксирован в самом SEED).
  - `reg_drafts.event_city` (открытый edit-черновик иначе вернёт делегату старый город на
    финализации, `services/reg_finalize.py:296`), `reg_started.event_city` (dropout-учёт),
    неотправленные `reg_submit_digest_queue.city` / `game_submit_digest_queue.city`
    (`reg_events` — история, её эта функция никогда не трогает).
  - Строка Google-таблицы: перенос со вкладки старого города на вкладку нового — СНАЧАЛА
    добавление в новую, ПОТОМ удаление из старой (сбой между шагами оставляет данные ДВАЖДЫ,
    что безопаснее потери — см. `memory/standalone-script-sheet-traps.md` и прецедент 23.09).
    Сбой листа НЕ откатывает уже применённую БД-часть — переезд БД важнее одной отстающей
    строки таблицы, которую менеджер потом сверит руками (отчёт называет проблему словами).

Сообщение делегату НЕ шлётся ни при каком `status_mode` — перевод города осознанно тихое
админ-действие (SEED прямо исключил уведомление делегата из объёма фазы).

aiogram-free (тот же разрез, что `services/reject_journal.py` против `handlers/admin_moderation.py`
— модуль ничего не знает про Bot/aiogram, вызывающий хендлер строит собственные сообщения)."""
from __future__ import annotations

import logging

from cities import get_city, get_setting_typed_for_city, normalize_city
from database.db import (
    get_user,
    record_answer_history,
    revert_user_to_pending,
    update_reg_draft_city,
    update_reg_started_city,
    update_unsent_game_digest_city,
    update_unsent_reg_digest_city,
    update_user_answers,
)
from reg_engine import _is_party_track, _is_short_track
import services.sheets as sheets_service

logger = logging.getLogger(__name__)

STATUS_MODE_KEEP = "keep"
STATUS_MODE_TO_MODERATION = "to_moderation"
STATUS_MODES = (STATUS_MODE_KEEP, STATUS_MODE_TO_MODERATION)


async def _target_track_mode(city_code: str) -> str:
    """'short' | 'full' — трек, «родной» городу `city_code`, тем же ключом, которым
    `reg_engine.resolve_track` решает судьбу НОВОГО делегата этого города
    (`cities.registration_mode`, per_city, default "short"). SEED (23.09): «трек short есть
    только у СПб, у Москвы только full» — это и есть источник того утверждения."""
    mode = await get_setting_typed_for_city("registration_mode", city_code)
    return mode if mode in ("short", "full") else "short"


async def _resolve_target_track(participant_type: str | None, new_city: str) -> tuple[str | None, bool]:
    """`(итоговый participant_type, сменился ли трек)`. Пати-трек НЕ трогаем НИКОГДА — у него
    свой гейт (`party_enabled`), не связанный с `registration_mode` города
    (`reg_engine.resolve_track`: «party track is authoritative no matter what registration_mode
    says»). Полная форма хранится литералом `"full"` (не `NULL`) — так её печатает карточка
    делегата и так её ожидает `_sheet_dispatch`/`_resolve_update_tab` ниже."""
    if _is_party_track(participant_type):
        return participant_type, False
    current_mode = "short" if _is_short_track(participant_type) else "full"
    target_mode = await _target_track_mode(new_city)
    if current_mode == target_mode:
        return participant_type, False
    return target_mode, True


async def preview_track_change(participant_type: str | None, new_city: str) -> tuple[str | None, bool]:
    """Публичная обёртка `_resolve_target_track` для UI подтверждения (карточка должна
    показать «Трек: краткая → полная», ничего не записывая) — та же резолюция, что применит
    `move_user_city`."""
    return await _resolve_target_track(participant_type, new_city)


async def move_user_city(
    telegram_id: int,
    new_city: str,
    *,
    status_mode: str,
    by_admin: int,
    dry_run: bool = False,
) -> dict:
    """Переводит делегата `telegram_id` в `new_city`. Возвращает отчёт — словарь с ключами:
    `ok` (bool), `error` (человекочитаемая причина отказа или `None`), `dry_run`,
    `before`/`after` (`event_city`/`participant_type`/`status`), `track_changed`,
    `status_changed`, `db_changes` (список затронутых таблиц), `sheet` (словарь с `old_tab`/
    `new_tab`/`moved`/`error`).

    `dry_run=True` — читает и резолвит ВСЁ (трек, вкладки, число строк на каждой), но НЕ
    пишет ни в БД, ни в лист; `ok=True` в dry_run означает «перевод возможен», не «выполнен».

    `status_mode`: `"keep"` — статус не трогаем; `"to_moderation"` — штатный возврат на
    модерацию (`database.db.revert_user_to_pending`, тот же примитив, что
    `services/reject_journal.py::return_to_moderation`), no-op если статус уже `pending`."""
    if status_mode not in STATUS_MODES:
        return {"ok": False, "error": f"Неизвестный режим статуса: {status_mode!r}", "dry_run": dry_run}

    if get_city(new_city) is None:
        return {"ok": False, "error": "Такого города нет в реестре", "dry_run": dry_run}

    user = await get_user(telegram_id)
    if user is None:
        return {"ok": False, "error": "Делегат не найден в базе", "dry_run": dry_run}

    old_city = normalize_city(user.get("event_city"))
    new_city = normalize_city(new_city)  # смыкает неизвестный/пустой код с дефолтом тем же правилом
    if old_city == new_city:
        return {"ok": False, "error": "Делегат уже в этом городе", "dry_run": dry_run}

    participant_type = user.get("participant_type")
    new_participant_type, track_changed = await _resolve_target_track(participant_type, new_city)
    current_status = user.get("status")

    report: dict = {
        "ok": True,
        "error": None,
        "dry_run": dry_run,
        "telegram_id": telegram_id,
        "before": {
            "event_city": old_city,
            "participant_type": participant_type,
            "status": current_status,
        },
        "after": {
            "event_city": new_city,
            "participant_type": new_participant_type,
            "status": current_status,
        },
        "track_changed": track_changed,
        "status_changed": False,
        "db_changes": [],
        "sheet": {"old_tab": None, "new_tab": None, "moved": False, "error": None},
    }

    # Резолв вкладок и рядов — читается ВСЕГДА (в т.ч. в dry_run), чтобы отчёт показывал
    # реальные имена вкладок и число строк ДО того, как что-либо применится.
    from handlers.registration import _sheet_dispatch
    from handlers.reg_schema import sheet_city_code
    from services.reg_finalize import _resolve_update_tab

    old_tab = await _resolve_update_tab(old_city, participant_type)
    new_tab = await _resolve_update_tab(new_city, new_participant_type)
    report["sheet"]["old_tab"] = old_tab
    report["sheet"]["new_tab"] = new_tab

    if dry_run:
        old_rows = await sheets_service.find_rows_by_id(old_tab, telegram_id)
        new_rows = await sheets_service.find_rows_by_id(new_tab, telegram_id)
        report["sheet"]["old_rows_found"] = old_rows
        report["sheet"]["new_rows_found"] = new_rows
        return report

    # ── 1. Лист: СНАЧАЛА новая вкладка, ПОТОМ старая (см. докстринг модуля) ─────────────────
    try:
        row_fn, _append_fn = _sheet_dispatch(new_participant_type)
        full_for_row = dict(user)
        full_for_row["event_city"] = new_city
        full_for_row["participant_type"] = new_participant_type
        new_city_code = await sheet_city_code(new_city)
        row = await row_fn(full_for_row, new_city_code)

        if new_tab is None:
            await sheets_service.append_to_sheet(row)
        else:
            await sheets_service.append_to_named_sheet(new_tab, row)

        new_rows_after = await sheets_service.find_rows_by_id(new_tab, telegram_id)
        if new_rows_after is None or len(new_rows_after) != 1:
            report["sheet"]["error"] = (
                f"после добавления в новую вкладку найдено {new_rows_after!r} строк(и) "
                "(ожидалась ровно 1) — старая строка НЕ удалена, проверьте таблицу вручную"
            )
            logger.error(
                "city_move: unexpected row count on new tab %r for telegram_id=%s: %r",
                new_tab, telegram_id, new_rows_after,
            )
        else:
            delete_result = await sheets_service.delete_row_by_id(old_tab, telegram_id)
            if delete_result == "ok":
                report["sheet"]["moved"] = True
            elif delete_result == "not_found_row":
                # Строки на старой вкладке уже не было (повторный запуск/её там не было) —
                # перенос фактически уже случился, это не ошибка.
                report["sheet"]["moved"] = True
            else:
                report["sheet"]["error"] = (
                    f"строка добавлена в новую вкладку, но старая НЕ удалена (код {delete_result!r}) "
                    "— проверьте таблицу вручную, дубль безопаснее потери"
                )
    except Exception as e:
        logger.error("city_move: sheet transfer failed for telegram_id=%s: %s", telegram_id, e)
        report["sheet"]["error"] = f"не удалось перенести строку в таблице: {e}"

    # ── 2. БД: users (event_city + трек) ────────────────────────────────────────────────────
    patch = {"event_city": new_city}
    if track_changed:
        patch["participant_type"] = new_participant_type
    written = await update_user_answers(telegram_id, patch, allowed_columns=list(patch.keys()))
    if written:
        changes = [{"column": k, "old": user.get(k), "new": v} for k, v in patch.items()]
        await record_answer_history(telegram_id, changes, source="admin")
        report["db_changes"].append("users")

    # ── 3. БД: reg_drafts / reg_started / digest-очереди ────────────────────────────────────
    if await update_reg_draft_city(telegram_id, new_city):
        report["db_changes"].append("reg_drafts")
    if await update_reg_started_city(telegram_id, new_city):
        report["db_changes"].append("reg_started")
    if await update_unsent_reg_digest_city(telegram_id, new_city):
        report["db_changes"].append("reg_submit_digest_queue")
    if await update_unsent_game_digest_city(telegram_id, new_city):
        report["db_changes"].append("game_submit_digest_queue")

    # ── 4. Статус ────────────────────────────────────────────────────────────────────────────
    if status_mode == STATUS_MODE_TO_MODERATION and current_status != "pending":
        reverted = await revert_user_to_pending(telegram_id, current_status)
        if reverted:
            report["status_changed"] = True
            report["after"]["status"] = "pending"
        else:
            logger.warning(
                "city_move: revert_user_to_pending(%s) вернул False (статус изменился "
                "параллельно) — перевод города уже применён", telegram_id,
            )
            report["status_note"] = (
                "статус успели изменить параллельно — перевод города применён, "
                "возврат на модерацию не сработал"
            )

    return report
