"""Координатор 25.09 — учёт доставки решения по заявке: разбор `users.decision_delivery_*`
(колонки завёл `database.db`, пишет `services.application_effects`) на категории для «🔍 Сверить
с БД» и «📨 Переотправить решения» (оба — `services/sheet_reconcile.py`/
`handlers/sheets/admin_sheet_reconcile.py`, Phase 33).

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
    шлёт ТОЛЬКО текст решения (`handlers.reg.reg_schema.resend_approve_text`), шаг оплаты НЕ
    открывается никогда (при `payment_enabled=on` обычный `approve_user` заново нарисовал бы
    пикер тарифов уже одобренному делегату и сбросил его FSM) и бонус-файл повторно не шлётся.
    `reason` для отказа — `services.applications.last_rejection_reason` (единая точка правды
    причины ПОСЛЕДНЕГО отказа, та же, что читает делегатский экран статуса/карточка менеджера).
    429 — уже обработан ВНУТРИ `apply_decision_effects` (один ретрай,
    `services.infra.telegram_send.send_with_retry`); здесь — только пауза между итерациями (антифлуд,
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


# ── Одиночная переотправка из карточки /find ────────────────────────────────────────────────

def failure_line(user: dict | None) -> str:
    """Строка карточки /find «Письмо о решении не дошло: <причина>» — пустая строка, если
    решения нет или сбоя не зафиксировано. Причину пишет `application_effects.
    _classify_decision_delivery_error` (уже человеческая), сюда она приходит как есть."""
    import html as _html
    u = user or {}
    if u.get("status") not in DECIDED_STATUSES or u.get("decision_delivery_status") != "failed":
        return ""
    reason = _html.escape(str(u.get("decision_delivery_error") or "причина неизвестна"))
    return f"\n\n⚠️ Письмо о решении не дошло: {reason}"


_PREVIEW_LEN = 80


async def preview_decision_text(user: dict) -> str:
    """Начало текста, который получит делегат (для экрана подтверждения). Те же функции, что
    собирают реальное письмо: одобрение — `handlers.reg.reg_schema._approve_text_for` (трек/город),
    отказ — `services.applications.reject_message_text` с последней причиной. Теги убираются,
    чтобы обрезка не оставила незакрытый тег."""
    import html as _html
    import re

    tid = user["telegram_id"]
    if user.get("status") == "rejected":
        from services.applications import last_rejection_reason, reject_message_text
        from services.i18n.i18n import context as _i18n_context
        lang, tr_map = await _i18n_context(tid)
        raw = await reject_message_text(await last_rejection_reason(tid), lang, tr_map)
    else:
        from domain.cities import cities_module_on, normalize_city
        from handlers.reg.reg_schema import _approve_text_for
        city_code = normalize_city(user.get("event_city")) if await cities_module_on() else None
        raw = await _approve_text_for(user.get("participant_type") or "full", city_code)
    plain = _html.unescape(re.sub(r"<[^>]+>", "", raw or ""))
    plain = " ".join(plain.split())
    if len(plain) > _PREVIEW_LEN:
        plain = plain[:_PREVIEW_LEN].rstrip() + "…"
    return plain


async def resend_one_decision(bot, telegram_id: int) -> dict:
    """Переотправка решения ОДНОМУ делегату (кнопка в карточке /find). Тот же путь, что у
    массовой: `apply_decision_effects(sheet=False, resend=True)` — тот же текст, та же запись
    `users.decision_delivery_*`; результат читаем из свежей строки, а не предполагаем успех.

    `{"ok": True, "delivered": True}` — дошло; `{"ok": True, "queued": True}` — делегат в тихих
    часах, письмо уйдёт утром; `{"ok": True, "delivered": False, "error": ...}` — не дошло;
    `{"ok": False, "error": ...}` — отправку не начинали (нет решения / уже идёт)."""
    key = f"resend1:{telegram_id}"
    if not _claim(key):
        return {"ok": False, "error": "уже отправляется — подождите несколько секунд"}
    try:
        from database.db import get_user
        from services.application_effects import apply_decision_effects
        from services.applications import last_rejection_reason

        user = await get_user(telegram_id)
        decision = (user or {}).get("status")
        if decision not in DECIDED_STATUSES:
            return {"ok": False, "error": "по заявке ещё нет решения"}
        reason = await last_rejection_reason(telegram_id) if decision == "rejected" else None
        try:
            await apply_decision_effects(bot, telegram_id, decision, reason, sheet=False, resend=True)
        except Exception as e:
            logger.error(f"resend_one_decision: сбой отправки {telegram_id}: {e}")
            return {"ok": True, "delivered": False, "error": f"ошибка отправки: {e}"}
        fresh = await get_user(telegram_id) or {}
        status = fresh.get("decision_delivery_status")
        if status == "delivered":
            return {"ok": True, "delivered": True}
        if status == "queued":
            return {"ok": True, "delivered": False, "queued": True}
        return {"ok": True, "delivered": False, "error": fresh.get("decision_delivery_error") or "не доставлено"}
    finally:
        _release(key)
