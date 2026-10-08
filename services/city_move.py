"""Phase 33 (delegate-card admin actions): перевод делегата между городами мероприятия — общий
примитив (33-SEED.md, прецедент ручного переноса 23.09: Анна Потаенко spb/short/approved ->
msk «как есть, одобренной»). Единая точка правды, вызываемая и карточкой `/find`
(`handlers/admin_city_move.py`), и разовым dry-run скриптом для стенда
(`scripts/move_city_dry_run.py`).

Объём (SEED «Зависимости города»):
  - `users.event_city` через `update_user_answers` + `record_answer_history(source="admin")`
    (`users.city` — родной город делегата из анкеты, НЕ трогаем, инвариант зафиксирован в самом
    SEED). Трек (`participant_type`) НЕ трогаем НИКОГДА, ни для party, ни для short/full —
    решение координатора 25.09 отменяет прежнее авто-переключение short↔full: прецедент 23.09
    (Анна Потаенко) был «как есть, одобренной», не «пересчитать трек под новый город». Если у
    нового города нет анкеты этого трека — это только повод предупредить менеджера на экране
    подтверждения (`preview_city_move`), не повод её сменить.
  - `reg_drafts.event_city` (открытый edit-черновик иначе вернёт делегату старый город на
    финализации, `services/reg_finalize.py:296`), `reg_started.event_city` (dropout-учёт),
    неотправленные `reg_submit_digest_queue.city` / `game_submit_digest_queue.city`
    (`reg_events` — история, её эта функция никогда не трогает).
  - Строка Google-таблицы: перенос со вкладки старого города на вкладку нового — СНАЧАЛА
    добавление в новую, ПОТОМ удаление из старой (сбой между шагами оставляет данные ДВАЖДЫ,
    что безопаснее потери — см. `memory/standalone-script-sheet-traps.md` и прецедент 23.09).
    Целевая вкладка = (новый город, СТАРЫЙ трек, `_resolve_sheet_targets`) — вкладка НЕ
    создаётся НИКОГДА (ревью 🔴: `append_to_named_sheet` создаёт вкладку сама, здесь запрещено
    — пишем через `services.sheets.append_to_existing_named_sheet`). Нет такой вкладки среди
    РЕАЛЬНЫХ (`spreadsheet.worksheets()`) — падаем на главную вкладку города; нет и её — не
    пишем вовсе, отчёт называет проблему словами. Сбой листа НЕ откатывает уже применённую
    БД-часть — переезд БД важнее одной отстающей строки таблицы, которую менеджер потом сверит
    руками.

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
from database.session_enroll_db import (
    count_enrollments_for_user,
    delete_enrollments_for_user,
)

logger = logging.getLogger(__name__)

# Коды результата функций листа (services/sheets.py) — словами для отчёта менеджеру.
_SHEET_RESULT_TEXT = {
    "not_found_tab": "вкладки нет в таблице",
    "not_found_row": "строки делегата на вкладке нет",
    "duplicate": "на вкладке несколько строк делегата",
    "error": "таблица недоступна",
}

STATUS_MODE_KEEP = "keep"
STATUS_MODE_TO_MODERATION = "to_moderation"
STATUS_MODES = (STATUS_MODE_KEEP, STATUS_MODE_TO_MODERATION)


async def _target_track_mode(city_code: str) -> str:
    """'short' | 'full' — трек, «родной» городу `city_code`, тем же ключом, которым
    `reg_engine.resolve_track` решает судьбу НОВОГО делегата этого города
    (`cities.registration_mode`, per_city, default "short"). Используется ТОЛЬКО для текста
    предупреждения на экране подтверждения (`_track_supported`) — трек делегата этим не
    меняется, см. докстринг модуля."""
    mode = await get_setting_typed_for_city("registration_mode", city_code)
    return mode if mode in ("short", "full") else "short"


async def _track_supported(participant_type: str | None, new_city: str) -> bool:
    """Допускает ли `registration_mode` города `new_city` ТЕКУЩИЙ трек делегата — party всегда
    `True` (свой гейт `party_enabled`, не связанный с `registration_mode`,
    `reg_engine.resolve_track`: «party track is authoritative no matter what registration_mode
    says»). `False` — повод для предупреждения на экране подтверждения, не для смены трека."""
    if _is_party_track(participant_type):
        return True
    current_mode = "short" if _is_short_track(participant_type) else "full"
    target_mode = await _target_track_mode(new_city)
    return current_mode == target_mode


async def _resolve_sheet_targets(new_city: str, participant_type: str | None) -> dict:
    """Определяет вкладку(и) для строки делегата в НОВОМ городе строго по списку РЕАЛЬНЫХ
    вкладок таблицы (`services.sheets.list_worksheet_titles`) — вкладка НЕ создаётся никогда.
    Вызывается заново на каждом шаге (экран подтверждения и сам перевод дают СВОЙ вызов) —
    состояние листа между ними могло измениться, повторно использовать чужой результат нельзя.

    `target_tab` — обычный маршрут ТЕКУЩЕГО трека делегата в `new_city` (`_resolve_update_tab`,
    тот же, что штатный аппендер при подаче анкеты); `None` означает главный лист (всегда
    считается существующим — список вкладок для этого случая не читается).
    `fallback_tab` — главная («main»-трек) вкладка ГОРОДА, если она отличается от `target_tab`
    (иначе `None` — совпадает с ним или сама тоже главный лист); нужна, когда у трека делегата
    в новом городе своей вкладки ещё нет (напр. краткая анкета там не заводилась).
    `target_exists`/`fallback_exists` — есть ли вкладка среди РЕАЛЬНЫХ (`None`, если сама
    вкладка `None`, либо список вкладок не удалось прочитать — Sheets временно недоступен).
    `write_tab` — куда реально уйдёт запись при ЭТОМ резолве: `target_tab`, `fallback_tab`,
    `None` (главный лист) или `False` — сигнал «никуда» (обе вкладки отсутствуют). Если список
    вкладок прочитать не удалось, резолв не гадает — пробует `target_tab` как обычно (сама
    запись — fail-soft, как и весь остальной модуль)."""
    from services.reg_finalize import _resolve_update_tab

    target_tab = await _resolve_update_tab(new_city, participant_type)
    fallback_tab = await _resolve_update_tab(new_city, None)
    if fallback_tab == target_tab:
        fallback_tab = None

    if target_tab is None:
        return {
            "target_tab": None, "target_exists": None,
            "fallback_tab": None, "fallback_exists": None,
            "write_tab": None,
        }

    titles = await sheets_service.list_worksheet_titles()
    if titles is None:
        return {
            "target_tab": target_tab, "target_exists": None,
            "fallback_tab": fallback_tab, "fallback_exists": None,
            "write_tab": target_tab,
        }

    titles_set = set(titles)
    target_exists = target_tab in titles_set
    fallback_exists = (fallback_tab in titles_set) if fallback_tab is not None else None

    if target_exists:
        write_tab = target_tab
    elif fallback_tab is not None and fallback_exists:
        write_tab = fallback_tab
    else:
        write_tab = False

    return {
        "target_tab": target_tab, "target_exists": target_exists,
        "fallback_tab": fallback_tab, "fallback_exists": fallback_exists,
        "write_tab": write_tab,
    }


async def preview_city_move(
    participant_type: str | None, new_city: str, telegram_id: int | None = None,
) -> dict:
    """Публичная точка правды для экрана подтверждения (`handlers/admin_city_move.py`) —
    ничего не пишет, только читает (список вкладок листа — сетевой вызов). Трек делегата НЕ
    меняется никогда (см. докстринг модуля) — `track_supported` только сигнализирует, допускает
    ли новый город текущий трек, а `sheet` — тот же резолв, что применит сам перевод
    (`_resolve_sheet_targets`), чтобы экран не разошёлся с тем, что реально запишется."""
    return {
        "track_supported": await _track_supported(participant_type, new_city),
        "sheet": await _resolve_sheet_targets(new_city, participant_type),
        # Записи на сессии привязаны к программе старого города и при переезде пропадают.
        "enrollments": await count_enrollments_for_user(telegram_id) if telegram_id else 0,
    }


async def move_user_city(
    telegram_id: int,
    new_city: str,
    *,
    status_mode: str,
    by_admin: int,
    dry_run: bool = False,
    history_source: str = "admin",
) -> dict:
    """Переводит делегата `telegram_id` в `new_city`. Возвращает отчёт — словарь с ключами:
    `ok` (bool), `error` (человекочитаемая причина отказа или `None`), `dry_run`,
    `before`/`after` (`event_city`/`participant_type`/`status` — `participant_type` в `after`
    ВСЕГДА равен `before` — трек не меняется, см. докстринг модуля), `status_changed`,
    `db_changes` (список затронутых таблиц), `sheet` (словарь с `old_tab`/`target_tab`/
    `target_exists`/`fallback_tab`/`fallback_exists`/`write_tab`/`moved`/`error` —
    `_resolve_sheet_targets`'а форма).

    `dry_run=True` — читает и резолвит ВСЁ (вкладки, число строк на каждой), но НЕ пишет ни в
    БД, ни в лист; `ok=True` в dry_run означает «перевод возможен», не «выполнен».

    `status_mode`: `"keep"` — статус не трогаем; `"to_moderation"` — штатный возврат на
    модерацию (`database.db.revert_user_to_pending`, тот же примитив, что
    `services/reject_journal.py::return_to_moderation`), no-op если статус уже `pending`.

    `history_source` — `source` записи `reg_answer_history` (решение координатора 25.09):
    дефолт `"admin"` — ручной перевод менеджером карточкой (`handlers/admin_city_move.py`,
    `scripts/move_city_dry_run.py`, вызовы без этого параметра не меняются). Трек «региональные
    форумы → Москва» (`services/regional_noshow_move.py::apply_move`) передаёт свой маркер
    (`"system:regional_offer"`) — перенос инициирован делегатом по кнопке предложения, не
    менеджером карточкой; экран истории правок показывает `source` как есть (см. `_EDITED_
    SOURCE_LABELS.get(source, source)` в `services/applications.py`), незнакомое значение не
    роняет экран, просто печатается сырым текстом."""
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

    participant_type = user.get("participant_type")  # трек НЕ меняется, см. докстринг модуля
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
            "participant_type": participant_type,
            "status": current_status,
        },
        "status_changed": False,
        "db_changes": [],
        "sheet": {
            "old_tab": None,
            "target_tab": None, "target_exists": None,
            "fallback_tab": None, "fallback_exists": None,
            "write_tab": None,
            "moved": False, "error": None,
        },
    }

    # Резолв вкладок и рядов — читается ВСЕГДА (в т.ч. в dry_run), чтобы отчёт показывал
    # реальные имена вкладок и число строк ДО того, как что-либо применится.
    from handlers.registration import _sheet_dispatch
    from handlers.reg_schema import sheet_city_code
    from services.reg_finalize import _resolve_update_tab

    old_tab = await _resolve_update_tab(old_city, participant_type)
    sheet_targets = await _resolve_sheet_targets(new_city, participant_type)
    report["sheet"].update({
        "old_tab": old_tab,
        "target_tab": sheet_targets["target_tab"],
        "target_exists": sheet_targets["target_exists"],
        "fallback_tab": sheet_targets["fallback_tab"],
        "fallback_exists": sheet_targets["fallback_exists"],
        "write_tab": sheet_targets["write_tab"],
    })
    write_tab = sheet_targets["write_tab"]

    report["enrollments"] = await count_enrollments_for_user(telegram_id)

    if dry_run:
        report["sheet"]["old_rows_found"] = await sheets_service.find_rows_by_id(old_tab, telegram_id)
        report["sheet"]["new_rows_found"] = (
            await sheets_service.find_rows_by_id(write_tab, telegram_id) if write_tab is not False else None
        )
        return report

    # ── 1. Лист: СНАЧАЛА новая вкладка, ПОТОМ старая (см. докстринг модуля) ─────────────────
    if write_tab is False:
        missing = " и ".join(
            f"«{t}»" for t in (sheet_targets["target_tab"], sheet_targets["fallback_tab"]) if t
        )
        report["sheet"]["error"] = f"лист не обновлён: нет вкладки {missing}"
    else:
        try:
            row_fn, _ = _sheet_dispatch(participant_type)
            full_for_row = dict(user)
            full_for_row["event_city"] = new_city
            new_city_code = await sheet_city_code(new_city)
            row = await row_fn(full_for_row, new_city_code)

            append_ok = False
            if write_tab == old_tab:
                # Старая и новая вкладка совпали (напр. у обоих городов нет своих вкладок —
                # обе строки живут на главной): append дал бы дубль, а delete после него снёс
                # бы по «ожидалась 1» ничего — обновляем строку на месте. Убеждаемся ЗАРАНЕЕ,
                # что строка на этой вкладке ровно одна (а не полагаемся на кросс-поиск
                # update_row_by_id по другим вкладкам, координатор 25.09 — он для промаха
                # именованной вкладки, тут вкладка та же и строка уже найдена).
                rows_here = await sheets_service.find_rows_by_id(old_tab, telegram_id)
                if rows_here is not None and len(rows_here) == 1 and await sheets_service.update_row_by_id(
                    old_tab, telegram_id, row,
                ):
                    report["sheet"]["moved"] = True
                else:
                    report["sheet"]["error"] = (
                        f"строка делегата на вкладке «{old_tab or 'главная'}» не найдена или их "
                        "несколько — лист не обновлён, проверьте таблицу вручную"
                    )
            elif write_tab is None:
                await sheets_service.append_to_sheet(row)
                append_ok = True
            else:
                append_result = await sheets_service.append_to_existing_named_sheet(write_tab, row)
                append_ok = append_result == "ok"
                if not append_ok:
                    report["sheet"]["error"] = (
                        f"не удалось дописать строку на вкладку «{write_tab}» "
                        f"({_SHEET_RESULT_TEXT.get(append_result, 'ошибка таблицы')}) "
                        "— старая строка НЕ удалена, проверьте таблицу вручную"
                    )

            if append_ok:
                new_rows_after = await sheets_service.find_rows_by_id(write_tab, telegram_id)
                if new_rows_after is None or len(new_rows_after) != 1:
                    report["sheet"]["error"] = (
                        "после добавления на новой вкладке не ровно одна строка делегата "
                        f"({'вкладка не читается' if new_rows_after is None else len(new_rows_after)}) "
                        "— старая строка НЕ удалена, проверьте таблицу вручную"
                    )
                    logger.error(
                        "city_move: unexpected row count on write tab %r for telegram_id=%s: %r",
                        write_tab, telegram_id, new_rows_after,
                    )
                else:
                    delete_result = await sheets_service.delete_row_by_id(old_tab, telegram_id)
                    if delete_result == "ok":
                        report["sheet"]["moved"] = True
                    elif delete_result == "not_found_row":
                        # Строки на старой вкладке уже не было (повторный запуск/её там не
                        # было) — перенос фактически уже случился, это не ошибка.
                        report["sheet"]["moved"] = True
                    else:
                        report["sheet"]["error"] = (
                            "строка добавлена, но старая НЕ удалена "
                            f"({_SHEET_RESULT_TEXT.get(delete_result, 'ошибка таблицы')}) "
                            "— проверьте таблицу вручную, дубль безопаснее потери"
                        )
        except Exception as e:
            logger.error("city_move: sheet transfer failed for telegram_id=%s: %s", telegram_id, e)
            report["sheet"]["error"] = f"не удалось перенести строку в таблице: {e}"

    # ── 2. БД: users (event_city) ────────────────────────────────────────────────────────────
    patch = {"event_city": new_city}
    written = await update_user_answers(telegram_id, patch, allowed_columns=list(patch.keys()))
    if written:
        changes = [{"column": k, "old": user.get(k), "new": v} for k, v in patch.items()]
        await record_answer_history(telegram_id, changes, source=history_source)
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
    # Записи на сессии программы старого города и подтверждение расписания не переезжают.
    if await delete_enrollments_for_user(telegram_id):
        report["db_changes"].append("session_enrollments")

    # ── 4. Статус ────────────────────────────────────────────────────────────────────────────
    if status_mode == STATUS_MODE_TO_MODERATION and current_status != "pending":
        reverted = await revert_user_to_pending(telegram_id, current_status)
        if reverted:
            report["status_changed"] = True
            report["after"]["status"] = "pending"
            try:  # место амбассадора держит только одобренная заявка
                from services import amb_status
                await amb_status.on_applications_unapproved([telegram_id])
            except Exception as e:
                logger.error("city_move: on_applications_unapproved(%s) failed: %s", telegram_id, e)
            try:
                from services import amb_journal
                await amb_journal.sync_revocations([telegram_id])
            except Exception as e:
                logger.error("city_move: sync_revocations(%s) failed: %s", telegram_id, e)
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
