"""Phase 23 (23-04, APP-TINDER-02, D-04..D-08): отбор заявок Mini App — «тиндер» карточек.

Зеркалит форму `miniapp/routers/review.py` (очередь сдач геймификации — СОСЕДНЯЯ, её не
трогаем), но с ДВУМЯ отличиями, продиктованными D-06/D-07:

1. Решение веб-слой пишет в `users.status` сразу (атомарно, `services.applications.
   claim_approve/claim_reject`), но его ПОБОЧНЫЕ эффекты (приветствие/отказ делегату, строка
   в Sheets) откладываются на `UNDO_WINDOW_SECONDS` — веб-процесс НЕ имеет права слать
   приветствие делегату сам (см. `services/application_effects.py`: welcome-хвост — aiogram-
   путь бота, единственное место, где он отправляется РОВНО один раз). Эффекты уходят через
   `miniapp_outbox` — тот же транспорт, что и остальные исходящие Mini App (план 19-04).
2. Массовое одобрение (`approve_all`) необратимо и без окна отмены (D-07) — эффекты ставятся
   в очередь СРАЗУ, город-подтверждение сверяется с привязкой менеджера (веб-аналог CR-02
   `appr_all_yes` бота).

Порядок решения по одной заявке — `POST /{tid}/approve|reject`:

    ПРОВЕРИТЬ скоуп (out_of_scope) -> claim_approve/claim_reject (атомарно, побеждает один)
    -> ПРОИГРАВШИЙ: {ok: false, "already"}, без единой записи
    -> ПОБЕДИТЕЛЬ: record_decision (эффекты отложены до effects_due_at)
       -> {ok: true, decision_id, undo_seconds}

Домен целиком в `services/applications.py` (тонкие обёртки над атомарными UPDATE,
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
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cities import ALL_CITIES, normalize_city
from services import applications
from settings_schema import get_setting_typed

from miniapp import outbox
from miniapp.avatars import initials, resolve_avatar
from miniapp.deps import Principal, require_cap, require_section

router = APIRouter()

# Те же слова, что алерт бота (_OUT_OF_SCOPE_ALERT в handlers/admin_core.py) — менеджер видел
# их в боте, дублировать текст в реестре незачем (служебная константа роутера, как у review.py).
OUT_OF_SCOPE_TEXT = "Эта заявка из другого города — переключите город."
CITY_REQUIRED_TEXT = "Подтвердите город — нажмите «Принять всех» ещё раз."
CITY_MISMATCH_TEXT = "Город изменился — подтвердите заново."
REASON_MAX = 500


# ── сметатель просроченных решений (D-06) ────────────────────────────────────────────────

async def _flush() -> None:
    await outbox.flush_application_decisions(datetime.now())


async def _delayed_flush() -> None:
    """Фоновая страховка: если менеджер закрыл приложение сразу после решения, следующий
    запрос кого-то другого может не случиться ещё долго — эта задача доносит эффекты сама
    через окно отмены + 1с, независимо от того, придёт ли ещё хоть один HTTP-запрос."""
    await asyncio.sleep(applications.UNDO_WINDOW_SECONDS + 1)
    await outbox.flush_application_decisions(datetime.now())


# ── GET /app/api/applications/next ───────────────────────────────────────────────────────

def _parse_offset(raw) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _resume_block(card_resume: dict) -> dict:
    kind = card_resume.get("kind")
    if kind == "file":
        return {"kind": "file", "url": f"/app/api/file/{card_resume['file_id']}"}
    if kind == "text":
        return {"kind": "text", "text": card_resume.get("text")}
    return {"kind": "none"}


@router.get("/app/api/applications/next")
async def applications_next(
    request: Request,
    offset: str | None = None,
    track: str | None = None,
    changed: str | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("applications")),
) -> dict:
    await _flush()
    off = _parse_offset(offset)
    # Неизвестное значение трека — не 400: чип фильтра, а не контракт (D-08).
    track_filter = track if track in applications.TRACK_FILTERS else None
    changed_only = str(changed).strip().lower() in ("1", "true")

    scope = await applications.manager_scope(p.city)
    row, total = await applications.queue_page(
        scope=scope, offset=off, track=track_filter, changed_only=changed_only,
    )
    if row is None:
        filter_active = bool(track_filter) or changed_only
        if total == 0:
            key = "miniapp_empty_applications_filtered" if filter_active else "miniapp_empty_applications"
            text = str(await get_setting_typed(key) or "")
        else:
            text = str(await get_setting_typed("miniapp_empty_applications_skipped") or "")
            text = text.replace("{count}", str(total))
        return {"empty": True, "remaining": total, "offset": off, "empty_text": text}

    card = await applications.card_payload(row)
    avatar_file_id = await resolve_avatar(request.app.state.cfg, row)
    avatar = {
        "url": f"/app/api/file/{avatar_file_id}" if avatar_file_id else None,
        "initials": initials(row.get("full_name")),
    }

    return {
        "application": {
            "telegram_id": row.get("telegram_id"),
            "full_name": row.get("full_name"),
            "username": row.get("username"),
            "city": row.get("event_city"),
            "registered_at": row.get("registration_date"),
        },
        "avatar": avatar,
        "badges": card["badges"],
        "main_fields": [{"label": label, "value": value} for label, value in card["main_fields"]],
        "extra_fields": [{"label": label, "value": value} for label, value in card["extra_fields"]],
        "resume": _resume_block(card["resume"]),
        "history": card["history"],
        "remaining": total,
        "position": off + 1,
        "offset": off,
        "filters": {
            "reject_templates": await applications.reject_reason_templates(),
            "chips": {
                "all": await get_setting_typed("miniapp_applications_filter_all"),
                "changed": await get_setting_typed("miniapp_applications_filter_changed"),
            },
        },
    }


# ── POST /app/api/applications/{tid}/approve|reject ─────────────────────────────────────

class RejectIn(BaseModel):
    reason: str = ""


async def _decide(tid: int, decision: str, reason: str | None, p: Principal) -> dict:
    if await applications.out_of_scope(p.city, tid):
        raise HTTPException(403, {"reason": "out_of_scope", "text": OUT_OF_SCOPE_TEXT})
    claim = applications.claim_approve if decision == "approved" else applications.claim_reject
    won = await claim(tid)
    if not won:
        return {"ok": False, "reason": "already"}
    decision_id = await applications.record_decision(tid, decision, reason, p.telegram_id, datetime.now())
    asyncio.create_task(_delayed_flush())
    return {"ok": True, "decision_id": decision_id, "undo_seconds": applications.UNDO_WINDOW_SECONDS}


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
