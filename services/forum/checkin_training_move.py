"""Перенос журнала «Тренировка» на сессию сегодняшнего дня.

Список и подтверждение — в `handlers/forum/admin_checkin_training_move.py`.
Запись — только `database.db.record_session_checkin`: статус заявки не трогаем,
повтор той же сессии не создаёт вторую строку. После переноса строки лога за
этот день снимаются, чтобы второй заход не прошёл по тем же людям ещё раз.
"""
from __future__ import annotations

from database.db import (
    delete_training_scans,
    list_training_scans,
    record_session_checkin,
)
from services.forum.checkin_training import msk_day
from services.forum.program import parallel_group, sessions_for_city_day

_LABEL_LIMIT = 60


def choice_label(session: dict) -> str:
    """Кнопка сессии: зал и слот, без id. Название — чтобы параллельные не слиплись."""
    hall = (session.get("hall_name") or "").strip() or "Без зала"
    slot = f"{session['start_time']}–{session['end_time']}"
    title = (session.get("title") or "").strip()
    text = f"{hall} · {slot}"
    if title:
        text = f"{text} · {title}"
    if len(text) > _LABEL_LIMIT:
        text = text[: _LABEL_LIMIT - 1].rstrip() + "…"
    return text


def person_name(row: dict) -> str:
    name = (row.get("full_name") or "").strip()
    return name or "Без имени"


async def training_move_screen(city: str) -> tuple[list[dict], list[dict]]:
    """Люди сегодняшнего журнала города и сессии этого же дня."""
    day = msk_day()
    return await list_training_scans(day, city), await sessions_for_city_day(city, day)


async def transfer_training_scans(city: str, session_id: int, *, staff_id: int | None) -> dict:
    """Перенести сегодняшний журнал города на сессию. Пустой журнал или чужая/не сегодняшняя
    сессия — ничего не пишем."""
    day = msk_day()
    rows = await list_training_scans(day, city)
    if not rows:
        return {"kind": "empty"}
    sessions = await sessions_for_city_day(city, day)
    session = next((s for s in sessions if s["id"] == session_id), None)
    if session is None:
        return {"kind": "bad_session"}
    others = [s["id"] for s in parallel_group(session, sessions) if s["id"] != session_id]
    counts = {"new": 0, "duplicate": 0, "moved": 0}
    done: list[int] = []
    for row in rows:
        status, _ts, _prev = await record_session_checkin(
            int(row["telegram_id"]), session_id, others,
            source="training", scanned_at=row.get("scanned_at"), by_staff_id=staff_id,
        )
        counts[status if status in counts else "new"] += 1
        done.append(int(row["telegram_id"]))
    await delete_training_scans(day, done)
    return {"kind": "ok", "counts": counts, "label": choice_label(session)}
