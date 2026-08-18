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

from database.db import get_setting

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
    "pending_reminder_interval": {
        "type": "int", "group": "reg", "label": "🕒 Тайминг батчей заявок",
        "prompt": (
            "Как часто бот присылает админам сводку «Заявок в ожидании: N» (режим "
            "«Пачкой»).\n\nВ СЕКУНДАХ. Примеры:\n900 = 15 мин\n1800 = 30 мин (по умолчанию)"
            "\n3600 = 1 час\n\nМеняется на лету, перезапуск не нужен."
        ),
        "default": 1800,
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
