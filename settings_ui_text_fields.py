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
