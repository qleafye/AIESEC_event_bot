"""SETTINGS_SCHEMA registry (REG-01/REG-02/REG-03, Phase 6 plan 06-01).

Single source of truth for `bot_settings` key metadata (type/group/label/prompt/default)
that both the admin UI and downstream consumers read from — the whole point of this
module is to remove the "coordination tax" of editing 4 scattered files (SETTINGS_FIELDS,
SETTINGS_GROUPS, PHOTO_FIELDS/FILE_FIELDS, and each ad-hoc consumer's own parse) every
time a setting is added or changed. Adding/editing a setting should require editing only
one entry in SETTINGS_SCHEMA.

Design (D-01..D-08, see .planning/phases/06-settings-schema-registry/06-CONTEXT.md):
- D-01: lives in its own top-level module — imports ONLY `database.db.get_setting`
  (one-directional dependency, no import of handlers.* — avoids import cycles).
- D-02: plain dict-by-key (`SETTINGS_SCHEMA = {key: {...}}`) — O(1) lookup, no dataclass
  (project convention: domain entities are plain dicts, see CONVENTIONS.md).
- D-03/D-04: type-driven parse dispatch. `type` taxonomy: toggle/int/list/date/text/enum/
  photo/file. An optional per-entry `"parse"` callable overrides the type-branch for rare
  special cases (exception, not the rule).
- D-05/D-08: `get_setting_typed(key)` is a thin async accessor — raw read via the existing
  `get_setting` (unchanged, D-07) then dispatch through the PURE sync `_parse_setting(key,
  raw)` helper, which is the unit-test surface (no DB, no async).

This plan (06-01) populates ONLY the `event` group (D-11 pilot: 10 text/enum keys + the
photo/file keys rendered on the event sub-screen) — later waves add reg/pay/party/consent/
toggle groups. Unmigrated keys simply are not present in SETTINGS_SCHEMA yet; `_parse_setting`
fails-soft (returns raw unchanged) for any key it doesn't recognize, so coexistence with the
legacy literal tables in handlers/admin.py holds throughout the incremental migration.
"""
from datetime import datetime

from database.db import get_setting, DEFAULT_CONSENT_VERSION

