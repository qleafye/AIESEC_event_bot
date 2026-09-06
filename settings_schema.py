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
  (project convention: domain entities are plain dicts, see docs/CONVENTIONS.md).
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
from reg_labels import REG_LABELS

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
        "prompt": (
            "Дата мероприятия — в свободной форме, как удобно читать делегату (например "
            "«12–13 сентября 2026»). Показывается в разделе «Информация о форуме» и в "
            "приветствии /start."
        ),
        "default": None,
        "per_city": True,
    },
    "event_time": {
        "type": "text", "group": "event", "label": "⌚ Время",
        "prompt": (
            "Время начала — свободный текст (например «10:00, сбор с 9:30»). Показывается "
            "рядом с датой в разделе «Информация о форуме» и в приветствии /start."
        ),
        "default": None,
        "per_city": True,
    },
    "event_place_name": {
        "type": "text", "group": "event", "label": "📍 Место",
        "prompt": (
            "Название площадки (например «Экспоцентр, павильон 2»). Показывается делегату в "
            "разделе «Информация о форуме» вместе с адресом."
        ),
        "default": None,
        "per_city": True,
    },
    "event_place_address": {
        "type": "text", "group": "event", "label": "📫 Адрес",
        "prompt": (
            "Адрес площадки — как делегату проще доехать (например «Москва, "
            "Краснопресненская наб., 14»). Показывается вместе с названием площадки."
        ),
        "default": None,
        "per_city": True,
    },
    "contact_person": {
        "type": "text", "group": "event", "label": "👤 Контакт",
        "prompt": (
            "Юзернейм контактного лица оргкомитета, например @username. Показывается "
            "делегату в разделе «📞 Контакты», если он ещё не задан там же."
        ),
        "default": None,
        "per_city": True,
    },
    "contact_vk": {
        "type": "text", "group": "event", "label": "🔵 VK",
        "prompt": (
            "Ссылка на группу ВКонтакте, например https://vk.com/aiesec_ru. Показывается "
            "делегату в разделе «📞 Контакты»."
        ),
        "default": None,
        "per_city": True,
    },
    "contact_tg": {
        "type": "text", "group": "event", "label": "🔹 TG",
        "prompt": (
            "Ссылка на Telegram-канал, например https://t.me/aiesec_ru. Показывается "
            "делегату в разделе «📞 Контакты»."
        ),
        "default": None,
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
            "ожиданиях (например: «конференции RusCo», «форума ЮЛид», «Годового отчёта»)"
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
    # Quick 260906-8uq (FAQ-01..06): экран «❓ Частые вопросы» показывается делегату ПЕРЕД
    # формой «Задать вопрос» — три текста рядом с двумя выше (та же группа «event», тот же
    # смысловой сосед).
    "faq_intro_text": {
        "type": "text", "group": "event", "label": "❓ Частые вопросы: вступление",
        "prompt": (
            "Текст над списком вопросов на экране «❓ Частые вопросы». Поддерживается HTML."
        ),
        "default": "Собрали ответы на частые вопросы — может, твой уже здесь:",
    },
    "faq_empty_text": {
        "type": "text", "group": "event", "label": "❓ Частые вопросы: пока пусто",
        "prompt": (
            "Что видит делегат по кнопке «❓ Частые вопросы» и на экране «Задать вопрос», "
            "пока в FAQ нет ни одного включённого пункта. Поддерживается HTML."
        ),
        "default": "Пока здесь пусто. Напиши свой вопрос — ответим и добавим сюда.",
    },
    "faq_ask_button_text": {
        "type": "text", "group": "event", "label": "❓ Частые вопросы: подпись кнопки «Спросить менеджера»",
        "prompt": (
            "Подпись кнопки под списком FAQ (и на пустом экране FAQ), которая открывает форму "
            "«Задать вопрос» — обычный текст кнопки, разметка не поддерживается."
        ),
        "default": "Не нашёл ответ — спросить менеджера",
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
        "prompt": (
            "Отправьте фото программы (можно с подписью) — оно появится делегату по кнопке "
            "«📅 Программа форума»."
        ),
        "default": None,
    },
    "speakers": {
        "type": "photo", "group": "event", "label": "🗣 Спикеры",
        "prompt": (
            "Отправьте одно фото со всеми спикерами (можно с подписью) — оно появится "
            "делегату по кнопке «🗣 Спикеры»."
        ),
        "default": None,
    },
    "start": {
        "type": "photo", "group": "event", "label": "💬 Фото приветствия",
        "prompt": (
            "Отправьте фото для приветственного сообщения — оно уйдёт вместе с текстом "
            "«💬 Приветствие» при первом /start."
        ),
        "default": None,
    },
    "venue": {
        "type": "photo", "group": "event", "label": "🏢 Площадка",
        "prompt": (
            "Отправьте фото площадки (можно с подписью) — оно появится вместе с адресом в "
            "разделе «Информация о форуме»."
        ),
        "default": None,
    },
    "reg_bonus": {
        "type": "file", "group": "event", "label": "🎁 Бонус за регистрацию",
        "prompt": (
            "Отправьте файл или фото бонуса (можно с подписью) — бот пришлёт его делегату "
            "сразу после отправки анкеты, пока включён тумблер «🎁 Бонус за регистрацию»."
        ),
        "default": None,
    },

    # ── REG-01/REG-03 (06-02): "reg" group ("📝 Регистрация") ──────────────────────────
    # Labels/prompts copied byte-for-byte from the pre-migration literal SETTINGS_FIELDS
    # tuples (handlers/admin.py). pending_reminder_interval's default is pinned to 1800 to
    # match services/reminders.py::DEFAULT_INTERVAL (T-06-07, proven byte-for-byte by
    # test_parse_equivalence_int).
    "source_options": {
        "type": "list", "group": "reg", "label": "📢 Источники",
        "prompt": (
            "Варианты ответа на вопрос «Откуда узнал(-а) о нас» — каждый с новой строки.\n\n"
            "Пусто = стандартный список («Соцсети АЙСЕК», «Университетские каналы» и т.п.)."
        ),
        "default": None,
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
    # ── Phase 21 Plan 02 (FORM-SYNC-04, D-25): 30 текстов анкеты Mini App + догонялки в
    # группе «📝 Регистрация» — тексты обращены к делегату отмечены per_city (закрытый список
    # из 9 ключей, ровно совпадающий с колонкой per_city «да» в UI-SPEC § Copywriting
    # Contract). Имена — с префиксом reg_form_*/reg_resume_* (не miniapp_form_* из UI-SPEC):
    # префикс miniapp_ у КАЖДОГО ключа обязан иметь group == "miniapp"
    # (tests/test_miniapp_registry.py), а D-25 требует группу "reg" — конфликт снят именем.
    # Плейсхолдеры ({step}/{total}/{count}/{date}) подставляются потребителем ТОЛЬКО через
    # .replace — .format падает на любой фигурной скобке в тексте менеджера (Pitfall 11).
    "reg_form_cta_text": {
        "type": "text", "group": "reg", "label": "📱 Кнопка «Заполнить в приложении»",
        "prompt": (
            "Подпись inline-кнопки, которая появляется ОДИН раз — сразу после приветствия "
            "бота, рядом с «Начать» (D-01)."
        ),
        "default": "📱 Заполнить в приложении",
        "per_city": True,
    },
    "reg_resume_continue_label": {
        "type": "text", "group": "reg", "label": "▶️ Кнопка «Продолжить с шага N»",
        "prompt": (
            "Кнопка на экране «есть незаконченная анкета» при /start. Подстановки {step} "
            "(номер шага) и {total} (всего шагов) подставляются автоматически."
        ),
        "default": "▶️ Продолжить с шага {step} из {total}",
    },
    "reg_resume_restart_label": {
        "type": "text", "group": "reg", "label": "🔄 Кнопка «Начать заново»",
        "prompt": "Вторая кнопка на том же экране — рядом с «Продолжить с шага N».",
        "default": "🔄 Начать заново",
    },
    "reg_resume_restart_confirm_text": {
        "type": "text", "group": "reg", "label": "❓ Подтверждение «Начать заново»",
        "prompt": (
            "Текст подтверждения перед тем, как стереть черновик анкеты. Подстановка "
            "{count} — сколько ответов уже дано, подставляется автоматически. "
            "Поддерживается HTML."
        ),
        "default": "Пропадут уже введённые ответы ({count}). Начать анкету заново?",
    },
    "reg_sync_from_app_text": {
        "type": "text", "group": "reg", "label": "📲 Подхватили ответы из приложения",
        "prompt": (
            "Разовая строка в чате, когда делегат вернулся в бота после правок в "
            "приложении — бот сообщает, что подхватил новые ответы. Поддерживается HTML."
        ),
        "default": "📲 Подхватил ответы, которые вы ввели в приложении.",
    },
    # Quick 260904-3vm (эстафета вместо двустороннего синхрона, решение владельца 04.09):
    # анкета в каждый момент открыта РОВНО в одном месте. Ни один из пяти ключей не per_city —
    # это тексты механики перехода между поверхностями, а не сценарий события (та же причина,
    # что у reg_form_conflict_text/reg_form_no_draft_text рядом).
    "reg_handoff_held_by_app_text": {
        "type": "text", "group": "reg", "label": "📱 Анкета сейчас в приложении",
        "prompt": (
            "Что отвечает бот, если делегат пишет текст в чат, пока анкета открыта в "
            "приложении — бот не принимает этот текст как ответ на вопрос, а показывает "
            "кнопку возврата в чат."
        ),
        "default": "📱 Анкета сейчас открыта в приложении. Продолжить здесь, в чате?",
    },
    "reg_handoff_to_bot_label": {
        "type": "text", "group": "reg", "label": "✍️ Кнопка «Продолжить в чате»",
        "prompt": "Подпись кнопки под сообщением выше — забирает анкету обратно в чат.",
        "default": "✍️ Продолжить в чате",
    },
    "reg_handoff_to_app_text": {
        "type": "text", "group": "reg", "label": "📱 Анкету забрали в приложение",
        "prompt": (
            "Уведомление делегату в чат сразу после того, как анкету открыли/забрали в "
            "приложении (кнопка «Заполнить в приложении» или «Забрать сюда»)."
        ),
        "default": (
            "📱 Анкета открыта в приложении — заполняй там. Захочешь вернуться в чат, "
            "просто напиши мне, и я предложу продолжить здесь."
        ),
    },
    "reg_handoff_resumed_text": {
        "type": "text", "group": "reg", "label": "✍️ Вернулись в чат",
        "prompt": "Разовая строка перед следующим вопросом — когда делегат забрал анкету обратно в чат.",
        "default": "✍️ Возвращаемся в чат. Вот твой вопрос:",
    },
    "reg_already_submitted_text": {
        "type": "text", "group": "reg", "label": "✅ Анкета уже отправлена",
        "prompt": (
            "Ответ бота на любой текст делегата ПОСЛЕ того, как анкета этого сезона уже "
            "подана (например, отправлена из приложения, а бот об этом ещё не знал)."
        ),
        "default": "✅ Анкета уже отправлена — мы её получили. Ответ придёт сюда, в чат.",
    },
    "reg_form_conflict_text": {
        "type": "text", "group": "reg", "label": "🔀 Уведомление о слиянии ответов",
        "prompt": (
            "Всплывающая подсказка в приложении, когда часть полей обновилась из чата "
            "бота — делегат менял анкету параллельно в двух местах."
        ),
        "default": "Ответы обновились из чата — некоторые поля мы обновили автоматически.",
    },
    "reg_form_not_set_text": {
        "type": "text", "group": "reg", "label": "➖ Плейсхолдер незаполненного поля",
        "prompt": (
            "Что видит делегат в приложении в обзоре анкеты напротив вопроса, на который "
            "ещё нет ответа."
        ),
        "default": "Не заполнено",
    },
    # UAT 21-12 находка 2: экран мастера показывал прогресс числом «N/M» без подписи — литерал
    # прямо в JS (нарушает D-25/«ни одного текста в JS»), и только когда включён тумблер
    # `reg_show_progress` (дефолт off — это отдельный, уже существующий переключатель
    # менеджера, не трогается). Подстановки {step}/{total} — тем же приёмом `.replace`, что
    # у reg_resume_continue_label (Pitfall 11: .format падает на «{» в тексте менеджера).
    "reg_form_progress_text": {
        "type": "text", "group": "reg", "label": "📊 Подпись прогресса анкеты",
        "prompt": (
            "Подпись над прогресс-баром мастера в приложении, когда включён тумблер «📊 "
            "Прогресс-бар анкеты». Подстановки {step} (номер шага) и {total} (всего шагов) "
            "подставляются автоматически."
        ),
        "default": "Шаг {step} из {total}",
    },
    "reg_form_submit_cta_text": {
        "type": "text", "group": "reg", "label": "✅ Кнопка «Отправить анкету»",
        "prompt": "Кнопка на последнем шаге приложения — для НОВОЙ анкеты (первая подача).",
        "default": "✅ Отправить анкету",
    },
    "reg_form_edit_submit_cta_text": {
        "type": "text", "group": "reg", "label": "💾 Кнопка «Отправить изменения»",
        "prompt": (
            "Кнопка на последнем шаге приложения — в режиме ПРАВКИ уже поданной анкеты."
        ),
        "default": "💾 Отправить изменения",
    },
    "reg_form_cancel_changes_text": {
        "type": "text", "group": "reg", "label": "↩️ Кнопка «Отменить изменения»",
        "prompt": (
            "Кнопка в обзоре правки анкеты — сбрасывает несохранённые правки до последней "
            "отправленной версии."
        ),
        "default": "↩️ Отменить изменения",
    },
    "reg_form_cancel_changes_confirm_text": {
        "type": "text", "group": "reg", "label": "❓ Подтверждение «Отменить изменения»",
        "prompt": (
            "Текст всплывающего подтверждения перед сбросом несохранённых правок анкеты."
        ),
        "default": (
            "Несохранённые правки пропадут, анкета вернётся к последней отправленной "
            "версии. Отменить изменения?"
        ),
    },
    "reg_form_continue_in_chat_text": {
        "type": "text", "group": "reg", "label": "💬 Кнопка «Продолжить в чате»",
        "prompt": (
            "Кнопка на КАЖДОМ шаге мастера в приложении — переключает делегата обратно в "
            "чат с ботом на том же вопросе."
        ),
        # Phase 23.1 (UI-REDESIGN-04): дефолт без эмодзи-иконки — кнопку и так рисует
        # icon("message-circle"), эмодзи в подписи дублировал бы её. Уже сохранённые
        # менеджером значения не трогаются.
        "default": "Продолжить в чате",
    },
    "reg_form_held_by_bot_text": {
        "type": "text", "group": "reg", "label": "💬 Анкета сейчас в чате",
        "prompt": (
            "Плита в приложении, когда анкету держит чат с ботом — приложение предлагает "
            "продолжить там или забрать анкету себе."
        ),
        "default": "💬 Анкета сейчас открыта в чате с ботом. Можно продолжить там или забрать её сюда.",
    },
    "reg_form_takeover_cta_text": {
        "type": "text", "group": "reg", "label": "📱 Кнопка «Забрать сюда»",
        "prompt": "Кнопка на плите выше — забирает анкету из чата в приложение.",
        "default": "📱 Забрать сюда",
    },
    "reg_form_consent_required_text": {
        "type": "text", "group": "reg", "label": "⚠️ Не подтверждены согласия",
        "prompt": (
            "Ошибка при попытке отправить анкету из приложения без подписанных согласий — "
            "приложение вернёт делегата к шагу согласий."
        ),
        "default": "Нужно подтвердить согласия — вернитесь к шагу «Согласия».",
    },
    "reg_form_no_draft_text": {
        "type": "text", "group": "reg", "label": "🗒️ Нет черновика для отправки",
        "prompt": (
            "Ошибка при попытке отправить анкету из приложения, когда черновика правки "
            "вообще не существует (обзор не подтянул ни одной правки перед отправкой) — "
            "отличается от «уже отправляется» (гонка с чатом)."
        ),
        "default": "Правки не сохранились — откройте поле ещё раз и попробуйте снова.",
    },
    "reg_form_resume_too_large_text": {
        "type": "text", "group": "reg", "label": "📦 Резюме слишком большое",
        "prompt": "Ошибка загрузки резюме в приложении, когда файл больше 20 МБ.",
        "default": "Файл больше 20 МБ — сожмите или пришлите ссылку в описании.",
    },
    "reg_form_resume_wrong_type_text": {
        "type": "text", "group": "reg", "label": "📄 Неверный формат резюме",
        "prompt": (
            "Ошибка загрузки резюме в приложении, когда формат файла не PDF и не DOCX."
        ),
        "default": "Резюме принимается как PDF или DOCX — другой формат не подойдёт.",
    },
    "reg_form_resume_text_only_text": {
        "type": "text", "group": "reg", "label": "📝 Резюме только текстом",
        "prompt": (
            "Ответ делегату, когда в его городе резюме принимается только текстом, а он "
            "присылает файл."
        ),
        "default": "Здесь резюме принимается текстом — напиши коротко в ответном сообщении.",
    },
    "reg_form_resume_upload_error_text": {
        "type": "text", "group": "reg", "label": "⚠️ Резюме не загрузилось (общая ошибка)",
        "prompt": (
            "Показывается, когда загрузка резюме файлом в приложении оборвалась не по "
            "причине формата/размера (обрыв связи, сервер недоступен) — те два случая "
            "уже называют свой текст сами (см. соседние ключи выше)."
        ),
        "default": "Не удалось загрузить файл — попробуй ещё раз или пришли резюме текстом.",
    },
    "reg_form_share_contact_text": {
        "type": "text", "group": "reg", "label": "📱 Кнопка «Поделиться номером»",
        "prompt": (
            "Подпись кнопки на шаге телефона в приложении, которая через Telegram передаёт "
            "номер делегата одним касанием (Bot API 6.9+). Вне Telegram или на старом "
            "клиенте кнопки нет вовсе — только ручной ввод."
        ),
        "default": "📱 Поделиться номером",
    },
    "reg_form_prior_answer_badge_text": {
        "type": "text", "group": "reg", "label": "🕓 Метка «из прошлой анкеты»",
        "prompt": (
            "Короткая подпись-бейдж рядом с полем, которое приложение заранее заполнило "
            "ответом из прошлой анкеты делегата-возвращенца (D-07)."
        ),
        "default": "из прошлой анкеты",
    },
    "reg_form_updated_in_chat_badge_text": {
        "type": "text", "group": "reg", "label": "🔄 Метка «обновлено в чате»",
        "prompt": (
            "Короткая подпись-бейдж рядом с полем, которое приложение перерисовало "
            "значением, введённым делегатом только что в чате бота."
        ),
        "default": "обновлено в чате",
    },
    "reg_form_complete_heading_text": {
        "type": "text", "group": "reg", "label": "🎉 Заголовок «Заявка принята»",
        "prompt": "Заголовок экрана в приложении сразу после отправки НОВОЙ анкеты.",
        "default": "Заявка принята",
        "per_city": True,
    },
    "reg_form_complete_body_text": {
        "type": "text", "group": "reg", "label": "🎉 Текст «Заявка принята»",
        "prompt": "Текст под заголовком того же экрана — сразу после отправки новой анкеты.",
        "default": "Спасибо! Мы всё получили — подробности пришли в чат с ботом.",
        "per_city": True,
    },
    "reg_form_edited_heading_text": {
        "type": "text", "group": "reg", "label": "✏️ Заголовок «Изменения отправлены»",
        "prompt": (
            "Заголовок экрана в приложении после правки уже одобренной анкеты — "
            "показывается, когда тумблер «Изменённая анкета — снова на модерацию» "
            "ВЫКЛЮЧЕН и статус остаётся прежним."
        ),
        "default": "Изменения отправлены",
        "per_city": True,
    },
    "reg_form_edited_pending_heading_text": {
        "type": "text", "group": "reg", "label": "🕓 Заголовок «Анкета снова на проверке»",
        "prompt": (
            "Заголовок того же экрана — показывается, когда тумблер «Изменённая анкета — "
            "снова на модерацию» ВКЛЮЧЁН, и правка вернула заявку в статус ожидания."
        ),
        "default": "Анкета снова на проверке",
        "per_city": True,
    },
    "reg_form_resubmit_heading_text": {
        "type": "text", "group": "reg", "label": "🔁 Заголовок «Заявка отправлена заново»",
        "prompt": "Заголовок экрана в приложении после повторной подачи ОТКЛОНЁННОЙ анкеты.",
        "default": "Заявка отправлена заново",
        "per_city": True,
    },
    "reg_form_closed_text": {
        "type": "text", "group": "reg", "label": "🚪 Регистрация закрыта (в приложении)",
        "prompt": (
            "Экран в приложении, когда регистрация закрыта тумблером или режимом города — "
            "новую анкету начать нельзя, кнопки продолжить нет. Поддерживается HTML."
        ),
        "default": (
            "Регистрация сейчас закрыта. Как только менеджер её откроет — анкета появится "
            "здесь снова."
        ),
        "per_city": True,
    },
    "reg_form_rejected_banner_text": {
        "type": "text", "group": "reg", "label": "🚫 Баннер «Заявку отклонили»",
        "prompt": (
            "Баннер над обзором правки анкеты для делегата, чью заявку отклонили — "
            "приглашает поправить ответы и отправить заново (D-10)."
        ),
        "default": "Заявку отклонили. Можно поправить ответы и отправить ещё раз.",
        "per_city": True,
    },
    "reg_form_profile_edit_cta_text": {
        "type": "text", "group": "reg", "label": "✏️ Кнопка «Изменить анкету» в профиле",
        "prompt": (
            "Кнопка в профиле делегата внутри приложения — открывает анкету на точечную "
            "правку (D-24)."
        ),
        # Phase 23.1 (UI-REDESIGN-04): дефолт без эмодзи-иконки — кнопку рисует
        # icon("pen-line") в screens/profile.js, эмодзи в подписи дублировал бы её.
        # Уже сохранённые менеджером значения не трогаются.
        "default": "Изменить анкету",
    },
    # Phase 23.1 (UI-REDESIGN-04): подписи экрана мастера по макетам 03.09 — плита шага,
    # список вопросов, кнопки «дальше»/«назад» вместо стрелки без подписи.
    "reg_form_questions_eyebrow": {
        "type": "text", "group": "reg", "label": "📋 Надзаголовок списка вопросов",
        "prompt": (
            "Надзаголовок над списком вопросов анкеты в приложении — виден и в мастере "
            "(под полем ответа), и в обзоре точечной правки."
        ),
        "default": "Вопросы анкеты",
    },
    "reg_form_more_questions_text": {
        "type": "text", "group": "reg", "label": "📋 Строка «сколько ещё впереди»",
        "prompt": (
            "Строка под списком вопросов в мастере, когда впереди есть ещё не показанные "
            "вопросы. Подстановка {n} — сколько вопросов осталось, подставляется "
            "автоматически."
        ),
        "default": "…и ещё {n} вопросов впереди",
    },
    "reg_form_draft_saved_text": {
        "type": "text", "group": "reg", "label": "💾 Пометка «черновик сохранён»",
        "prompt": (
            "Мелкая пометка справа от полосы прогресса мастера — видна, когда черновик "
            "анкеты уже существует (после первого ответа)."
        ),
        "default": "Черновик сохранён",
    },
    "reg_form_next_cta_text": {
        "type": "text", "group": "reg", "label": "➡️ Кнопка «дальше» в мастере",
        "prompt": "Кнопка перехода к следующему вопросу мастера в приложении.",
        "default": "Дальше",
    },
    "reg_form_back_cta_text": {
        "type": "text", "group": "reg", "label": "⬅️ Кнопка «назад» в мастере",
        "prompt": (
            "Кнопка перехода к предыдущему вопросу мастера в приложении — видна начиная "
            "со второго шага."
        ),
        "default": "Назад",
    },
    "reg_edited_admin_label": {
        "type": "text", "group": "reg", "label": "✏️ Пометка «Изменена» менеджеру",
        "prompt": (
            "Пометка в карточке заявки бота для менеджера, когда делегат поправил уже "
            "поданную анкету. Подстановка {date} — дата правки, подставляется автоматически."
        ),
        "default": "✏️ Изменена {date}",
    },
    "reg_resubmit_admin_label": {
        "type": "text", "group": "reg", "label": "🔁 Пометка «Повторная подача» менеджеру",
        "prompt": (
            "Пометка в карточке заявки бота для менеджера, когда отклонённый делегат подал "
            "анкету заново."
        ),
        "default": "🔁 Повторная подача",
    },
    # Quick 260904-liz: пометка в карточке повторно поданной заявки о причине прошлого отказа —
    # менеджер разбирал повторную заявку без памяти о том, за что отказал в прошлый раз.
    "reg_prev_reject_admin_label": {
        "type": "text", "group": "reg", "label": "🚫 Пометка «Ранее отклонена» менеджеру",
        "prompt": (
            "Пометка в карточке заявки бота для менеджера, когда отклонённый делегат подал "
            "анкету заново. Подстановка {reason} — причина прошлого отказа. Если причины нет, "
            "пометка не показывается вовсе."
        ),
        "default": "🚫 Ранее отклонена: {reason}",
    },
    "reg_edit_history_button_label": {
        "type": "text", "group": "reg", "label": "🕓 Кнопка «История» в карточке заявки",
        "prompt": (
            "Подпись кнопки в карточке заявки бота, которая открывает менеджеру историю "
            "правок анкеты (D-15)."
        ),
        "default": "🕓 История",
    },
    "reg_nudge_chat_button_text": {
        "type": "text", "group": "reg", "label": "💬 Догонялка: кнопка «в чате»",
        "prompt": (
            "Первая из двух кнопок в сообщении-догонялке брошенной анкеты (D-21) — "
            "продолжить в чате с ботом. Текст самой догонялки не меняется, это подпись "
            "кнопки."
        ),
        "default": "💬 Продолжить в чате",
    },
    "reg_nudge_app_button_text": {
        "type": "text", "group": "reg", "label": "📱 Догонялка: кнопка «в приложении»",
        "prompt": (
            "Вторая кнопка в сообщении-догонялке — продолжить в приложении; показывается, "
            "только если раздел «📝 Анкета» включён (D-08, D-21)."
        ),
        "default": "📱 Продолжить в приложении",
        "per_city": True,
    },
    # Phase 21 Plan 02 (FORM-SYNC-05, D-12): правка одобренной анкеты по умолчанию НЕ снимает
    # заявку с одобрения — включает менеджер осознанно. Экран-тумблер подключает план 21-07.
    "toggle_reg_edit_remoderation": {
        "type": "enum", "group": "reg", "label": "Изменённая анкета — снова на модерацию",
        "options": ["on", "off"],
        "prompt": (
            "Выключено (по умолчанию): делегат поправил уже одобренную анкету — статус "
            "остаётся «Одобрена», в карточке заявки появляется пометка «✏️ Изменена». "
            "Включено: правка возвращает заявку в статус «Ждёт проверки» и уходит "
            "уведомление модераторам по городу — как о новой заявке."
        ),
        "default": "off",
    },
    # Phase 27 (27-03, LANG-04/LANG-10): выбор драйвера машинного перевода делегатской
    # анкеты. Служебный ключ — в SETTINGS_GROUPS/_REG_FIELD_ORDER намеренно НЕ добавляется
    # (тот же приём, что у toggle_reg_edit_remoderation выше: живёт в реестре ради
    # get_setting_typed/валидации/дефолта, но не для повседневной настройки менеджером —
    # переключение делает разработчик/план 27-06 напрямую). Дефолт "embedded" — решение
    # владельца на чекпоинте 27-01 (модель грузится только пока очередь непуста и
    # выгружается сразу после — см. services/i18n_engine.py::EmbeddedArgosDriver.unload).
    "delegate_lang_driver": {
        "type": "enum", "group": "reg", "label": "Драйвер перевода анкеты (служебное)",
        "options": ["embedded", "http"],
        "prompt": None,
        "default": "embedded",
    },
    # HTTP-фоллбэк для delegate_lang_driver="http" — адрес сайдкара LibreTranslate
    # (docker-compose.yml, профиль "i18n", НЕ поднимается по умолчанию). Правило владельца
    # 18.08: настройки живут внутри бота, не в .env — даже для служебного значения.
    "delegate_lang_http_url": {
        "type": "text", "group": "reg", "label": "Адрес HTTP-драйвера перевода (служебное)",
        "prompt": None,
        "default": "http://libretranslate:5000",
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
    # Quick 260902-vth: журналы правок анкеты и вопросов делегатов — листы В ТОЙ ЖЕ таблице
    # заявок (решение владельца), не отдельная таблица. Пересборка ЦЕЛИКОМ (sync_named_worksheet,
    # clear+update), не append по событию — обрыв прокси даёт «лист не обновился», а не молча
    # потерянную строку (память проекта sheet-append-no-retry-on-proxy-drop).
    "history_sheet_tab": {
        "type": "text", "group": "sheets", "label": "🕓 История правок",
        "prompt": (
            "Название вкладки с журналом правок уже поданных анкет (кто что менял и когда). "
            "Если такой вкладки в таблице нет — бот создаст новую с этим именем."
            "\n\nОставьте пустым — будет «История правок»."
        ),
        "default": "История правок",
    },
    "questions_sheet_tab": {
        "type": "text", "group": "sheets", "label": "❓ Вопросы делегатов",
        "prompt": (
            "Название вкладки с вопросами, которые делегаты задают боту («❓ Задать вопрос»). "
            "Если такой вкладки в таблице нет — бот создаст новую с этим именем."
            "\n\nОставьте пустым — будет «Вопросы»."
        ),
        "default": "Вопросы",
    },
    # Тип "enum" on/off, НЕ "toggle" — toggle-тип зарезервирован за reg_q_*
    # (REG_DEFAULTS/пресет автоматически переписывают только toggle-ключи, см. комментарий у
    # event_city_enabled ниже); enum — та же форма, что payment_enabled/consent_enabled.
    "sheet_logs_autosync": {
        "type": "enum", "group": "sheets", "label": "🔄 Обновлять журналы автоматически",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },

    # ── REG-01/REG-03 (06-02): "pay" group ("💳 Оплата") ────────────────────────────────
    "payment_options": {
        "type": "list", "group": "pay", "label": "💳 Варианты оплаты",
        "prompt": (
            "Варианты участия (билеты/тарифы), каждый — отдельной строкой:\nНазвание | "
            "Цена\n\nПример:\nПолный билет|5000\nСтудент|3000\n\nЦена 0 = бесплатно. Если "
            "вариант один — участник его не выбирает, сразу видит реквизиты.\n\n"
            "Необязательное третье поле — фильтр по треку: Название | Цена | треки (треки "
            "— через запятую, пишутся латиницей: full — полная регистрация, "
            "party_overnight — вечеринка с ночёвкой, party_noovernight — вечеринка без "
            "ночёвки). Без третьего поля тариф виден ВСЕМ трекам. Пример строки только для "
            "вечеринки:\nВход на вечеринку|1000|party_overnight,party_noovernight"
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
            "Текст, который увидит гость по вечеринковой пригласительной ссылке, пока трек "
            "«🎉 Трек вечеринки» выключен. Показывается вместе с кнопкой «Перейти к полной "
            "регистрации».\n\nОставьте пустым — будет стандартный текст."
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
    # Quick 260904-3vm (E2, УАТ 04.09): автоприём (short-трек без модерации и подобные
    # сценарии) — своя причина принятия, не «прошёл отбор». БЕЗ per_city — та же причина, что у
    # approve_text__party рядом: третья ось «трек × город» отложена.
    "approve_text__auto": {
        "type": "text", "group": "reg", "label": "✅ После автоприёма (без модерации)",
        "prompt": (
            "Что видит участник, когда заявку принимают сразу, без проверки менеджером "
            "(например, короткая форма с автоодобрением). Пусто — возьмётся текст "
            "«🎉 После одобрения»."
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
    "reg_q_age": {"type": "toggle", "group": "reg_questions", "label": "🎂 Возраст", "prompt": None, "default": "on", "per_city": True},
    "reg_q_vk": {"type": "toggle", "group": "reg_questions", "label": "🔵 ВК", "prompt": None, "default": "on", "per_city": True},
    "reg_q_email": {"type": "toggle", "group": "reg_questions", "label": "📧 Email", "prompt": None, "default": "off", "per_city": True},
    "reg_q_phone": {"type": "toggle", "group": "reg_questions", "label": "📱 Телефон", "prompt": None, "default": "off", "per_city": True},
    "reg_q_city": {"type": "toggle", "group": "reg_questions", "label": "🏙 Город", "prompt": None, "default": "off", "per_city": True},
    "reg_q_source": {"type": "toggle", "group": "reg_questions", "label": "📢 Источник", "prompt": None, "default": "on", "per_city": True},
    "reg_q_lc": {"type": "toggle", "group": "reg_questions", "label": "🏢 Лок. комитет", "prompt": None, "default": "off", "per_city": True},
    "reg_q_position": {"type": "toggle", "group": "reg_questions", "label": "👔 Позиция", "prompt": None, "default": "off", "per_city": True},
    "reg_q_education": {"type": "toggle", "group": "reg_questions", "label": "🎓 Образование", "prompt": None, "default": "on", "per_city": True},
    "reg_q_university": {"type": "toggle", "group": "reg_questions", "label": "🏫 ВУЗ", "prompt": None, "default": "on", "per_city": True},
    "reg_q_course": {"type": "toggle", "group": "reg_questions", "label": "📖 Курс", "prompt": None, "default": "on", "per_city": True},
    "reg_q_study_field": {"type": "toggle", "group": "reg_questions", "label": "🎯 Направление обучения", "prompt": None, "default": "on", "per_city": True},
    "reg_q_specialty": {"type": "toggle", "group": "reg_questions", "label": "📝 Специальность", "prompt": None, "default": "off", "per_city": True},
    "reg_q_work": {"type": "toggle", "group": "reg_questions", "label": "💼 Работа", "prompt": None, "default": "on", "per_city": True},
    "reg_q_work_sphere": {"type": "toggle", "group": "reg_questions", "label": "🏭 Сфера работы", "prompt": None, "default": "on", "per_city": True},
    "reg_q_skills": {"type": "toggle", "group": "reg_questions", "label": "💡 Навыки", "prompt": None, "default": "on", "per_city": True},
    "reg_q_expectations": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания (общие)", "prompt": None, "default": "on", "per_city": True},
    "reg_q_attendance": {"type": "toggle", "group": "reg_questions", "label": "📍 Формат", "prompt": None, "default": "off", "per_city": True},
    "reg_q_informal_day": {"type": "toggle", "group": "reg_questions", "label": "🏕 Неформальный день", "prompt": None, "default": "off", "per_city": True},
    "reg_q_comments": {"type": "toggle", "group": "reg_questions", "label": "💬 Доп. комментарии", "prompt": None, "default": "off", "per_city": True},
    "reg_q_department": {"type": "toggle", "group": "reg_questions", "label": "🏢 Департамент", "prompt": None, "default": "off", "per_city": True},
    "reg_q_aiesec_role": {"type": "toggle", "group": "reg_questions", "label": "🎖 Позиция в АЙСЕК", "prompt": None, "default": "off", "per_city": True},
    "reg_q_certificate": {"type": "toggle", "group": "reg_questions", "label": "📄 Справка в ВУЗ", "prompt": None, "default": "off", "per_city": True},
    "reg_q_alumni_status": {"type": "toggle", "group": "reg_questions", "label": "🎓 Аламни/айсекер", "prompt": None, "default": "off", "per_city": True},
    "reg_q_english": {"type": "toggle", "group": "reg_questions", "label": "🇬🇧 Англ. язык", "prompt": None, "default": "off", "per_city": True},
    "reg_q_allergies": {"type": "toggle", "group": "reg_questions", "label": "🤧 Аллергии", "prompt": None, "default": "off", "per_city": True},
    "reg_q_food": {"type": "toggle", "group": "reg_questions", "label": "🥗 Питание", "prompt": None, "default": "off", "per_city": True},
    "reg_q_arrival": {"type": "toggle", "group": "reg_questions", "label": "🚌 Приезд", "prompt": None, "default": "off", "per_city": True},
    "reg_q_housing": {"type": "toggle", "group": "reg_questions", "label": "🏠 Проживание", "prompt": None, "default": "off", "per_city": True},
    "reg_q_bed_sharing": {"type": "toggle", "group": "reg_questions", "label": "🛏 Общая кровать", "prompt": None, "default": "off", "per_city": True},
    "reg_q_bed_partner": {"type": "toggle", "group": "reg_questions", "label": "🛏 Сосед по кровати", "prompt": None, "default": "off", "per_city": True},
    "reg_q_transport": {"type": "toggle", "group": "reg_questions", "label": "🚗 Трансфер", "prompt": None, "default": "off", "per_city": True},
    "reg_q_payment_date": {"type": "toggle", "group": "reg_questions", "label": "💳 Дата оплаты", "prompt": None, "default": "off", "per_city": True},
    "reg_q_cc_shop": {"type": "toggle", "group": "reg_questions", "label": "🛍 CC-shop", "prompt": None, "default": "off", "per_city": True},
    "reg_q_exp_organizers": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания: организация", "prompt": None, "default": "off", "per_city": True},
    "reg_q_exp_content": {"type": "toggle", "group": "reg_questions", "label": "💬 Ожидания: контент", "prompt": None, "default": "off", "per_city": True},
    "reg_q_volunteer": {"type": "toggle", "group": "reg_questions", "label": "🙋 Волонтёр", "prompt": None, "default": "off", "per_city": True},
    "reg_q_arrival_date": {"type": "toggle", "group": "reg_questions", "label": "📅 Дата приезда", "prompt": None, "default": "off", "per_city": True},
    "reg_q_birth_date": {"type": "toggle", "group": "reg_questions", "label": "🎂 Дата рождения", "prompt": None, "default": "off", "per_city": True},
    "reg_q_goal": {"type": "toggle", "group": "reg_questions", "label": "🎯 Цель участия", "prompt": None, "default": "off", "per_city": True},
    "reg_q_formats": {"type": "toggle", "group": "reg_questions", "label": "📋 Форматы форума", "prompt": None, "default": "off", "per_city": True},
    "reg_q_ambassador": {"type": "toggle", "group": "reg_questions", "label": "🧡 Амбассадор", "prompt": None, "default": "off", "per_city": True},
    "reg_q_resume": {"type": "toggle", "group": "reg_questions", "label": "📄 Резюме", "prompt": None, "default": "off", "per_city": True},

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
    # Quick 260904-dq1: «🌙 Тихие часы» — не будить делегата ночью решением по заявке/сдачей
    # задания/напоминанием. Один тумблер на событие (не per_city — сама фича либо есть, либо
    # нет), сами часы «с»/«до» расходятся по городам ниже. Дефолт OFF: включённая фича не
    # должна ничего задерживать, пока менеджер сам не решит её включить.
    "quiet_hours_enabled": {
        "type": "enum", "group": "toggles", "label": "🌙 Тихие часы",
        "options": ["on", "off"], "prompt": None, "default": "off",
    },
    # Phase 27 (27-02, LANG-01): английский язык делегатской анкеты — модуль целиком.
    # Дефолт OFF ОБЯЗАТЕЛЕН (A-05, 27-CONTEXT.md): при выключенном модуле поведение бота
    # обязано быть байт-в-байт прежним — это условие прохождения golden-снимков
    # (tests/test_reg_engine_parity.py, tests/test_refac_snapshot_260816.py). Дефолты
    # включаются менеджером кнопкой (раздел «📝 Анкета»), код "on"/"off" ему не показывается.
    "delegate_lang_enabled": {
        "type": "enum", "group": "toggles", "label": "🌐 Английский язык анкеты",
        "options": ["on", "off"], "default": "off",
        "prompt": (
            "Включает английский язык для делегатской анкеты и текстов после подачи "
            "(тексты вопросов, варианты ответов, служебные слова). Пока перевод конкретного "
            "текста ещё не готов — делегат видит русский (перевод не мешает регистрации). "
            "Админка и заявки в карточке модерации остаются русскими всегда."
        ),
    },
    # Phase 27 (27-02, LANG-01): спрашивать язык только у тех делегатов, чей клиент Telegram
    # не на русском (resolve_lang в services/i18n.py) — не молчаливое переключение по
    # language_code (часть российских делегатов держит клиент на английском). Дефолт ON:
    # если менеджер включил модуль, разумно по умолчанию сразу предлагать выбор.
    "delegate_lang_ask_on_start": {
        "type": "enum", "group": "toggles", "label": "🌐 Спрашивать язык при старте",
        "options": ["on", "off"], "default": "on",
        "prompt": (
            "Пока «Английский язык анкеты» включён: спрашивать выбор языка кнопками при "
            "первом /start у делегатов, чей Telegram не на русском. Выключено — все "
            "начинают анкету на русском, язык можно сменить позже (переключатель в меню)."
        ),
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
    "reg_resume_mode": {
        "type": "enum", "group": "toggles", "label": "📄 Резюме",
        "options": ["file_or_text", "text_only"], "prompt": None, "default": "file_or_text",
        "per_city": True,
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
    # Quick 260813: экран «✏️ Права роли» — чекбоксы (handlers/admin_roles.py::show_role_caps),
    # не набор кода текстом. Этот prompt здесь не показывается ни в одном живом экране (ключ
    # исключён из редактируемых в Mini App и в общем settings_edit — своя поверхность правки),
    # оставлен человекочитаемым на случай ручного просмотра реестра.
    "role_caps_reg_manager": {
        "type": "list", "group": "roles", "label": "🛂 Права роли: Менеджер регистраций",
        "prompt": (
            "Отмечайте права галочками в боте: 🔧 Управление → «👥 Роли и доступы» → "
            "«✏️ Права роли: 🛂 Менеджер регистраций»."
        ),
        "default": ["moderate_reg", "moderate_receipts"],
    },
    "role_caps_game_manager": {
        "type": "list", "group": "roles", "label": "🎮 Права роли: Менеджер геймификации",
        "prompt": (
            "Отмечайте права галочками в боте: 🔧 Управление → «👥 Роли и доступы» → "
            "«✏️ Права роли: 🎮 Менеджер геймификации»."
        ),
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
    # Quick 260906-8uq (FAQ-01..06): форма записи ДОСЛОВНО как у соседних кнопок меню — экран
    # «🔘 Кнопки меню» и пер-городные резолверы подхватывают её автоматически по группе `menu`.
    # Сама кнопка рисуется только пока в FAQ есть хотя бы один включённый пункт
    # (`database.db.has_faq_for_city`, keyboards/builders.py::get_main_menu_kb).
    "menu_faq": {
        "type": "enum", "group": "menu", "label": "❓ Частые вопросы",
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
    # Phase 27 (27-04, LANG-01): переключатель языка анкеты в главном меню. Форма записи —
    # та же группа/тип/опции, что у прочих menu_* (экран «🔘 Кнопки меню» и пер-городные
    # резолверы подхватывают её автоматически по группе `menu`), но `default: "off"` —
    # ЕДИНСТВЕННОЕ отступление от конвенции menu_* default "on" в этом файле (см.
    # `tests/test_settings_percity_resolver.py::_MENU_DEFAULT_OVERRIDES`). Причина: кнопка не
    # должна «внезапно появиться» у всех событий в момент, когда план 27 просто попадает в
    # прод, — её включает менеджер explicitly, ПОСЛЕ того как включил сам модуль
    # (`delegate_lang_enabled`, group="toggles" выше) и увидел, что перевод анкеты готов.
    # `keyboards/builders.py::get_main_menu_kb` дополнительно гейтит эту кнопку модулем (тот
    # же приём, что у menu_miniapp/miniapp_enabled) — value=="on" здесь одной не достаточно.
    "menu_lang": {
        "type": "enum", "group": "menu", "label": "🌐 Язык / Language",
        "options": ["on", "off"], "prompt": None, "default": "off",
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
    "dashboard_block_utm": {
        "type": "enum", "group": "dashboard", "label": "🔗 Метки кампаний (UTM)",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    "dashboard_block_months": {
        "type": "enum", "group": "dashboard", "label": "📅 По месяцам",
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
            "например #037EF3. Базовый синий АЙСЕК — #037EF3."
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
        "type": "enum", "group": "miniapp", "label": "⚙️ Настройки",
        "options": ["on", "off"], "prompt": None, "default": "on",
    },
    # Phase 21 Plan 02 (FORM-SYNC-05, D-08): раздел анкеты делегата. Label едет в
    # body.dataset.sectionLabels и становится подписью плитки хаба — отдельного ключа
    # плитки не заводим (miniapp/routers/page.py:62).
    "miniapp_section_form": {
        "type": "enum", "group": "miniapp", "label": "📝 Анкета",
        "options": ["on", "off"], "prompt": (
            "Выключено — исчезают: плитка «📝 Анкета» в хабе, кнопка «Заполнить в "
            "приложении» после приветствия бота, «✏️ Изменить анкету» в профиле, кнопка "
            "приложения в догонялке брошенных анкет (D-08)."
        ), "default": "on",
    },
    # Quick 260906-8uq (FAQ-05): раздел делегата «❓ Частые вопросы» — рядом с остальными
    # делегатскими разделами (после "form", до "review", как в miniapp/deps.py::SECTIONS).
    "miniapp_section_faq": {
        "type": "enum", "group": "miniapp", "label": "❓ Частые вопросы",
        "options": ["on", "off"], "prompt": (
            "Выключено — исчезают: плитка «❓ Частые вопросы» в делегатском хабе и экран "
            "#/faq."
        ), "default": "on",
    },
    # Phase 23 (APP-TINDER-01, D-09/D-13): раздел отбора заявок менеджера — «тиндер» карточек.
    "miniapp_section_applications": {
        "type": "enum", "group": "miniapp", "label": "🗂 Отбор заявок",
        "options": ["on", "off"], "prompt": (
            "Выключено — исчезают: плитка «🗂 Отбор заявок» в хабе менеджера и экран "
            "#/applications."
        ), "default": "on",
    },
    # Quick 260904-2cj (QJRN-01..04): раздел журнала вопросов делегатов менеджера.
    "miniapp_section_questions": {
        "type": "enum", "group": "miniapp", "label": "❓ Вопросы делегатов",
        "options": ["on", "off"], "prompt": (
            "Выключено — исчезают: плитка «❓ Вопросы делегатов» в хабе менеджера и экран "
            "#/questions."
        ), "default": "on",
    },
    "miniapp_empty_questions": {
        "type": "text", "group": "miniapp", "label": "❓ Пустой журнал вопросов",
        "prompt": "Текст, когда вопросов делегатов пока нет (например: «Вопросов пока нет.»).",
        "default": "Вопросов пока нет.",
    },
    "miniapp_questions_answer_button": {
        "type": "text", "group": "miniapp", "label": "❓ Кнопка «Отправить ответ»",
        "prompt": "Подпись кнопки отправки ответа делегату под раскрытым вопросом.",
        "default": "Отправить ответ",
    },
    "miniapp_questions_sent_toast": {
        "type": "text", "group": "miniapp", "label": "❓ Тост «Ответ отправлен»",
        "prompt": "Текст тоста сразу после того, как ответ ушёл делегату.",
        "default": "Ответ отправлен",
    },
    # Quick 260904-kk6 (Q2): плейсхолдер и подпись кнопки-тоггла формы ответа — раньше были
    # литералами в questions.js (плейсхолдер) и иконкой без подписи для скринридера (тоггл).
    "miniapp_questions_answer_placeholder": {
        "type": "text", "group": "miniapp", "label": "❓ Плейсхолдер поля ответа",
        "prompt": "Серый текст-подсказка в пустом поле ответа делегату (например: «Ответ делегату»).",
        "default": "Ответ делегату",
    },
    "miniapp_questions_answer_toggle_label": {
        "type": "text", "group": "miniapp", "label": "❓ Кнопка «Ответить на вопрос»",
        "prompt": (
            "Подпись кнопки-иконки, которая раскрывает форму ответа под вопросом — её слышит "
            "менеджер со скринридером, на экране показана только иконка."
        ),
        "default": "Ответить",
    },
    # Quick 260906-8uq (FAQ-06): кнопка «В FAQ» под отвеченным вопросом журнала в приложении —
    # симметрично боту (задача 4), но без полноценного экрана правки (follow-up, not_in_scope).
    "miniapp_questions_to_faq_button": {
        "type": "text", "group": "miniapp", "label": "❓ Кнопка «В FAQ»",
        "prompt": "Подпись кнопки, которая кладёт отвеченный вопрос в FAQ.",
        "default": "❓ В FAQ",
    },
    "miniapp_questions_to_faq_saved_toast": {
        "type": "text", "group": "miniapp", "label": "❓ Тост «Пункт добавлен в FAQ»",
        "prompt": "Текст тоста сразу после того, как пункт сохранён в FAQ.",
        "default": "Добавлено в FAQ",
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
    # Quick 260904-8o3 Task 2 (E3): менеджер, который сам одобренный делегат, по
    # `is_staff_upload` всегда «делегат» — ассет оформления настроек грузится с ЯВНЫМ
    # контекстом (`target=settings_asset`), поэтому у него собственный ключ подписи, а не
    # caption_staff (тот остаётся для обложек заданий, moderate_game).
    "miniapp_upload_caption_settings": {
        "type": "text", "group": "miniapp", "label": "🎨 Подпись ассета оформления",
        "prompt": (
            "Подпись файла, загруженного менеджером в настройках оформления Mini App "
            "(например: «Файл сохранён — можно выбрать его в настройках оформления»)."
        ),
        "default": "🎨 Файл сохранён — можно выбрать его в настройках оформления.",
    },
    # Phase 21 Plan 02 (FORM-SYNC-05, Pattern 5): подпись копии резюме, которую бот шлёт
    # делегату в чат после загрузки резюме через приложение — третьей рядом с caption_delegate/
    # caption_staff выше (та же тройка подписей загрузки Mini App).
    "miniapp_upload_caption_resume": {
        "type": "text", "group": "miniapp", "label": "📎 Подпись копии резюме",
        "prompt": "Подпись файла-копии резюме, которую бот присылает делегату в чат (например: «📎 Резюме получено»).",
        "default": "📎 Резюме получено",
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
        "options": ["bluebook", "youlead", "realtalk", "custom"], "prompt": None, "default": "bluebook",
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
    "miniapp_theme_pattern": {
        "type": "photo", "group": "miniapp", "label": "🖼 Паттерн плиты",
        "prompt": (
            "Отправьте картинку фирменного паттерна — она ляжет фоном синей плиты на "
            "экранах приложения. Без неё используется паттерн выбранного набора "
            "оформления: «ЮЛид» — фирменный, «АЙСЕК — классика» — без паттерна."
        ),
        "default": None,
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
            "Слоган под заголовком на первом открытии приложения (например: «Делаешь задания "
            "— получаешь монеты.»)."
        ),
        "default": "Делаешь задания — получаешь монеты.",
    },
    "miniapp_onboarding_cta": {
        "type": "text", "group": "miniapp", "label": "👋 Кнопка приветственного экрана",
        "prompt": "Подпись кнопки на приветственном экране (например: «Погнали»).",
        "default": "Погнали",
    },
    # Phase 23.1 (UI-REDESIGN-03): привет-экран — герой, заголовок и шаги «как это работает»
    "miniapp_onboarding_hero": {
        "type": "text", "group": "miniapp", "label": "👋 Заголовок приветственного экрана",
        "prompt": "Крупный курсивный заголовок на первом экране приложения (например: «Привет!»).",
        "default": "Привет!",
    },
    "miniapp_onboarding_steps_title": {
        "type": "text", "group": "miniapp", "label": "👋 Надзаголовок «Как это работает»",
        "prompt": (
            "Надпись над списком шагов на приветственном экране (например: «Как это работает»)."
        ),
        "default": "Как это работает",
    },
    "miniapp_onboarding_steps": {
        "type": "text", "group": "miniapp", "label": "👋 Шаги «Как это работает»",
        "prompt": (
            "Три шага приветственного экрана. Каждый шаг с новой мысли, шаги разделяйте точкой "
            "с запятой; внутри шага заголовок и пояснение разделяйте тире с пробелами. Пример: "
            "Заполни анкету — 14 вопросов, минут на пять.; Делай задания — сторис и фото."
        ),
        "default": (
            "Заполни анкету — 14 вопросов, минут на пять. Черновик сохраняется сам.; "
            "Делай задания — сторис, фото, знакомства с делегатами из других городов.; "
            "Копи монеты — на форуме обменяешь их в CC-shop."
        ),
    },
    # Phase 23.1 (UI-REDESIGN-02): тексты хаба делегата — плита, факты, надзаголовки, якорь
    "miniapp_hub_balance_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🏷 Надзаголовок баланса в хабе",
        "prompt": "Надпись над числом монет на первом экране приложения (например: «Твой баланс»).",
        "default": "Твой баланс",
    },
    "miniapp_hub_balance_unit": {
        "type": "text", "group": "miniapp", "label": "🪙 Подпись единицы баланса в хабе",
        "prompt": "Слово после числа монет на первом экране приложения (например: «монет»).",
        "default": "монет",
    },
    "miniapp_hub_tasks_fact_text": {
        "type": "text", "group": "miniapp", "label": "🎯 Факт «заданий сдано» в хабе",
        "prompt": (
            "Строка «сколько заданий сдано» на первом экране приложения. Обязаны быть обе "
            "подстановки {done} (сдано) и {total} (всего активных заданий) — они заменяются "
            "числами автоматически, например: «{done} из {total} заданий сдано»."
        ),
        "default": "{done} из {total} заданий сдано",
    },
    "miniapp_hub_days_fact_text": {
        "type": "text", "group": "miniapp", "label": "🗓 Факт «дней до форума» в хабе",
        "prompt": (
            "Строка обратного отсчёта до форума на первом экране приложения. Обязана быть "
            "подстановка {days} (сколько дней осталось) — заменяется числом автоматически, "
            "например: «{days} дней до форума»."
        ),
        "default": "{days} дней до форума",
    },
    "miniapp_hub_countdown_date": {
        "type": "text", "group": "miniapp", "label": "🗓 Дата отсчёта до форума",
        "prompt": (
            "Дата начала события в формате ДД.ММ.ГГГГ, например 12.09.2026 — из неё считается "
            "«сколько дней осталось» на главном экране приложения. Пусто — строки с обратным "
            "отсчётом не будет."
        ),
        "default": None,
        "per_city": True,
    },
    "miniapp_hub_next_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🏷 Надзаголовок «Следующее» в хабе",
        "prompt": (
            "Надпись над карточкой ближайшего задания на первом экране приложения (например: "
            "«Следующее»)."
        ),
        "default": "Следующее",
    },
    "miniapp_hub_sections_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🏷 Надзаголовок «Разделы» в хабе",
        "prompt": "Надпись над списком разделов на первом экране приложения (например: «Разделы»).",
        "default": "Разделы",
    },
    # Quick 260904-aup (UAT D3): плита «Анкета на проверке / заявка отклонена» над плитками
    # хаба (`GET /app/api/hub/status`) — до этого делегат, отправивший анкету, видел почти
    # пустой хаб с одной плиткой «Анкета» и не понимал, что происходит.
    "miniapp_hub_pending_heading_text": {
        "type": "text", "group": "miniapp", "label": "⏳ Заголовок «Анкета на проверке» в хабе",
        "prompt": "Заголовок плиты, которую видит делегат дома, пока его заявка на рассмотрении.",
        "default": "⏳ Анкета на проверке",
    },
    "miniapp_hub_pending_body_text": {
        "type": "text", "group": "miniapp", "label": "⏳ Текст «Анкета на проверке» в хабе",
        "prompt": (
            "Текст под заголовком «Анкета на проверке». Обязана быть подстановка {days} — "
            "заменяется числом дней из настройки «⏳ Анкета на проверке: обычно за сколько "
            "дней ответ» ниже, например: «Заявку получили, ответ придёт сюда же, в чат с "
            "ботом, обычно за {days} дн.»"
        ),
        "default": "Заявку получили. Ответ придёт сюда же, в чат с ботом, обычно за {days} дн.",
    },
    "miniapp_hub_pending_days": {
        "type": "int", "group": "miniapp", "label": "⏳ Анкета на проверке: обычно за сколько дней ответ",
        "prompt": (
            "Через сколько дней делегат обычно получает решение по заявке — подставляется в "
            "текст «Анкета на проверке» выше вместо {days}. В ДНЯХ, например: 3."
        ),
        "default": 3,
        "per_city": True,
    },
    "miniapp_hub_rejected_heading_text": {
        "type": "text", "group": "miniapp", "label": "❌ Заголовок «Заявка отклонена» в хабе",
        "prompt": "Заголовок плиты, которую видит делегат дома, если его заявку отклонили.",
        "default": "❌ Заявка отклонена",
    },
    "miniapp_hub_rejected_body_text": {
        "type": "text", "group": "miniapp", "label": "❌ Текст «Заявка отклонена» в хабе",
        "prompt": "Текст под заголовком «Заявка отклонена» — решение организаторов уже пришло в чат.",
        "default": "Решение организаторов пришло в чат с ботом. Анкету можно поправить и подать заново.",
    },
    "miniapp_hub_rejected_cta_text": {
        "type": "text", "group": "miniapp", "label": "❌ Кнопка «Подать заново» в хабе",
        "prompt": "Подпись кнопки на плите «Заявка отклонена», ведущей на экран анкеты.",
        "default": "Подать заново",
    },
    # Quick 260904-liz: строка причины отказа на плите «Заявка отклонена» — до этой правки
    # делегат видел только «решение пришло в чат» и не понимал, что поправить в анкете.
    "miniapp_hub_rejected_reason_text": {
        "type": "text", "group": "miniapp", "label": "❌ Строка «Причина» на плите «Заявка отклонена»",
        "prompt": (
            "Строка с причиной отказа на плите «Заявка отклонена» в приложении делегата. "
            "Обязательна подстановка {reason} — вместо неё встанет причина, которую менеджер "
            "написал при отказе. Если причины нет (старый отказ), строка не показывается вовсе."
        ),
        "default": "Причина: {reason}",
    },
    # Quick 260903 (дашборд из хаба менеджера): подпись плитки «Дашборд» в хабе менеджера —
    # сам адрес НЕ здесь. `config.DASHBOARD_PUBLIC_URL`/`cfg.public_url` остаются деплойным
    # значением (D-05/D-19, handlers/admin.py:332, handlers/admin_dashboard.py) — заводить
    # второй bot_settings-ключ для того же адреса значило бы держать URL в двух местах,
    # которые могут разойтись. Плитка появляется в хабе сама, когда адрес задан при деплое.
    "miniapp_tile_dashboard_label": {
        "type": "text", "group": "miniapp", "label": "🏷 Подпись плитки «Дашборд» в хабе менеджера",
        "prompt": (
            "Текст на плитке, которая открывает веб-дашборд из хаба менеджера мини-приложения "
            "(например: «📊 Дашборд»)."
        ),
        "default": "📊 Дашборд",
    },
    # Phase 23.1 (UI-REDESIGN-05): подписи профиля делегата по макету 04-profile.png
    "miniapp_profile_contacts_eyebrow": {
        "type": "text", "group": "miniapp", "label": "👤 Надзаголовок «контакты»",
        "prompt": "Надпись над разделом контактов в профиле делегата (например: «Контакты»).",
        "default": "Контакты",
    },
    "miniapp_profile_form_eyebrow": {
        "type": "text", "group": "miniapp", "label": "📝 Надзаголовок «анкета»",
        "prompt": "Надпись над разделом ответов анкеты в профиле делегата (например: «Анкета»).",
        "default": "Анкета",
    },
    "miniapp_profile_form_progress_text": {
        "type": "text", "group": "miniapp", "label": "📝 Строка заполненности анкеты",
        "prompt": (
            "Строка «сколько вопросов анкеты заполнено» в профиле делегата. Обязаны быть обе "
            "подстановки {filled} (заполнено) и {total} (всего вопросов) — заменяются числами "
            "автоматически, например: «{filled} из {total} вопросов»."
        ),
        "default": "{filled} из {total} вопросов",
    },
    "miniapp_profile_submitted_text": {
        "type": "text", "group": "miniapp", "label": "📝 Пометка «отправлена»",
        "prompt": (
            "Пометка даты подачи анкеты в профиле делегата. Обязана быть подстановка {date} — "
            "заменяется датой автоматически, например: «Отправлена {date}»."
        ),
        "default": "Отправлена {date}",
    },
    "miniapp_profile_edited_text": {
        "type": "text", "group": "miniapp", "label": "📝 Пометка «изменена»",
        "prompt": (
            "Пометка даты правки анкеты в профиле делегата — дописывается к пометке подачи "
            "через « · », только если анкету правили. Обязана быть подстановка {date} — "
            "заменяется датой автоматически, например: «изменена {date}»."
        ),
        "default": "изменена {date}",
    },
    # D-10 (23.1-CONTEXT.md O-2): дата одобрения заявки — третий, необязательный фрагмент той
    # же строки (после «Отправлена…»/«изменена…»), только если заявка уже одобрена.
    "miniapp_profile_approved_text": {
        "type": "text", "group": "miniapp", "label": "📝 Пометка «одобрена»",
        "prompt": (
            "Пометка даты одобрения заявки в профиле делегата — дописывается к пометке подачи "
            "через « · », только если заявка уже одобрена. Обязана быть подстановка {date} — "
            "заменяется датой автоматически, например: «одобрена {date}»."
        ),
        "default": "одобрена {date}",
    },
    "miniapp_profile_privacy_note": {
        "type": "text", "group": "miniapp", "label": "🔒 Заметка о приватности анкеты",
        "prompt": (
            "Короткая заметка о приватности под анкетой в профиле делегата (например: «Анкету "
            "видит только менеджер АЙСЕК.»)."
        ),
        "default": (
            "Анкету видит только менеджер АЙСЕК. Город и трек после одобрения не меняются."
        ),
    },
    # Quick 260904-aup Task 3 (UAT D10): имя в плите профиля у делегата БЕЗ поданной анкеты
    # (`users.full_name` тогда пуст) — фолбэк, когда даже first_name из Telegram недоступен
    # (cookie-вход дашборда его не несёт).
    "miniapp_profile_greeting_fallback_text": {
        "type": "text", "group": "miniapp", "label": "👋 Имя-заглушка в плите профиля",
        "prompt": (
            "Что печатать в плите профиля, если у делегата ещё нет анкеты и Telegram не отдал "
            "имя (например: «Привет!»)."
        ),
        "default": "Привет!",
    },
    # Phase 23.1 (UI-REDESIGN-06): подписи карточки задания по макету 05-task.png
    "miniapp_task_todo_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🎯 Надзаголовок «что сделать»",
        "prompt": "Надпись над описанием задания на его карточке (например: «Что сделать»).",
        "default": "Что сделать",
    },
    "miniapp_task_proof_eyebrow": {
        "type": "text", "group": "miniapp", "label": "📎 Надзаголовок «нужно прислать»",
        "prompt": (
            "Надпись над блоком «что прислать в подтверждение» на карточке задания (например: "
            "«Нужно прислать»)."
        ),
        "default": "Нужно прислать",
    },
    "miniapp_task_proof_note": {
        "type": "text", "group": "miniapp", "label": "📎 Пояснение к блоку «нужно прислать»",
        "prompt": (
            "Короткое пояснение под блоком «что прислать» на карточке задания (например: «На "
            "присланном должно быть видно, что задание выполнено.»)."
        ),
        "default": "На присланном должно быть видно, что задание выполнено.",
    },
    "miniapp_task_deadline_left_text": {
        "type": "text", "group": "miniapp", "label": "⏳ Строка «сколько осталось»",
        "prompt": (
            "Строка «сколько дней осталось до дедлайна» на карточке задания. Обязана быть "
            "подстановка {days} — заменяется числом автоматически, например: «осталось {days} "
            "дн.»."
        ),
        "default": "осталось {days} дн.",
    },
    "miniapp_task_review_note": {
        "type": "text", "group": "miniapp", "label": "🔍 Пометка «кто и как быстро проверяет»",
        "prompt": (
            "Короткая пометка, кто и как быстро проверяет сдачу, на карточке задания "
            "(например: «менеджер, обычно за день»)."
        ),
        "default": "менеджер, обычно за день",
    },
    # Phase 23.1 (UI-REDESIGN-06): подписи плит списочных экранов — задания/монеты/рейтинг
    "miniapp_tasks_plate_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🎯 Надзаголовок плиты заданий",
        "prompt": "Надпись над числом активных заданий на экране «Задания» (например: «Активные задания»).",
        "default": "Активные задания",
    },
    "miniapp_leaderboard_plate_eyebrow": {
        "type": "text", "group": "miniapp", "label": "🏆 Надзаголовок плиты рейтинга",
        "prompt": "Надпись над своим местом на экране «Рейтинг» (например: «Твоё место»).",
        "default": "Твоё место",
    },
    "miniapp_leaderboard_plate_unit": {
        "type": "text", "group": "miniapp", "label": "🏆 Подпись под местом в рейтинге",
        "prompt": (
            "Строка под своим местом на экране «Рейтинг». Обязана быть подстановка {total} "
            "(сколько всего участников) — заменяется числом автоматически, например: "
            "«из {total}»."
        ),
        "default": "из {total}",
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

    # ── Phase 23 Plan 01 (APP-TINDER-01, D-05/D-06/D-08): надписи экрана «🗂 Отбор заявок»
    # Mini App — карточка-«тиндер», undo-тост, шторка причины отказа, фильтры-чипы. Правило
    # 0-хардкода (CLAUDE.md): ни одного русского литерала в JS.
    "miniapp_empty_applications": {
        "type": "text", "group": "miniapp", "label": "🗂 Пустая очередь заявок",
        "prompt": "Текст, когда заявок на модерации нет вообще (например: «Заявок на модерации нет.»).",
        "default": "Заявок на модерации нет.",
    },
    "miniapp_empty_applications_skipped": {
        "type": "text", "group": "miniapp", "label": "🗂 Очередь заявок: всё пропущено",
        "prompt": (
            "Текст, когда менеджер пропустил все заявки в очереди (сами заявки остались). "
            "«{count}» подставится числом оставшихся заявок (например: «Пропущено всё — "
            "осталось {count}.»)."
        ),
        "default": "Пропущено всё — осталось {count}.",
    },
    "miniapp_empty_applications_filtered": {
        "type": "text", "group": "miniapp", "label": "🗂 Заявок нет по фильтру",
        "prompt": "Текст, когда по включённому фильтру (трек/«изменённые») заявок нет (например: «По этому фильтру заявок нет — снимите фильтр.»).",
        "default": "По этому фильтру заявок нет — снимите фильтр.",
    },
    "miniapp_applications_show_all": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Показать всё»",
        "prompt": "Подпись кнопки, которая разворачивает остальные заполненные ответы анкеты на карточке заявки.",
        "default": "Показать всё",
    },
    # Quick 260904-kk6 (D17): та же кнопка в раскрытом состоянии — до этой правки подпись не
    # менялась обратно на «Показать всё» после сворачивания.
    "miniapp_applications_hide_all": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Свернуть»",
        "prompt": "Подпись той же кнопки карточки заявки, когда остальные ответы анкеты уже развёрнуты (нажатие сворачивает их обратно).",
        "default": "Свернуть",
    },
    "miniapp_applications_undo_button": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Отменить»",
        "prompt": "Подпись кнопки отмены в тосте после решения по заявке.",
        "default": "Отменить",
    },
    "miniapp_applications_approved_toast": {
        "type": "text", "group": "miniapp", "label": "🗂 Тост «Принято»",
        "prompt": "Текст тоста сразу после одобрения заявки.",
        "default": "Принято",
    },
    "miniapp_applications_rejected_toast": {
        "type": "text", "group": "miniapp", "label": "🗂 Тост «Отклонено»",
        "prompt": "Текст тоста сразу после отклонения заявки.",
        "default": "Отклонено",
    },
    "miniapp_applications_undone_toast": {
        "type": "text", "group": "miniapp", "label": "🗂 Тост «Отменено»",
        "prompt": "Текст тоста после того, как менеджер нажал «Отменить» и заявка вернулась в очередь.",
        "default": "Отменено — заявка вернулась в очередь.",
    },
    "miniapp_applications_approve_all_confirm": {
        "type": "text", "group": "miniapp", "label": "🗂 Подтверждение «Принять всех»",
        "prompt": (
            "Текст подтверждения массового одобрения. «{count}» подставится числом заявок "
            "(например: «Одобрить все {count} заявок? Отменить будет нельзя.»); название "
            "города дописывает приложение отдельной строкой, как appr_all_confirm бота."
        ),
        "default": "Одобрить все {count} заявок? Отменить будет нельзя.",
    },
    "miniapp_applications_reject_no_reason": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Отклонить без причины»",
        "prompt": "Подпись кнопки отказа без указания причины в шторке отказа.",
        "default": "Отклонить без причины",
    },
    "miniapp_applications_reject_own_reason": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Своя причина»",
        "prompt": "Подпись кнопки, которая открывает поле произвольного текста причины отказа.",
        "default": "Своя причина",
    },
    # Квик 260904-7e7 (D18): шторка отказа стала модальным низовым листом — своя кнопка отмены.
    "miniapp_applications_reject_cancel": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Отмена» в шторке отказа",
        "prompt": "Подпись кнопки закрытия шторки отказа без выбора причины.",
        "default": "Отмена",
    },
    # Найдено планом 23-05 (D-25): экрану «🗂 Отбор заявок» не хватало ещё шести подписей —
    # без них пришлось бы либо хардкодить текст в JS, либо не подписывать кнопку вовсе.
    "miniapp_applications_approve_button": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Принять»",
        "prompt": "Подпись кнопки принятия заявки (свайп вправо делает то же самое) — видимый текст на десктопе, aria-label на телефоне.",
        "default": "Принять",
    },
    "miniapp_applications_reject_button": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Отклонить»",
        "prompt": "Подпись кнопки отказа (свайп влево открывает ту же шторку) — видимый текст на десктопе, aria-label на телефоне.",
        "default": "Отклонить",
    },
    "miniapp_applications_resume_open": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Открыть резюме»",
        "prompt": "Подпись кнопки, которая открывает приложенный файл резюме.",
        "default": "Открыть резюме",
    },
    "miniapp_applications_resume_none": {
        "type": "text", "group": "miniapp", "label": "🗂 Текст «Резюме не приложено»",
        "prompt": "Текст на месте резюме, если делегат его не приложил (и текстом не ответил).",
        "default": "Резюме не приложено",
    },
    "miniapp_applications_history_label": {
        "type": "text", "group": "miniapp", "label": "🗂 Заголовок «История правок»",
        "prompt": "Заголовок свёрнутого блока истории правок анкеты на карточке заявки.",
        "default": "История правок",
    },
    # Task 3 (D-07): «Принять всех N» и честное «отменить уже нельзя» вместо молчания.
    "miniapp_applications_approve_all_button": {
        "type": "text", "group": "miniapp", "label": "🗂 Кнопка «Принять всех N»",
        "prompt": "Подпись кнопки массового одобрения в шапке очереди. «{count}» подставится числом заявок в очереди.",
        "default": "Принять всех {count}",
    },
    "miniapp_applications_undo_too_late": {
        "type": "text", "group": "miniapp", "label": "🗂 Текст «Отменить уже нельзя»",
        "prompt": "Спокойный текст, когда менеджер нажал «Отменить», но окно уже истекло (эффекты решения уже ушли делегату) — план 23-05.",
        "default": "Решение уже отправлено делегату — отменить нельзя.",
    },
    "miniapp_applications_filter_all": {
        "type": "text", "group": "miniapp", "label": "🗂 Фильтр «Все»",
        "prompt": "Подпись чипа-фильтра «без фильтра по треку».",
        "default": "Все",
    },
    # Task 3 (D-08): три подписи трек-чипов — API 23-04 отдавал только chips.all/changed.
    "miniapp_applications_filter_full": {
        "type": "text", "group": "miniapp", "label": "🗂 Фильтр «Полный трек»",
        "prompt": "Подпись чипа-фильтра «только заявки полного трека» (D-08, план 23-05).",
        "default": "Полный",
    },
    "miniapp_applications_filter_party": {
        "type": "text", "group": "miniapp", "label": "🗂 Фильтр «Вечеринка»",
        "prompt": "Подпись чипа-фильтра «только заявки трека вечеринки» (D-08, план 23-05).",
        "default": "Вечеринка",
    },
    "miniapp_applications_filter_short": {
        "type": "text", "group": "miniapp", "label": "🗂 Фильтр «Краткая анкета»",
        "prompt": "Подпись чипа-фильтра «только заявки краткой анкеты (акция)» (D-08, план 23-05).",
        "default": "Краткая",
    },
    "miniapp_applications_filter_changed": {
        "type": "text", "group": "miniapp", "label": "🗂 Фильтр «Изменённые»",
        "prompt": "Подпись чипа-фильтра «только изменённые/повторные заявки».",
        "default": "Изменённые",
    },

    # ── Phase 22 Plan 02 (WEB-SET-02/03, D-15, 22-UI-SPEC § Copywriting Contract): надписи
    # веб-экрана «⚙️ Настройки» Mini App — правило 0-хардкода (Phase 17.1), тот же паттерн, что
    # miniapp_confirm_disable_text выше. per_city нет (управляющий текст интерфейса менеджера,
    # не текст для делегата). Подписи/подсказки самих 259 настроек здесь НЕ дублируются — их
    # рендерит экран из существующего label/prompt каждого ключа (D-06).
    "miniapp_settings_search_placeholder_text": {
        "type": "text", "group": "miniapp", "label": "🔍 Плейсхолдер строки поиска настроек",
        "prompt": "Подсказка в пустой строке поиска настроек — покажите пример слова.",
        "default": "Найти настройку — например, «приветствие» или «дедлайн»",
    },
    "miniapp_settings_search_count_text": {
        "type": "text", "group": "miniapp", "label": "🔍 Счётчик результатов поиска настроек",
        "prompt": (
            "Строка под поиском настроек. «{shown}» — сколько показано сейчас, «{total}» — "
            "сколько всего настроек."
        ),
        "default": "Показано {shown} из {total}",
    },
    "miniapp_settings_search_empty_heading_text": {
        "type": "text", "group": "miniapp", "label": "🔍 Заголовок пустого поиска настроек",
        "prompt": "Заголовок, когда поиск по настройкам ничего не нашёл.",
        "default": "Ничего не нашлось",
    },
    "miniapp_settings_search_empty_body_text": {
        "type": "text", "group": "miniapp", "label": "🔍 Текст пустого поиска настроек",
        "prompt": "Пояснение под заголовком пустого поиска настроек.",
        "default": "Попробуйте другое слово или проверьте, нет ли опечатки.",
    },
    "miniapp_settings_search_suggest_text": {
        "type": "text", "group": "miniapp", "label": "🔍 Подсказка похожих слов поиска настроек",
        "prompt": (
            "Строка под пустым результатом поиска настроек, если нашлись близкие слова. "
            "«{suggestions}» подставится списком предложенных слов."
        ),
        "default": "Возможно, вы имели в виду: {suggestions}",
    },
    "miniapp_settings_value_default_text": {
        "type": "text", "group": "miniapp", "label": "🏷 Маркер поля «по умолчанию»",
        "prompt": "Маркер состояния поля настройки, когда оно не менялось.",
        "default": "по умолчанию",
    },
    "miniapp_settings_value_set_text": {
        "type": "text", "group": "miniapp", "label": "🏷 Маркер поля «задано»",
        "prompt": "Маркер состояния поля настройки, когда менеджер задал своё значение.",
        "default": "задано",
    },
    "miniapp_settings_value_not_set_text": {
        "type": "text", "group": "miniapp", "label": "🏷 Маркер поля «не задано»",
        "prompt": "Маркер-плейсхолдер поля настройки, у которого нет ни своего, ни дефолтного значения.",
        "default": "не задано",
    },
    "miniapp_settings_reset_default_label_text": {
        "type": "text", "group": "miniapp", "label": "🔄 Кнопка «Сбросить к умолчанию»",
        "prompt": "Подпись кнопки сброса поля настройки к значению по умолчанию.",
        "default": "Сбросить к умолчанию",
    },
    "miniapp_settings_reset_default_confirm_text": {
        "type": "text", "group": "miniapp", "label": "🔄 Подтверждение сброса поля настройки",
        "prompt": (
            "Текст подтверждения сброса поля настройки к дефолту. «{default}» подставится "
            "самим значением по умолчанию."
        ),
        "default": "Значение вернётся к «{default}». Сбросить?",
    },
    "miniapp_settings_reset_city_label_text": {
        "type": "text", "group": "miniapp", "label": "🏙 Кнопка «Как везде» (сброс города)",
        "prompt": "Подпись кнопки сброса переопределения настройки для выбранного города.",
        "default": "Как везде",
    },
    "miniapp_settings_city_own_badge_text": {
        "type": "text", "group": "miniapp", "label": "🏙 Маркер «своё значение города»",
        "prompt": "Маркер поля настройки, когда у выбранного города есть переопределение.",
        "default": "✏️ своё",
    },
    "miniapp_settings_city_default_badge_text": {
        "type": "text", "group": "miniapp", "label": "🏙 Маркер «как везде»",
        "prompt": "Маркер поля настройки, когда у выбранного города нет своего переопределения.",
        "default": "как везде",
    },
    "miniapp_settings_city_override_count_text": {
        "type": "text", "group": "miniapp", "label": "🏙 Счётчик переопределений по городам",
        "prompt": (
            "Маркер поля настройки при виде «все города» — «{count}» подставится числом "
            "городов со своим значением."
        ),
        "default": "🏙 {count}",
    },
    "miniapp_settings_city_override_list_text": {
        "type": "text", "group": "miniapp", "label": "🏙 Список городов с переопределением",
        "prompt": (
            "Разворачивается по тапу на счётчик переопределений. «{cities}» подставится "
            "перечнем городов через запятую."
        ),
        "default": "Переопределено для: {cities}",
    },
    "miniapp_settings_preview_button_text": {
        "type": "text", "group": "miniapp", "label": "👁 Кнопка превью текста настройки",
        "prompt": "Кнопка под текстовым полем настройки с плейсхолдерами — открывает превью.",
        "default": "👁 Показать как увидит делегат",
    },
    "miniapp_settings_preview_heading_text": {
        "type": "text", "group": "miniapp", "label": "👁 Заголовок панели превью",
        "prompt": "Заголовок панели, показывающей текст настройки как его увидит делегат.",
        "default": "Так увидит делегат",
    },
    # Quick 260904-8o3 Task 3 (E5/E6): живая мини-плита в группе «🎨 Оформление» — реагирует на
    # смену пресета/паттерна/шрифта ДО сохранения (POST /app/api/admin/theme/preview).
    "miniapp_settings_theme_preview_heading_text": {
        "type": "text", "group": "miniapp", "label": "🎨 Заголовок превью оформления",
        "prompt": "Заголовок над мини-плитой предпросмотра оформления в настройках.",
        "default": "Как это увидит делегат",
    },
    "miniapp_settings_theme_preview_eyebrow_text": {
        "type": "text", "group": "miniapp", "label": "🎨 Надзаголовок мини-плиты превью",
        "prompt": "Маленькая подпись над числом на мини-плите предпросмотра (например: «Твой баланс»).",
        "default": "Твой баланс",
    },
    "miniapp_settings_theme_preview_sub_text": {
        "type": "text", "group": "miniapp", "label": "🎨 Подпись под числом мини-плиты превью",
        "prompt": "Подпись под числом-образцом на мини-плите предпросмотра оформления.",
        "default": "монет на счету",
    },
    "miniapp_settings_batch_bar_text": {
        "type": "text", "group": "miniapp", "label": "💾 Кнопка «Сохранить N изменений»",
        "prompt": (
            "Плавающая кнопка сохранения правок настроек. «{count}» подставится числом "
            "несохранённых изменений. Три формы слова через «|» в фигурных скобках "
            "({изменение|изменения|изменений}) подставятся сами по числу — 1/2-4/остальное."
        ),
        "default": "Сохранить {count} {изменение|изменения|изменений}",
    },
    "miniapp_settings_batch_discard_text": {
        "type": "text", "group": "miniapp", "label": "💾 Кнопка «Отменить правки»",
        "prompt": "Вторая кнопка панели сохранения — сбрасывает несохранённые правки настроек.",
        "default": "Отменить правки",
    },
    "miniapp_settings_diff_heading_text": {
        "type": "text", "group": "miniapp", "label": "💾 Заголовок диалога проверки изменений",
        "prompt": "Заголовок диалога, который показывает список правок настроек перед сохранением.",
        "default": "Проверьте изменения",
    },
    "miniapp_settings_diff_was_label_text": {
        "type": "text", "group": "miniapp", "label": "💾 Метка «Было» в диалоге изменений",
        "prompt": "Метка старого значения в строке диалога проверки изменений.",
        "default": "Было",
    },
    "miniapp_settings_diff_will_label_text": {
        "type": "text", "group": "miniapp", "label": "💾 Метка «Станет» в диалоге изменений",
        "prompt": "Метка нового значения в строке диалога проверки изменений.",
        "default": "Станет",
    },
    "miniapp_settings_diff_confirm_cta_text": {
        "type": "text", "group": "miniapp", "label": "💾 Кнопка подтверждения обычного сохранения",
        "prompt": "Кнопка подтверждения диалога изменений, когда в пачке нет опасных пунктов.",
        "default": "💾 Сохранить изменения",
    },
    "miniapp_settings_diff_confirm_dangerous_cta_text": {
        "type": "text", "group": "miniapp", "label": "💾 Кнопка подтверждения с опасным пунктом",
        "prompt": "Кнопка подтверждения диалога изменений, когда в пачке есть хотя бы один опасный пункт.",
        "default": "Да, сохранить",
    },
    "miniapp_settings_saved_toast_text": {
        "type": "text", "group": "miniapp", "label": "💾 Тост «Сохранено»",
        "prompt": "Тост после успешного пакетного сохранения настроек.",
        "default": "Сохранено",
    },
    "miniapp_settings_error_toast_text": {
        "type": "text", "group": "miniapp", "label": "💾 Тост ошибки сохранения",
        "prompt": "Тост при ошибке пакетного сохранения настроек, не привязанной к конкретному полю.",
        "default": "Не получилось сохранить — попробуйте ещё раз.",
    },
    "miniapp_settings_stale_badge_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Маркер «изменено в боте»",
        "prompt": (
            "Маркер поля настройки, чьё значение поменяли в боте, пока менеджер правил его "
            "в приложении."
        ),
        "default": "Изменено в боте",
    },
    "miniapp_settings_stale_current_value_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Актуальное значение из бота",
        "prompt": (
            "Подстрока под полем с маркером «изменено в боте». «{value}» подставится "
            "актуальным значением."
        ),
        "default": "Сейчас: {value}",
    },
    "miniapp_settings_stale_overwrite_label_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Кнопка «Перезаписать»",
        "prompt": "Кнопка в диалоге изменений — принять правку менеджера поверх значения из бота.",
        "default": "Перезаписать",
    },
    "miniapp_settings_stale_keep_label_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Кнопка «Оставить как в боте»",
        "prompt": "Кнопка в диалоге изменений — отменить локальную правку этого поля.",
        "default": "Оставить как в боте",
    },
    "miniapp_settings_sheets_needs_confirm_text": {
        "type": "text", "group": "miniapp", "label": "📄 Подтверждение режима вкладки таблицы",
        "prompt": (
            "Пункт диалога изменений для ключей режима записи вкладки таблицы. «{tab}» — имя "
            "вкладки, «{rows}» — число строк в ней, «{consequence}» подставится описанием "
            "последствий из логики бота."
        ),
        "default": "Вкладка «{tab}» уже существует, в ней {rows} строк. {consequence}",
    },
    "miniapp_settings_upload_413_text": {
        "type": "text", "group": "miniapp", "label": "📎 Ошибка загрузки: файл слишком большой",
        "prompt": "Инлайн-ошибка загрузки photo/file-поля настройки, когда файл больше лимита.",
        "default": "Файл больше 20 МБ — уменьшите размер и попробуйте снова.",
    },
    "miniapp_settings_upload_offline_text": {
        "type": "text", "group": "miniapp", "label": "📎 Ошибка загрузки: нет соединения",
        "prompt": "Инлайн-ошибка загрузки photo/file-поля настройки при обрыве сети.",
        "default": "Нет соединения — проверьте интернет и попробуйте снова.",
    },
    "miniapp_settings_upload_wrong_type_text": {
        "type": "text", "group": "miniapp", "label": "📎 Ошибка загрузки: не тот тип файла",
        "prompt": "Инлайн-ошибка загрузки photo/file-поля настройки при неподходящем типе файла.",
        "default": "Этот тип файла не подходит для этого поля.",
    },
    "miniapp_settings_forbidden_text": {
        "type": "text", "group": "miniapp", "label": "⛔ Экран «нет прав» на настройках",
        "prompt": "Экран/тост, когда у менеджера нет права «settings» или выбран не его город.",
        "default": "Недостаточно прав для этого действия — обратитесь к тому, кто настраивал ваш доступ.",
    },
    "miniapp_settings_loading_text": {
        "type": "text", "group": "miniapp", "label": "⏳ Текст загрузки экрана настроек",
        "prompt": "Индикатор при первичной загрузке списка настроек.",
        "default": "Загружаем настройки…",
    },
    "miniapp_settings_load_error_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Ошибка загрузки экрана настроек",
        "prompt": "Ошибка первичной загрузки списка настроек, рядом кнопка «Повторить».",
        "default": "Не удалось загрузить настройки — попробуйте ещё раз.",
    },
    "miniapp_settings_dangerous_saved_toast_text": {
        "type": "text", "group": "miniapp", "label": "⚡ Тост мгновенного сохранения опасного тумблера",
        "prompt": (
            "Тост после сохранения опасного тумблера тапом (сохраняется сразу, вне общего "
            "сохранения — отличается от обычного тоста «Сохранено»)."
        ),
        "default": "Сохранено сразу — это изменение применяется без общего сохранения.",
    },

    # ── Phase 22 Plan 02 (WEB-SET-02/03, план 22-01 `settings_ops.dangerous_confirm_key`):
    # тексты подтверждения для опасных ключей, у которых сегодня в боте нет подтверждения
    # вовсе — правило «подтверждение называет реальный ущерб» (CLAUDE.md), формулировкой в
    # духе miniapp_confirm_disable_text выше.
    "miniapp_settings_confirm_reg_mode_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Подтверждение: смена режима регистрации",
        "prompt": (
            "Текст подтверждения смены режима анкеты — какая форма перестанет открываться "
            "делегатам."
        ),
        "default": (
            "Сменить режим регистрации? Делегаты, которые ещё не отправили анкету, увидят "
            "другую форму — часть уже заполненных полей может исчезнуть с экрана."
        ),
    },
    "miniapp_settings_confirm_approval_mode_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Подтверждение: смена модерации заявок",
        "prompt": (
            "Текст подтверждения переключения модерации заявок — начнут ли заявки одобряться "
            "автоматически."
        ),
        "default": (
            "Сменить режим модерации? Новые заявки начнут одобряться автоматически (или, "
            "наоборот, перестанут) — уже поданные заявки это не тронет."
        ),
    },
    "miniapp_settings_confirm_event_type_text": {
        "type": "text", "group": "miniapp", "label": "⚠️ Подтверждение: смена типа события",
        "prompt": (
            "Текст подтверждения смены типа события — пресет включит/выключит модули оплаты "
            "и согласий."
        ),
        "default": (
            "Сменить тип события? Пресет переключит модули «Оплата» и «Согласия» — часть "
            "настроенных текстов и вопросов может стать недоступна делегатам."
        ),
    },

    # ── Phase 22 Plan 07 (D-16, владелец 03.09: «стартовый экран из плиток по разделам»):
    # стартовый экран настроек — два ряда плиток разделов (`sections[].tier` считает
    # `settings_ops.SETTINGS_MAIN_SECTIONS`, роутер ничего не решает сам) + подпись счётчика
    # на плитке. Подписи разделов/групп на плитках — существующие `section.label` из
    # `SECTION_GROUPS` (T-19-45), новых кодов человеку не показываем.
    "miniapp_settings_row_main_label": {
        "type": "text", "group": "miniapp", "label": "🧩 Заголовок ряда плиток «Нужно менеджеру»",
        "prompt": "Заголовок первого ряда плиток разделов на стартовом экране настроек.",
        "default": "Нужно менеджеру",
    },
    "miniapp_settings_row_rare_label": {
        "type": "text", "group": "miniapp", "label": "🧩 Заголовок ряда плиток «Реже»",
        "prompt": "Заголовок второго ряда плиток разделов на стартовом экране настроек.",
        "default": "Реже",
    },
    "miniapp_settings_tile_count_text": {
        "type": "text", "group": "miniapp", "label": "🧩 Счётчик настроек на плитке раздела",
        "prompt": (
            "Подпись под названием раздела на плитке. «{n}» подставится числом настроек в "
            "разделе. Три формы слова через «|» в фигурных скобках "
            "({настройка|настройки|настроек}) подставятся сами по числу — 1/2-4/остальное."
        ),
        "default": "{n} {настройка|настройки|настроек}",
    },

    # ── Phase 22 Plan 07 (D-17 Task 3, владелец 03.09: «одна строка на вопрос, три
    # маленьких тумблера вместо трёх отдельных строк») — заголовки трёх колонок матрицы
    # «трек × вопрос» на экране «📋 Вопросы регистрации».
    "miniapp_settings_reg_matrix_full_label_text": {
        "type": "text", "group": "miniapp", "label": "🧩 Матрица вопросов: колонка «Полная»",
        "prompt": "Заголовок колонки полной формы регистрации в матрице вопросов анкеты.",
        "default": "Полная",
    },
    "miniapp_settings_reg_matrix_party_label_text": {
        "type": "text", "group": "miniapp", "label": "🧩 Матрица вопросов: колонка «Party»",
        "prompt": "Заголовок колонки трека вечеринки в матрице вопросов анкеты.",
        "default": "Вечеринка",
    },
    "miniapp_settings_reg_matrix_short_label_text": {
        "type": "text", "group": "miniapp", "label": "🧩 Матрица вопросов: колонка «Краткая»",
        "prompt": "Заголовок колонки краткой формы регистрации в матрице вопросов анкеты.",
        "default": "Короткая",
    },

    # ── Quick 260902-tzh: «🧾 Поля карточки заявки» — какие ответы анкеты показывать
    # менеджеру в карточке отбора и до какой длины обрезать длинный ответ. Own group "apps"
    # (как dashboard/miniapp выше): свой экран `handlers/admin_modcard.py`, эти два ключа
    # НЕ добавляются в handlers.admin_settings.SETTINGS_FIELDS/SETTINGS_GROUPS — второй
    # конкурирующей поверхности правки не заводим. Дефолт modcard_fields ПОВТОРЯЕТ
    # moderation_card.DEFAULT_CARD_STEPS литералом (import moderation_card сюда даёт цикл:
    # moderation_card -> reg_engine -> settings_schema) — дрейф закрыт сторожем
    # test_registry_default_matches_service_default.
    # Quick 260906-6xe: `type: "multi"` — набор закрыт (43 шага `moderation_card.CARD_STEPS`),
    # веб рисует чекбоксы с подписями вместо кодов (CLAUDE.md: кодов человеку не показываем).
    # `options_ref` — строка "модуль:атрибут", а не импорт `moderation_card` на уровне модуля
    # (тот же цикл, что и с дефолтом выше) и не литерал 43 подписей копией (гарантированный
    # дрейф) — карта читается лениво, в момент вызова `multi_options`/`multi_labels`.
    # `empty_value` — сентинел бота (`moderation_card.EMPTY_SENTINEL`): пустой набор из веба
    # обязан писаться так же, как снятие всех тумблеров в боте, а не пустой строкой (которую
    # `_parse_setting` читал бы обратно как дефолтные 20 вопросов).
    "modcard_fields": {
        "type": "multi", "group": "apps", "label": "🧾 Поля карточки заявки",
        "options_ref": "moderation_card:CARD_STEPS",
        "empty_value": "—",
        "prompt": (
            "Какие ответы анкеты видит менеджер в карточке заявки. Один набор и для "
            "карточки в чате, и для карточки в приложении."
        ),
        "default": [
            "age", "city", "education_status", "university", "course", "local_committee",
            "position", "alumni_status", "aiesec_role", "source", "work_sphere",
            "english_level", "attendance_format", "goal", "expectations", "exp_organizers",
            "exp_content", "missing_skills", "volunteer", "resume",
        ],
    },
    "modcard_answer_limit": {
        "type": "int", "group": "apps", "label": "✂️ Длина ответа в карточке",
        "prompt": (
            "Отмечайте кнопкой готовый вариант в боте: ⚙️ /admin → 📋 Заявки → 🧾 Поля "
            "карточки заявки"
        ),
        "default": 300,
    },

    # Phase 23 Plan 01 (APP-TINDER-01, D-05): шаблоны причин отказа для шторки отказа Mini App
    # (и карточки бота, паритет). Own group "apps" — правится ОБЩИМ списочным редактором
    # (handlers/admin_settings_lists.py: ➕ добавить / 🗑 удалить / ✏️ заменить целиком), нового
    # экрана бота не заводим — достаточно попадания в _APPS_FIELD_ORDER.
    "reject_reason_templates": {
        "type": "list", "group": "apps", "label": "✍️ Причины отказа",
        "prompt": "Отправьте варианты причин отказа, каждый с новой строки",
        "default": [
            "Анкета заполнена не полностью",
            "Не подходит по критериям участия",
            "Мест на это мероприятие уже нет",
            "Заявка-дубликат",
        ],
    },

    # Quick 260904-dq1: «с»/«до» тихих часов — per_city (часы тишины у Владивостока и Москвы
    # разные), время московское, как везде в боте (см. services/quiet_hours.py). Метка
    # "format": "time" — новая необязательная мета, читает settings_validation.py.
    "quiet_hours_start": {
        "type": "text", "group": "apps", "label": "🌙 Тихие часы: с",
        "prompt": (
            "С какого часа не слать делегату решения по заявке, результаты проверки заданий "
            "и напоминания — они дождутся конца тихих часов. Формат <code>ЧЧ:ММ</code>, "
            "например <code>22:00</code>. Время московское, как везде в боте."
        ),
        "default": "22:00",
        "per_city": True,
        "format": "time",
    },
    "quiet_hours_end": {
        "type": "text", "group": "apps", "label": "🌙 Тихие часы: до",
        "prompt": (
            "До какого часа держится тишина — после него отложенные уведомления уходят "
            "делегату. Формат <code>ЧЧ:ММ</code>, например <code>09:00</code>. Время "
            "московское, как везде в боте."
        ),
        "default": "09:00",
        "per_city": True,
        "format": "time",
    },
    # БЕЗ per_city: приписка адресована МЕНЕДЖЕРУ (не делегату), один текст на событие.
    "quiet_hours_manager_notice_text": {
        "type": "text", "group": "apps", "label": "🌙 Приписка менеджеру о тихих часах",
        "prompt": (
            "Что менеджер увидит рядом со своим решением ночью, если оно отложено до конца "
            "тихих часов. Плейсхолдер <code>{time}</code> — время конца окна по городу "
            "делегата (например «09:00»)."
        ),
        "default": "🌙 делегат узнает в {time}",
    },
}


