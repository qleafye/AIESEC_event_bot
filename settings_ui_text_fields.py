"""Записи реестра и порядок на экранах бота для подписей, вынесенных из кода (бэклог «🛠» P1).

Корневой модуль без импортов проекта: `settings_schema` вливает `UI_TEXT_SCHEMA` в
`SETTINGS_SCHEMA`, `handlers/admin_settings.py` дописывает `*_FIELD_ORDER` в хвост экранов
групп — так ключ виден менеджеру в боте, а не только в приложении.
"""

_CMD_TAIL = (
    "\n\nДо 256 символов. Применяется сразу после сохранения.\n\nЕсли очистить поле, бот этот "
    "список команд не трогает — останется заданный раньше (в том числе через BotFather)."
)

UI_TEXT_SCHEMA: dict[str, dict] = {
    # ── Команды в синей кнопке «Меню» (services/bot_commands.py) ────────────────────────────
    "bot_command_start_text": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: подпись /start",
        "prompt": (
            "Подпись команды /start в синей кнопке «Меню» слева от поля ввода — её видят все. "
            "Например: «Главное меню»." + _CMD_TAIL
        ),
        "default": "Главное меню",
    },
    "bot_command_admin_text": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: подпись /admin",
        "prompt": (
            "Подпись команды /admin в кнопке «Меню». Её видят только организаторы — "
            "суперадмины и все, кому выдана роль в «Роли и доступы»; делегатам команда не "
            "показывается. Например: «Панель организатора».\n\nНовый организатор увидит "
            "команду в меню после перезапуска бота или следующей правки этой подписи." + _CMD_TAIL
        ),
        "default": "Панель организатора",
    },
    "bot_command_start_text_en": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: /start по-английски",
        "prompt": (
            "Подпись /start для тех, у кого Telegram на английском. Например: «Main menu»."
            + _CMD_TAIL
        ),
        "default": "Main menu",
    },
    "bot_command_admin_text_en": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: /admin по-английски",
        "prompt": (
            "Подпись /admin для организаторов с Telegram на английском. Например: «Organizer "
            "panel»." + _CMD_TAIL
        ),
        "default": "Organizer panel",
    },
}

# Экран «🎪 Событие» — рядом с именем и описанием бота.
BOT_COMMAND_FIELD_ORDER = [
    "bot_command_start_text", "bot_command_admin_text",
    "bot_command_start_text_en", "bot_command_admin_text_en",
]

_BTN = (
    "\n\nЭто подпись кнопки: без разметки, коротко — Telegram обрезает длинные подписи. "
    "Если очистить поле, вернётся подпись по умолчанию."
)


def _button(label: str, where: str, default: str) -> dict:
    return {"type": "text", "group": "reg", "label": label,
            "prompt": f"{where} Например: «{default}».{_BTN}", "default": default}


UI_TEXT_SCHEMA.update({
    # ── Кнопки в фоновых сообщениях делегатам ───────────────────────────────────────────────
    "broadcast_mute_button_text": _button(
        "🔕 Кнопка «Не присылать сегодня»",
        "Кнопка под необязательными рассылками: делегат отключает их до завтра.",
        "🔕 Не присылать сегодня"),
    "broadcast_unmute_button_text": _button(
        "🔔 Кнопка «Присылать всё»",
        "Кнопка, которая появляется вместо «🔕 Не присылать сегодня» после нажатия — вернуть "
        "все рассылки.", "🔔 Присылать всё"),
    "checkin_not_arrived_coming_button_text": _button(
        "🚪 «Не пришёл»: кнопка «Уже еду»",
        "Первая кнопка под сообщением «Не пришёл» (раздел «✅ Отметки на форуме»).", "🚶 Уже еду"),
    "checkin_not_arrived_cant_button_text": _button(
        "🚪 «Не пришёл»: кнопка «Не смогу прийти»",
        "Вторая кнопка под сообщением «Не пришёл».", "😔 Не смогу прийти"),
    "checkin_not_arrived_here_button_text": _button(
        "🚪 «Не пришёл»: кнопка «Я на месте»",
        "Третья кнопка под сообщением «Не пришёл» — в ответ бот покажет QR.", "📍 Я на месте"),
    "regional_noshow_accept_button_text": _button(
        "🚌 Перенос в Москву: кнопка «Перенести»",
        "Кнопка под предложением переноса заявки на форум другого города. {target_city} бот "
        "заменит названием города назначения.", "✅ Перенести заявку: {target_city}"),
    "regional_noshow_confirm_button_text": _button(
        "🚌 Перенос в Москву: кнопка подтверждения",
        "Кнопка на шаге «Перенести заявку? Анкету заново заполнять не нужно».",
        "✅ Да, перенести"),
    "regional_noshow_decline_button_text": _button(
        "🚌 Перенос в Москву: кнопка отказа",
        "Кнопка отказа — и под предложением переноса, и на шаге подтверждения.", "Нет, спасибо"),
})

# Экран «📋 Заявки» — рядом с текстами тех же рассылок.
BACKGROUND_BUTTON_FIELD_ORDER = [
    "checkin_not_arrived_coming_button_text", "checkin_not_arrived_cant_button_text",
    "checkin_not_arrived_here_button_text", "regional_noshow_accept_button_text",
    "regional_noshow_confirm_button_text", "regional_noshow_decline_button_text",
    "broadcast_mute_button_text", "broadcast_unmute_button_text",
]


async def ui_text(key: str) -> str:
    """Значение подписи; пустое — подпись по умолчанию (пустую кнопку Telegram не примет)."""
    from settings_schema import get_setting_typed  # ленивый: settings_schema импортирует этот модуль

    return (await get_setting_typed(key) or "").strip() or UI_TEXT_SCHEMA[key]["default"] or ""
