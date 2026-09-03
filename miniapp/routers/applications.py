"""Phase 23 (23-04, APP-TINDER-02, D-04..D-08): отбор заявок Mini App — «тиндер» карточек.

Зеркалит форму `miniapp/routers/review.py` (очередь сдач геймификации — СОСЕДНЯЯ, её не
трогаем), но с ДВУМЯ отличиями, продиктованными D-06/D-07:

1. Решение веб-слой пишет в `users.status` сразу (атомарно, `services.applications.
   claim_approve/claim_reject`), но его ПОБОЧНЫЕ эффекты (приветствие/отказ делегату, строка
   в Sheets) откладываются на `UNDO_WINDOW_SECONDS` — веб-процесс НЕ имеет права слать
   приветствие сам (`approve_user` — aiogram-путь бота, единственное место, где оно
   отправляется РОВНО один раз). Эффекты уходят через `miniapp_outbox` — тот же транспорт,
   что и остальные исходящие Mini App (план 19-04).
2. Массовое одобрение (`approve_all`) необратимо и без окна отмены (D-07) — эффекты ставятся
   в очередь СРАЗУ, город-подтверждение сверяется с привязкой менеджера (веб-аналог CR-02
   `appr_all_yes` бота).

(Следующая задача плана дописывает POST-решения/undo/approve_all и сметатель просроченных
эффектов — эта задача поднимает только очередь и карточку.)

Домен целиком в `services/applications.py` (тонкие обёртки над атомарными UPDATE,
очередь/карточка/журнал отмены) — здесь нет ни одного SQL и ни одной копии правила; аватар —
`miniapp/avatars.py` (план 23-03).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from services import applications
from settings_schema import get_setting_typed

from miniapp.avatars import initials, resolve_avatar
from miniapp.deps import Principal, require_cap, require_section

router = APIRouter()


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