# ── Phase 25 (CITYQ-01): группа "reg_prompts" — тексты вопросов анкеты, переопределяемые по
# городу. Пары (step_key, setting_key) — байт-в-байт порядок `_prompt_steps()`
# (handlers/admin_reg_config.py): первая — full_name (setting_key=None, вопрос без reg_q_*
# тумблера), дальше — одна пара на каждую тройку `reg_engine.REG_FLOW`, в её порядке.
# Литерал, а не импорт REG_FLOW: `reg_engine` импортирует ЭТОТ модуль (SETTINGS_SCHEMA),
# обратный импорт дал бы цикл.
REG_PROMPT_STEPS: tuple[tuple[str, str | None], ...] = (
    ("full_name", None),
    ("age", "reg_q_age"),
    ("phone", "reg_q_phone"),
    ("alumni_status", "reg_q_alumni_status"),
    ("vk", "reg_q_vk"),
    ("city", "reg_q_city"),
    ("education_status", "reg_q_education"),
    ("course", "reg_q_course"),
    ("university", "reg_q_university"),
    ("study_field", "reg_q_study_field"),
    ("goal", "reg_q_goal"),
    ("formats", "reg_q_formats"),
    ("expectations", "reg_q_expectations"),
    ("source", "reg_q_source"),
    ("ambassador", "reg_q_ambassador"),
    ("resume", "reg_q_resume"),
    ("email", "reg_q_email"),
    ("local_committee", "reg_q_lc"),
    ("position", "reg_q_position"),
    ("specialty", "reg_q_specialty"),
    ("work_status", "reg_q_work"),
    ("work_sphere", "reg_q_work_sphere"),
    ("missing_skills", "reg_q_skills"),
    ("attendance_format", "reg_q_attendance"),
    ("informal_day", "reg_q_informal_day"),
    ("comments", "reg_q_comments"),
    ("department", "reg_q_department"),
    ("aiesec_role", "reg_q_aiesec_role"),
    ("needs_certificate", "reg_q_certificate"),
    ("english_level", "reg_q_english"),
    ("allergies", "reg_q_allergies"),
    ("food_pref", "reg_q_food"),
    ("arrival", "reg_q_arrival"),
    ("housing", "reg_q_housing"),
    ("bed_sharing", "reg_q_bed_sharing"),
    ("bed_partner", "reg_q_bed_partner"),
    ("transport", "reg_q_transport"),
    ("cc_shop", "reg_q_cc_shop"),
    ("exp_organizers", "reg_q_exp_organizers"),
    ("exp_content", "reg_q_exp_content"),
    ("volunteer", "reg_q_volunteer"),
    ("arrival_date", "reg_q_arrival_date"),
    ("birth_date", "reg_q_birth_date"),
    ("payment_plan_date", "reg_q_payment_date"),
)

