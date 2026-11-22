"""Хвост журнала на карточке /find. Строки из admin_actions, человеку — без кодов."""

from __future__ import annotations

import html

_SHOWN = 8

_STATUS = {
    "pending": "На рассмотрении",
    "approved": "Одобрена",
    "rejected": "Отклонена",
    "waitlist": "Лист ожидания",
}

_PLAIN = {
    "grant_edit": "Открыта правка анкеты",
    "revoke_edit": "Правка анкеты закрыта",
    "grant_resubmit": "Разрешена повторная подача",
    "revoke_resubmit": "Повторная подача отозвана",
    "replace_resume": "Резюме заменено",
    "reset_stuck": "Сброшена зависшая анкета",
    "approve_rejected": "Отклонённая заявка одобрена",
    "write_message": "Отправлено сообщение делегату",
    "resend_decision": "Решение отправлено ещё раз",
}


def _status(code) -> str | None:
    if not isinstance(code, str):
        return None
    return _STATUS.get(code)


async def _city(code) -> str | None:
    if not code or not isinstance(code, str):
        return None
    from domain.cities import ALL_CITIES, city_label, get_city

    label = await city_label(code)
    if code != ALL_CITIES and get_city(code) is None and label == code:
        return None
    return html.escape(label)


def _stamp(row: dict) -> str:
    raw = row.get("created_at")
    if not raw:
        return ""
    return " · " + html.escape(str(raw))


async def _body(row: dict) -> str:
    action = row.get("action") or ""
    before = row.get("before") if isinstance(row.get("before"), dict) else {}
    after = row.get("after") if isinstance(row.get("after"), dict) else {}
    if action in ("city_move", "city_move_regional"):
        src = await _city(before.get("event_city"))
        dst = await _city(after.get("event_city"))
        if src and dst:
            text = f"Перевод в город: {src} → {dst}"
        elif dst or src:
            text = f"Перевод в город: {dst or src}"
        else:
            text = "Перевод в город"
        left, right = _status(before.get("status")), _status(after.get("status"))
        if left and right and left != right:
            text += f". {left} → {right}"
        return text
    if action == "revert_pending":
        left, right = _status(before.get("status")), _status(after.get("status"))
        if left and right:
            return f"Возврат на модерацию: {left} → {right}"
        return "Возврат на модерацию"
    if action == "grant_role":
        from domain.access.capabilities import ROLES

        code = after.get("role") if isinstance(after.get("role"), str) else None
        label = (ROLES.get(code) or {}).get("label") if code else None
        if label:
            return "Выдана роль: " + html.escape(label)
        return "Выдана роль"
    return _PLAIN.get(action) or "Записано действие"


async def _line(row: dict) -> str:
    return "• " + await _body(row) + _stamp(row)


async def journal_html(telegram_id: int) -> str:
    from database.db import list_admin_actions

    rows = await list_admin_actions(int(telegram_id), limit=_SHOWN)
    if not rows:
        return ""
    lines = ["", "<b>Журнал</b>"]
    for row in rows:
        lines.append(await _line(row))
    return "\n".join(lines)
