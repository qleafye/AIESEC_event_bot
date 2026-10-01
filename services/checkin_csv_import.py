"""Загрузка выгрузки офлайн-сканера (D-09/D-10): отметка найденных кодов и текст отчёта.

Разбор файла — `services.checkin.find_checkin_records`; хендлер бота (выбор точки, ответ на
кнопку) — `handlers/admin_checkin.py::checkin_point_pick`. Здесь — цикл отметки и подсчёт:
каждая запись попадает ровно в одну графу отчёта, удалённая или пересозданная сессия не
выдаётся за «уже были», записи без времени скана перечислены отдельно (они отмечены временем
загрузки, и если файл грузят на следующий день — вход ляжет на день загрузки)."""
from __future__ import annotations

import html

from cities import city_label_or_none, normalize_city
from services import checkin_forum_day
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
        approx = rec["scanned_at"] is None
        result = await record_arrival(
            user, point, source="csv", scanned_at=rec["scanned_at"], approx=approx,
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
            res["untimed"] += 1
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
