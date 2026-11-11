"""Отбор «Позвать резерв»: живой автоотказ города шапки. Чат и Mini App зовут эти
функции. Статус заявки здесь не меняется. Отправку письма делает процесс бота.
"""
from __future__ import annotations

from database import db
from domain.cities import ALL_CITIES, admin_selected_city, city_label, city_scope
from services.infra.timeutil import msk_now

READY_LETTER = "{name}, вас зовут на площадку из резерва. Вход — только {date}. Город: {city}."
PICK_CITY = (
    "📮 <b>Позвать резерв</b>\n\n"
    "Сначала выберите конкретный город в шапке. "
    "Пока стоят «Все города», никого не зовём."
)
PICK_PLAIN = (
    "Сначала выберите конкретный город в шапке бота. "
    "Пока стоят «Все города», никого не зовём."
)
EMPTY_LIST = "Живых автоотказов по этому городу нет."
FILE_NOTE = "Файл к письму по-прежнему прикладывается в чате. Отсюда уходит письмо и QR."
NEED_CAP = (
    "Чтобы звать резерв, нужно право разбирать заявки. "
    "Попросите того, кто выдаёт доступ, его включить."
)


def confirm_line(n: int, when: str) -> str:
    return f"Позовём {n}. Статус „одобрен“ не появится. Вход — только {when}."


def today_label() -> str:
    return msk_now().strftime("%d.%m.%Y")


def letter_body(template: str | None, *, name: str, date: str, city: str) -> str:
    import html

    raw = (template or "").strip() or READY_LETTER
    who = html.escape(name.strip()) if (name or "").strip() else "друг"
    return (
        raw.replace("{name}", who)
        .replace("{date}", html.escape(date))
        .replace("{city}", html.escape(city))
    )


def report_text(*, new: int, already: int, failed: int, blocked: int, qr_miss: list[str]) -> str:
    import html

    lines = [
        f"Новым позвали {new}.",
        f"Уже были приглашены сегодня {already}.",
        f"Не доставлено {failed}.",
    ]
    if blocked:
        lines.append("Делегат заблокировал бота — сообщение не дошло.")
    if qr_miss:
        names = ", ".join(html.escape(item) for item in qr_miss)
        lines.append(f"QR не приложился: {names}.")
    return "\n".join(lines)


def user_in_city_scope(event_city: str | None, scope) -> bool:
    if scope is None:
        return True
    code, exclude = scope
    if not exclude:
        return event_city == code
    return event_city is None or event_city not in exclude


def _rule_ids(raw) -> list[int]:
    import json

    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


async def rows_for_header(admin_id: int, rule_id: int | None = None) -> list[dict] | None:
    """None — шапка «Все города»: выборки нет и слать нечего."""
    code = await admin_selected_city(admin_id)
    if code == ALL_CITIES:
        return None
    return await db.list_live_reserve_rows(city_scope=city_scope(code), rule_id=rule_id)


async def ids_for_action(admin_id: int, *, rule_id: int | None, mode: str,
                          selected: list[int]) -> list[int] | None:
    rows = await rows_for_header(admin_id, rule_id)
    if rows is None:
        return None
    live = [int(row["telegram_id"]) for row in rows]
    if mode == "all":
        return live
    live_set = set(live)
    return [int(item) for item in selected if int(item) in live_set]


async def _city_phrase(code: str | None) -> str:
    if not code or code == ALL_CITIES:
        return "площадка"
    return await city_label(code)


def _person_name(row: dict) -> str:
    return " ".join((row.get("full_name") or "").split()) or "без имени"


async def screen(admin_id: int) -> dict:
    """Список для экрана. rule_id не принимаем: фильтр одного правила остаётся в чате."""
    code = await admin_selected_city(admin_id)
    when = today_label()
    if code == ALL_CITIES:
        return {"pick_city": True, "text": PICK_PLAIN, "people": [], "when": when}
    rows = await rows_for_header(admin_id, None) or []
    people = [{"id": int(row["telegram_id"]), "name": _person_name(row)} for row in rows]
    label = await city_label(code) if code else ""
    if people:
        text = f"Город: {label}. В списке {len(people)}. Вход — только {when}."
    else:
        text = EMPTY_LIST
    return {
        "pick_city": False,
        "text": text,
        "people": people,
        "when": when,
        "file_note": FILE_NOTE,
    }


def _mode(raw: str | None) -> str:
    return "all" if raw == "all" else "selected"


async def invite_decision(admin_id: int, *, mode: str, selected: list[int],
                           confirm: bool) -> dict:
    """Подтверждение или постановка в очередь. Письмо отсюда не шлётся."""
    code = await admin_selected_city(admin_id)
    if code == ALL_CITIES:
        return {"ok": False, "text": PICK_PLAIN}
    ids = await ids_for_action(
        admin_id, rule_id=None, mode=_mode(mode), selected=list(selected or []),
    )
    if not ids:
        return {"ok": False, "text": "Некого звать по этому списку."}
    when = today_label()
    if not confirm:
        return {
            "ok": True,
            "need_confirm": True,
            "count": len(ids),
            "text": f"{confirm_line(len(ids), when)}\n\n{FILE_NOTE}",
        }
    return {
        "ok": True,
        "count": len(ids),
        "text": "Письмо поставлено в очередь. Статус «одобрен» не появится.",
        "enqueue": {
            "admin_id": int(admin_id),
            "mode": _mode(mode),
            "selected": ids,
        },
    }


__all__ = [
    "EMPTY_LIST",
    "FILE_NOTE",
    "NEED_CAP",
    "PICK_CITY",
    "PICK_PLAIN",
    "READY_LETTER",
    "confirm_line",
    "ids_for_action",
    "invite_decision",
    "letter_body",
    "report_text",
    "rows_for_header",
    "screen",
    "today_label",
    "user_in_city_scope",
]
