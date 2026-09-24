"""Идеи №31/№32 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`, B1): журнал
площадки «кто что сделал в день форума» и снятие ошибочной отметки.

Что журналится (таблица `venue_log`, `database/db.py`):
- `checkin` — живая отметка волонтёра (сканер Mini App, поиск по фамилии) со статусом
  `new`/`moved`. Повторный скан (`duplicate`) и отказы не пишутся — они ничего не меняют.
  Пишет `services.checkin.record_arrival`, одна строка на скан; id строки уходит во фронт
  сканера как ключ кнопки «↩️ Отменить».
- `csv_upload` — загрузка выгрузки офлайн-сканера, ОДНОЙ строкой со счётчиками: отметки из
  файла построчно и так лежат в `checkins` (`by_staff_id`, `source="csv"`), сотни строк
  журнала на один файл утопили бы остальное.
- `undo` — волонтёр отменил свой скан в окне отмены; `revoke` — менеджер снял отметку.
- `reissue_qr` — перевыпуск QR; `pass_once` — пропуск «разово» (кнопки пока нет —
  ждёт ответа DXP на Q-01, тип заведён заранее, чтобы журнал не пришлось менять).

Снятие отметки = удаление строки `checkins` (см. комментарий над `undo_venue_checkin` в
`database/db.py`): потребители — рассылки «пришёл/не пришёл», `checkin_not_arrived`, отзыв
о сессии (`services.session_feedback.deliver_feedback_prompts` перечитывает отмеченных в
момент срабатывания джобы, отдельная отмена джобы не нужна), счётчики — читают `checkins`
в момент работы и видят снятие сразу. Ячейка «Пришёл» в таблице очищается при снятии входа.

aiogram-free: модуль зовут и бот, и Mini App (отдельный процесс без Bot). Все записи в
журнал fail-soft — сбой журнала не имеет права сорвать саму отметку."""
from __future__ import annotations

import logging
from datetime import timedelta

from database.db import (
    get_program_session,
    get_reg_started_by_id,
    get_user,
    revoke_checkin as _db_revoke_checkin,
    undo_venue_checkin,
    venue_log_add,
)
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

ACTION_CHECKIN = "checkin"
ACTION_UNDO = "undo"
ACTION_REVOKE = "revoke"
ACTION_REISSUE_QR = "reissue_qr"
ACTION_PASS_ONCE = "pass_once"
ACTION_CSV_UPLOAD = "csv_upload"

ACTION_LABELS = {
    ACTION_CHECKIN: "✅ отметил(а)",
    ACTION_UNDO: "↩️ отменил(а) свой скан",
    ACTION_REVOKE: "🗑 снял(а) отметку",
    ACTION_REISSUE_QR: "🔄 перевыпустил(а) QR",
    ACTION_PASS_ONCE: "🎫 пропустил(а) разово",
    ACTION_CSV_UPLOAD: "📤 загрузил(а) файл сканера",
}

SOURCE_LABELS = {
    "miniapp": "сканер",
    "manual": "поиск по фамилии",
    "csv": "файл сканера",
    "auto_session": "авто со сканом сессии",
    "bot": "бот",
}

# Окно кнопки «↩️ Отменить» на плашке сканера. Сервер даёт запас на задержку сети — фронт
# прячет кнопку через UNDO_WINDOW_SECONDS, а запрос, отправленный в последнюю секунду,
# всё равно должен пройти.
UNDO_WINDOW_SECONDS = 10
_UNDO_SERVER_GRACE_SECONDS = 5

ENTRY_POINT = "entry"  # тот же литерал, что services.checkin.ENTRY_POINT (импорт дал бы цикл)
ENTRY_LABEL = "🚪 Вход"


def staff_display_name(*, first_name: str | None = None, last_name: str | None = None,
                       username: str | None = None) -> str | None:
    """Имя волонтёра для журнала — снимок из Telegram на момент действия («Анна Петрова
    (@anna)»); волонтёр может вовсе не быть делегатом, строки в `users` у него нет."""
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    uname = (username or "").lstrip("@")
    if name and uname:
        return f"{name} (@{uname})"
    return name or (f"@{uname}" if uname else None)