# REG-01: event-group entries — labels/prompts copied byte-for-byte from the pre-migration
# literal SETTINGS_FIELDS/PHOTO_FIELDS/FILE_FIELDS tables (handlers/admin.py) so the render
# snapshot test proves zero drift. Order of the text/enum keys matches the pre-migration
# SETTINGS_GROUPS "event" row (handlers/admin.py:398-401) — later consumers rely on this
# insertion order being preserved (dict iteration order) when filtering by group/type.
SETTINGS_SCHEMA = {
    # Phase 09.2 (A): the per_city flag below means this value can be overridden
    # per event-city. Override storage key is `{key}__city__{code}` (the registry key +
    # `cities.PER_CITY_SEP` + a code from `cities.city_codes()`); the resolver is
    # `cities.get_setting_for_city`/`get_setting_typed_for_city`. Absence of the flag means
    # this setting stays global-only — SETTINGS_SCHEMA is the single source of truth for
    # what can be overridden by city (CONTEXT A); nothing else may decide this.
    "event_date": {
        "type": "text", "group": "event", "label": "🗓 Дата",
        "prompt": "Введите дату форума", "default": None,
        "per_city": True,
    },
    "event_time": {
        "type": "text", "group": "event", "label": "⌚ Время",
        "prompt": "Введите время проведения", "default": None,
        "per_city": True,
    },
    "event_place_name": {
        "type": "text", "group": "event", "label": "📍 Место",
        "prompt": "Введите название площадки", "default": None,
        "per_city": True,
    },
    "event_place_address": {
        "type": "text", "group": "event", "label": "📫 Адрес",
        "prompt": "Введите адрес площадки", "default": None,
        "per_city": True,
    },
    "contact_person": {
        "type": "text", "group": "event", "label": "👤 Контакт",
        "prompt": "Введите юзернейм контактного лица (например @username)", "default": None,
        "per_city": True,
    },
    "contact_vk": {
        "type": "text", "group": "event", "label": "🔵 VK",
        "prompt": "Введите ссылку на группу ВК", "default": None,
        "per_city": True,
    },
    "contact_tg": {
        "type": "text", "group": "event", "label": "🔹 TG",
        "prompt": "Введите ссылку на Telegram-канал", "default": None,
        "per_city": True,
    },
    "start_text": {
        "type": "text", "group": "event", "label": "💬 Приветствие",
        "prompt": "Введите текст приветствия при /start (поддерживается HTML-разметка)",
        "default": None,
        "per_city": True,
    },
    "start_text_registered": {
        "type": "text", "group": "event", "label": "🔁 Приветствие вернувшимся",
        "prompt": (
            "Текст, который увидит уже зарегистрированный участник, когда снова нажмёт "
            "/start (поддерживается HTML-разметка).\n\nЕсли не задать — бот пришлёт короткое "
            "приветствие для вернувшихся с указанием на меню. Обычное приветствие для новичков "
            "правится отдельно, полем «💬 Приветствие»."
        ),
        "default": None,
        "per_city": True,
    },
    "event_name": {
        "type": "text", "group": "event", "label": "🎪 Название меро",
        "prompt": (
            "Название мероприятия в родительном падеже — подставляется в вопрос об "
            "ожиданиях (например: «конференции RusCo», «форума YouLead», «Годового отчёта»)"
        ),
        "default": None,
    },
    # Phase 07.3 (A): event_season is deliberately NOT per_city — a season is one entity
    # shared across all cities of an event, not scoped per city like start_text_registered.
    "event_season": {
        "type": "text", "group": "event", "label": "🎉 Сезон события",
        "prompt": (
            "Название текущего сезона — им помечаются все новые регистрации.\n\n"
            "Например: YL'26 · SumMeet'26 · RusCo'27\n\n"
            "Пока не задано — бот работает как раньше: прошлыми считаются только "
            "отклонённые заявки."
        ),
        "default": None,
    },
    # Phase 07.3 (A): start_text_returning is also NOT per_city — a global banner for
    # returning delegates, unlike per-city start_text_registered.
    "start_text_returning": {
        "type": "text", "group": "event", "label": "🔄 Приветствие делегату прошлого сезона",
        "prompt": (
            "Текст для того, кто уже был с нами на прошлом событии и снова нажал /start "
            "(поддерживается HTML). Подстановка {season} даст название его прошлого "
            "сезона.\n\nЕсли не задать — бот пришлёт текст по умолчанию."
        ),
        "default": None,
    },
    # Phase 17.1 (17.1-02): recall/возвращение — три текста, которые прошлый делегат видит
    # чаще всего: CTA под баннером /start и два экрана «прошлый ответ» в анкете. Как и
    # start_text_returning выше — НЕ per_city (сезон и возврат — сущности события, не города).
    # Дефолт непустой (идиома 16-01: консьюмер читает через get_setting_typed, без `or`).
    "start_returning_cta_text": {
        "type": "text", "group": "event", "label": "🔄 Кнопка «Обновить анкету»: подпись",
        "prompt": (
            "Короткое сообщение под приветствием делегата прошлого сезона — над кнопкой "
            "«🚀 Обновить анкету». Поддерживается HTML."
        ),
        "default": "Хочешь участвовать снова? Обновим анкету — прошлые ответы предложу оставить.",
    },
    "recall_resume_prompt_text": {
        "type": "text", "group": "event", "label": "📎 Прошлое резюме: вопрос",
        "prompt": (
            "Что видит делегат прошлого сезона на шаге «резюме», если у нас уже есть его "
            "файл: под текстом кнопки «📎 Оставить прошлое резюме» / «📤 Загрузить новое». "
            "Само резюме не показывается — только факт, что оно есть."
        ),
        "default": (
            "У нас есть твоё резюме с прошлой регистрации. "
            "Оставить его или прислать новое?"
        ),
    },
    "recall_generic_prompt_text": {
        "type": "text", "group": "event", "label": "🔁 Прошлый ответ: экран «оставить/изменить»",
        "prompt": (
            "Экран для делегата прошлого сезона на каждом вопросе анкеты, где сохранился его "
            "прошлый ответ: под текстом кнопки «✅ Оставить» / «✏️ Изменить».\n\n"
            "Подставляются:\n{label} — название вопроса (например «Университет»)\n"
            "{display} — его прошлый ответ\n\nПлейсхолдеры не убирать — иначе делегат не "
            "поймёт, о каком вопросе речь. Поддерживается HTML."
        ),
        "default": "<b>{label}</b>\n\nПрошлый ответ: <b>{display}</b>\n\nОставить или изменить?",
    },
    # Phase 17.1 (17.1-03): empty-state'ы информационных кнопок меню и оба экрана «❓ Задать
    # вопрос» (handlers/user_actions.py) — раньше литералы. Живут в «🎪 Событие/Медиа» рядом с
    # фото программы/спикеров и контактами, к которым относятся; вопрос организаторам —
    # тот же «канал связи с оргами», что и контакты. НЕ per_city (сосед contact_person —
    # per_city, но empty-state один на всё событие; захотят по городам — отдельным решением).
    # Дефолты байт-в-байт равны прежним литералам; консьюмеры читают get_setting_typed.
    "program_empty_text": {
        "type": "text", "group": "event", "label": "📅 Программа: ещё не загружена",
        "prompt": (
            "Что видит делегат по кнопке «📅 Программа форума», пока фото программы не "
            "загружено (нет ни в настройках, ни в файлах бота). Поддерживается HTML."
        ),
        "default": "Программа форума ещё не загружена.",
    },
    "speakers_empty_text": {
        "type": "text", "group": "event", "label": "🗣 Спикеры: ещё не загружены",
        "prompt": (
            "Что видит делегат по кнопке «🗣 Спикеры», пока фото спикеров не загружено. "
            "Поддерживается HTML."
        ),
        "default": "Список спикеров формируется и скоро появится здесь.",
    },
    "contacts_empty_text": {
        "type": "text", "group": "event", "label": "📞 Контакты: не указаны",
        "prompt": (
            "Что видит делегат по кнопке «📞 Контакты», пока не заполнены ни «👤 Контакт», "
            "ни «🔵 VK», ни «🔹 TG» (для его города или глобально). Поддерживается HTML."
        ),
        "default": "Контакты пока не указаны. Обратитесь к организаторам.",
    },
    "ask_question_prompt_text": {
        "type": "text", "group": "event", "label": "❓ Задать вопрос: приглашение",
        "prompt": (
            "Что видит делегат по кнопке «❓ Задать вопрос» — приглашение написать вопрос "
            "(ниже кнопка «Отмена»). Поддерживается HTML."
        ),
        "default": "Напиши свой вопрос, и мы передадим его организаторам.",
    },
    "ask_question_sent_text": {
        "type": "text", "group": "event", "label": "❓ Задать вопрос: отправлен",
        "prompt": (
            "Подтверждение делегату после того, как его вопрос ушёл менеджерам. "
            "Поддерживается HTML."
        ),
        "default": "Твой вопрос отправлен!",
    },
    "poll_intro_text": {
        "type": "text", "group": "event", "label": "📊 Опрос: сообщение перед опросом",
        "prompt": (
            "Текст, который делегат получает ПЕРЕД каждым опросом из раздела «📊 Опросы» "
            "(например: «Помоги нам — ответь на короткий опрос 👇»). Поддерживается HTML."
            "\n\nОставьте пустым — бот пришлёт только сам опрос, без вступления."
        ),
        "default": "",
    },
    "event_type": {
        "type": "enum", "group": "event", "label": "🎭 Тип события",
        "options": ["forum", "conference", "custom"],
        "prompt": (
            "Напишите одно слово: forum (форум) / conference (конференция) / custom "
            "(вручную).\n\nДля forum и conference бот сам включит/выключит модули оплаты "
            "и согласий — потом можно поправить кнопками выше."
        ),
        "default": None,
    },
    # Photo/file entries rendered on the event sub-screen (handlers/admin.py PHOTO_FIELDS/
    # FILE_FIELDS). Registry key is the derived-key PREFIX, not the actual bot_settings row
    # (f"{prefix}_photo_file_id" / f"{prefix}_doc_file_id") — the upload-flow and lookup
    # mechanics stay special-cased outside the registry (D-10). Metadata only here.
    "program": {
        "type": "photo", "group": "event", "label": "📅 Программа",
        "prompt": "Отправьте фото программы (можно с подписью).", "default": None,
    },
    "speakers": {
        "type": "photo", "group": "event", "label": "🗣 Спикеры",
        "prompt": "Отправьте одно фото со всеми спикерами (можно с подписью).", "default": None,
    },
    "start": {
        "type": "photo", "group": "event", "label": "💬 Фото приветствия",
        "prompt": "Отправьте фото для приветственного сообщения (/start).", "default": None,
    },
    "venue": {
        "type": "photo", "group": "event", "label": "🏢 Площадка",
        "prompt": "Отправьте фото площадки (можно с подписью).", "default": None,
    },
    "reg_bonus": {
        "type": "file", "group": "event", "label": "🎁 Бонус за регистрацию",
        "prompt": "Отправьте файл или фото бонуса (можно с подписью).", "default": None,
    },

    # ── REG-01/REG-03 (06-02): "reg" group ("📝 Регистрация") ──────────────────────────
    # Labels/prompts copied byte-for-byte from the pre-migration literal SETTINGS_FIELDS
    # tuples (handlers/admin.py). pending_reminder_interval's default is pinned to 1800 to
    # match services/reminders.py::DEFAULT_INTERVAL (T-06-07, proven byte-for-byte by
    # test_parse_equivalence_int).
    "source_options": {
        "type": "list", "group": "reg", "label": "📢 Источники",
        "prompt": "Отправьте варианты источников, каждый с новой строки", "default": None,
    },
    "reg_complete_text": {
        "type": "text", "group": "reg", "label": "✅ После регистрации",
        "prompt": (
            "Текст, который участник увидит СРАЗУ после отправки анкеты (например "
            "«Поздравляем, заявка принята! Рассмотрим за 2-3 дня»). Поддерживается HTML."
        ),
        "default": None,
        "per_city": True,
    },
    # Phase 09.2 (B, Q2 resolution): city override applies ONLY to this base `approve_text`.
    # `approve_text__party` below stays global — composing a THIRD axis (track x city) on
    # top of the existing track override was explicitly punted by CONTEXT.md/RESEARCH.md
    # Open Question 2; a party delegate never gets a per-city approve text in this wave.
    "approve_text": {
        "type": "text", "group": "reg", "label": "🎉 После одобрения",
        "prompt": (
            "Отдельный текст, который участник увидит, когда менеджер ОДОБРИТ заявку. "
            "Поддерживается HTML."
        ),
        "default": None,
        "per_city": True,
    },
    "reject_text": {
        "type": "text", "group": "reg", "label": "🚫 При отклонении",
        "prompt": (
            "Текст, который участник увидит, когда менеджер ОТКЛОНИТ заявку (перед "
            "указанной менеджером причиной). Оставьте пустым — стандартный «К сожалению, "
            "твоя заявка отклонена.»"
        ),
        "default": None,
    },
    # Phase 17.1 (17.1-01): гейт «заявка ещё на модерации» — сосед reject_text выше уже был
    # ключом, а pending-ветка того же `_gate_decision` оставалась литералом в
    # handlers/user_actions.py::ensure_registered. Здесь `default` непустой (в отличие от
    # reject_text с default=None + `or "..."` в коде): консьюмер читает через
    # get_setting_typed, идиома 16-01.
    "pending_gate_text": {
        "type": "text", "group": "reg", "label": "⏳ Заявка на рассмотрении",
        "prompt": (
            "Что видит участник, который уже подал заявку, но её ещё не одобрили, когда "
            "жмёт любую кнопку меню. Поддерживается HTML."
        ),
        "default": "⏳ Твоя заявка на рассмотрении. Доступ откроется после одобрения.",
    },
    "pending_reminder_interval": {
        "type": "int", "group": "reg", "label": "🕒 Тайминг батчей заявок",
        "prompt": (
            "Как часто бот присылает админам сводку «Заявок в ожидании: N» (режим "
            "«Пачкой»).\n\nВ СЕКУНДАХ. Примеры:\n900 = 15 мин\n1800 = 30 мин (по умолчанию)"
            "\n3600 = 1 час\n\nМеняется на лету, перезапуск не нужен."
        ),
        "default": 1800,
    },
    # Quick 260819 (schema-completeness): «догонялка» брошенных анкет (services/scheduler.py::
    # nudge_incomplete_registrations). Дефолты = прежние литералы в коде (120 минут /
    # DEFAULT_NUDGE_TEXT); читаются джобой при КАЖДОМ прогоне — правка применяется без
    # перезапуска. Тумблер nudge_enabled — в "toggles" ниже; интервал сканирования — в "system".
    "nudge_after_minutes": {
        "type": "int", "group": "reg", "label": "⏰ Догонялка: через сколько минут",
        "prompt": (
            "Сколько минут молчания участника ждать, прежде чем напомнить ему о незавершённой "
            "анкете (одно напоминание на человека).\n\nВ МИНУТАХ. Примеры:\n"
            "120 = 2 часа (по умолчанию)\n60 = 1 час\n1440 = сутки"
        ),
        "default": 120,
    },
    # Quick 260820-rms: окно, в котором бот считает вернувшегося человека тем же самым, кто
    # бросил анкету — и подставляет его прежний город/трек молча, без вопроса. Строка
    # reg_started не истекает сама (её читают «Незавершённые» и догонялка), поэтому без окна
    # делегат, вернувшийся через две недели, навсегда оставался приписан к городу, выбранному
    # в тот раз, и экран выбора города ему больше не показывался.
    "reg_resume_ttl_hours": {
        "type": "int", "group": "reg", "label": "⏳ Продолжить анкету: сколько часов помнить",
        "prompt": (
            "Человек начал анкету, бросил и вернулся. Сколько часов бот молча продолжает с "
            "его прежним городом и форматом участия, а не спрашивает заново.\n\n"
            "В ЧАСАХ. Примеры:\n24 = сутки (по умолчанию)\n2 = только в рамках вечера\n"
            "720 = месяц\n\nПозже этого срока бот спросит город заново — прежние ответы "
            "никуда не денутся."
        ),
        "default": 24,
    },
    "nudge_text": {
        "type": "text", "group": "reg", "label": "⏰ Догонялка: текст напоминания",
        "prompt": (
            "Текст, который получит человек, начавший регистрацию и бросивший её.\n\n"
            "Пример:\n👋 Вы начали регистрацию, но не завершили её. Отправьте /start, "
            "чтобы продолжить — это займёт пару минут."
        ),
        "default": (
            "👋 Вы начали регистрацию, но не завершили её. "
            "Отправьте /start, чтобы продолжить — это займёт пару минут."
        ),
    },
    "city_options": {
        "type": "list", "group": "reg", "label": "🏙 Города (варианты)",
        "prompt": (
            "Города-кнопки для вопроса «Город». Каждый город — на отдельной строке.\n\n"
            "Кнопка «Другое» добавится сама. Оставьте пустым — будет стандартный список."
        ),
        "default": None,
    },
    "study_field_options": {
        "type": "list", "group": "reg", "label": "🎯 Направления обучения (варианты)",
        "prompt": (
            "Варианты-кнопки для вопроса «Направление обучения». Каждый — на отдельной "
            "строке.\n\nПусто = стандартный список."
        ),
        "default": None,
    },
    "goal_options": {
        "type": "list", "group": "reg", "label": "🎯 Цель участия (варианты)",
        "prompt": (
            "Варианты для вопроса «Цель участия» — участник сможет выбрать несколько. "
            "Каждый — на отдельной строке.\n\nПусто = стандартный список."
        ),
        "default": None,
    },
    "formats_options": {
        "type": "list", "group": "reg", "label": "📋 Форматы форума (варианты)",
        "prompt": (
            "Варианты для вопроса «Форматы форума» — выбор нескольких. Каждый — на "
            "отдельной строке.\n\nПусто = стандартный список."
        ),
        "default": None,
    },
    "university_options": {
        "type": "list", "group": "reg", "label": "🏫 Список ВУЗов",
        "prompt": (
            "ВУЗы-кнопки для режима «выбор из базы». Каждый ВУЗ — на отдельной строке.\n\n"
            "Кнопка «Другое» добавится сама. Пусто = встроенный список."
        ),
        "default": None,
    },
    # Phase 17.1 (17.1-03, schema-completeness): ключи, которые handlers/registration.py
    # давно читал из bot_settings, но которых не было в реестре — менеджер их не видел в UI и
    # мог поменять только через разработчика. Дефолты байт-в-байт равны прежним литералам /
    # DEFAULT_CITY_FORK_TEXT (registration.py теперь берёт константу отсюда). Поведение чтения
    # не меняется: `get_setting(k) or DEFAULT` -> get_setting_typed(k) (текст: None -> дефолт;
    # «-» в админке удаляет ключ, пустую строку Telegram прислать не даёт).
    "city_fork_text": {
        "type": "text", "group": "reg", "label": "🏙 Выбор города: вопрос",
        "prompt": (
            "Экран выбора города мероприятия при /start (когда включён «🏙 Выбор города "
            "мероприятия» и городов больше одного) — текст над кнопками городов."
        ),
        "default": "Выбери город мероприятия:",
    },
    # Предотбор (VERIF-01/02): тексты гейта по списку отобранных. Консьюмер html.escape'ит их
    # перед отправкой (как и раньше) — поэтому НЕ в HTML_SETTINGS и без «Поддерживается HTML».
    "preselect_no_username_text": {
        "type": "text", "group": "reg", "label": "🎯 Предотбор: нет @username",
        "prompt": (
            "Что видит человек без @username при включённом предотборе (проверить его по "
            "списку отобранных нельзя). Обычный текст, без разметки."
        ),
        "default": (
            "Чтобы продолжить, задайте @username в настройках Telegram и снова отправьте /start."
        ),
    },
    "preselect_fail_text": {
        "type": "text", "group": "reg", "label": "🎯 Предотбор: не прошёл",
        "prompt": (
            "Что видит человек, которого нет в списке отобранных (вкладка «🎯 Отобранные»). "
            "Ниже бот добавит ссылку из «🎯 Предотбор: ссылка», если она задана. Обычный "
            "текст, без разметки."
        ),
        "default": "Отбор не пройден.",
    },
    "preselect_link": {
        "type": "text", "group": "reg", "label": "🎯 Предотбор: ссылка",
        "prompt": (
            "Куда отправить не прошедшего отбор — ссылка на канал/сайт (например "
            "https://t.me/aiesec_ru). Пусто — без ссылки, только текст."
        ),
        "default": None,
    },
    # НЕ в реестре намеренно (служебные значения bot_settings, менеджеру их не редактировать):
    #   sheet_header_schema  — снимок заголовков вкладки регистраций, пишет reg_schema.py при
    #                          смене состава вопросов (JSON, читает registration.py при синке);
    #   preselect_manual_ids — ручные исключения предотбора по telegram_id (задаются
    #                          разработчиком/командой, а не в «⚙️ Настройки»);
    #   start_photo_file_id  — file_id, который пишет фото-визард настроек (регистрируется
    #                          через префикс "start" типа photo выше, а не отдельным ключом).
    # ── Quick 260815-3hw: "sheets" group ("📄 Вкладки таблицы") ─────────────────────────
    # CLAUDE.md «бот для людей» — все имена вкладок Google-таблицы (раньше размазанные
    # между .env, хардкодом в коде и парой уже-реестровых ключей) собраны в одну группу,
    # чтобы менеджер переименовывал любую вкладку кнопкой, без разработчика. Дефолты — БУКВА
    # В БУКВУ старые хардкоды (short_sheet_tab/party_sheet_tab физически переехали сюда из
    # reg/party ниже по файлу, а не продублированы — единственный источник правды один).
    # main_sheet_tab имеет `default: None` НАМЕРЕННО: реестровый дефолт подменил бы 4-ступенчатую
    # цепочку резолва в services/sheets.py::_get_sheet (bot_settings -> .env -> legacy-пин ->
    # RuntimeError, инцидент 058def0) — non-None дефолт здесь сделал бы .env вечно недостижимым.
    "main_sheet_tab": {
        "type": "text", "group": "sheets", "label": "📄 Основная (регистрации)",
        "prompt": (
            "Название основной вкладки Google-таблицы, куда пишутся обычные регистрации.\n\n"
            "Если вкладки с таким именем ещё нет — бот создаст новую при первой синхронизации.\n\n"
            "Оставьте пустым — вкладка возьмётся из .env (GOOGLE_SHEET_TAB)."
        ),
        "default": None,
    },
    "short_sheet_tab": {
        "type": "text", "group": "sheets", "label": "⚡ Краткая форма (акция)",
        "prompt": (
            "Название вкладки в Google-таблице, куда пишутся заявки краткой формы "
            "(отдельно от основной). Если такой вкладки в таблице нет — бот создаст новую с "
            "этим именем.\n\nОставьте пустым — будет «Краткая»."
        ),
        "default": "Краткая",
    },
    "party_sheet_tab": {
        "type": "text", "group": "sheets", "label": "🎉 Вечеринка (Party)",
        "prompt": (
            "Название вкладки в Google-таблице, куда пишутся вечеринковые заявки (отдельно "
            "от основной). Если такой вкладки в таблице нет — бот создаст новую с этим именем."
            "\n\nОставьте пустым — будет «Party»."
        ),
        "default": "Party",
    },
    "incomplete_sheet_tab": {
        "type": "text", "group": "sheets", "label": "📝 Незавершённые анкеты",
        "prompt": (
            "Название вкладки, куда бот выгружает анкеты, которые начали заполнять, но не "
            "закончили. Если такой вкладки в таблице нет — бот создаст новую с этим именем."
            "\n\nОставьте пустым — будет «Незавершённые»."
        ),
        "default": "Незавершённые",
    },
    "polls_sheet_tab": {
        "type": "text", "group": "sheets", "label": "📊 Опросы",
        "prompt": (
            "Название вкладки, куда бот выгружает результаты опросов (кнопка «📄 В таблицу» "
            "в карточке опроса). Если такой вкладки в таблице нет — бот создаст новую."
            "\n\nОставьте пустым — будет «Опросы»."
        ),
        "default": "Опросы",
    },
    "game_matrix_tab": {
        "type": "text", "group": "sheets", "label": "🎮 Гейма (матрица)",
        "prompt": (
            "Название вкладки с матрицей «участники × задания» геймификации. Если такой "
            "вкладки в таблице нет — бот создаст новую с этим именем.\n\nОставьте пустым — "
            "будет «Гейма»."
        ),
        "default": "Гейма",
    },
    "game_history_tab": {
        "type": "text", "group": "sheets", "label": "🕓 История сдач",
        "prompt": (
            "Название вкладки с историей всех сдач заданий геймификации. Если такой вкладки "
            "в таблице нет — бот создаст новую с этим именем.\n\nОставьте пустым — будет "
            "«История сдач»."
        ),
        "default": "История сдач",
    },
    "preselect_tab": {
        "type": "text", "group": "sheets", "label": "🎯 Отобранные (предотбор)",
        "prompt": (
            "Название вкладки со списком @username, кого пускать в бота при включённом "
            "предотборе. Бот только ЧИТАЕТ эту вкладку, не создаёт и не пишет в неё."
            "\n\nОставьте пустым — будет «Отобранные»."
        ),
        "default": "Отобранные",
    },
    "city_tab_suffix__short": {
        "type": "text", "group": "sheets", "label": "🏙 Приписка: краткая форма",
        "prompt": (
            "Что бот добавляет к названию города для вкладки краткой формы этого города. "
            "Например: «Акция» → у Санкт-Петербурга вкладка «СПб Акция». Пробел бот добавит "
            "сам.\n\nОставьте пустым — будет «Акция»."
        ),
        "default": " Акция",
    },
    "city_tab_suffix__party": {
        "type": "text", "group": "sheets", "label": "🏙 Приписка: вечеринка",
        "prompt": (
            "Что бот добавляет к названию города для вечеринковой вкладки этого города. "
            "Например: «Party» → у Санкт-Петербурга вкладка «СПб Party». Пробел бот добавит "
            "сам.\n\nОставьте пустым — будет «Party»."
        ),
        "default": " Party",
    },
    "city_tab_suffix__incomplete": {
        "type": "text", "group": "sheets", "label": "🏙 Приписка: незавершённые",
        "prompt": (
            "Что бот добавляет к названию города для вкладки незавершённых анкет этого "
            "города. Например: «Незавершённые» → у Санкт-Петербурга вкладка «СПб "
            "Незавершённые». Пробел бот добавит сам.\n\nОставьте пустым — будет "
            "«Незавершённые»."
        ),
        "default": " Незавершённые",
    },
    "city_tab_suffix__game": {
        "type": "text", "group": "sheets", "label": "🏙 Приписка: гейма (матрица)",
        "prompt": (
            "Что бот добавляет к названию города для вкладки геймификации этого города "
            "(матрица «участники × задания» только по этому городу). Например: «Гейма» → у "
            "Санкт-Петербурга вкладка «СПб Гейма». Пробел бот добавит сам.\n\nОставьте "
            "пустым — будет «Гейма»."
        ),
        "default": " Гейма",
    },
    "city_tab_suffix__game_history": {
        "type": "text", "group": "sheets", "label": "🏙 Приписка: история сдач",
        "prompt": (
            "Что бот добавляет к названию города для вкладки с историей сдач заданий этого "
            "города. Например: «История сдач» → у Санкт-Петербурга вкладка «СПб История "
            "сдач». Пробел бот добавит сам.\n\nОставьте пустым — будет «История сдач»."
        ),
        "default": " История сдач",
    },

    # ── REG-01/REG-03 (06-02): "pay" group ("💳 Оплата") ────────────────────────────────
    "payment_options": {
        "type": "list", "group": "pay", "label": "💳 Варианты оплаты",
        "prompt": (
            "Варианты участия (билеты/тарифы), каждый — отдельной строкой:\nНазвание | "
            "Цена\n\nПример:\nПолный билет|5000\nСтудент|3000\n\nЦена 0 = бесплатно. Если "
            "вариант один — участник его не выбирает, сразу видит реквизиты.\n\n"
            "Необязательное третье поле — фильтр по треку: Название | Цена | треки (треки "
            "— через запятую, значения: full, party_overnight, party_noovernight). Без "
            "третьего поля тариф виден ВСЕМ трекам. Пример строки только для party:\n"
            "Вход на вечеринку|1000|party_overnight,party_noovernight"
        ),
        "default": None,
    },
    "payment_requisites": {
        "type": "text", "group": "pay", "label": "💰 Реквизиты оплаты",
        "prompt": (
            "Общие реквизиты: банк, номер карты, ФИО получателя. Показываются, если для "
            "ЛК участника не задана своя карта (см. «💳 Реквизиты по ЛК»). Обычный текст."
        ),
        "default": None,
    },
    "payment_requisites_by_lc": {
        "type": "list", "group": "pay", "label": "💳 Реквизиты по ЛК",
        "prompt": (
            "Своя карта для каждого ЛК — каждый комитет собирает на свои реквизиты.\n\n"
            "Каждый ЛК — отдельной строкой в формате:\nНазвание ЛК | реквизиты\n\n"
            "Название ЛК должно совпадать с кнопкой в вопросе про ЛК (EG, SPUEF, Moscow, "
            "Tyumen, Ufa, Ekaterinburg).\n\nПример:\nMoscow | Сбер 1234 5678 9012 3456, "
            "Иван И.\nSPUEF | Тинькофф 9876 5432, Пётр П.\n\nЕсли ЛК участника нет в списке "
            "— покажутся общие «💰 Реквизиты оплаты»."
        ),
        "default": None,
    },
    "payment_deadline": {
        "type": "date", "group": "pay", "label": "📅 Дедлайн оплаты",
        "prompt": (
            "Крайний срок оплаты в формате ДД.ММ.ГГГГ ЧЧ:ММ.\n\nПример: 15.08.2026 23:59"
            "\n\nПо этому сроку бот сам пришлёт участнику напоминания за 3 дня и за 1 день."
        ),
        "default": None,
    },
    "payment_reminder_text": {
        "type": "text", "group": "pay", "label": "⏰ Текст напоминания об оплате",
        "prompt": (
            "Текст, который бот шлёт участнику за 3 дня и за 1 день до дедлайна оплаты."
            "\n\nОставьте пустым — будет стандартный текст."
        ),
        "default": None,
    },
    "payment_overdue_text": {
        "type": "text", "group": "pay", "label": "⌛ Текст «оплата просрочена»",
        "prompt": (
            "Финальный пинг участнику, когда дедлайн оплаты прошёл, а чек так и не "
            "загружен.\n\nОставьте пустым — будет стандартный текст."
        ),
        "default": None,
    },
    "penalty_schedule": {
        "type": "list", "group": "pay", "label": "⚠️ Штрафы за отмену",
        "prompt": (
            "Необязательно. Каждая строка: дата | сумма возврата/остатка.\n\nПример:\n"
            "01.08.2026|3000\n10.08.2026|0\n\nОставьте пустым (отправьте «-»), если "
            "штрафов нет."
        ),
        "default": None,
    },
    # Phase 17.1 (17.1-02): делегатские экраны платёжного потока (handlers/payment.py) —
    # раньше литералы, менеджер не мог их поменять. Дефолты байт-в-байт равны прежним
    # литералам; консьюмеры читают через get_setting_typed (дефолт непустой, идиома 16-01).
    # Подстановка плейсхолдеров — цепочкой .replace, как у coins_manual_notify_text: текст
    # менеджера может содержать посторонние {}, на которых .format упал бы.
    "payment_option_picker_header_text": {
        "type": "text", "group": "pay", "label": "💳 Выбор варианта: заголовок",
        "prompt": (
            "Первая строка экрана выбора варианта участия (когда вариантов больше одного). "
            "Ниже бот сам добавит реквизиты, если они заданы. Поддерживается HTML."
        ),
        "default": "💳 Выбери вариант участия:",
    },
    "payment_details_template_text": {
        "type": "text", "group": "pay", "label": "💰 Экран оплаты: шаблон",
        "prompt": (
            "Сообщение с суммой и реквизитами, которое делегат получает после одобрения "
            "(или после выбора варианта).\n\n"
            "Подставляются:\n{option} — название варианта участия\n{amount} — сумма в рублях "
            "(без знака ₽)\n{requisites} — блок «📋 Реквизиты: …» с пустой строкой после него "
            "(или ничего, если реквизиты не заданы)\n{deadline} — блок «📅 Дедлайн: …» с пустой "
            "строкой после него (или ничего, если дедлайн не задан)\n{penalties} — блок "
            "«⚠️ Штрафы за отмену: …» с пустой строкой после него (или ничего, если штрафов "
            "нет)\n\nБлоки {requisites}/{deadline}/{penalties} бот собирает сам из настроек "
            "«💰 Реквизиты оплаты», «📅 Дедлайн оплаты», «⚠️ Штрафы за отмену» — тут задаётся "
            "только их место в сообщении. Плейсхолдеры лучше не убирать: без {amount} и "
            "{requisites} делегат не поймёт, сколько и куда платить. Поддерживается HTML."
        ),
        "default": (
            "💰 <b>Оплата участия</b>\n\nВариант: {option}\nСумма: {amount} ₽\n\n"
            "{requisites}{deadline}{penalties}📎 Загрузи чек оплаты (PDF-документ или скриншот)."
        ),
    },
    "payment_pay_later_text": {
        "type": "text", "group": "pay", "label": "⏭ «Оплачу позже»: ответ",
        "prompt": (
            "Первая строка ответа на кнопку «⏭ Оплачу позже». Ниже бот сам добавит реквизиты "
            "(если заданы) и подсказку про кнопку «💳 Оплата» в меню. Поддерживается HTML."
        ),
        "default": "Ок! Оплатишь позже.",
    },
    "payment_pay_later_menu_hint_text": {
        "type": "text", "group": "pay", "label": "⏭ «Оплачу позже»: подсказка про меню",
        "prompt": (
            "Последняя строка ответа на кнопку «⏭ Оплачу позже» — где потом искать оплату. "
            "Поддерживается HTML."
        ),
        "default": "Кнопка «💳 Оплата» будет в меню, пока чек не отправлен.",
    },
    "payment_receipt_received_text": {
        "type": "text", "group": "pay", "label": "✅ Чек получен: подтверждение",
        "prompt": (
            "Что видит делегат сразу после того, как прислал чек (PDF или скриншот). "
            "Поддерживается HTML."
        ),
        "default": "✅ Чек получен! Менеджер проверит его в ближайшее время.",
    },

    # ── REG-01/REG-03 (06-02): "party" group ("🎉 Party") ───────────────────────────────
    # party_closed_text carries a non-None `default` — this is the exact string that used to
    # live in handlers/admin.py::_SETTINGS_DISPLAY_DEFAULTS (T-06-06); moving it here is what
    # lets admin.py derive that dict from the registry instead of a separate literal table.
    # party_sheet_tab moved OUT of this group to "sheets" (quick 260815-3hw) — see above.
    "party_closed_text": {
        "type": "text", "group": "party", "label": "🎉 Текст «вечеринка закрыта»",
        "prompt": (
            "Текст, который увидит гость по вечеринковой ссылке (?start=party_over / "
            "party_noover), пока трек выключен (party_enabled = ❌). Показывается вместе "
            "с кнопкой «Перейти к полной регистрации».\n\nОставьте пустым — будет "
            "стандартный текст."
        ),
        "default": "Регистрация на вечеринку сейчас закрыта.",
    },
    # Phase 09.2 (B, Q2 resolution): deliberately NO "per_city" flag here. City override
    # composes with the base `approve_text` only, not with this party-track variant — a
    # party delegate's approve text is decided purely by track (party vs global), never by
    # city, in this wave. See the comment above the base `approve_text` entry.
    "approve_text__party": {
        "type": "text", "group": "party", "label": "🎉 После одобрения (Party)",
        "prompt": (
            "Текст, который увидит PARTY-делегат при одобрении заявки (переопределяет "
            "общий «🎉 После одобрения» только для трека Party). Оставьте пустым или "
            "отправьте «-» — party-делегат получит общий текст «🎉 После одобрения». "
            "Поддерживается HTML."
        ),
        "default": None,
    },

    # Phase 17.1 (17.1-03, schema-completeness): развилка формата при /start
    # (party_fork_question=on) — читалась из bot_settings, но в реестре не была. Дефолт
    # байт-в-байт равен прежнему DEFAULT_PARTY_FORK_TEXT (registration.py берёт его отсюда).
    "party_fork_text": {
        "type": "text", "group": "party", "label": "🔀 Развилка формата: вопрос",
        "prompt": (
            "Экран выбора формата участия при /start (когда включены «🎉 Трек вечеринки» и "
            "«🔀 Вопрос-развилка формата») — текст над кнопками «Полная регистрация» / "
            "«🎉 Гости с ночёвкой» / «🎉 Гости без ночёвки»."
        ),
        "default": "Выбери формат участия:",
    },

    # ── REG-01/REG-03 (06-02): "consent" group ("📋 Согласия") ──────────────────────────
    "consent_button_text": {
        "type": "text", "group": "consent", "label": "✅ Текст кнопки согласия",
        "prompt": "Надпись на кнопке согласия (по умолчанию «Согласен(-на)»).",
        "default": None,
    },
    "consent_list": {
        "type": "list", "group": "consent", "label": "📋 Список согласий",
        "prompt": (
            "Согласия, которые участник примет в конце анкеты.\n\nКаждое согласие — "
            "отдельной строкой в формате:\nВидимое название | короткий_ключ_латиницей\n\n"
            "Ключ нужен, чтобы привязать к согласию PDF. Пример (две строки):\nСогласие на "
            "обработку данных|data\nПолитика конфиденциальности|policy\n\nЕсли на телефоне "
            "Enter отправляет сообщение и несколько строк ввести не получается — раздели "
            "согласия точкой с запятой «;» в одну строку:\nСогласие на обработку "
            "данных|data; Политика конфиденциальности|policy\n\nПосле сохранения загрузите "
            "PDF в разделе «🧾 PDF согласий»."
        ),
        "default": None,
    },
    # Quick 260822 (версионирование согласий): какую редакцию текста/PDF подписывает
    # делегат. Пишется в аудит-таблицу при каждой подписи (database.db.record_user_consent);
    # менеджер поднимает её руками, когда правит consent_list или перезаливает PDF.
    "consent_version": {
        "type": "text", "group": "consent", "label": "🔖 Версия согласия",
        "prompt": (
            "Метка редакции согласия — например 2026-08 или v2. Меняйте её при правке "
            "текста или PDF согласия: так в заявке видно, кто подписал старую редакцию."
        ),
        "default": DEFAULT_CONSENT_VERSION,
    },
    "consent_recollect_text": {
        "type": "text", "group": "consent", "label": "🔁 Текст просьбы пересогласиться",
        "prompt": (
            "Сообщение делегату перед повторным показом согласия, когда редакция "
            "изменилась (показывается, если включено «Просить пересогласие»)."
        ),
        "default": (
            "Мы обновили текст согласия на обработку данных. Пожалуйста, прочитайте "
            "его ещё раз и подтвердите."
        ),
    },

    # ── REG-01/REG-02 (06-04, D-06/D-12): "reg_questions" group — every reg_q_* toggle.
    # Labels copied byte-for-byte from handlers/registration.py::REG_LABELS (cannot import
    # that module here — registration.py imports SETTINGS_SCHEMA, so the reverse import would
    # cycle, T-06-14). Defaults copied byte-for-byte from the pre-migration REG_DEFAULTS
    # literal (handlers/registration.py:197-241) — verified count 43 (not 44, see 06-04
    # SUMMARY deviation note). handlers/registration.py::REG_DEFAULTS is now DERIVED from
    # these entries (a comprehension filtering type == "toggle"), not the other way round.
    "reg_q_age": {"type": "toggle", "group": "reg_questions", "label": "🎂 Возраст", "prompt": None, "default": "on"},
    "reg_q_vk": {"type": "toggle", "group": "reg_questions", "label": "🔵 ВК", "prompt": None, "default": "on"},
    "reg_q_email": {"type": "toggle", "group": "reg_questions", "label": "📧 Email", "prompt": None, "default": "off"},
    "reg_q_phone": {"type": "toggle", "group": "reg_questions", "label": "📱 Телефон", "prompt": None, "default": "off"},
    "reg_q_city": {"type": "toggle", "group": "reg_questions", "label": "🏙 Город", "prompt": None, "default": "off"},
    "reg_q_source": {"type": "toggle", "group": "reg_questions", "label": "📢 Источник", "prompt": None, "default": "on"},
    "reg_q_lc": {"type": "toggle", "group": "reg_questions", "label": "🏢 Лок. комитет", "prompt": None, "default": "off"},
    "reg_q_position": {"type": "toggle", "group": "reg_questions", "label": "👔 Позиция", "prompt": None, "default": "off"},
    "reg_q_education": {"type": "toggle", "group": "reg_questions", "label": "🎓 Образование", "prompt": None, "default": "on"},
    "reg_q_university": {"type": "toggle", "group": "reg_questions", "label": "🏫 ВУЗ", "prompt": None, "default": "on"},
    "reg_q_course": {"type": "toggle", "group": "reg_questions", "label": "📖 Курс", "prompt": None, "default": "on"},
    "reg_q_study_field": {"type": "toggle", "group": "reg_questions", "label": "🎯 Направление обучения", "prompt": None, "default": "on"},
    "reg_q_specialty": {"type": "toggle", "group": "reg_questions", "label": "📝 Специальность", "prompt": None, "default": "off"},
    "reg_q_work": {"type": "toggle", "group": "reg_questions", "label": "💼 Работа", "prompt": None, "default": "on"},
    "reg_q_work_sphere": {"type": "toggle", "group": "reg_questions", "label": "🏭 Сфера работы", "prompt": None, "default": "on"},
    "reg_q_skills": {"type": "toggle", "group": "reg_questions", "label": "💡 Навыки", "prompt": None, "default": "on"},
    "reg_q_expectations": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания (общие)", "prompt": None, "default": "on"},
    "reg_q_attendance": {"type": "toggle", "group": "reg_questions", "label": "📍 Формат", "prompt": None, "default": "off"},
    "reg_q_informal_day": {"type": "toggle", "group": "reg_questions", "label": "🏕 Неформальный день", "prompt": None, "default": "off"},
    "reg_q_comments": {"type": "toggle", "group": "reg_questions", "label": "💬 Доп. комментарии", "prompt": None, "default": "off"},
    "reg_q_department": {"type": "toggle", "group": "reg_questions", "label": "🏢 Департамент", "prompt": None, "default": "off"},
    "reg_q_aiesec_role": {"type": "toggle", "group": "reg_questions", "label": "🎖 Позиция AIESEC", "prompt": None, "default": "off"},
    "reg_q_certificate": {"type": "toggle", "group": "reg_questions", "label": "📄 Справка в ВУЗ", "prompt": None, "default": "off"},
    "reg_q_alumni_status": {"type": "toggle", "group": "reg_questions", "label": "🎓 Аламни/айсекер", "prompt": None, "default": "off"},
    "reg_q_english": {"type": "toggle", "group": "reg_questions", "label": "🇬🇧 Англ. язык", "prompt": None, "default": "off"},
    "reg_q_allergies": {"type": "toggle", "group": "reg_questions", "label": "🤧 Аллергии", "prompt": None, "default": "off"},
    "reg_q_food": {"type": "toggle", "group": "reg_questions", "label": "🥗 Питание", "prompt": None, "default": "off"},
    "reg_q_arrival": {"type": "toggle", "group": "reg_questions", "label": "🚌 Приезд", "prompt": None, "default": "off"},
    "reg_q_housing": {"type": "toggle", "group": "reg_questions", "label": "🏠 Проживание", "prompt": None, "default": "off"},
    "reg_q_bed_sharing": {"type": "toggle", "group": "reg_questions", "label": "🛏 Общая кровать", "prompt": None, "default": "off"},
    "reg_q_bed_partner": {"type": "toggle", "group": "reg_questions", "label": "🛏 Сосед по кровати", "prompt": None, "default": "off"},
    "reg_q_transport": {"type": "toggle", "group": "reg_questions", "label": "🚗 Трансфер", "prompt": None, "default": "off"},
    "reg_q_payment_date": {"type": "toggle", "group": "reg_questions", "label": "💳 Дата оплаты", "prompt": None, "default": "off"},
    "reg_q_cc_shop": {"type": "toggle", "group": "reg_questions", "label": "🛍 CC-shop", "prompt": None, "default": "off"},
    "reg_q_exp_organizers": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания: организация", "prompt": None, "default": "off"},
    "reg_q_exp_content": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания: контент", "prompt": None, "default": "off"},
    "reg_q_volunteer": {"type": "toggle", "group": "reg_questions", "label": "🙋 Волонтёр", "prompt": None, "default": "off"},
    "reg_q_arrival_date": {"type": "toggle", "group": "reg_questions", "label": "📅 Дата приезда", "prompt": None, "default": "off"},
    "reg_q_birth_date": {"type": "toggle", "group": "reg_questions", "label": "🎂 Дата рождения", "prompt": None, "default": "off"},
    "reg_q_goal": {"type": "toggle", "group": "reg_questions", "label": "🎯 Цель участия", "prompt": None, "default": "off"},
    "reg_q_formats": {"type": "toggle", "group": "reg_questions", "label": "📋 Форматы форума", "prompt": None, "default": "off"},
    "reg_q_ambassador": {"type": "toggle", "group": "reg_questions", "label": "🧡 Амбассадор", "prompt": None, "default": "off"},
    "reg_q_resume": {"type": "toggle", "group": "reg_questions", "label": "📄 Резюме", "prompt": None, "default": "off"},

    # ── REG-01 (06-04, D-12): "toggles" group — feature-switch enums. Consumer read-sites
    # (handlers/admin.py, handlers/registration.py, handlers/payment.py, services/scheduler.py)
    # are NOT wired in this plan — that is 06-05 (admin) / 06-06 (registration/payment/
    # scheduler). Defaults verified byte-for-byte from those live call sites (06-04-PLAN.md
    # interfaces table); type is "enum" (not "toggle") so get_setting_typed returns the exact
    # resolved STRING the existing `!= "on"`/`== "on"`/string-comparison call sites depend on
    # (D-15's `raw if raw else default` falsy->default branch reproduces the live
    # `get_setting(k) or "<default>"` idiom byte-for-byte, incl. empty-string).
    "party_enabled": {
        "type": "enum", "group": "toggles", "label": "🎉 Трек вечеринки",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "party_fork_question": {
        "type": "enum", "group": "toggles", "label": "🔀 Вопрос-развилка формата",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "reg_bonus_enabled": {
        "type": "enum", "group": "toggles", "label": "🎁 Бонус за регистрацию",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "payment_enabled": {
        "type": "enum", "group": "toggles", "label": "💳 Модуль оплаты",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "consent_enabled": {
        "type": "enum", "group": "toggles", "label": "📋 Модуль согласий",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "payment_reminders_enabled": {
        "type": "enum", "group": "toggles", "label": "⏰ Автонапоминания об оплате",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    # Quick 260819 (schema-completeness): два фоновых напоминания, раньше читались только
    # через `get_setting(...) != "off"` (дефолт on) и менеджеру были недоступны. Enum on/off
    # (не toggle — см. комментарий у menu_* ниже), кнопки — на лендинге «⚙️ Настройки».
    "pending_reminder_enabled": {
        "type": "enum", "group": "toggles", "label": "📋 Сводка о заявках в ожидании",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "nudge_enabled": {
        "type": "enum", "group": "toggles", "label": "⏰ Догонялка брошенных анкет",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    # Quick 260822: гейт пересогласия. Дефолт OFF — поведение /start для уже
    # зарегистрированных не меняется, пока менеджер не включит тумблер в «📋 Согласия».
    "consent_recollect_enabled": {
        "type": "enum", "group": "toggles", "label": "🔁 Просить пересогласие при новой редакции",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "edu_conditional": {
        "type": "enum", "group": "toggles", "label": "🎓 Условный вопрос об образовании",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "reg_show_progress": {
        "type": "enum", "group": "toggles", "label": "📊 Прогресс-бар анкеты",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "reg_university_mode": {
        "type": "enum", "group": "toggles", "label": "🏫 Режим выбора ВУЗа",
        "options": ["text", "list"], "prompt": None, "default": "text",
    },
    "registration_mode": {
        "type": "enum", "group": "toggles", "label": "📝 Форма регистрации",
        "options": ["short", "full"], "prompt": None, "default": "short",
        "per_city": True,
    },
    "pending_notify_mode": {
        "type": "enum", "group": "toggles", "label": "🔔 Уведомление о заявке",
        "options": ["instant", "batched"], "prompt": None, "default": "batched",
    },
    "full_approval": {
        "type": "enum", "group": "toggles", "label": "✅ Модерация (полная форма)",
        "options": ["manual", "auto"], "prompt": None, "default": "manual",
    },
    "short_approval": {
        "type": "enum", "group": "toggles", "label": "✅ Модерация (краткая форма)",
        "options": ["manual", "auto"], "prompt": None, "default": "auto",
    },
    "party_approval": {
        "type": "enum", "group": "toggles", "label": "✅ Модерация вечеринки",
        "options": ["manual", "auto"], "prompt": None, "default": "manual",
    },
    # Phase 07.1 (CITY-01): master gate for the city-selection screen/deep-links. Type
    # "enum" (NOT "toggle" — REG_DEFAULTS/preset auto-overwrite only "toggle"-typed keys,
    # see handlers/registration.py REG_DEFAULTS + handlers/admin.py preset loop). Default
    # "off" — deploying this plan changes nothing for anyone; a manager flips it on later.
    "event_city_enabled": {
        "type": "enum", "group": "toggles", "label": "🏙 Выбор города мероприятия",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    # Phase 17.1 (17.1-03, schema-completeness): гейт предотбора по Google-таблице
    # (VERIF-01/02). Тип "enum" on/off, НЕ "toggle" — по той же причине, что и
    # event_city_enabled выше (toggle-тип зарезервирован за reg_q_* и переписывается пресетом).
    # Дефолт "off" = прежний `get_setting("preselect_enabled") or "off"`. Кнопка-переключатель
    # на лендинге настроек — `toggle_preselect_enabled` (handlers/admin_settings.py).
    "preselect_enabled": {
        "type": "enum", "group": "toggles", "label": "🎯 Предотбор по таблице",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },

    # Phase 8 (ROLE-02, D-09/D-10): role -> capability matrix + per-role kill switch.
    # `role_caps_<role>` is `type: "list"` (one capability per line, or `;`-separated —
    # same idiom as source_options/city_options); the `_parse_setting` list branch already
    # returns `list(default)` on empty raw, so no new parsing machinery is needed.
    # `role_<role>_enabled` is `type: "enum"` (NOT "toggle") — byte-identical to
    # payment_enabled/consent_enabled/party_enabled, so it plugs into the existing generic
    # `_toggle_module_setting` helper (reads get_setting_typed(key) == "on") with zero
    # conversion at the call site (see 08-01-PLAN.md Task 2 discretion note).
    "role_caps_reg_manager": {
        "type": "list", "group": "roles", "label": "🛂 Права роли: Менеджер регистраций",
        "prompt": "Права роли, по одному на строке (или через «;»): moderate_reg, "
                  "moderate_receipts, moderate_game, broadcast, settings, stats, checkin",
        "default": ["moderate_reg", "moderate_receipts"],
    },
    "role_caps_game_manager": {
        "type": "list", "group": "roles", "label": "🎮 Права роли: Менеджер геймификации",
        "prompt": "Права роли, по одному на строке (или через «;»): moderate_reg, "
                  "moderate_receipts, moderate_game, broadcast, settings, stats, checkin",
        "default": ["moderate_game"],
    },
    "role_reg_manager_enabled": {
        "type": "enum", "group": "roles", "label": "🛂 Роль «Менеджер регистраций»",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "role_game_manager_enabled": {
        "type": "enum", "group": "roles", "label": "🎮 Роль «Менеджер геймификации»",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },

    # ── Phase 09.1 (A): "game" group ("🎮 Геймификация") ────────────────────────────────
    # Every text the free-form submission flow shows a delegate/manager, editable without
    # touching code (CLAUDE.md «мы делаем фулл редактируемого бота»). Defaults are byte-for-
    # byte the literals that used to live in handlers/user_actions.py (_GS_PROOF_PROMPTS +
    # the hardcoded "Принято!.." string) -- moving them here is a no-op for existing behavior.
    "game_proof_prompt_photo": {
        "type": "text", "group": "game", "label": "🎮 Промпт: фото/скрин",
        "prompt": (
            "Что увидит делегат, если задание просит только фото/скрин (единственный "
            "отмеченный тип подтверждения). Оставьте пустым — вернётся текст по умолчанию."
        ),
        "default": "Пришли скриншот/фото:",
    },
    "game_proof_prompt_pdf": {
        "type": "text", "group": "game", "label": "🎮 Промпт: файл",
        "prompt": (
            "Что увидит делегат, если задание просит только файл (единственный отмеченный "
            "тип подтверждения). Оставьте пустым — вернётся текст по умолчанию."
        ),
        "default": "Пришли файл (PDF):",
    },
    "game_proof_prompt_text": {
        "type": "text", "group": "game", "label": "🎮 Промпт: текст",
        "prompt": (
            "Что увидит делегат, если задание просит только текст (единственный отмеченный "
            "тип подтверждения). Оставьте пустым — вернётся текст по умолчанию."
        ),
        "default": "Напиши текстом:",
    },
    "game_proof_prompt_link": {
        "type": "text", "group": "game", "label": "🎮 Промпт: ссылка",
        "prompt": (
            "Что увидит делегат, если задание просит только ссылку (единственный отмеченный "
            "тип подтверждения). Оставьте пустым — вернётся текст по умолчанию."
        ),
        "default": "Пришли ссылку:",
    },
    "game_proof_prompt_any": {
        "type": "text", "group": "game", "label": "🎮 Промпт: несколько типов / не важно",
        "prompt": (
            "Что увидит делегат, если задание не сужает тип подтверждения (ни одного "
            "отмеченного типа) или просит сразу несколько. Оставьте пустым — вернётся "
            "текст по умолчанию."
        ),
        "default": "Пришли подтверждение:",
    },
    "game_proof_done_hint": {
        "type": "text", "group": "game", "label": "🎮 Подсказка «жми Готово»",
        "prompt": (
            "Строка, которая всегда добавляется в конец промпта сдачи — объясняет "
            "делегату, что финализирует сдачу только кнопка «✅ Готово», не таймаут."
        ),
        "default": "Когда всё прислал — нажми «✅ Готово».",
    },
    "game_proof_done_button": {
        "type": "text", "group": "game", "label": "🎮 Подпись кнопки «Готово»",
        "prompt": "Текст инлайн-кнопки, которой делегат финализирует сдачу.",
        "default": "✅ Готово",
    },
    "game_proof_empty_hint": {
        "type": "text", "group": "game", "label": "🎮 Подсказка: пустая сдача",
        "prompt": (
            "Всплывающая подсказка, если делегат жмёт «Готово» до того, как прислал "
            "хоть одну часть — единственная серверная валидация сдачи."
        ),
        "default": "Сначала пришли хотя бы одну часть — фото, файл, текст или ссылку.",
    },
    "game_submit_accepted_text": {
        "type": "text", "group": "game", "label": "🎮 Текст «Принято!»",
        "prompt": "Что увидит делегат сразу после успешной финализации сдачи.",
        "default": "Принято! Менеджер проверит и начислит монеты.",
    },
    # Phase 16 (16-02, GAME-UI-02): Экран 3 -- одно служебное сообщение-счётчик сдачи,
    # редактируемое на каждую часть, и кнопка «Убрать последнее» на нём.
    "game_proof_collected_template": {
        "type": "text", "group": "game", "label": "🎮 Счётчик сдачи: шаблон",
        "prompt": (
            "Служебное сообщение под промптом сдачи — бот обновляет его на каждую присланную "
            "часть. Подставляются: {count} — сколько частей уже собрано, {breakdown} — "
            "разбивка по видам (например «📸2 ✍️1»). Пример: «Частей: {count} · {breakdown}»."
        ),
        "default": "Частей: {count} · {breakdown}",
    },
    "game_proof_remove_last_button": {
        "type": "text", "group": "game", "label": "🎮 Кнопка «Убрать последнее»",
        "prompt": (
            "Текст инлайн-кнопки на счётчике сдачи, которой делегат убирает последнюю "
            "присланную часть из черновика (до нажатия «✅ Готово»)."
        ),
        "default": "🗑 Убрать последнее",
    },

    # Phase 14 (GAME-10): resubmit-limit after rejection, int precedent =
    # pending_reminder_interval above.
    "game_resubmit_limit": {
        "type": "int", "group": "game", "label": "🔁 Лимит перезаливов после отклонения",
        "prompt": (
            "Сколько раз делегат может переотправить сдачу после отклонения по ОДНОМУ "
            "заданию. Счёт ведётся по паре «задание + делегат».\n\n"
            "Примеры:\n0 = без лимита (по умолчанию)\n"
            "2 = после двух отклонений бот попросит делегата написать менеджеру\n\n"
            "Меняется на лету, перезапуск не нужен."
        ),
        "default": 0,
    },

    # Quick 260819-gtl (CONTEXT.md decision 8): title/photo wizard step prompts — new keys,
    # same group "game". Old tasks (title IS NULL) fall back to task_title()'s auto-derived
    # first line, never these prompts (prompts are shown only while CREATING/editing a task).
    "game_task_title_prompt": {
        "type": "text", "group": "game", "label": "🎮 Промпт: название задания",
        "prompt": (
            "Первый шаг визарда создания задания — просит короткое название (1–60 символов, "
            "переносы строк заменяются на пробел)."
        ),
        "default": "Название задания (коротко, до 60 символов):",
    },
    "game_task_photo_prompt": {
        "type": "text", "group": "game", "label": "🎮 Промпт: фото-обложка задания",
        "prompt": (
            "Шаг визарда после описания задания — просит фото-обложку или пропуск. "
            "Показывается и при создании, и при замене фото у существующего задания."
        ),
        "default": "📷 Пришли фото-обложку или нажми ⏭ Пропустить.",
    },

    # Phase 16 (16-03, GAME-UI-03): менеджерские экраны заданий — заголовок превью «как видит
    # делегат» (карточка правки существующего задания), заголовок и кнопка публикации на
    # финальном шаге визарда создания.
    "game_task_preview_intro": {
        "type": "text", "group": "game", "label": "🎮 Превью задания: заголовок",
        "prompt": (
            "Первая строка экрана «👁 Как видит делегат» в карточке правки задания — под ней "
            "идёт карточка задания ровно в том виде, в каком её увидит делегат."
        ),
        "default": "👁 Так увидит делегат в списке заданий:",
    },
    "game_wizard_preview_title": {
        "type": "text", "group": "game", "label": "🎮 Визард: заголовок превью",
        "prompt": (
            "Первая строка финального шага создания задания (перед карточкой «как увидит "
            "делегат» и кнопкой публикации). Поддерживается HTML."
        ),
        "default": "👁 <b>Так увидит делегат</b>",
    },
    "game_wizard_publish_btn": {
        "type": "text", "group": "game", "label": "🎮 Визард: кнопка публикации",
        "prompt": "Подпись кнопки, которой менеджер публикует новое задание на финальном шаге.",
        "default": "✅ Опубликовать",
    },

    # Phase 16 (16-01, GAME-UI-01): RU-подписи категорий (единственный источник —
    # handlers/game_labels.py::category_label; коды GAME_CATEGORIES в БД не меняются) + тексты
    # редизайна экранов «🎯 Задания»/«🪙 Баланс».
    "game_category_label_light": {
        "type": "text", "group": "game", "label": "🎮 Категория «Light» (RU)",
        "prompt": "Как категория «Light» подписывается делегату в списке заданий и карточке.",
        "default": "Лёгкое",
    },
    "game_category_label_medium": {
        "type": "text", "group": "game", "label": "🎮 Категория «Medium» (RU)",
        "prompt": "Как категория «Medium» подписывается делегату в списке заданий и карточке.",
        "default": "Среднее",
    },
    "game_category_label_hard": {
        "type": "text", "group": "game", "label": "🎮 Категория «Hard» (RU)",
        "prompt": "Как категория «Hard» подписывается делегату в списке заданий и карточке.",
        "default": "Сложное",
    },
    "game_category_label_referral": {
        "type": "text", "group": "game", "label": "🎮 Категория «Referral» (RU)",
        "prompt": "Как категория «Referral» подписывается делегату в списке заданий и карточке.",
        "default": "Реферальное",
    },
    "game_category_label_special": {
        "type": "text", "group": "game", "label": "🎮 Категория «Special» (RU)",
        "prompt": "Как категория «Special» подписывается делегату в списке заданий и карточке.",
        "default": "Особое",
    },
    "game_task_list_empty": {
        "type": "text", "group": "game", "label": "🎮 Список заданий: пусто",
        "prompt": "Что видит делегат в «🎯 Задания», если активных заданий сейчас нет.",
        "default": "Активных заданий сейчас нет. Загляни попозже!",
    },
    "game_task_list_page_label": {
        "type": "text", "group": "game", "label": "🎮 Список заданий: подпись страницы",
        "prompt": (
            "Заголовок страницы списка заданий, когда заданий больше одной страницы.\n\n"
            "Подставляются:\n{page} — номер текущей страницы\n{total} — всего страниц\n\n"
            "Плейсхолдеры не убирать — бот сам подставит значения."
        ),
        "default": "стр. {page}/{total}",
    },
    "game_task_detail_status_label": {
        "type": "text", "group": "game", "label": "🎮 Карточка задания: статус",
        "prompt": (
            "Строка статуса в карточке задания.\n\n"
            "Подставляется:\n{status} — уже готовая RU-фраза статуса (включая «· попытка K "
            "из N», если применимо)\n\nПлейсхолдер не убирать."
        ),
        "default": "Статус: {status}",
    },
    "balance_screen_header": {
        "type": "text", "group": "game", "label": "🪙 Баланс: заголовок",
        "prompt": (
            "Заголовок экрана «🪙 Баланс».\n\nПодставляются:\n{balance} — текущий баланс\n"
            "{rank} — место в общем рейтинге («—», если у делегата ещё нет ни одной операции)\n"
            "{total} — сколько всего человек в рейтинге\n\nПлейсхолдеры не убирать."
        ),
        "default": "🪙 Баланс: {balance} монет\nМесто в общем рейтинге: {rank} из {total}",
    },
    "balance_history_empty": {
        "type": "text", "group": "game", "label": "🪙 Баланс: пустая история",
        "prompt": "Что видит делегат на экране баланса, если у него ещё не было ни одной операции.",
        "default": "Пока не было ни одной операции.",
    },
    "balance_source_manual_label": {
        "type": "text", "group": "game", "label": "🪙 Баланс: подпись источника «вручную»",
        "prompt": "Подпись источника операции в истории баланса, когда причина не указана и монеты начислены/списаны вручную менеджером.",
        "default": "вручную",
    },
    "balance_source_task_label": {
        "type": "text", "group": "game", "label": "🪙 Баланс: подпись источника «задание»",
        "prompt": "Подпись источника операции в истории баланса, когда причина не указана и монеты начислены за задание.",
        "default": "задание",
    },

    # Phase 17.1 (17.1-01): экраны «🏆 Рейтинг», «📜 История монет» и рефералка — последние
    # делегатские тексты монетного блока, остававшиеся литералами в handlers/user_actions.py.
    # Группа та же, "game": менеджер правит весь монетный блок в одном месте.
    # Phase 17.1 (17.1-01): RU-подписи типов подтверждения — зеркально game_category_label_*
    # выше (единственный источник — handlers/game_labels.py::proof_types_label; коды
    # GAME_PROOF_TYPES в БД не меняются). Админская копия
    # handlers/admin_gamification.py::_proof_types_label остаётся литеральной до 16-03,
    # который репойнтит её на game_labels (не в скоупе).
    "game_proof_type_label_photo": {
        "type": "text", "group": "game", "label": "🎮 Тип подтверждения «фото» (RU)",
        "prompt": "Как тип подтверждения «фото/скриншот» подписывается делегату в карточке задания.",
        "default": "📷 Скриншот/фото",
    },
    "game_proof_type_label_pdf": {
        "type": "text", "group": "game", "label": "🎮 Тип подтверждения «PDF» (RU)",
        "prompt": "Как тип подтверждения «PDF-файл» подписывается делегату в карточке задания.",
        "default": "📄 PDF",
    },
    "game_proof_type_label_text": {
        "type": "text", "group": "game", "label": "🎮 Тип подтверждения «текст» (RU)",
        "prompt": "Как тип подтверждения «текст» подписывается делегату в карточке задания.",
        "default": "✍️ Текст",
    },
    "game_proof_type_label_link": {
        "type": "text", "group": "game", "label": "🎮 Тип подтверждения «ссылка» (RU)",
        "prompt": "Как тип подтверждения «ссылка» подписывается делегату в карточке задания.",
        "default": "🔗 Ссылка",
    },
    "game_proof_type_unspecified_text": {
        "type": "text", "group": "game", "label": "🎮 Тип подтверждения не задан",
        "prompt": (
            "Что стоит в строке «Нужно прислать: …», если у задания не выбран ни один тип "
            "подтверждения (принимается что угодно)."
        ),
        "default": "не важно",
    },
    "game_task_overdue_hint_text": {
        "type": "text", "group": "game", "label": "⏰ Карточка задания: срок вышел",
        "prompt": (
            "Строка в карточке задания, когда дедлайн уже прошёл. Дедлайн мягкий: сдать "
            "задание всё ещё можно, решение по монетам остаётся за менеджером."
        ),
        "default": "⏰ Срок вышел — отправить можно, монеты решит менеджер",
    },
    "leaderboard_header_text": {
        "type": "text", "group": "game", "label": "🏆 Рейтинг: заголовок",
        "prompt": "Первая строка экрана «🏆 Рейтинг» (и команды /рейтинг). Поддерживается HTML.",
        "default": "🏆 <b>Рейтинг по монетам</b>",
    },
    "leaderboard_empty_text": {
        "type": "text", "group": "game", "label": "🏆 Рейтинг: пусто",
        "prompt": "Что видит делегат в рейтинге, пока ни у кого нет монет.",
        "default": "Пока ни у кого нет монет.",
    },
    "leaderboard_rank_line_text": {
        "type": "text", "group": "game", "label": "🏆 Рейтинг: строка «твоё место»",
        "prompt": (
            "Последняя строка экрана рейтинга — про самого делегата.\n\n"
            "Подставляются:\n{rank} — его место («—», если у него ещё нет монет)\n"
            "{balance} — его баланс\n{total} — сколько всего человек в рейтинге («—», если "
            "монет нет ни у кого)\n\nПлейсхолдеры не убирать — бот сам подставит значения. "
            "Поддерживается HTML."
        ),
        "default": "Твоё место: <b>{rank}</b> · баланс: <b>{balance}</b>",
    },
    "balance_history_header_text": {
        "type": "text", "group": "game", "label": "📜 История монет: заголовок",
        "prompt": (
            "Первая строка экрана «📜 История» (открывается с экрана «🪙 Баланс»). "
            "Поддерживается HTML."
        ),
        "default": "📜 <b>История монет</b>",
    },
    "referral_link_prompt_text": {
        "type": "text", "group": "game", "label": "🔗 Реферальная ссылка: текст",
        "prompt": (
            "Что видит делегат по кнопке «🔗 Моя реферальная ссылка».\n\n"
            "Подставляется:\n{link} — его личная ссылка-приглашение\n\n"
            "Плейсхолдер не убирать — иначе делегат не увидит саму ссылку."
        ),
        "default": "Отправь эту ссылку друзьям, чтобы пригласить их на форум!\n\n{link}",
    },
    "referral_list_header_text": {
        "type": "text", "group": "game", "label": "👥 Приглашённые: заголовок списка",
        "prompt": (
            "Заголовок над списком приглашённых (кнопка «👥 Мои приглашённые»).\n\n"
            "Подставляется:\n{count} — сколько человек пришло по ссылке\n\n"
            "Плейсхолдер не убирать. Поддерживается HTML."
        ),
        "default": "👥 <b>Твои приглашённые ({count}):</b>",
    },
    "referral_list_empty_text": {
        "type": "text", "group": "game", "label": "👥 Приглашённые: пусто",
        "prompt": (
            "Что видит делегат по кнопке «👥 Мои приглашённые», пока по его ссылке никто "
            "не зарегистрировался.\n\nПодставляется:\n{link} — его личная ссылка-приглашение\n\n"
            "Плейсхолдер не убирать — иначе делегату нечего будет переслать друзьям."
        ),
        "default": (
            "Пока никто не зарегистрировался по твоей ссылке.\n\n"
            "Поделись ей с друзьями:\n{link}"
        ),
    },

    # Phase 14 (GAME-09): текст уведомления делегату после ручного начисления/списания монет
    # (кнопочный визард «🪙 Монеты вручную» и /coins). Нейтрален к знаку -- тем же текстом
    # сообщается и о списании. `.replace` подстановка (не `.format`) -- см. _notify_manual_coins.
    "coins_manual_notify_text": {
        "type": "text", "group": "game", "label": "🪙 Сообщение делегату о ручных монетах",
        "prompt": (
            "Что получит делегат сразу после того, как менеджер вручную начислит или спишет "
            "монеты (кнопка «🪙 Монеты вручную» или команда /coins).\n\n"
            "Подставляются:\n"
            "{delta} — сумма со знаком (например «+5» или «−3»)\n"
            "{reason} — причина, которую написал менеджер\n"
            "{balance} — новый баланс делегата после операции\n\n"
            "Сообщение уходит сразу после подтверждения операции."
        ),
        "default": "🪙 Баланс изменён: {delta} монет.\nПричина: {reason}\nТекущий баланс: {balance}",
    },
    # Phase 16 (16-04, GAME-UI-03, Экран 8): quick-pick сумм на шаге «Сколько монет?» --
    # разбирается fail-soft (не-числа пропускаются), свободный ввод остаётся всегда.
    "coins_manual_amount_presets": {
        "type": "text", "group": "game", "label": "🪙 Быстрые суммы (через запятую)",
        "prompt": (
            "Числа через запятую — доступны как кнопки быстрого выбора суммы при начислении/"
            "списании монет вручную. Знак (+/-) добавляется автоматически по уже выбранному "
            "действию.\n\nПример: 5,10,20"
        ),
        "default": "5,10,20",
    },
    # Quick 260822: уведомления менеджеру о сдачах — каждую отдельно или дайджестом по окну
    # тишины. Enum без текстового ввода: на экране «🎮 Геймификация» это тумблер
    # (handlers/admin_gamification.py::toggle_game_submit_notify), ключ в _GAME_FIELD_ORDER
    # НЕ входит — менеджер не должен печатать код варианта. Подписи вариантов — рядом, в
    # GAME_SUBMIT_NOTIFY_MODE_LABELS (единственный источник для тумблера и алерта).
    "game_submit_notify_mode": {
        "type": "enum", "group": "game", "label": "📥 Уведомления о сдачах",
        "options": ["each", "digest"],
        "prompt": None,
        "default": "each",
    },
    "game_submit_digest_minutes": {
        "type": "int", "group": "game", "label": "📥 Дайджест сдач: окно тишины (мин)",
        "prompt": (
            "Через сколько минут тишины слать сводку о новых сдачах, например 15.\n\n"
            "Работает только в режиме «Пачкой (дайджест)»: каждая новая сдача откладывает "
            "отправку ещё на столько минут, сводка уходит, когда поток стихает."
        ),
        "default": 15,
    },

    # ── Phase 14 (CFG-01): group "system" — тайминги прокси переехали из .env. Оба значения
    # читаются ОДИН раз в конструкторе FailoverAiohttpSession (services/proxy_session.py) —
    # правка тут применяется только после перезапуска бота (14-RESEARCH.md, Pitfall 5,
    # решение (a): переносим, но честно пишем об этом менеджеру в prompt).
    "proxy_recheck_seconds": {
        "type": "int", "group": "system", "label": "⏱ Возврат на основной прокси",
        "prompt": (
            "Сколько секунд бот сидит на резервном прокси, прежде чем снова попробовать "
            "вернуться на основной канал.\n\nВ СЕКУНДАХ. Примеры:\n"
            "600 = 10 минут (по умолчанию)\n1800 = 30 минут\n\n"
            "⚠️ Применяется только после перезапуска бота."
        ),
        "default": 600,
    },
    "proxy_connect_timeout": {
        "type": "int", "group": "system", "label": "⏱ Таймаут подключения к прокси",
        "prompt": (
            "Сколько секунд бот ждёт УСТАНОВКИ соединения с прокси (не ответа Telegram), "
            "прежде чем считать канал мёртвым и переключиться на резерв.\n\n"
            "В СЕКУНДАХ. Примеры:\n5 = пять секунд на установку соединения (по умолчанию)\n"
            "0 = без ограничения (как было раньше — мёртвый канал вешает бота на 20-90 сек)\n\n"
            "⚠️ Применяется только после перезапуска бота."
        ),
        "default": 5,
    },
    # Quick 260819 (schema-completeness): интервалы фоновых джоб планировщика
    # (services/scheduler.py::init_scheduler). Дефолты = прежние литералы `_int_or_default(...)`.
    # Как и прокси-тайминги выше, читаются один раз при старте — применяются после перезапуска.
    "nudge_scan_minutes": {
        "type": "int", "group": "system", "label": "⏱ Догонялка: как часто проверять",
        "prompt": (
            "Как часто бот ищет брошенные анкеты, чтобы напомнить о них.\n\nВ МИНУТАХ. "
            "Примеры:\n15 = каждые 15 минут (по умолчанию)\n60 = раз в час\n\n"
            "⚠️ Применяется только после перезапуска бота."
        ),
        "default": 15,
    },
    "allowlist_refresh_minutes": {
        "type": "int", "group": "system", "label": "⏱ Предотбор: обновление списка из таблицы",
        "prompt": (
            "Как часто бот перечитывает вкладку предотбора из Google-таблицы.\n\nВ МИНУТАХ. "
            "Примеры:\n60 = раз в час (по умолчанию)\n15 = каждые 15 минут\n\n"
            "⚠️ Применяется только после перезапуска бота."
        ),
        "default": 60,
    },
    "incomplete_sync_hours": {
        "type": "int", "group": "system", "label": "⏱ Вкладка «Незавершённые»: автообновление",
        "prompt": (
            "Как часто бот сам обновляет вкладку незавершённых регистраций в таблице.\n\n"
            "В ЧАСАХ. Примеры:\n2 = раз в два часа (по умолчанию)\n1 = каждый час\n\n"
            "⚠️ Применяется только после перезапуска бота."
        ),
        "default": 2,
    },

    # ── Phase 09.2 (B): group "menu" — тумблеры кнопок главного меню ────────────────────
    # Source of these 9 keys/labels: keyboards/builders.py::MENU_BUTTONS (copied byte-for-
    # byte, emoji included). Deliberately `type: "enum"`, options ["on", "off"], NOT
    # `type: "toggle"`. Reason: `REG_DEFAULTS` is derived from SETTINGS_SCHEMA by filtering
    # `type == "toggle"` (handlers/registration.py:237-239), and `_apply_event_preset`
    # (handlers/admin.py:3061-3069) unconditionally sweeps EVERY REG_DEFAULTS key on every
    # tap of an event-type preset (forum/conference/custom), writing "on"/"off" to each.
    # If menu_* were "toggle"-typed, tapping any preset in the admin UI would silently
    # force every menu button off (none of them match a reg_q_* preset name). This is the
    # SAME established workaround already used by payment_enabled/consent_enabled/
    # role_reg_manager_enabled/event_city_enabled — all documented "NOT toggle" for the
    # exact same reason.
    # `per_city: True` — Phase 09.2 (A): kept as an editable, per-city-overridable setting,
    # same mechanism as any other per_city key. Consumers (get_main_menu_kb, the admin
    # "🔘 Кнопки главного меню" screen) are NOT migrated in this plan — they stay on raw
    # get_setting reads until plans 09.2-03/09.2-06; registry and raw reads coexist by
    # design during incremental migration (Phase 6 invariant).
    "menu_referral": {
        "type": "enum", "group": "menu", "label": "🔗 Моя реферальная ссылка",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_invites": {
        "type": "enum", "group": "menu", "label": "👥 Мои приглашённые",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_info": {
        "type": "enum", "group": "menu", "label": "ℹ️ Информация о форуме",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_program": {
        "type": "enum", "group": "menu", "label": "📅 Программа форума",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_speakers": {
        "type": "enum", "group": "menu", "label": "🗣 Спикеры",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_contacts": {
        "type": "enum", "group": "menu", "label": "📞 Контакты",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_question": {
        "type": "enum", "group": "menu", "label": "❓ Задать вопрос",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_coins": {
        "type": "enum", "group": "menu", "label": "🪙 Мои монеты",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    "menu_game_tasks": {
        "type": "enum", "group": "menu", "label": "🎯 Задания",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },
    # Phase 19 (D-10): текстовая reply-кнопка «📱 Приложение» рядом с «🎯 Задания». Форма
    # записи ОБЯЗАНА совпадать с menu_game_tasks — экран «🔘 Кнопки меню» и пер-городные
    # резолверы подхватывают кнопку по группе `menu` автоматически. Сама кнопка рисуется
    # только при включённом тумблере miniapp_enabled (точки входа — план 19-08).
    "menu_miniapp": {
        "type": "enum", "group": "menu", "label": "📱 Приложение",
        "options": ["on", "off"], "prompt": None, "default": "on",
        "per_city": True,
    },

    # ── Phase 15 (15-02, D-19): экран «📊 Дашборд» — тумблеры блоков веб-дашборда. Только
    # чекбоксы фиксированного набора (CLAUDE.md «бот для людей» — кодовые значения ключей
    # менеджеру не показываем, текстовый ввод не просим). Own group "dashboard" — эти ключи
    # намеренно НЕ добавлены в handlers.admin_settings.SETTINGS_FIELDS/SETTINGS_GROUPS: у них
    # свой отдельный экран (handlers/admin_dashboard.py), попадание в «Прочие» дало бы вторую
    # конкурирующую поверхность правки (тот же принцип, что применён к role_caps_* в Phase 8).
    # per_city не ставим — дашборд один на стек (D-04).
    #
    # Дефолты "on" у всех блоков, КРОМЕ dashboard_block_game — "off". Глобального тумблера
    # самого модуля геймификации в реестре нет (есть только per_city пункт меню
    # menu_game_tasks), поэтому D-12 «гейма показывается только при включённом модуле»
    # реализуется этим явным менеджерским тумблером плюс проверкой наличия данных на стороне
    # дашборда — а не выдуманным новым флагом модуля.
    "dashboard_block_funnel": {
        "type": "enum", "group": "dashboard", "label": "🪜 Воронка",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_dynamics": {
        "type": "enum", "group": "dashboard", "label": "📈 Динамика по дням",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_universities": {
        "type": "enum", "group": "dashboard", "label": "🏫 ВУЗы",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_sources": {
        "type": "enum", "group": "dashboard", "label": "📢 Источники",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_courses": {
        "type": "enum", "group": "dashboard", "label": "📖 Курсы",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_study_fields": {
        "type": "enum", "group": "dashboard", "label": "🎯 Направления",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_dropout": {
        "type": "enum", "group": "dashboard", "label": "🚪 Где бросают",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_game": {
        "type": "enum", "group": "dashboard", "label": "🎮 Гейма",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },

    # ── Phase 19 (D-06): экран «🎨 Оформление» Mini App — тумблеры, оформление, чекбоксы
    # разделов и тексты, которых нет в группах game/menu. Own group "miniapp": как и
    # dashboard_block_*, эти ключи НЕ добавляются в handlers.admin_settings.SETTINGS_FIELDS/
    # SETTINGS_GROUPS — у них своя поверхность правки (план 19-08), иначе они всплыли бы в
    # «📦 Прочие» второй конкурирующей поверхностью. per_city нет — приложение одно на стек.
    #
    # Дефолт miniapp_enabled — "off": новая поверхность включается менеджером осознанно.
    # Все miniapp_section_* по умолчанию "on". Тексты экранов бота (game_*/menu_*) здесь НЕ
    # дублируются — экраны читают существующие ключи (правило Phase 17.1: 0 хардкода).
    "miniapp_enabled": {
        "type": "enum", "group": "miniapp", "label": "📱 Mini App включён",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "miniapp_staff_only": {
        "type": "enum", "group": "miniapp", "label": "🔒 Только менеджерам",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "miniapp_accent": {
        "type": "text", "group": "miniapp", "label": "🎨 Цвет акцента",
        "prompt": (
            "Цвет акцента приложения в формате HEX: решётка и шесть символов после неё, "
            "например #037EF3. Базовый синий AIESEC — #037EF3."
        ),
        "default": "#037EF3",
    },
    "miniapp_logo": {
        "type": "photo", "group": "miniapp", "label": "🖼 Лого мероприятия",
        "prompt": "Отправьте фото логотипа — оно появится в шапке приложения.",
        "default": None,
    },
    # Разделы-чекбоксы: по одному на экран приложения (делегат + менеджер).
    "miniapp_section_tasks": {
        "type": "enum", "group": "miniapp", "label": "🎯 Задания",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_coins": {
        "type": "enum", "group": "miniapp", "label": "🪙 Монеты",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_leaderboard": {
        "type": "enum", "group": "miniapp", "label": "🏆 Рейтинг",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_profile": {
        "type": "enum", "group": "miniapp", "label": "👤 Профиль",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_review": {
        "type": "enum", "group": "miniapp", "label": "🎮 Проверка сдач",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_admin_tasks": {
        "type": "enum", "group": "miniapp", "label": "🗂 Задания (менеджер)",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_stats": {
        "type": "enum", "group": "miniapp", "label": "📊 Статистика",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "miniapp_section_settings": {
        "type": "enum", "group": "miniapp", "label": "⚙️ Настройки-лайт",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    # Тексты, специфичные для Mini App (человеческие дефолты — менеджер может не трогать).
    "miniapp_open_text": {
        "type": "text", "group": "miniapp", "label": "💬 Сообщение с кнопкой приложения",
        "prompt": (
            "Текст сообщения, которое бот присылает вместе с кнопкой «Открыть приложение» "
            "(например: «Задания, монеты и рейтинг — в одном экране»)."
        ),
        "default": "Задания, монеты и рейтинг — в одном экране. Открывай приложение 👇",
    },
    "miniapp_open_button": {
        "type": "text", "group": "miniapp", "label": "🔘 Подпись кнопки приложения",
        "prompt": "Подпись кнопки, открывающей приложение (например: «📱 Открыть приложение»).",
        "default": "📱 Открыть приложение",
    },
    "miniapp_open_in_bot_text": {
        "type": "text", "group": "miniapp", "label": "🤖 Экран «откройте через бота»",
        "prompt": (
            "Текст, который видит человек, открывший приложение не из Telegram "
            "(например: «Это приложение открывается из бота — нажмите кнопку ниже»)."
        ),
        "default": (
            "Это приложение открывается из бота. Нажмите «📱 Приложение» в меню бота — "
            "и всё заработает."
        ),
    },
    "miniapp_login_button": {
        "type": "text", "group": "miniapp", "label": "🔑 Кнопка входа для менеджеров",
        "prompt": "Подпись кнопки запасного входа через Telegram (например: «Войти через Telegram»).",
        "default": "Войти через Telegram",
    },
    "miniapp_login_hint": {
        "type": "text", "group": "miniapp", "label": "ℹ️ Пояснение под кнопкой входа",
        "prompt": (
            "Короткое пояснение под кнопкой входа: для кого она и что сделать после "
            "(например: «Вход для менеджеров. После входа вернитесь на адрес приложения»)."
        ),
        "default": (
            "Вход для менеджеров. После входа вернитесь на адрес приложения — оно откроется "
            "в обычном браузере."
        ),
    },
    "miniapp_session_expired_text": {
        "type": "text", "group": "miniapp", "label": "⏳ Сессия истекла",
        "prompt": "Текст при истёкшей сессии (например: «Сессия истекла — откройте приложение заново»).",
        "default": "Сессия истекла — откройте приложение заново.",
    },
    "miniapp_disabled_text": {
        "type": "text", "group": "miniapp", "label": "🚧 Приложение выключено",
        "prompt": "Текст, когда приложение выключено тумблером (например: «Приложение временно недоступно»).",
        "default": "Приложение временно недоступно. Всё то же самое есть в боте.",
    },
    "miniapp_no_access_text": {
        "type": "text", "group": "miniapp", "label": "⛔ Нет доступа",
        "prompt": "Текст, когда раздел недоступен или выключен (например: «Этот раздел вам недоступен»).",
        "default": "Этот раздел сейчас недоступен.",
    },
    "miniapp_upload_too_large_text": {
        "type": "text", "group": "miniapp", "label": "📦 Файл слишком большой",
        "prompt": "Текст при файле больше 20 МБ (например: «Файл больше 20 МБ — пришлите его через бота»).",
        "default": "Файл больше 20 МБ — пришлите его через бота.",
    },
    "miniapp_profile_edit_hint": {
        "type": "text", "group": "miniapp", "label": "✏️ Подсказка «Изменить — в боте»",
        "prompt": "Подсказка у кнопки «Изменить — в боте» в профиле (например: «Анкета правится в боте»).",
        "default": "Анкета правится в боте — кнопка откроет нужный шаг.",
    },
    "miniapp_upload_caption_delegate": {
        "type": "text", "group": "miniapp", "label": "📎 Подпись копии сдачи",
        "prompt": "Подпись файла, который бот пересылает делегату как копию сдачи (например: «копия сдачи»).",
        "default": "копия сдачи",
    },
    "miniapp_upload_caption_staff": {
        "type": "text", "group": "miniapp", "label": "📎 Подпись файла менеджера",
        "prompt": "Подпись файла, загруженного менеджером из приложения (например: «загружено из приложения»).",
        "default": "загружено из приложения",
    },
    "miniapp_confirm_disable_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Подтверждение: выключить приложение",
        "prompt": (
            "Текст второго тапа при выключении Mini App — что именно пропадёт (например: "
            "«Выключить Mini App? Приложение исчезнет у всех делегатов и менеджеров»)."
        ),
        "default": (
            "Выключить Mini App? Приложение исчезнет у всех делегатов и менеджеров, включая "
            "вас — включить обратно получится только из бота."
        ),
    },
    "miniapp_confirm_staff_only_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Подтверждение: скрыть от делегатов",
        "prompt": (
            "Текст второго тапа при включении «Только менеджерам» — что именно пропадёт "
            "(например: «Скрыть приложение от делегатов?»)."
        ),
        "default": (
            "Скрыть приложение от делегатов? Оно останется доступным только менеджерам, "
            "делегаты увидят его снова, когда вы выключите этот тумблер."
        ),
    },

    # ── Phase 19.1 Plan 02 (D-03/D-04/D-07/D-08/D-15/D-16/D-18): ручки пресетов оформления —
    # web_theme.py разрешает их в CSS-переменные, «🎨 Оформление» бота их выставляет (план
    # 19.1-07). miniapp_accent НЕ дублируется новым ключом — существующий ключ остаётся
    # «акцентом» ради миграции уже настроенных стендов (UI-SPEC).
    "miniapp_theme_preset": {
        "type": "enum", "group": "miniapp", "label": "🎭 Пресет оформления",
        "options": ["bluebook", "youlead", "custom"], "prompt": None, "default": "bluebook",
    },
    "miniapp_theme_secondary": {
        "type": "text", "group": "miniapp", "label": "🎨 Вторичный цвет",
        "prompt": (
            "Вторичный цвет оформления в формате HEX: решётка и шесть символов после неё, "
            "например #F48924."
        ),
        "default": "#F48924",
    },
    "miniapp_theme_bg": {
        "type": "text", "group": "miniapp", "label": "🎨 Цвет фона",
        "prompt": (
            "Цвет фона приложения в формате HEX: решётка и шесть символов после неё, "
            "например #F3F4F7."
        ),
        "default": "#F3F4F7",
    },
    "miniapp_theme_heading_font": {
        "type": "enum", "group": "miniapp", "label": "🔤 Шрифт заголовков",
        "options": ["raleway", "raleway_italic", "lato"], "prompt": None, "default": "raleway",
    },
    "miniapp_theme_playful_tone": {
        "type": "enum", "group": "miniapp", "label": "😄 Игривый тон текстов",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "miniapp_theme_pattern_enabled": {
        "type": "enum", "group": "miniapp", "label": "✨ Бренд-паттерн на фоне",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    "miniapp_logo_dark": {
        "type": "photo", "group": "miniapp", "label": "🌙 Лого для тёмной темы",
        "prompt": (
            "Отправьте фото логотипа для тёмной темы — необязательно, без него используется "
            "обычное лого."
        ),
        "default": None,
    },
    "miniapp_cover": {
        "type": "photo", "group": "miniapp", "label": "🖼 Обложка приложения",
        "prompt": "Отправьте фото обложки — она появится на приветственном экране.",
        "default": None,
    },
    "miniapp_cover_dark": {
        "type": "photo", "group": "miniapp", "label": "🌙 Обложка для тёмной темы",
        "prompt": (
            "Отправьте фото обложки для тёмной темы — необязательно, без него используется "
            "обычная обложка."
        ),
        "default": None,
    },
    "miniapp_sticker_empty": {
        "type": "photo", "group": "miniapp", "label": "🖼 Стикер «пусто»",
        "prompt": "Отправьте стикер/картинку для пустых списков (заданий, монет и т.п.).",
        "default": None,
    },
    "miniapp_sticker_success": {
        "type": "photo", "group": "miniapp", "label": "🖼 Стикер «успех»",
        "prompt": "Отправьте стикер/картинку для экрана успеха (сдача принята и т.п.).",
        "default": None,
    },
    "miniapp_sticker_error": {
        "type": "photo", "group": "miniapp", "label": "🖼 Стикер «ошибка»",
        "prompt": "Отправьте стикер/картинку для экрана ошибки.",
        "default": None,
    },
    "miniapp_sticker_top1": {
        "type": "photo", "group": "miniapp", "label": "🖼 Стикер «топ-1»",
        "prompt": "Отправьте стикер/картинку для делегата на первом месте рейтинга.",
        "default": None,
    },
    "miniapp_coin_icon": {
        "type": "photo", "group": "miniapp", "label": "🪙 Своя иконка монеты",
        "prompt": (
            "Отправьте свою иконку монеты — необязательно, без неё используется иконка "
            "пресета."
        ),
        "default": None,
    },
    "miniapp_onboarding_text": {
        "type": "text", "group": "miniapp", "label": "👋 Текст приветственного экрана",
        "prompt": (
            "Текст, который делегат видит при первом открытии приложения (например: "
            "«Привет! Делаешь задания — получаешь монеты.»)."
        ),
        "default": "Привет! Делаешь задания — получаешь монеты.",
    },
    "miniapp_onboarding_cta": {
        "type": "text", "group": "miniapp", "label": "👋 Кнопка приветственного экрана",
        "prompt": "Подпись кнопки на приветственном экране (например: «Погнали»).",
        "default": "Погнали",
    },
    "miniapp_empty_admin_tasks": {
        "type": "text", "group": "miniapp", "label": "🗂 Пустой список активных заданий",
        "prompt": "Текст, когда у менеджера ещё нет активных заданий (например: «Заданий пока нет.»).",
        "default": "Заданий пока нет.",
    },
    "miniapp_empty_admin_tasks_archived": {
        "type": "text", "group": "miniapp", "label": "🗂 Пустой архив заданий",
        "prompt": "Текст, когда архив заданий менеджера пуст (например: «Архив пуст.»).",
        "default": "Архив пуст.",
    },
    "miniapp_empty_admin_coins": {
        "type": "text", "group": "miniapp", "label": "🪙 Пустой журнал монет",
        "prompt": (
            "Текст, когда журнал ручных операций с монетами пуст (например: «Ручных операций "
            "пока не было.»)."
        ),
        "default": "Ручных операций пока не было.",
    },
    "miniapp_empty_review": {
        "type": "text", "group": "miniapp", "label": "🔍 Пустая очередь проверки",
        "prompt": (
            "Текст, когда сдач на проверку нет вообще (например: «Сдач на проверке нет.»)."
        ),
        "default": "Сдач на проверке нет.",
    },
    "miniapp_empty_review_skipped": {
        "type": "text", "group": "miniapp", "label": "🔍 Очередь проверки: всё пропущено",
        "prompt": (
            "Текст, когда менеджер пропустил все сдачи в очереди (сами сдачи остались). "
            "«{count}» подставится числом оставшихся сдач (например: «Пропущено всё — "
            "осталось {count}.»)."
        ),
        "default": "Пропущено всё — осталось {count}.",
    },
}


# Quick 260822: человеческие подписи режимов game_submit_notify_mode (CLAUDE.md: коды
# capability/enum менеджеру не показываем). Ключи = options выше.
GAME_SUBMIT_NOTIFY_MODE_LABELS = {
    "each": "Каждую сдачу отдельно",
    "digest": "Пачкой (дайджест)",
}


def _parse_setting(key, raw):
    """Pure sync parse dispatch (D-03/D-08) — no DB, no async, the unit-test surface.

    Looks up `SETTINGS_SCHEMA[key]` for `type`/`default` and dispatches by type. If `key`
    is not (yet) registered, returns `raw` unchanged — fail-soft, so coexistence with
    unmigrated keys (still read via raw `get_setting` elsewhere) holds during the
    incremental migration.
    """
    entry = SETTINGS_SCHEMA.get(key)
    if entry is None:
        return raw

    # Optional per-entry override (D-03) — rare exception, not the rule.
    override = entry.get("parse")
    if override is not None:
        return override(raw)

    entry_type = entry["type"]
    default = entry.get("default")

    if entry_type == "text":
        # text default is None; the render layer's own truthiness check handles empty
        # display — only a genuinely missing (None) raw falls back to default.
        return raw if raw is not None else default

    if entry_type == "enum":
        # CRITICAL byte-for-byte contract (D-15): falsy -> default. Both None AND
        # empty-string resolve to default — this matches the live `get_setting(k) or
        # "<default>"` idiom every feature-switch consumer relies on. Do NOT use
        # `is not None` here — it would diverge from the live idiom on empty-string.
        return raw if raw else default

    if entry_type in ("photo", "file"):
        # Passthrough raw file_id (D-10); None falls back to default (None).
        return raw if raw is not None else default

    if entry_type == "int":
        # Lifted verbatim from services/scheduler.py::_int_or_default /
        # services/reminders.py::_reminder_interval — positive int or default;
        # None/empty/garbage/<=0 -> default.
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    if entry_type == "date":
        # Lifted verbatim from services/scheduler.py::_parse_schedule_dt — admin datetime
        # 'ДД.ММ.ГГГГ ЧЧ:ММ'; bad input -> default (None for unset date entries).
        try:
            return datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
        except (TypeError, ValueError, AttributeError):
            return default

    if entry_type == "list":
        # Phase 8 (ROLE-02): a registry `default` may itself be a non-empty list literal
        # (role_caps_* — see D-09), unlike every pre-phase-8 "list" entry (which used
        # `default: None`). Real DB reads never hand this branch anything but str|None
        # (bot_settings.value is TEXT), so this only fires for an already-a-list `raw` --
        # pass it through unchanged instead of crashing on `.splitlines()`.
        if isinstance(raw, list):
            return raw
        # Lifted verbatim from handlers/registration.py::_get_options (splitlines/strip),
        # extended to also accept the `;` inline separator already used elsewhere in the
        # project for the Telegram "Enter = send" mobile trap.
        if raw:
            items = [
                segment.strip()
                for line in raw.splitlines()
                for segment in line.split(";")
                if segment.strip()
            ]
            if items:
                return items
        return list(default) if default else []

    if entry_type == "toggle":
        # Lifted verbatim from handlers/admin.py::_is_question_on (the reg_q question
        # idiom, admin.py:2095-2097) — returns bool.
        return (raw == "on") if raw is not None else (default == "on")

    # Unknown type in the registry itself — fail-soft rather than raise.
    return raw


async def get_setting_typed(key: str):
    """Thin async accessor (D-05) — raw read via the existing `get_setting` (database.db,
    unchanged, D-07) then dispatch through the pure `_parse_setting`. Does not duplicate
    raw I/O; calls `get_setting` exactly once."""
    raw = await get_setting(key)
    return _parse_setting(key, raw)
