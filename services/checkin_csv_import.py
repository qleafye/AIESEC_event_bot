"""Загрузка выгрузки офлайн-сканера (D-09/D-10): отметка найденных кодов и текст отчёта.

Разбор файла — `services.checkin.find_checkin_records`; хендлер бота (выбор точки, ответ на
кнопку) — `handlers/admin_checkin.py::checkin_point_pick`. Здесь — цикл отметки и подсчёт:
каждая запись попадает ровно в одну графу отчёта, удалённая или пересозданная сессия не
выдаётся за «уже были», записи без времени скана перечислены отдельно.

День записи без времени: дата в строке есть — полдень этой даты; даты нет, а точка — вход —
полдень первого дня форума города делегата (если загрузка не в этот же день — иначе файл,
загруженный 04.10, клал вход на 04.10); иначе — время загрузки. Всё такое помечено
«примерным» и отдельной строкой отчёта.

Неоднозначная дата «03/10/2026» (оба числа ≤ 12): разбор выбирает д/м, а при AM/PM — м/д
(американская локаль). Если выбранный день не попадает в день сессии (точка — сессия) или в
окно форума города делегата (вход), а переставленный — попадает, берётся переставленный и это
отдельная строка отчёта. Ни один не попадает или окна нет — остаётся выбор разбора."""
from __future__ import annotations

import html

from cities import city_label_or_none, normalize_city
from services import checkin_forum_day
from services import timeutil
from services.checkin import ENTRY_POINT, parse_qr_payload, record_arrival, resolve_scanned_user
from services.program import scanned_outside_session_window

# Больше стольких кодов — до цикла показываем «⏳ Отмечаю…»: 500 строк — ~20 секунд.
PROGRESS_THRESHOLD = 30

LOST_FILE_TEXT = (
    "Этот файл уже обработан или потерялся после перезапуска бота — отмечать нечего. "
    "Если отметки ещё не загружены, пришлите файл заново: «✅ Отметки на форуме» → загрузка файла."
)
SESSION_GONE_TEXT = (
    "Сессию не нашёл — её удалили или пересоздали, пока выбирали точку. Ничего не отмечено, "
    "файл помню — выберите точку заново:"
)


def progress_text(n: int) -> str:
    return f"⏳ Отмечаю кодов: {n}. Это может занять до минуты — файл повторно не присылайте."


async def import_records(records: list[dict], point: str, *, session: dict | None,
                         bound_city: str | None, staff_id: int, bot, labels: dict) -> dict:
    """Отмечает каждую запись выгрузки на точке `point`. `labels` — машинный код отказа ->
    подпись отчёта (`handlers/admin_checkin.py::_DENIAL_LABELS`). Возвращает счётчики и
    список `flagged` [(подпись, разобранный QR)] для строк «Требуют внимания»."""
    res = {
        "new": 0, "duplicate": 0, "moved": 0, "outside": 0, "day_mismatch": 0,
        "other_city": 0, "point_gone": 0, "untimed": 0, "off_day": 0, "flagged": [],
        "date_only": 0, "forum_day_assumed": 0, "swapped": 0,
    }
    for rec in records:
        parsed = parse_qr_payload(rec["qr"])
        user, denial_code = await resolve_scanned_user(parsed["token"], point=point, source="csv")
        if denial_code is not None:
            res["flagged"].append((labels.get(denial_code, denial_code), parsed))
            continue
        if bound_city is not None and normalize_city(user.get("event_city")) != bound_city:
            res["other_city"] += 1
            continue
        rec = await _pick_reading(rec, user, point, session)
        if rec.get("swapped"):
            res["swapped"] += 1
        approx = rec["scanned_at"] is None
        scanned_at, untimed_kind = await _untimed_stamp(rec, user, point) if approx else (rec["scanned_at"], None)
        result = await record_arrival(
            user, point, source="csv", scanned_at=scanned_at, approx=approx,
            by_staff_id=staff_id, bot=bot,
        )
        status = result["status"]
        if status == "wrong_city":
            res["flagged"].append((result.get("reason_text", "другой город форума"), parsed))
            continue
        if status == "invalid_point":
            res["point_gone"] += 1
            continue
        if approx:
            res[untimed_kind] += 1
        if point == ENTRY_POINT and status == "new" and await checkin_forum_day.off_day_for_scan(user, result.get("scanned_at")):
            res["off_day"] += 1
        if result.get("day_mismatch"):
            res["day_mismatch"] += 1
        if session is not None and rec["scanned_at"] and scanned_outside_session_window(session, rec["scanned_at"]):
            res["outside"] += 1
        if status in ("new", "moved"):
            res[status] += 1
        else:
            res["duplicate"] += 1

    flagged = res["flagged"]
    res["not_found"] = sum(1 for reason, _row in flagged if reason == labels["no_user"])
    res["replaced"] = sum(1 for reason, _row in flagged if reason == labels["token_replaced"])
    res["wrong_city"] = sum(1 for reason, _row in flagged if reason not in labels.values())
    res["not_approved"] = len(flagged) - res["not_found"] - res["replaced"] - res["wrong_city"]
    return res


