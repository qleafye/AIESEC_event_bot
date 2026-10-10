"""Строки экрана «ℹ️ Информация о форуме» делегата: всё, что о мероприятии уже известно.

Приёмка 10.10 (s2.menu.1): экран требовал текстовую «🗓 Дата» И «📍 Место» сразу — при заданной
только «🗓 Дата начала форума» делегат видел «пока заполняется». Теперь каждая строка
показывается сама по себе; пустой список — вызывающий пишет «пока заполняется».
"""
from __future__ import annotations

import html


async def info_lines(code: str | None, tr) -> list[str]:
    """Строки «🗓 Дата / ⌚ Время / 📍 Место / 🏙 Город» (HTML) для города делегата. `tr` —
    переводчик подписей и значений (`reg_i18n.tr_text` с языком делегата)."""
    from domain.cities import cities_module_on, city_label_or_none, get_setting_for_city
    from domain.settings.ui_text_fields import ui_tr  # подписи строк — из настроек (info_*_label_text)
    from services.reject_rules import forum_date_for

    event_date = await get_setting_for_city("event_date", code) or await forum_date_for(code)
    event_time = await get_setting_for_city("event_time", code)
    place_name = await get_setting_for_city("event_place_name", code)

    lines = []
    if event_date:
        lines.append(f"🗓 <b>{await ui_tr('info_date_label_text', tr)}:</b> {html.escape(tr(event_date))}")
    if event_time:
        lines.append(f"⌚ <b>{await ui_tr('info_time_label_text', tr)}:</b> {html.escape(tr(event_time))}")
    if place_name:
        lines.append(f"📍 <b>{await ui_tr('info_place_label_text', tr)}:</b> {html.escape(tr(place_name))}")
    # Город — только рядом с другими сведениями: сам по себе он делегату ничего не сообщает.
    city_text = await city_label_or_none(code) if lines and code and await cities_module_on() else None
    if city_text:
        lines.append(f"🏙 <b>{tr('Город')}:</b> {html.escape(tr(city_text))}")
    return lines
