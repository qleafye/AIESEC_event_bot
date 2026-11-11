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
from domain.cities import (
    ALL_CITIES,
    admin_selected_city,
    cities_module_on,
    city_label,
    default_city_code,
)
from services.forum.checkin_training import msk_day
from services.forum.program import parallel_group, sessions_for_city_day

NEED_CAP = (
    "Чтобы перенести сканы с тренировки, нужно право заявок. "
    "Попросите того, кто выдаёт доступ, его включить."
)
PICK_CITY = "Сначала выберите один город в шапке админки — перенос идёт по одному городу."
EMPTY = "Переносить некого."
NOT_TODAY = "На сегодня сессий нет — выбрать некуда."
BAD_SESSION = "Эта сессия не сегодняшнего дня этого города. Откройте список заново."
OTHER_CITY = (
    "Этот город ведёт другой человек. Выберите в шапке свой город "
    "и откройте перенос снова."
)

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


def _people(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = "человек"
    elif n10 in (2, 3, 4) and n100 not in (12, 13, 14):
        word = "человека"
    else:
        word = "человек"
    return f"{n} {word}"


async def header_city(admin_id: int) -> str | None:
    """Город шапки. «Все города» — перенос не начинаем. Модуль выключен — город по умолчанию."""
    code = await admin_selected_city(admin_id)
    if code is None:
        if not await cities_module_on():
            return default_city_code()
        return None
    if code == ALL_CITIES:
        return None
    return code


async def _city_ok(admin_id: int, code: str) -> bool:
    from domain.settings.ops import per_city_visible_codes

    return code in await per_city_visible_codes(admin_id)


async def app_screen(admin_id: int) -> dict:
    code = await header_city(admin_id)
    if code is None:
        return {"pick_city": True, "text": PICK_CITY, "people": [], "sessions": []}
    if not await _city_ok(admin_id, code):
        return {"pick_city": False, "text": OTHER_CITY, "people": [], "sessions": []}
    rows, sessions = await training_move_screen(code)
    if not rows:
        return {"pick_city": False, "text": EMPTY, "people": [], "sessions": []}
    label = await city_label(code)
    people = [{"name": person_name(row)} for row in rows]
    choices = [{"id": int(session["id"]), "label": choice_label(session)} for session in sessions]
    if choices:
        text = (
            f"Перенос сканов с тренировки — {label}. "
            f"Сегодня на тренировке {_people(len(people))}. "
            "Куда перенести — выберите сессию."
        )
    else:
        text = f"Перенос сканов с тренировки — {label}. {NOT_TODAY}"
    return {"pick_city": False, "text": text, "people": people, "sessions": choices}


def _confirm_text(rows: list[dict], label: str) -> str:
    shown = [person_name(row) for row in rows[:25]]
    lines = [
        f"Эти люди ({_people(len(rows))}) будут отмечены на сессии «{label}».",
        "",
        *[f"• {name}" for name in shown],
    ]
    if len(rows) > 25:
        lines.append(f"… и ещё {len(rows) - 25}")
    lines.extend([
        "",
        "Повторное нажатие их второй раз не отметит. Статус заявки не меняется.",
    ])
    return "\n".join(lines)


async def move_decision(admin_id: int, session_id: int, *, confirm: bool) -> dict:
    code = await header_city(admin_id)
    if code is None:
        return {"ok": False, "text": PICK_CITY}
    if not await _city_ok(admin_id, code):
        return {"ok": False, "text": OTHER_CITY}
    rows, sessions = await training_move_screen(code)
    if not rows:
        return {"ok": False, "text": EMPTY}
    session = next((item for item in sessions if int(item["id"]) == int(session_id)), None)
    if session is None:
        return {"ok": False, "text": BAD_SESSION}
    label = choice_label(session)
    if not confirm:
        return {"ok": True, "need_confirm": True, "text": _confirm_text(rows, label)}
    result = await transfer_training_scans(code, int(session_id), staff_id=admin_id)
    if result["kind"] == "empty":
        return {"ok": False, "text": EMPTY}
    if result["kind"] == "bad_session":
        return {"ok": False, "text": BAD_SESSION}
    counts = result["counts"]
    return {
        "ok": True,
        "text": (
            f"Готово. Отмечены на сессии «{result['label']}».\n\n"
            f"Новые: {counts['new']}\n"
            f"Уже были на этой сессии: {counts['duplicate']}\n"
            f"Перенесены с параллельной сессии того же слота: {counts['moved']}"
        ),
    }