_FULL_NAME_PROMPT_LABEL = "🪪 Фамилия и Имя"

for _step_key, _setting_key in REG_PROMPT_STEPS:
    _human = REG_LABELS.get(_setting_key, _step_key) if _setting_key else _FULL_NAME_PROMPT_LABEL
    SETTINGS_SCHEMA[f"reg_prompt_{_step_key}"] = {
        "type": "text", "group": "reg_prompts",
        "label": f"✏️ Текст: {_human}",
        "prompt": "Свой текст этого вопроса анкеты. «-» — вернуть стандартный.",
        "default": None, "per_city": True,
    }
del _step_key, _setting_key, _human


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

    if entry_type in ("list", "multi"):
        # Quick 260906-6xe: `multi` — a `list` whose value set is CLOSED (checkbox UI +
        # server-side label<->code mapping in `multi_options`/`multi_labels`/`multi_codes`
        # below); storage/read shape is byte-identical to `list`, so this branch is shared
        # and unchanged — the bot's own screen keeps reading `modcard_fields` exactly as
        # before this migration.
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


def _multi_options_map(key: str) -> dict[str, str]:
    """Ленивая (внутри функции — на уровне модуля цикл, см. комментарий у `modcard_fields`)
    загрузка карты код->подпись для записи типа `multi` по мете `options_ref`
    (`"module:ATTR"`). Не `multi` / нет `options_ref` / модуль без атрибута -> пустая карта
    (fail-soft, как остальной реестр)."""
    entry = SETTINGS_SCHEMA.get(key)
    if entry is None or entry.get("type") != "multi":
        return {}
    ref = entry.get("options_ref")
    if not ref or ":" not in ref:
        return {}
    module_name, attr_name = ref.split(":", 1)
    import importlib

    try:
        module = importlib.import_module(module_name)
        mapping = getattr(module, attr_name)
    except (ImportError, AttributeError):
        return {}
    return dict(mapping)