async def _fits_event(day: str, user: dict, point: str, session: dict | None) -> bool | None:
    """День «YYYY-MM-DD» — день сессии/окно форума города делегата? `None` — сверить не с чем."""
    if session is not None:
        return day == session.get("day")
    if point != ENTRY_POINT or not str(user.get("event_city") or "").strip():
        return None
    window = await checkin_forum_day.forum_window(normalize_city(user.get("event_city")))
    if window is None:
        return None
    return f"{window[0]:%Y-%m-%d}" <= day <= f"{window[1]:%Y-%m-%d}"


async def _pick_reading(rec: dict, user: dict, point: str, session: dict | None) -> dict:
    """Неоднозначная «a/b/гггг»: переставленная читка, если только она попадает в событие."""
    alt = rec.get("alt")
    field = "scanned_at" if rec.get("scanned_at") else "day" if rec.get("day") else None
    if not alt or field is None:
        return rec
    if await _fits_event(rec[field][:10], user, point, session) is False and await _fits_event(alt[:10], user, point, session):
        return {**rec, field: alt, "swapped": True}
    return rec


async def _untimed_stamp(rec: dict, user: dict, point: str) -> tuple[str | None, str]:
    """Время записи без времени скана и графа отчёта (см. докстринг модуля)."""
    if rec.get("day"):
        return f"{rec['day']} 12:00:00", "date_only"
    if point == ENTRY_POINT and str(user.get("event_city") or "").strip():
        window = await checkin_forum_day.forum_window(normalize_city(user.get("event_city")))
        if window and timeutil.msk_now().date() != window[0]:
            return f"{window[0]:%Y-%m-%d} 12:00:00", "forum_day_assumed"
    return None, "untimed"


async def report_lines(res: dict, *, row_limit: int) -> list[str]:
    """Строки отчёта о загрузке (HTML) — без строки учебных кодов и счётчика прихода, их
    добавляет хендлер."""
    lines = [
        "✅ <b>Отметки загружены</b>",
        "",
        f"Отмечено новых: {res['new']} · уже были: {res['duplicate']} · "
        f"не найдено: {res['not_found']} · не одобрены: {res['not_approved']} · "
        f"QR заменён: {res['replaced']}",
    ]
    if res["wrong_city"]:
        lines.append(f"Другой город форума: {res['wrong_city']}")
    if res["other_city"]:
        lines.append(f"Другой город: {res['other_city']} (не отмечены)")
    if res["moved"]:
        lines.append(f"Перенесено с другой сессии слота: {res['moved']}")
    if res["point_gone"]:
        lines.append(
            f"⚠️ Не отмечено: {res['point_gone']} — сессию удалили или пересоздали во время загрузки. "
            "Пришлите файл ещё раз и выберите точку заново."
        )
    if res["untimed"]:
        lines.append(
            f"⚠️ Без времени скана в файле: {res['untimed']} — отмечены временем загрузки (примерно)."
        )
    if res.get("swapped"):
        lines.append(
            f"⚠️ Дата вида «03/10/2026» прочитана наоборот (день ↔ месяц), чтобы попасть в день форума: "
            f"{res['swapped']}."
        )
    if res.get("date_only"):
        lines.append(
            f"⚠️ В файле только дата, без времени: {res['date_only']} — отмечены полднем этой даты (примерно)."
        )
    if res.get("forum_day_assumed"):
        lines.append(
            f"⚠️ Без даты и времени в файле: {res['forum_day_assumed']} — вход поставлен на первый день "
            "форума города делегата, полдень (примерно). Если сканировали в другой день — проверьте "
            "в «📓 Журнал площадки»."
        )
    if res["off_day"]:
        lines.append(
            f"⚠️ Вход не в день форума делегата: {res['off_day']} (всё равно отмечено). Если это была "
            "проба сканера — снимите лишние отметки: «📓 Журнал площадки» → «🗑 Снять отметку делегату»."
        )
    if res["outside"]:
        lines.append(f"⚠️ Время скана вне интервала сессии: {res['outside']} (всё равно отмечено)")
    if res["day_mismatch"]:
        lines.append(f"⚠️ Сессия не в день загрузки — проверьте: {res['day_mismatch']} (всё равно отмечено)")
    flagged = res["flagged"]
    shown = flagged[:row_limit]
    if shown:
        lines.append("")
        lines.append("<b>Требуют внимания:</b>")
        for reason, row in shown:
            name = html.escape(row["full_name"] or "(без имени)")
            # В QR лежит КОД города («msk») — человеку подпись, даже при выключенном модуле.
            city = html.escape(await city_label_or_none(row["city"]) or "—")
            lines.append(f"❔ {name} · {city} — {reason}")
    remaining = len(flagged) - len(shown)
    if remaining > 0:
        lines.append(f"…и ещё {remaining}")
    return lines
