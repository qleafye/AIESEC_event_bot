"""Вкладка «Журнал баллов»: каждая строка `coins` — отдельной строкой таблицы.

Зачем: менеджер видит «кто, кому, сколько, откуда, за что» по каждому движению баллов, не
открывая бота; сумма колонки «Баллы» по человеку равна его балансу и «Итого баллов» во вкладке
«Гейма», потому что в журнале ВСЕ строки `coins`, а не только часть источников.

Новые сверху (порядок задаёт запрос `list_coins_log`): вкладку открывают проверить свежее
(«мне вчера в чате начислили?»), без прокрутки к строке 3000. «Баллы» — число со знаком, а не
текст «+5»: число суммируется и фильтруется в Sheets.

Модуль чистый: только stdlib, без БД и без handlers.*.
"""
from __future__ import annotations

from datetime import datetime

COINS_LOG_HEADERS = [
    "Когда", "Кому", "@ник", "telegram_id", "Город", "Баллы", "Откуда",
    "Кто начислил", "За что", "Ссылка на сообщение",
]

UNKNOWN_SOURCE_LABEL = "Прочее"

SOURCE_LABELS: dict[str | None, str] = {
    "chat": "Чат",
    "manual": "Вручную",
    "transfer": "Перенос из таблицы",
    "task": "За задание",
    "referral": "За приглашённого",
    "referral_reversal": "Отмена за приглашённого",
    None: "Старая запись",
}


def chat_message_link(chat_id, message_id) -> str:
    """Ссылка на сообщение супергруппы; для остальных чатов и без данных — пустая строка."""
    if chat_id is None or message_id is None:
        return ""
    cid = str(chat_id)
    if not cid.startswith("-100"):
        return ""
    return f"https://t.me/c/{cid[4:]}/{message_id}"


def _when(stamp) -> str:
    if not stamp:
        return "-"
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except ValueError:
        return str(stamp)


def _handle(*names) -> str:
    for n in names:
        n = (n or "").strip().lstrip("@")
        if n and n != "-":  # «-» анкета пишет тем, у кого нет ника
            return "@" + n
    return ""


def _name(*names) -> str:
    for n in names:
        n = (n or "").strip()
        if n:
            return n
    return ""


def _who_credited(e: dict) -> str:
    changed_by = e.get("changed_by")
    if changed_by is None:
        return "-"
    name = _name(e.get("changer_full_name"))
    handle = _handle(e.get("changer_username"), e.get("changer_chat_username"))
    if name and handle:
        return f"{name} ({handle})"
    if name or handle:
        return name or handle
    return _name(e.get("changer_chat_first_name")) or str(changed_by)


def build_coins_log(entries: list[dict], city_labels: dict[str, str]) -> tuple[list[str], list[list]]:
    """(шапка, строки) вкладки; порядок строк = порядок `entries`."""
    rows: list[list] = []
    for e in entries:
        source = e.get("source")
        city = e.get("user_event_city")
        link = chat_message_link(e.get("award_chat_id"), e.get("award_message_id")) \
            if source == "chat" else ""
        rows.append([
            _when(e.get("timestamp")),
            _name(e.get("user_full_name"), e.get("user_chat_first_name")) or "-",
            _handle(e.get("user_username"), e.get("user_chat_username")) or "-",
            e.get("user_id"),
            "-" if not city else city_labels.get(city, city),
            int(e.get("delta") or 0),
            SOURCE_LABELS.get(source, UNKNOWN_SOURCE_LABEL),
            _who_credited(e),
            e.get("reason") or "",
            link,
        ])
    return list(COINS_LOG_HEADERS), rows