def multi_options(key: str) -> list[tuple[str, str]]:
    """Закрытый набор `(код, подпись)` записи `multi`, в порядке карты `options_ref`.

    ЕДИНСТВЕННЫЙ маппинг код<->подпись в проекте для типа `multi` (как у `theme_css_vars`
    — второго места быть не должно): в JSON веб-API уезжают ПОДПИСИ, в БД ложатся КОДЫ,
    перевод — только через эту функцию и `multi_labels`/`multi_codes` ниже. Подписи
    статичны (читаются из модульной константы, не из БД), поэтому маппинг не может
    разъехаться между отрисовкой и сохранением.
    """
    return list(_multi_options_map(key).items())


def multi_labels(key: str, codes: list[str]) -> list[str]:
    """Коды -> подписи, в порядке набора (`multi_options`); неизвестный код молча
    отбрасывается (тот же fail-soft приём, что `moderation_card.enabled_steps`)."""
    mapping = _multi_options_map(key)
    chosen = set(codes or [])
    return [label for code, label in mapping.items() if code in chosen]


def multi_codes(key: str, labels: list[str]) -> tuple[list[str] | None, str | None]:
    """Подписи -> коды, в порядке набора (`multi_options`). Успех -> `(коды, None)`.
    Первая подпись вне закрытого набора -> `(None, эта_подпись)` — вызывающий
    (`settings_validation`) сам решает, как её показать в тексте отказа."""
    mapping = _multi_options_map(key)
    label_to_code = {label: code for code, label in mapping.items()}
    wanted = set(labels or [])
    for label in labels or []:
        if label not in label_to_code:
            return None, label
    chosen = {label_to_code[label] for label in wanted}
    return [code for code in mapping if code in chosen], None


async def get_setting_typed(key: str):
    """Thin async accessor (D-05) — raw read via the existing `get_setting` (database.db,
    unchanged, D-07) then dispatch through the pure `_parse_setting`. Does not duplicate
    raw I/O; calls `get_setting` exactly once."""
    raw = await get_setting(key)
    return _parse_setting(key, raw)
