"""Записи реестра и порядок на экранах бота для подписей, вынесенных из кода (бэклог «🛠» P1).

Корневой модуль без импортов проекта: `settings_schema` вливает `UI_TEXT_SCHEMA` в
`SETTINGS_SCHEMA`, `handlers/admin_settings.py` дописывает `*_FIELD_ORDER` в хвост экранов
групп — так ключ виден менеджеру в боте, а не только в приложении.
"""

_CMD_TAIL = (
    "\n\nДо 256 символов. Применяется сразу после сохранения.\n\nПока поле не задано или "
    "очищено, бот этот список команд не трогает — остаётся заданный раньше (в том числе через "
    "BotFather)."
)

UI_TEXT_SCHEMA: dict[str, dict] = {
    # ── Команды в синей кнопке «Меню» (services/bot_commands.py) ────────────────────────────
    "bot_command_start_text": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: подпись /start",
        "prompt": (
            "Подпись команды /start в синей кнопке «Меню» слева от поля ввода — её видят все. "
            "Например: «Главное меню»." + _CMD_TAIL
        ),
        "default": None,
    },
    "bot_command_admin_text": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: подпись /admin",
        "prompt": (
            "Подпись команды /admin в кнопке «Меню». Её видят только организаторы — "
            "суперадмины и все, кому выдана роль в «Роли и доступы»; делегатам команда не "
            "показывается. Например: «Панель организатора».\n\nНовый организатор увидит "
            "команду в меню после перезапуска бота или следующей правки этой подписи." + _CMD_TAIL
        ),
        "default": None,
    },
    "bot_command_start_text_en": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: /start по-английски",
        "prompt": (
            "Подпись /start для тех, у кого Telegram на английском. Например: «Main menu»."
            + _CMD_TAIL
        ),
        "default": None,
    },
    "bot_command_admin_text_en": {
        "type": "text", "group": "event", "label": "⌨️ Кнопка «Меню»: /admin по-английски",
        "prompt": (
            "Подпись /admin для организаторов с Telegram на английском. Например: «Organizer "
            "panel»." + _CMD_TAIL
        ),
        "default": None,
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

_FRAG = (
    "\n\nБез разметки. Если очистить поле, вернётся текст по умолчанию. Делегатам с "
    "английским языком свой текст уходит в переводе, а пока перевод не готов — по-русски."
)


def _text(group: str, label: str, where: str, default: str) -> dict:
    return {"type": "text", "group": group, "label": label,
            "prompt": f"{where} Сейчас: «{default}».{_FRAG}", "default": default}


def _in_group(group: str, entry: dict) -> dict:
    return {**entry, "group": group}


UI_TEXT_SCHEMA.update({
    # ── «ℹ️ Информация о форуме», «📞 Контакты» (handlers/user_actions.py) ───────────────────
    "info_screen_title_text": _text(
        "event", "ℹ️ Информация: заголовок",
        "Заголовок экрана кнопки «Информация о форуме», когда дата и место заданы.",
        "Информация о мероприятии"),
    "info_date_label_text": _text(
        "event", "ℹ️ Информация: подпись «Дата»",
        "Подпись перед датой на экране информации о мероприятии.", "Дата"),
    "info_time_label_text": _text(
        "event", "ℹ️ Информация: подпись «Время»",
        "Подпись перед временем — на экране информации и в ответе на кнопку даты.", "Время"),
    "info_place_label_text": _text(
        "event", "ℹ️ Информация: подпись «Место»",
        "Подпись перед названием площадки на экране информации.", "Место"),
    "info_pending_text": _text(
        "event", "ℹ️ Информация: пока не заполнено",
        "Первая строка экрана информации, пока у города не заданы дата или площадка.",
        "Информация о мероприятии пока заполняется."),
    "info_choose_text": _text(
        "event", "ℹ️ Информация: «выбери кнопку»",
        "Вторая строка того же экрана — над кнопками даты и места.",
        "Выбери, что тебя интересует:"),
    "info_date_lead_text": {
        "type": "text", "group": "event", "label": "🗓 Дата: начало фразы",
        "prompt": (
            "Начало фразы в ответе на кнопку даты: «‹это› 25 октября!». Например: «Форум "
            "пройдет».\n\nПока не задано, бот пишет «Форум пройдет», а у конференции — "
            "«Конференция пройдет»." + _FRAG
        ),
        "default": None,
    },
    "info_date_pending_text": _text(
        "event", "🗓 Дата: пока уточняется",
        "Ответ на кнопку даты, пока дата города не задана.",
        "🗓 Дата пока уточняется. Скоро сообщим! 🙂"),
    "info_venue_title_text": _text(
        "event", "📍 Место: начало заголовка",
        "Начало заголовка в ответе на кнопку места: «‹это› — Технопарк!».", "Наша площадка"),
    "info_address_label_text": _text(
        "event", "📍 Место: подпись «Адрес»", "Подпись перед адресом площадки.", "Адрес"),
    "info_place_pending_text": _text(
        "event", "📍 Место: пока уточняется",
        "Ответ на кнопку места, пока площадка города не задана.",
        "📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!"),
    "contacts_person_label_text": _text(
        "event", "📞 Контакты: подпись к контакту",
        "Подпись перед контактным лицом на экране «Контакты».", "По всем вопросам пиши сюда"),
    "contacts_groups_label_text": _text(
        "event", "📞 Контакты: подпись к группам",
        "Подпись над ссылками на группы VK и Telegram на экране «Контакты».", "Наши группы"),
    # ── Кнопки экранов «🪙 Баланс» и «🎯 Задания» ───────────────────────────────────────────
    "balance_history_button_text": _in_group("game", _button(
        "🪙 Баланс: кнопка «История»", "Кнопка на экране «🪙 Баланс» — история баллов.",
        "📜 История")),
    "balance_top_button_text": _in_group("game", _button(
        "🪙 Баланс: кнопка «Рейтинг»", "Кнопка на экране «🪙 Баланс» — общий рейтинг.",
        "🏆 Рейтинг")),
    "balance_back_button_text": _in_group("game", _button(
        "🪙 Баланс: кнопка «назад к балансу»",
        "Кнопка возврата на экран «🪙 Баланс» из истории и рейтинга.", "◀️ Баланс")),
    "balance_history_prev_button_text": _in_group("game", _button(
        "📜 История баллов: кнопка «Раньше»", "Листает историю баллов к более старым записям.",
        "← Раньше")),
    "balance_history_next_button_text": _in_group("game", _button(
        "📜 История баллов: кнопка «Позже»", "Листает историю баллов к более новым записям.",
        "Позже →")),
    "wave_rating_button_text": _in_group("amb", _button(
        "🏅 Кнопка «Рейтинг волны»",
        "Кнопка под списком заданий у амбассадора, пока идёт его волна.", "🏅 Рейтинг волны")),
})

# Экран «🎪 Событие» — после команд меню: тексты экранов информации и контактов.
INFO_SCREEN_FIELD_ORDER = [
    "info_screen_title_text", "info_date_label_text", "info_time_label_text",
    "info_place_label_text", "info_pending_text", "info_choose_text", "info_date_lead_text",
    "info_date_pending_text", "info_venue_title_text", "info_address_label_text",
    "info_place_pending_text", "contacts_person_label_text", "contacts_groups_label_text",
]
# Экран «🎮 Геймификация» — кнопки «🪙 Баланс». Кнопка волны — в settings_amb_fields.py.
GAME_SCREEN_FIELD_ORDER = [
    "balance_history_button_text", "balance_top_button_text", "balance_back_button_text",
    "balance_history_prev_button_text", "balance_history_next_button_text",
]


async def ui_text(key: str) -> str:
    """Значение подписи; пустое — подпись по умолчанию (пустую кнопку Telegram не примет)."""
    from settings_schema import get_setting_typed  # ленивый: settings_schema импортирует этот модуль

    return (await get_setting_typed(key) or "").strip() or UI_TEXT_SCHEMA[key]["default"] or ""


async def ui_tr(key: str, translate) -> str:
    """Подпись из настроек на языке делегата (`translate` — `reg_i18n.tr_text` с его
    контекстом), экранированная для сообщений с HTML-разметкой."""
    import html

    return html.escape(translate(await ui_text(key)))
