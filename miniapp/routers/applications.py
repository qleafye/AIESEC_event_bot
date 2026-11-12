"""Phase 23 (23-04, APP-TINDER-02, D-04..D-08): отбор заявок Mini App — «тиндер» карточек.

Зеркалит форму `miniapp/routers/review.py` (очередь сдач геймификации — СОСЕДНЯЯ, её не
трогаем), но с ДВУМЯ отличиями, продиктованными D-06/D-07:

1. Решение веб-слой пишет в `users.status` сразу (атомарно, `services.applications.applications.
   claim_approve/claim_reject`), но его ПОБОЧНЫЕ эффекты (приветствие/отказ делегату, строка
   в Sheets) откладываются на `UNDO_WINDOW_SECONDS` — веб-процесс НЕ имеет права слать
   приветствие делегату сам (см. `services/applications/application_effects.py`: welcome-хвост — aiogram-
   путь бота, единственное место, где он отправляется РОВНО один раз). Эффекты уходят через
   `miniapp_outbox` — тот же транспорт, что и остальные исходящие Mini App (план 19-04).
2. Массовое одобрение (`approve_all`) необратимо и без окна отмены (D-07) — эффекты ставятся
   в очередь СРАЗУ, город-подтверждение сверяется с привязкой менеджера (веб-аналог CR-02
   `appr_all_yes` бота).

Порядок решения по одной заявке — `POST /{tid}/approve|reject`:

    ПРОВЕРИТЬ скоуп (out_of_scope) -> claim_web_decision: флип статуса + строка журнала
       одной транзакцией (побеждает один, эффекты отложены до effects_due_at)
    -> ПРОИГРАВШИЙ: {ok: false, "already"}, без единой записи
    -> ПОБЕДИТЕЛЬ: {ok: true, decision_id, undo_seconds}

Домен целиком в `services/applications/applications.py` (тонкие обёртки над атомарными UPDATE,
очередь/карточка/журнал отмены) — здесь нет ни одного SQL и ни одной копии правила; аватар —
`miniapp/avatars.py` (план 23-03).

`_flush()` — дешёвый сметатель просроченных решений (один индексированный запрос по
`miniapp/outbox.py::flush_application_decisions`), зовётся (1) в начале каждого запроса
очереди/решения этого роутера и (2) фоновой задачей через `UNDO_WINDOW_SECONDS + 1` после
каждого решения — так эффекты доезжают, даже если менеджер закрыл приложение сразу после
тапа. Третий, независимый сметатель — джоба бота
`services/scheduler.py::miniapp_outbox_drain_job` (страховка на случай падения веб-процесса
внутри окна отмены). Все три идемпотентны — `claim_due_application_decisions` заявляет
каждую строку ровно одному вызову (T-23-01/T-23-20).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from domain.cities import ALL_CITIES, cities_module_on, city_label, city_label_or_none, normalize_city
from services.applications import applications
from services.comms import quiet_hours
from domain.settings.schema import get_setting_typed

from miniapp import file_tokens, outbox
from miniapp.avatars import initials, resolve_avatar
from miniapp.deps import Principal, require_cap, require_section
from miniapp.timeutil import now_msk_naive

router = APIRouter()

# Те же слова, что алерт бота (_OUT_OF_SCOPE_ALERT в handlers/settings/admin_core.py) — менеджер видел
# их в боте, дублировать текст в реестре незачем (служебная константа роутера, как у review.py).
OUT_OF_SCOPE_TEXT = "Эта заявка из другого города — переключите город."
CITY_REQUIRED_TEXT = "Подтвердите город — нажмите «Принять всех» ещё раз."
CITY_MISMATCH_TEXT = "Город изменился — подтвердите заново."
REASON_MAX = 500


# ── сметатель просроченных решений (D-06) ────────────────────────────────────────────────

async def _flush() -> None:
    await outbox.flush_application_decisions(now_msk_naive())


async def _delayed_flush() -> None:
    """Фоновая страховка: если менеджер закрыл приложение сразу после решения, следующий
    запрос кого-то другого может не случиться ещё долго — эта задача доносит эффекты сама
    через окно отмены + 1с, независимо от того, придёт ли ещё хоть один HTTP-запрос."""
    await asyncio.sleep(applications.UNDO_WINDOW_SECONDS + 1)
    await outbox.flush_application_decisions(now_msk_naive())


# ── GET /app/api/applications/next ───────────────────────────────────────────────────────

def _parse_offset(raw) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _resume_block(card_resume: dict, token: str) -> dict:
    """Приёмка 17.09 (п.2): паритет с ботом расширен на все четыре исхода развилки резюме
    (SU-04) — файл/ссылка/текст/нет, раньше карточка Mini App резюме не показывала вовсе.
    `kind == "file"`: прямая Nextcloud-ссылка (`card_resume["url"]`), если загрузка удалась,
    иначе безопасная ссылка по `file_id` через существующий токен-механизм
    (`miniapp/file_tokens.py`, тот же приём, что у аватара) — сырой `file_id` клиенту не
    отдаётся ни в одном из двух случаев.

    Приёмка 19.09 (review-260919, «Модерация» находки №2/№3): `kind == "mini"` — ветка
    мини-профиля, `mini` — `[{label, value}]` от `moderation_card.mini_resume_fields` (готовые
    подписи, D-25 «0 хардкода» на клиенте, см. `screens/applications.js::resumeNode`).
    `warning` пробрасывается для ЛЮБОГО исхода — `resume_type` задан, но данных нет ни в одном
    кармане (сигнал потери данных, не «резюме не приложено»)."""
    kind = card_resume.get("kind")
    warning = bool(card_resume.get("warning"))
    if kind == "file":
        url = card_resume.get("url") or file_tokens.file_url(card_resume["file_id"], token)
        return {"kind": "file", "url": url, "warning": warning}
    if kind == "link":
        return {"kind": "link", "url": card_resume.get("url"), "warning": warning}
    if kind == "text":
        return {"kind": "text", "text": card_resume.get("text"), "warning": warning}
    if kind == "mini":
        return {"kind": "mini", "mini": card_resume.get("mini") or [], "warning": warning}
    return {"kind": "none", "warning": warning}


@router.get("/app/api/applications/next")
async def applications_next(
    request: Request,
    offset: str | None = None,
    track: str | None = None,
    changed: str | None = None,
    flagged: str | None = None,
    messenger: str | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    off = _parse_offset(offset)
    # Неизвестное значение трека — не 400: чип фильтра, а не контракт (D-08).
    track_filter = track if track in applications.TRACK_FILTERS else None
    changed_only = str(changed).strip().lower() in ("1", "true")
    # Phase 31 (31-09, D-20): «только помеченные правилами» — тот же приём разбора, что у
    # `changed` (T-31-09-02: закрытый список "1"/"true", всё остальное — выключенный фильтр,
    # чип, а не контракт).
    flagged_only = str(flagged).strip().lower() in ("1", "true")
    # Чип, не контракт: «все» и неизвестное слово не сужают очередь.
    messenger_filter = messenger if messenger in ("telegram", "vk") else None

    scope = await applications.manager_scope(p.city)
    row, total = await applications.queue_page(
        scope=scope, offset=off, track=track_filter, changed_only=changed_only,
        flagged_only=flagged_only, messenger=messenger_filter,
    )
    if row is None:
        filter_active = bool(track_filter) or changed_only or flagged_only or bool(messenger_filter)
        if total == 0:
            key = "miniapp_empty_applications_filtered" if filter_active else "miniapp_empty_applications"
            text = str(await get_setting_typed(key) or "")
        else:
            text = str(await get_setting_typed("miniapp_empty_applications_skipped") or "")
            text = text.replace("{count}", str(total))
        return {"empty": True, "remaining": total, "offset": off, "empty_text": text}

    card = await applications.card_payload(row)
    duplicate = await applications.duplicate_hint(row, city=p.city)
    if duplicate:
        card["badges"].append({"kind": "duplicate", "text": duplicate["warning"]})
    cfg = request.app.state.cfg
    # Токен МЕНЕДЖЕРА (p.telegram_id), не заявителя: маршрут файла проверит can_read_file под
    # менеджером — городской скоуп сохраняется, право проверяется и здесь (очередь заявок уже
    # отфильтрована скоупом), и в маршруте файла (quick 260910-w3j, требование 3).
    file_token = file_tokens.mint_file_token(cfg.bot_token, p.telegram_id)
    avatar_file_id = await resolve_avatar(cfg, row)
    avatar = {
        "url": file_tokens.file_url(avatar_file_id, file_token) if avatar_file_id else None,
        "initials": initials(row.get("full_name")),
    }

    # Plan 23-06 (Known Stub #2 из 23-05): человекочитаемый город менеджера для «Принять всех N»
    # (D-07 хотел «число + город»). Тот же приём, что review.py::applications_next делает для
    # города делегата (city_label(normalize_city(...))), но здесь — привязка МЕНЕДЖЕРА
    # (`p.city`), а не карточки; `None` привязки = «все города» (ALL_CITIES), модуль выключен —
    # поля вовсе нет (клиент не рисует вторую строку подтверждения).
    manager_city_label = None
    if await cities_module_on():
        manager_city_label = await city_label(normalize_city(p.city) if p.city is not None else ALL_CITIES)

    waitlist_on = await get_setting_typed("apps_waitlist_enabled") == "on"
    return {
        "application": {
            "telegram_id": row.get("telegram_id"),
            "full_name": row.get("full_name"),
            "username": row.get("username"),
            "city": row.get("event_city"),
            # Подпись для человека (JS показывает её, `city` — код для логики) — всегда, даже
            # при выключенном модуле городов: иначе в строке карточки «msk» (CLAUDE.md).
            "city_label": await city_label_or_none(row.get("event_city")),
            "registered_at": row.get("registration_date"),
            "messenger": applications.messenger_kind(row),
            "messenger_label": applications.MESSENGER_LABELS[applications.messenger_kind(row)],
            # Номер нужен только кнопке решения, на карточку его не печатаем.
            "vk_id": row.get("vk_id") if row.get("telegram_id") is None else None,
        },
        "avatar": avatar,
        "badges": card["badges"],
        "main_fields": [{"label": label, "value": value} for label, value in card["main_fields"]],
        "extra_fields": [{"label": label, "value": value} for label, value in card["extra_fields"]],
        "resume": _resume_block(card["resume"], file_token),
        "history": card["history"],
        "remaining": total,
        "position": off + 1,
        "offset": off,
        "city_label": manager_city_label,
        "filters": {
            "reject_templates": await applications.reject_reason_templates(),
            "chips": {
                "all": await get_setting_typed("miniapp_applications_filter_all"),
                # D-08 (найдено планом 23-05, задача 3): три подписи трека — те же коды, что
                # `applications.TRACK_FILTERS` (full/party/short), но текст менеджер видит
                # только здесь, не в query-параметре.
                "full": await get_setting_typed("miniapp_applications_filter_full"),
                "party": await get_setting_typed("miniapp_applications_filter_party"),
                "short": await get_setting_typed("miniapp_applications_filter_short"),
                "changed": await get_setting_typed("miniapp_applications_filter_changed"),
                "flagged": await get_setting_typed("miniapp_applications_filter_flagged"),
                "messenger_telegram": "Telegram",
                "messenger_vk": "ВК",
                "messenger_all": "Все мессенджеры",
            },
        },
        "waitlist_enabled": waitlist_on,
        "waitlist_button": "📋 В лист ожидания" if waitlist_on else None,
        "duplicate": duplicate,
    }


# ── POST /app/api/applications/{tid}/approve|reject ─────────────────────────────────────

class RejectIn(BaseModel):
    reason: str = ""


async def _decide(tid: int, decision: str, reason: str | None, p: Principal) -> dict:
    if await applications.out_of_scope(p.city, tid):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    # Флип статуса и строка окна отмены — одной транзакцией: иначе между двумя коммитами
    # одобрение видно без окна отмены и засчитывается в ступени амбассадора.
    decision_id = await applications.claim_web_decision(
        tid, decision, reason, p.telegram_id, now_msk_naive(),
    )
    if decision_id is None:
        return {"ok": False, "reason": "already"}
    asyncio.create_task(_delayed_flush())
    # Quick 260904-dq1: приписка «делегат узнает в 09:00» для тоста менеджера. Сама проверка
    # окна при отправке живёт в apply_decision_effects (вызовется позже, после окна отмены,
    # через application_decided) — здесь только снимок «попадёт ли в тихие часы сейчас».
    quiet_notice = await quiet_hours.manager_notice(now_msk_naive(), tid)
    return {
        "ok": True, "decision_id": decision_id, "undo_seconds": applications.UNDO_WINDOW_SECONDS,
        "quiet_notice": quiet_notice,
    }


@router.post("/app/api/applications/{tid}/approve")
async def applications_approve(
    tid: int,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    return await _decide(tid, "approved", None, p)


@router.post("/app/api/applications/{tid}/reject")
async def applications_reject(
    tid: int,
    body: RejectIn | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    # D-05: причина по желанию — пустая строка допустима, обрезаем длинную по лимиту шторки.
    reason = (body.reason if body is not None else "").strip()[:REASON_MAX] or None
    return await _decide(tid, "rejected", reason, p)


_WAITLIST_TOAST = "Заявка в листе ожидания. Делегат увидит: Лист ожидания."
_WAITLIST_OFF_TEXT = (
    "Лист ожидания выключен. Включите «📋 Лист ожидания» в разделе «📋 Заявки»."
)


@router.post("/app/api/applications/{tid}/waitlist")
async def applications_waitlist(
    tid: int,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    """Та же одна транзакция, что у одобрения, но без окна причины отказа."""
    await _flush()
    if await applications.out_of_scope(p.city, tid):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    if await get_setting_typed("apps_waitlist_enabled") != "on":
        raise HTTPException(403, {"reason": "waitlist_off", "text": _WAITLIST_OFF_TEXT})
    decision_id = await applications.claim_web_decision(
        tid, "waitlist", None, p.telegram_id, now_msk_naive(),
    )
    if decision_id is None:
        return {"ok": False, "reason": "already"}
    asyncio.create_task(_delayed_flush())
    quiet_notice = await quiet_hours.manager_notice(now_msk_naive(), tid)
    return {
        "ok": True,
        "decision_id": decision_id,
        "undo_seconds": applications.UNDO_WINDOW_SECONDS,
        "quiet_notice": quiet_notice,
        "toast": _WAITLIST_TOAST,
    }


_VK_MISSING_TEXT = "Такой заявки во ВК нет. Обновите очередь."
_VK_LINKED_TEXT = "Эта заявка привязана к Telegram. Решите её в общей очереди."


async def _decide_vk(vk_id: int, decision: str, reason: str | None, p: Principal) -> dict:
    """Карточка только из ВК. Окна «Отменить» нет — журналу нужен Telegram."""
    kind, user = await applications.vk_decision_target(vk_id)
    if kind == "missing":
        raise HTTPException(404, {"reason": "not_found", "text": _VK_MISSING_TEXT})
    if kind == "linked":
        raise HTTPException(404, {"reason": "linked", "text": _VK_LINKED_TEXT})
    if await applications.vk_row_out_of_scope(p.city, user):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    if decision == "waitlist" and await get_setting_typed("apps_waitlist_enabled") != "on":
        raise HTTPException(403, {"reason": "waitlist_off", "text": _WAITLIST_OFF_TEXT})
    ok = await applications.claim_vk_decision(vk_id, decision, reason)
    if not ok:
        return {"ok": False, "reason": "already"}
    if decision == "waitlist":
        toast = _WAITLIST_TOAST
    elif decision == "approved":
        toast = str(await get_setting_typed("miniapp_applications_approved_toast") or "")
    else:
        toast = str(await get_setting_typed("miniapp_applications_rejected_toast") or "")
    return {"ok": True, "undo_seconds": 0, "toast": toast}


@router.post("/app/api/applications/vk/{vk_id}/approve")
async def applications_vk_approve(
    vk_id: int,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    return await _decide_vk(vk_id, "approved", None, p)


@router.post("/app/api/applications/vk/{vk_id}/reject")
async def applications_vk_reject(
    vk_id: int,
    body: RejectIn | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    reason = (body.reason if body is not None else "").strip()[:REASON_MAX] or None
    return await _decide_vk(vk_id, "rejected", reason, p)


@router.post("/app/api/applications/vk/{vk_id}/waitlist")
async def applications_vk_waitlist(
    vk_id: int,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    return await _decide_vk(vk_id, "waitlist", None, p)


class KeepIn(BaseModel):
    telegram_id: int | None = None
    vk_id: int | None = None


@router.post("/app/api/applications/keep")
async def applications_keep(
    body: KeepIn,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    """Оставить эту заявку: другие с тем же именем и телефоном в очереди отклоняются."""
    await _flush()
    user = await applications.load_kept_user(body.telegram_id, body.vk_id)
    if user is None:
        raise HTTPException(404, {"reason": "not_found", "text": "Заявка не найдена. Обновите очередь."})
    if user.get("telegram_id") is not None:
        if await applications.out_of_scope(p.city, int(user["telegram_id"])):
            raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    elif await applications.vk_row_out_of_scope(p.city, user):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    result = await applications.reject_other_applications(
        user, city=p.city, by=p.telegram_id, now=now_msk_naive(),
    )
    decision_ids = result["decision_ids"]
    n = len(decision_ids) + result["vk_rejected"]
    if decision_ids:
        asyncio.create_task(_delayed_flush())
    if n == 0:
        return {
            "ok": True,
            "undo_seconds": 0,
            "text": "В очереди нет другой заявки с тем же именем и телефоном. Ничего не отклонено.",
        }
    if len(decision_ids) == 1 and result["vk_rejected"] == 0:
        toast = str(await get_setting_typed("miniapp_applications_rejected_toast") or "")
        return {
            "ok": True,
            "decision_id": decision_ids[0],
            "undo_seconds": applications.UNDO_WINDOW_SECONDS,
            "toast": toast,
        }
    return {
        "ok": True,
        "undo_seconds": 0,
        "text": f"Отклонено других заявок: {n}. Им уйдёт отказ. Эта заявка осталась.",
    }


# ── POST /app/api/applications/undo ──────────────────────────────────────────────────────

class UndoIn(BaseModel):
    decision_id: int


@router.post("/app/api/applications/undo")
async def applications_undo(
    body: UndoIn,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    decision = await applications.get_decision(body.decision_id)
    # T-23-17: чужое решение отменить нельзя — сверяем decided_by ДО единой попытки claim,
    # иначе даже отвергнутый ответ уже необратимо украл бы claim у настоящего владельца.
    if decision is None or decision["decided_by"] != p.telegram_id:
        raise HTTPException(404, {"reason": "not_found"})
    if decision["effects_sent_at"] is not None or decision["undone_at"] is not None:
        return {"ok": False, "reason": "too_late"}
    result = await applications.undo_decision(body.decision_id)
    if not result.get("ok"):
        # Гонка: сметатель забрал строку между нашим чтением и claim — тот же смысл, что и
        # «окно уже вышло».
        return {"ok": False, "reason": "too_late"}
    return {"ok": True}


# ── POST /app/api/applications/approve_all ───────────────────────────────────────────────

class ApproveAllIn(BaseModel):
    city: str | None = None


@router.post("/app/api/applications/approve_all")
async def applications_approve_all(
    body: ApproveAllIn,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    raw_city = (body.city or "").strip()
    if not raw_city:
        raise HTTPException(400, {"reason": "city_required", "text": CITY_REQUIRED_TEXT})
    # CR-02 веб-аналог: подтверждённый город обязан совпадать с ТЕКУЩЕЙ привязкой менеджера
    # (p.city), а не с тем, что было в момент показа диалога на клиенте.
    if p.city is None:
        matches = raw_city == ALL_CITIES
    else:
        matches = normalize_city(raw_city) == normalize_city(p.city)
    if not matches:
        raise HTTPException(403, {"reason": "city_mismatch", "text": CITY_MISMATCH_TEXT})

    scope = await applications.manager_scope(p.city)
    ids = await applications.claim_approve_all(scope)
    if not ids:
        return {"ok": False, "reason": "already"}
    # D-07: массовое одобрение необратимо — эффекты в очередь СРАЗУ, без окна отмены/журнала.
    await outbox.enqueue("application_mass_approved", {"ids": ids})
    return {"ok": True, "count": len(ids)}
