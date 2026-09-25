"""Координатор 25.09 — учёт доставки решения по заявке: разбор `users.decision_delivery_*`
(колонки завёл `database.db`, пишет `services.application_effects`) на категории для «🔍 Сверить
с БД» и «📨 Переотправить решения» (оба — `services/sheet_reconcile.py`/
`handlers/admin_sheet_reconcile.py`, Phase 33).

Память auto-approve-incident-260906: 38 заявок были одобрены молча без письма делегату, и
узнать об этом раньше можно было только по логам сервера — этот модуль превращает разрыв в
факт БД, который видно в отчёте.

`summarize_deliveries` — ЧИСТАЯ функция над уже прочитанным списком `users` (второй SQL-запрос
не заводим, `sheet_reconcile.build_report` и так читает current-season пользователей ради
раскладки по вкладкам) — модуль остаётся БЕЗ импорта aiogram на верхнем уровне, тот же разрез,
что у `services/sheet_reconcile.py` (его собственный докстринг: «aiogram-free, вызывающий
хендлер строит текст/клавиатуры сам»). `resend_undelivered_decisions` физически требует `bot` —
её собственный импорт `services.application_effects`/`services.applications` ЛЕНИВЫЙ (внутри
функции), чтобы модуль, импортированный ТОЛЬКО ради `summarize_deliveries` (как это делает
`sheet_reconcile.py`), не тянул aiogram транзитивно."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Три человеческие причины сбоя, которые классифицирует
# `services.application_effects._classify_decision_delivery_error` — константы здесь, единая
# точка правды: application_effects.py импортирует их ОТСЮДА (а не задаёт своими литералами),
# чтобы отчёт/переотправка и сама запись учёта не разъехались по написанию строки.
ERROR_BLOCKED = "бот заблокирован делегатом"
ERROR_DEACTIVATED = "пользователь удалён"
ERROR_CHAT_NOT_FOUND = "чат не найден"

DECIDED_STATUSES = ("approved", "rejected")

# Пауза между одиночными отправками переотправки — тот же порядок, что у
# `services/sheet_reconcile.py::_STATUS_PAUSE_S` (переотправка тоже трогает по одному делегату
# за раз, каждый — отдельный сетевой вызов Telegram).
_RESEND_PAUSE_S = 0.3

_INFLIGHT: set[str] = set()  # тот же приём двойного тапа, что sheet_reconcile.py::_claim/_release


def _claim(key: str) -> bool:
    if key in _INFLIGHT:
        return False
    _INFLIGHT.add(key)
    return True


def _release(key: str) -> None:
    _INFLIGHT.discard(key)


def _scope_key(city_scope: tuple | None) -> str:
    return city_scope[0] if city_scope else "*"


def _item(u: dict) -> dict:
    return {
        "tid": u["telegram_id"],
        "name": u.get("full_name"),
        "username": u.get("username"),
        "city": u.get("event_city"),
        "decision": u.get("status"),
        "error": u.get("decision_delivery_error"),
    }


def summarize_deliveries(users: list[dict]) -> dict:
    """Раскладка уже прочитанного списка `users` (current-season, уже с учётом city_scope —
    вызывающий передаёт ровно тот список, что сам отфильтровал) по категориям доставки решения:

    - `failed` — попытка была, письмо не дошло (`decision_delivery_status == 'failed'`)
    - `blocked` — подмножество `failed`, где причина ИМЕННО «бот заблокирован делегатом»:
      этим переотправка не шлёт письмо повторно (заведомо бессмысленно), список — «написать
      вручную»
    - `resendable` — `failed` МИНУС `blocked`: кандидаты «📨 Переотправить решения»
    - `queued` — в очереди тихих часов, доставится сам (не считается недоставкой)
    - `unknown` — решение уже принято (`status` in approved/rejected), но
      `decision_delivery_status` пуст: либо решение ДО миграции (признака не было), либо
      какой-то путь решения ещё не проведён через `apply_decision_effects`/`mass_approve_effects`
      (см. докстринг применения в `services/application_effects.py`) — честно «неизвестно»,
      НЕ «не доставлено»."""
    decided = [u for u in users if u.get("status") in DECIDED_STATUSES]
    failed = [_item(u) for u in decided if u.get("decision_delivery_status") == "failed"]
    blocked = [it for it in failed if it["error"] == ERROR_BLOCKED]
    resendable = [it for it in failed if it["error"] != ERROR_BLOCKED]
    queued = [_item(u) for u in decided if u.get("decision_delivery_status") == "queued"]
    unknown = [_item(u) for u in decided if not u.get("decision_delivery_status")]
    return {
        "failed": failed, "blocked": blocked, "resendable": resendable,
        "queued": queued, "unknown": unknown,
    }


async def resend_undelivered_decisions(bot, *, city_scope: tuple | None = None) -> dict:
    """«📨 Переотправить решения» (задача координатора 25.09, п.4). Пересчитывает недоставленные
    СВЕЖИМ снимком (статус/доставка могли смениться между показом отчёта и тапом «Да») —
    `resendable` берётся из `summarize_deliveries` НАД ТОЛЬКО ЧТО прочитанным списком, значит
    уже доставленным/успевшим измениться делегатам письмо повторно не уходит структурно, без
    отдельной проверки. Заблокировавшим бота НЕ шлём (`blocked` — отдельный список «написать
    вручную», возвращается вызывающему для рендера).

    Отправка — ТЕМ ЖЕ кодом, что при модерации: `services.application_effects.
    apply_decision_effects(bot, tid, decision, reason, sheet=False, resend=True)` — текст решения
    строится там же, где всегда (не дублируем), лист сверка правит отдельной кнопкой «Выправить
    статусы», переотправка её не трогает. `resend=True` — координатор 25.09: для `approved` это
    шлёт ТОЛЬКО текст решения (`handlers.reg_schema.resend_approve_text`), шаг оплаты НЕ
    открывается никогда (при `payment_enabled=on` обычный `approve_user` заново нарисовал бы
    пикер тарифов уже одобренному делегату и сбросил его FSM) и бонус-файл повторно не шлётся.
    `reason` для отказа — `services.applications.last_rejection_reason` (единая точка правды
    причины ПОСЛЕДНЕГО отказа, та же, что читает делегатский экран статуса/карточка менеджера).
    429 — уже обработан ВНУТРИ `apply_decision_effects` (один ретрай,
    `services.telegram_send.send_with_retry`); здесь — только пауза между итерациями (антифлуд,
    тот же порядок, что `sheet_reconcile.py`).

    Двойной тап — тот же in-memory замок, что `sheet_reconcile.py::_claim/_release`, отдельным
    пространством ключей (`resend:*`), чтобы переотправка не блокировала «Дописать»/«Выправить»
    и наоборот."""
    key = f"resend:{_scope_key(city_scope)}"
    if not _claim(key):
        return {"ok": False, "error": "уже выполняется — подождите завершения предыдущего запуска"}
    try:
        from database.db import get_user
        from services.application_effects import apply_decision_effects
        from services.applications import last_rejection_reason
        from services.sheet_reconcile import _current_season_users  # ленивый импорт против цикла

        users = await _current_season_users(city_scope=city_scope)
        summary = summarize_deliveries(users)
        targets = summary["resendable"]
        blocked = summary["blocked"]
        if not targets:
            return {"ok": True, "done": 0, "failed": [], "total": 0, "blocked": blocked}

        done = 0
        failed: list[dict] = []
        for it in targets:
            tid = it["tid"]
            decision = it["decision"]
            reason = await last_rejection_reason(tid) if decision == "rejected" else None
            try:
                await apply_decision_effects(bot, tid, decision, reason, sheet=False, resend=True)
            except Exception as e:
                logger.error(f"resend_undelivered_decisions: сбой отправки {tid}: {e}")
                failed.append({**it, "reason": f"ошибка отправки: {e}"})
                await asyncio.sleep(_RESEND_PAUSE_S)
                continue
            # apply_decision_effects сама пишет учёт (fail-soft — может промолчать при сбое
            # записи) — читаем СВЕЖИЙ статус, а не предполагаем успех молча.
            fresh = await get_user(tid)
            if (fresh or {}).get("decision_delivery_status") == "delivered":
                done += 1
            else:
                failed.append({
                    **it, "reason": (fresh or {}).get("decision_delivery_error") or "не доставлено",
                })
            await asyncio.sleep(_RESEND_PAUSE_S)
        return {"ok": True, "done": done, "failed": failed, "total": len(targets), "blocked": blocked}
    except Exception as e:
        logger.error(f"resend_undelivered_decisions: неожиданный сбой: {e}")
        return {
            "ok": False, "crashed": True, "error": "не удалось переотправить",
            "done": 0, "failed": [], "total": 0, "blocked": [],
        }
    finally:
        _release(key)