def staff_name_of(tg_user) -> str | None:
    """То же для объекта пользователя Telegram (aiogram `User` — утиная типизация, модуль
    aiogram-free)."""
    if tg_user is None:
        return None
    return staff_display_name(
        first_name=getattr(tg_user, "first_name", None), last_name=getattr(tg_user, "last_name", None),
        username=getattr(tg_user, "username", None),
    )


async def log_by(tg_user, action: str, **kwargs) -> int | None:
    """`log_action` от имени пользователя Telegram, нажавшего кнопку в боте, — одна строка
    врезки в хендлере (перевыпуск QR, загрузка CSV)."""
    return await log_action(action, staff_id=getattr(tg_user, "id", None), staff_name=staff_name_of(tg_user), **kwargs)


async def person_name(telegram_id: int | None) -> str:
    """Человеческое имя по id: ФИО из анкеты -> @username -> «id N»."""
    if telegram_id is None:
        return "—"
    user = await get_user(telegram_id)
    if user:
        name = user.get("full_name") or (f"@{user['username'].lstrip('@')}" if user.get("username") else None)
        if name:
            return str(name)
    started = await get_reg_started_by_id(telegram_id)
    if started and started.get("username"):
        return f"@{str(started['username']).lstrip('@')}"
    return f"id {telegram_id}"


async def point_label(point: str | None) -> str:
    if not point or point == ENTRY_POINT:
        return ENTRY_LABEL
    if point.startswith("session:"):
        try:
            session = await get_program_session(int(point.split(":", 1)[1]))
        except ValueError:
            session = None
        if session:
            from services.program import session_point_label  # ленивый: program тянет timeutil и т.п.
            return f"🎤 {session_point_label(session)}"
        return "🎤 сессия (удалена из программы)"
    return point


def _city_of(user: dict | None) -> str | None:
    import cities as _cities  # ленивый импорт — тот же приём, что services.checkin

    return _cities.normalize_city((user or {}).get("event_city"))


async def log_live_checkin(
    user: dict, point: str, *, status: str, scanned_at: str, source: str,
    by_staff_id: int | None, staff_name: str | None,
    previous: dict | None = None, auto_entry_at: str | None = None,
) -> int | None:
    """Строка журнала живой отметки (`record_arrival`, статусы new/moved). Возвращает id —
    ключ кнопки «↩️ Отменить» на плашке сканера. Fail-soft: `None` при сбое."""
    try:
        details: dict = {"scanned_at": scanned_at, "status": status, "point_label": await point_label(point)}
        if previous:
            details["previous"] = previous
            details["previous_label"] = await point_label(previous.get("point"))
        if auto_entry_at:
            details["auto_entry_at"] = auto_entry_at
        return await venue_log_add({
            "action": ACTION_CHECKIN, "staff_id": by_staff_id, "staff_name": staff_name,
            "telegram_id": user.get("telegram_id"), "city": _city_of(user), "point": point,
            "source": source, "details": details,
        })
    except Exception:
        logger.exception("venue_log: не записал отметку %s/%s", user.get("telegram_id"), point)
        return None


async def log_action(action: str, *, staff_id: int | None, staff_name: str | None,
                     telegram_id: int | None = None, city: str | None = None,
                     point: str | None = None, source: str | None = "bot",
                     details: dict | None = None) -> int | None:
    """Не-отметочное действие (перевыпуск QR, загрузка CSV, пропуск разово). Город, если не
    передан, берётся у делегата. Fail-soft."""
    try:
        if city is None and telegram_id is not None:
            city = _city_of(await get_user(telegram_id))
        return await venue_log_add({
            "action": action, "staff_id": staff_id, "staff_name": staff_name,
            "telegram_id": telegram_id, "city": city, "point": point, "source": source,
            "details": details or {},
        })
    except Exception:
        logger.exception("venue_log: не записал %s (staff=%s, tid=%s)", action, staff_id, telegram_id)
        return None


