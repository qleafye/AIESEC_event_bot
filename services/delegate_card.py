"""Строки «Статус / Город / Сезон» карточки делегата в `/find` (handlers/admin.py::cmd_find_user).

Приёмка 01.10: без них менеджер не видит, одобрен ли делегат и из какого города его переводит,
пока не нажмёт кнопку на карточке. Вынесено из handlers/admin.py — файл у потолка размера.
"""
from __future__ import annotations

import html

STATUS_LABELS = {
    "pending": "⏳ На рассмотрении",
    "approved": "✅ Одобрена",
    "rejected": "❌ Отклонена",
    "waitlist": "📋 Лист ожидания",
}


async def status_city_season_lines(user: dict) -> str:
    """Готовый HTML-хвост карточки (каждая строка с `\\n` впереди). Город — только при включённом
    модуле городов: без него подпись «Город» у всех одна и та же и ничего не говорит."""
    from cities import cities_module_on, city_label, normalize_city

    status = user.get("status")
    lines = [f"Статус: {STATUS_LABELS.get(status) or html.escape(str(status or '—'))}"]
    if await cities_module_on():
        code = user.get("event_city")
        city_text = html.escape(await city_label(normalize_city(code))) if code else "не выбран"
        lines.append(f"Город: {city_text}")
    if user.get("season"):
        lines.append(f"Сезон: {html.escape(str(user['season']))}")
    return "".join(f"\n{line}" for line in lines)