async def _clear_arrived_in_sheet(telegram_id: int) -> None:
    """Снят вход -> пустая ячейка «Пришёл» (тот же точечный апдейт, что при отметке)."""
    try:
        from services.sheets import update_arrived_in_sheet
        await update_arrived_in_sheet(telegram_id, "")
    except Exception:
        logger.exception("venue_log: не очистил «Пришёл» в таблице для %s", telegram_id)


async def undo_last_scan(staff_id: int, staff_name: str | None, log_id: int) -> str:
    """Волонтёр отменяет СВОЮ ПОСЛЕДНЮЮ отметку в окне отмены (идея №32). Все проверки — в
    `database.db.undo_venue_checkin` одной транзакцией. Возвращает код:
    `"ok"` | `"not_found"` | `"not_yours"` | `"expired"` | `"not_last"` | `"gone"`."""
    not_before = (msk_now() - timedelta(
        seconds=UNDO_WINDOW_SECONDS + _UNDO_SERVER_GRACE_SECONDS,
    )).strftime("%Y-%m-%d %H:%M:%S")
    code, event = await undo_venue_checkin(log_id, staff_id, not_before=not_before, undo_entry={
        "action": ACTION_UNDO, "staff_id": staff_id, "staff_name": staff_name,
    })
    if code == "ok" and event is not None:
        if event.get("point") == ENTRY_POINT or event["details"].get("auto_entry_at"):
            await _clear_arrived_in_sheet(event["telegram_id"])
    return code


async def revoke_mark(checkin_id: int, *, staff_id: int, staff_name: str | None) -> dict | None:
    """Менеджер снимает ОДНУ отметку делегата (идея №32). Снимается только выбранная строка:
    снятие входа не трогает отметки на сессиях (о них предупреждает экран подтверждения).
    Возвращает снятую строку `checkins` или `None`, если её уже нет."""
    from database.db import get_checkin

    row = await get_checkin(checkin_id)
    if row is None:
        return None
    user = await get_user(row["telegram_id"])
    removed = await _db_revoke_checkin(checkin_id, {
        "action": ACTION_REVOKE, "staff_id": staff_id, "staff_name": staff_name,
        "city": _city_of(user), "source": "bot",
        "details": {"point_label": await point_label(row["point"])},
    })
    if removed is not None and removed["point"] == ENTRY_POINT:
        await _clear_arrived_in_sheet(removed["telegram_id"])
    return removed


def _short_time(stamp: str | None) -> str:
    """«2026-10-03 10:15:42» -> «03.10 10:15»."""
    if not stamp or len(stamp) < 16:
        return stamp or "—"
    return f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]}"


async def describe(row: dict) -> str:
    """Строка журнала для экрана бота — без HTML (экранирует вызывающий):
    «03.10 10:15 · Анна (@anna) ✅ отметил(а) Иванов Иван — 🚪 Вход (сканер)»."""
    details = row.get("details") or {}
    who = row.get("staff_name") or await person_name(row.get("staff_id"))
    action = ACTION_LABELS.get(row.get("action"), row.get("action") or "?")
    parts = [f"{_short_time(row.get('created_at'))} · {who} {action}"]
    if row.get("telegram_id") is not None:
        parts.append(f" {await person_name(row['telegram_id'])}")
    label = details.get("point_label") or (await point_label(row["point"]) if row.get("point") else None)
    if row.get("action") == ACTION_CSV_UPLOAD:
        parts.append(
            f" — {label or ENTRY_LABEL}: новых {details.get('new', 0)}, "
            f"уже были {details.get('duplicate', 0)}, не найдено {details.get('not_found', 0)}"
        )
    elif label:
        parts.append(f" — {label}")
    if row.get("action") == ACTION_CHECKIN:
        src = SOURCE_LABELS.get(row.get("source") or "")
        if src:
            parts.append(f" ({src})")
        if details.get("previous_label"):
            parts.append(f", перенос с «{details['previous_label']}»")
        if row.get("undone_at"):
            parts.append(" · отменено")
    if row.get("action") == ACTION_REVOKE and details.get("scanned_at"):
        parts.append(f", была в {_short_time(details['scanned_at'])[-5:]}")
    return "".join(parts)
