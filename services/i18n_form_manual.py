"""Квик 260917-en (владелец, приёмка на английском делегате): «перевод интерфейса очень криво
сделан». `i18n_ui_en.py` (ярус A — служебные слова) и `services/i18n_miniapp_manual.py` (тексты
приложения вне анкеты) уже рукописные — здесь тот же ярус B (`manual=1`, `services/i18n.py::tr()`
ищет по `src_hash` содержимого, не по ключу реестра), но для КОРПУСА САМОЙ АНКЕТЫ
(`services/i18n_sources.py::DELEGATE_GROUPS` — `reg_prompts`/`reg`/`party`): подсказки вопросов,
списки вариантов, подписи и ошибки движка (`reg_engine.py`), которые до этой правки переводились
только машиной (argos) и звучали плохо («Write your age number:»).

Два словаря:

- `FORM_DEFAULT_EN` — перевод ДЕФОЛТОВ корпуса: то, что вернёт `services/i18n_sources.py::corpus()`
  на чистой БД (без единой правки менеджера) — `PROMPT_DEFAULTS`/`STEP_HELP`/`STEP_HELP_EXAMPLES`/
  `_GENERIC_FALLBACK_LABEL`/`REG_LABELS`/`_SUMMARY_FIELD_LABELS`/`reg_options.*`/`SELECT_CONFIG`/
  `MULTI_CONFIG`/`config.UNIVERSITIES` (`reg_engine.py`, `reg_labels.py`, `reg_options.py`), литералы
  `registration.py`/`reg_flow.py`/`reg_resume.py`/`reg_consent.py`/`user_actions.py`/`payment.py`/
  `application_effects.py`/`reg_schema.py` (эти модули aiogram-зависимы и не импортируются
  `i18n_sources.py` — литералы продублированы там буквально, см. докстринг `code_literals()`), и
  ~190 голых ключей реестра группы `reg`/`event`/`game`/`pay` (`reg_form_*`, `reg_status_*`,
  `reg_resume_*`, `reg_review_*`, `reg_header_settings_*`, `reg_multi_*`, `payment_*`, `balance_*`,
  `leaderboard_*`, `faq_*`, `game_*` и т.д. — их дефолты `SETTINGS_SCHEMA[key]["default"]`).
  Сторож покрытия (`tests/test_i18n_form_manual_coverage_260917.py`) требует ручной перевод для
  КАЖДОЙ строки `corpus()` — новый ключ реестра без перевода роняет тест с понятным сообщением.

- `EVENT_TEXTS_260917` — РЕАЛЬНЫЕ тексты события «Юлид 26/2» на 17.09 (снятые с прода и стенда:
  `reg_prompt_*` оверрайды, `approve_text`/`approve_text__city__*`, `start_text`, `reg_complete_text`,
  `reg_prompt_resume`, событийные даты, новый город «Тюмень» в `city_options`) — менеджеры
  переписали эти ключи под конкретное мероприятие, машинный перевод (513 строк на стенде,
  `manual=0`) звучал плохо так же, как и дефолты. Сторож покрытия (а) их НЕ требует — это снимок
  конкретного события, не часть кодовой базы. Не входят: `event_season` (техническая метка сезона,
  не язык), `payment_requisites`/`payment_options` реквизиты (правило владельца — реквизиты не
  переводим), ссылки на согласия внутри `start_text` переведены как заголовки навигации
  («Privacy Policy»/«Personal Data Processing Consent») — САМИ документы (PDF по ссылкам) остаются
  русскими и не редактировались, это не юридический текст согласия (LANG-09), а подпись ссылки на
  него.

Формат обоих словарей — тот же, что у `i18n_miniapp_manual.MANUAL_EN`: RU-текст БАЙТ-В-БАЙТ (после
`.strip()`, `services.i18n.src_hash`) -> EN. Плейсхолдеры (`{min}`, `{n}`, `{count}`, `{season}`,
`{имя}`...) и HTML-теги (`<b>`, `<i>`, `<a href>`, `<blockquote>`, `<u>`) перенесены дословно —
`tr()` подставляет их ПОСЛЕ перевода, сам перевод не трогает токены. Ключи БЕЗ ведущего эмодзи там,
где `services.i18n_glossary`-слой мог бы его отделять, здесь не нужны: `tr()` ищет по хешу ВСЕГО
содержимого (в отличие от `i18n_ui_en.tr_text`, у которого своя логика `split_leading_symbols`
для яруса A) — эмодзи в начале строки остаётся частью ключа.

Стиль перевода (приёмка 17.09): дружелюбно, на «you», коротко; «AIESEC»/«YouLead» латиницей
(английский текст — не под правило владельца о кириллице бренда в русских текстах); «CV» (не
«resume»/«summary», сверено с `i18n_miniapp_manual.MANUAL_EN`); кнопки — глаголом.
"""
from __future__ import annotations

import logging

from database.db import seed_manual_translations
from services.i18n import src_hash
from i18n_ui_en import MENU_EN  # литеральный словарь без импортов проекта — цикла нет

logger = logging.getLogger(__name__)

# Отдельные origin_key на каждый словарь — сид дефолтов и сид реальных событийных текстов могут
# развиваться независимо (событийный снимок устареет и будет заменён/удалён после сезона YL 26/2,
# словарь дефолтов кодовой базы — постоянный), но оба уважают ручную правку менеджера одинаково
# (см. докстринг `seed()`).
ORIGIN_DEFAULT = "form_manual_seed_default"
ORIGIN_EVENT = "form_manual_seed_event_260917"


# ── PROMPT_DEFAULTS (reg_engine.py) — тексты вопросов анкеты по умолчанию ───────────────────
_PROMPT_DEFAULTS_EN = {
    "Напиши свои ФИО (Фамилия Имя Отчество):": "Enter your full name (last, first, middle name):",
    "Напиши свой возраст числом:": "Enter your age as a number:",
    "Укажи номер телефона:": "Enter your phone number:",
    "Ты аламни или айсекер?": "Are you an alum or a current AIESEC member?",
    # Приёмка (стенд, 17.09, находка живого прогона): ВК принимает и ссылку vk.com/... — прежний
    # перевод («Enter your VK handle as @username:») не называл вторую форму ввода, хотя
    # STEP_HELP.vk (ниже) её уже упоминает — согласовано терминологией «VK username».
    "Введи свой ник в ВК в формате @username:": "Enter your VK username (@username) or a profile link:",
    "Из какого ты города?": "What city are you from?",
    "Учишься ли ты сейчас?": "Are you currently studying?",
    "На каком ты курсе?": "What year are you in?",
    "Откуда ты узнал(а) о нас?": "How did you hear about us?",
    "Хочешь стать амбассадором форума?": "Want to become a forum ambassador?",
    "Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.": "Attach your CV as a file (PDF or DOCX), or type it as text.",
    "Укажи свой email:": "Enter your email:",
    "Локальный комитет:": "Local Committee:",
    "Твоя позиция:": "Your position:",
    "Какая у тебя специальность?": "What's your major?",
    "Работаешь ли ты сейчас?": "Are you currently working?",
    "В какой сфере ты работаешь?": "What field do you work in?",
    "Каких навыков тебе сейчас не хватает?": "What skills do you feel you're missing right now?",
    "В каком формате ты будешь присутствовать?": "In what format will you attend?",
    "Планируете ли вы посетить второй неформальный день (пройдёт загородом)?": "Are you planning to join the second, informal day (held outside the city)?",
    "Любые вопросы/комментарии/пожелания:": "Any questions/comments/wishes:",
    "Твой департамент:": "Your department:",
    "Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):": "Your position (Member/TL/Manager/VP/LCP/Coordinator):",
    "Нужна справка в ВУЗ?": "Do you need a certificate for your university?",
    "Уровень английского:": "English level:",
    "Есть ли у тебя аллергии на продукты/запахи? (если нет — поставь «-»)": "Do you have any allergies to food/smells? (if none — put «-»)",
    "Особенности питания? Напиши, если ты веган/вегетарианец (иначе — обычное):": "Any dietary needs? Let us know if you're vegan/vegetarian (otherwise — regular):",
    "Когда приедешь?": "When will you arrive?",
    "Где будешь жить?": "Where will you stay?",
    "На площадке много двуспальных кроватей. Готов(а) спать с кем-то на одной кровати?": "There are a lot of double beds at the venue. Are you okay sharing a bed with someone?",
    "С кем хотел(а) бы делить кровать? Напиши имя или «без разницы».": "Who would you like to share a bed with? Write a name, or «doesn't matter».",
    "Как добираешься до площадки?": "How are you getting to the venue?",
    "Что бы ты хотел(а) видеть в CC-shop?": "What would you like to see in the CC-shop?",
    "Ожидания от команды организаторов?": "Expectations from the organizing team?",
    "Ожидания от контента?": "Expectations from the content?",
    "Хочешь быть волонтёром?": "Want to be a volunteer?",
    "Что из этого пробовал(а)? Можно выбрать несколько.": "Which of these have you tried? You can pick several.",
    "Какой у тебя опыт работы?": "What's your work experience?",
    "Когда готов(а) выйти на работу?": "When are you ready to start working?",
    "Пришли ссылку на резюме, портфолио или профиль — например hh.ru, GitHub, LinkedIn": "Send a link to your CV, portfolio, or profile — e.g. hh.ru, GitHub, LinkedIn",
    "Расскажи о своих проектах — учебных, пет-, рабочих. Что делал(а), какую роль играл(а)?": "Tell us about your projects — academic, pet, or work ones. What did you do, what role did you play?",
    "Есть ссылка на портфолио, GitHub или соцсети с работами? Можно пропустить.": "Got a link to a portfolio, GitHub, or social media with your work? You can skip this.",
    "В каком направлении хочешь развиваться?": "What direction do you want to grow in?",
    "Кейс-чемпионат — это возможность решить бизнес-кейс от партнёров и получить обратную связь. Участвуешь?": "The case championship is a chance to solve a business case from our partners and get feedback. Want to join?",
}

# ── STEP_HELP / STEP_HELP_EXAMPLES (reg_engine.py) — подсказки формата и их примеры ─────────
_STEP_HELP_EN = {
    "Фамилия и имя минимум, например «Иванова Мария».": "Last name and first name at minimum, e.g. «Smith Anna».",
    "Число от 10 до 120, например «19».": "A number from 10 to 120, e.g. «19».",
    "Формат имя@домен, например «ivanova@example.com».": "Format name@domain, e.g. «anna@example.com».",
    "Цифры, можно с плюсом впереди, например «+79161234567».": "Digits, optionally starting with a plus, e.g. «+79161234567».",
    "Ник в ВК («@username») или ссылка vk.com/username, без пробелов, например «@ivanova_maria».": "Your VK username (@username) or a vk.com/username profile link, no spaces, e.g. «@anna_smith».",
    "Файл PDF или DOCX до 10 МБ, либо текст ответом в чате.": "A PDF or DOCX file up to 10 MB, or text in your reply in the chat.",
    # STEP_HELP_EXAMPLES — согласованы с примерами выше (то же имя, тот же email).
    "Иванова Мария": "Smith Anna",
    "19": "19",
    "ivanova@example.com": "anna@example.com",
    "+79161234567": "+79161234567",
    "@ivanova_maria": "@anna_smith",
    "Резюме текстом: 2 года опыта в маркетинге.": "CV as text: 2 years of experience in marketing.",
}

# ── REG_LABELS (reg_labels.py) — подписи вопросов анкеты с эмодзи ───────────────────────────
_REG_LABELS_EN = {
    "🪪 ФИО": "🪪 Full name",
    "🎂 Возраст": "🎂 Age",
    "🔵 ВК": "🔵 VK",
    "📧 Email": "📧 Email",
    "📱 Телефон": "📱 Phone",
    "🏙 Город": "🏙 City",
    "📢 Источник": "📢 Source",
    "🏢 Лок. комитет": "🏢 Local Committee",
    "👔 Позиция": "👔 Position",
    "🎓 Образование": "🎓 Education",
    "🏫 ВУЗ": "🏫 University",
    "📖 Курс": "📖 Year",
    "📝 Специальность": "📝 Major",
    "💼 Работа": "💼 Work",
    "🏭 Сфера работы": "🏭 Field of work",
    "💡 Навыки": "💡 Skills",
    "💬 Ожидания (общие)": "💬 Expectations (general)",
    "🏕 Неформальный день": "🏕 Informal day",
    "📍 Формат": "📍 Format",
    "💬 Доп. комментарии": "💬 Additional comments",
    "🏢 Департамент": "🏢 Department",
    "🎖 Позиция в АЙСЕК": "🎖 AIESEC position",
    "📄 Справка в ВУЗ": "📄 University certificate",
    "🎓 Аламни/айсекер": "🎓 Alum/AIESEC member",
    "🇬🇧 Англ. язык": "🇬🇧 English level",
    "🤧 Аллергии": "🤧 Allergies",
    "🥗 Питание": "🥗 Diet",
    "🚌 Приезд": "🚌 Arrival",
    "🏠 Проживание": "🏠 Accommodation",
    "🛏 Общая кровать": "🛏 Shared bed",
    "🛏 Сосед по кровати": "🛏 Bed partner",
    "🚗 Трансфер": "🚗 Transport",
    "💳 Дата оплаты": "💳 Payment date",
    "🛍 CC-shop": "🛍 CC-shop",
    "💬 Ожидания: организация": "💬 Expectations: organization",
    "💬 Ожидания: контент": "💬 Expectations: content",
    "🙋 Волонтёр": "🙋 Volunteer",
    "📅 Дата приезда": "📅 Arrival date",
    "🎂 Дата рождения": "🎂 Date of birth",
    "🎯 Направление обучения": "🎯 Field of study",
    "🎯 Цель участия": "🎯 Goal of participation",
    "📋 Форматы форума": "📋 Forum formats",
    "🧡 Амбассадор": "🧡 Ambassador",
    "📄 Резюме": "📄 CV",
    "🧰 Стек и инструменты": "🧰 Stack and tools",
    "💼 Опыт работы": "💼 Work experience",
    "🚀 Готовность к работе": "🚀 Readiness to work",
    "🔗 Резюме ссылкой": "🔗 CV link",
    "🧩 Проекты": "🧩 Projects",
    "🖼 Портфолио": "🖼 Portfolio",
    "🧭 Направление развития": "🧭 Growth direction",
    "🏆 Кейс-чемпионат": "🏆 Case championship",
}

# ── _SUMMARY_FIELD_LABELS (reg_engine.py) — подписи сводки анкеты (bare, без эмодзи) ────────
# + reg_engine.summary_fields() computed-строки (bool -> «Да»/None, факт вложения файла).
_SUMMARY_LABELS_EN = {
    "ФИО": "Full name",
    "Возраст": "Age",
    "Дата приезда": "Arrival date",
    "Дата рождения": "Date of birth",
    "Email": "Email",
    "Телефон": "Phone",
    "ВК": "VK",
    "Город": "City",
    "Источник": "Source",
    "Лок. комитет": "Local Committee",
    "Позиция": "Position",
    "Образование": "Education",
    "ВУЗ": "University",
    "Курс": "Year",
    "Специальность": "Major",
    "Направление обучения": "Field of study",
    "Сфера работы": "Field of work",
    "Навыки": "Skills",
    "Ожидания": "Expectations",
    "Неформальный день": "Informal day",
    "Формат": "Format",
    "Комментарии": "Comments",
    "Департамент": "Department",
    "Позиция АЙСЕК": "AIESEC position",
    "Аламни/айсекер": "Alum/AIESEC member",
    "Справка в ВУЗ": "University certificate",
    "Английский": "English",
    "Аллергии": "Allergies",
    "Питание": "Diet",
    "Приезд": "Arrival",
    "Проживание": "Accommodation",
    "Общая кровать": "Shared bed",
    "Сосед по кровати": "Bed partner",
    "Трансфер": "Transport",
    "Дата план. оплаты": "Planned payment date",
    "CC-shop": "CC-shop",
    "Ожидания от орг": "Expectations: organizers",
    "Ожидания от контента": "Expectations: content",
    "Волонтёр": "Volunteer",
    "Цель участия": "Goal of participation",
    "Форматы форума": "Forum formats",
    "Стек": "Stack",
    "Опыт": "Experience",
    "Готовность": "Readiness",
    "Резюме (ссылка)": "CV (link)",
    "Проекты": "Projects",
    "Портфолио": "Portfolio",
    "Направление развития": "Growth direction",
    "Кейс-чемпионат": "Case championship",
    # reg_engine.summary_fields() computed
    "Работа": "Work",
    "Амбассадор": "Ambassador",
    "Город форума": "Forum city",
    # Приёмка 09.10: заголовок над результатами поиска справочника в чате.
    "Нашли — выбери:": "Found it — pick one:",
    "Резюме": "CV",
    "прикреплено файлом": "attached as a file",
}

# ── reg_options.py + SELECT_CONFIG/MULTI_CONFIG (reg_engine.py) — варианты ответа ───────────
_OPTIONS_EN = {
    # PARTY_TRACK_OPTIONS
    "Полная регистрация": "Full registration",
    "🎉 Гости с ночёвкой": "🎉 Guests with overnight stay",
    "🎉 Гости без ночёвки": "🎉 Guests without overnight stay",
    # DEFAULT_SOURCE_OPTIONS («Другое» уже в UI_EN)
    "Соцсети Юлид": "YouLead social media",
    "Соцсети АЙСЕК": "AIESEC social media",
    "Университетские каналы": "University channels",
    "Рассказал друг/знакомый": "A friend/acquaintance told me",
    "Узнал от амбассадора": "Heard from an ambassador",
    "Узнал от блогера": "Heard from a blogger",
    # EDUCATION_STATUS_OPTIONS
    "Да, в ВУЗе или колледже": "Yes, at a university or college",
    "Нет, завершил(а) обучение": "No, I've graduated",
    "Нет, не получал(а) образование": "No, I haven't studied",
    # COURSE_OPTIONS — числа языконезависимы, идентичная запись нужна только сторожу покрытия.
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5+": "5+",
    "Магистратура/Аспирантура": "Master's/PhD",
    # DEPARTMENT_OPTIONS — коды департаментов АЙСЕК, не язык, идентичная запись.
    "OGV": "OGV",
    "OGT": "OGT",
    "MKT": "MKT",
    "F&L": "F&L",
    "BD": "BD",
    "LCP": "LCP",
    "EwA": "EwA",
    # AIESEC_ROLE_OPTIONS — коды позиций, идентичная запись.
    "Member": "Member",
    "TL": "TL",
    "Manager": "Manager",
    "VP": "VP",
    "Coordinator": "Coordinator",
    # ENGLISH_LEVEL_OPTIONS
    "Начальный": "Beginner",
    "Средний": "Intermediate",
    "Продвинутый": "Advanced",
    "Свободный": "Fluent",
    # ARRIVAL_OPTIONS
    "В дни конфы": "On conference days",
    "Заранее": "In advance",
    "После": "After",
    # HOUSING_OPTIONS
    "Хост": "Host family",
    "Сам(а)": "On my own",
    "Не нужно": "Not needed",
    # POSITION_OPTIONS
    "Айсекер": "AIESEC member",
    "Аламни": "Alum",
    "Друг АЙСЕК": "Friend of AIESEC",
    # LOCAL_COMMITTEE_OPTIONS — коды ЛК, большинство уже латиницей, идентичная запись.
    "EG": "EG",
    "SPUEF": "SPUEF",
    "Moscow": "Moscow",
    "Tyumen": "Tyumen",
    "Ufa": "Ufa",
    "Ekaterinburg": "Ekaterinburg",
    # TRANSPORT_OPTIONS
    "Трансфер до площадки": "Shuttle to the venue",
    "Самостоятельно": "On my own",
    # ALUMNI_STATUS_OPTIONS (Аламни/Айсекер уже выше)
    "Ни то, ни другое": "Neither",
    # AMBASSADOR_OPTIONS
    "Да!": "Yes!",
    "Пока нет": "Not yet",
    # SELECT_CONFIG["city"] — дефолтный список городов (не путать с cities.CITIES/city_texts()).
    "Москва и МО": "Moscow and the Moscow Region",
    "Санкт-Петербург": "Saint Petersburg",
    "Новосибирск": "Novosibirsk",
    "Екатеринбург": "Yekaterinburg",
    "Казань": "Kazan",
    "Нижний Новгород": "Nizhny Novgorod",
    "Красноярск": "Krasnoyarsk",
    "Уфа": "Ufa",
    # study_field_options
    "Бизнес и управление": "Business and management",
    "IT и технологии": "IT and technology",
    "Социальные и гуманитарные науки": "Social sciences and humanities",
    "Математические и естественные науки": "Mathematics and natural sciences",
    # experience_options
    "Нет опыта": "No experience",
    "Пет-проекты": "Pet projects",
    "Стажировка": "Internship",
    "Коммерческий опыт до года": "Commercial experience under a year",
    "Коммерческий опыт больше года": "Commercial experience over a year",
    # readiness_options
    "Готов(а) выйти сейчас": "Ready to start now",
    "Через 3–6 месяцев": "In 3–6 months",
    "После выпуска": "After graduation",
    "Пока не ищу работу": "Not looking for a job yet",
    # goal_options
    "Найти возможность трудоустройства": "Find a job opportunity",
    "Прокачать свои hard и soft skills": "Level up my hard and soft skills",
    "Пообщаться с людьми из моей сферы, нетворкинг": "Meet people from my field, networking",
    "Получить карьерную консультацию от HR": "Get career advice from an HR specialist",
    "Узнать о деятельности компаний": "Learn about companies' work",
    # formats_options
    "Панельные дискуссии": "Panel discussions",
    "Мастер-классы": "Workshops",
    "Сессии со спикерами": "Speaker sessions",
    "Нетворкинг-сессии": "Networking sessions",
    "Ярмарка открытых вакансий": "Open job fair",
    # stack_options
    "Python": "Python",
    "JavaScript / TypeScript": "JavaScript / TypeScript",
    "Java / Kotlin": "Java / Kotlin",
    "C# / .NET": "C# / .NET",
    "Go": "Go",
    "SQL и базы данных": "SQL and databases",
    "Аналитика и данные": "Analytics and data",
    "Дизайн и UX": "Design and UX",
    "Мобильная разработка": "Mobile development",
    "DevOps и облака": "DevOps and cloud",
    "Тестирование": "Testing",
    "Информационная безопасность": "Information security",
    # config.UNIVERSITIES — общеизвестные латинские сокращения питерских ВУЗов.
    "ИТМО": "ITMO University",
    "Политех": "Polytech (SPbPU)",
    "ЛЭТИ": "LETI",
    "ГУАП": "GUAP",
    "БОООНЧ": "Bonch (SPbSUT)",
    "Горный": "Mining University",
    "Военмех": "Voenmeh",
    "СПбГАСУ": "SPbGASU",
    "СПбГУТ": "SPbSUT",
    "Технологический институт (СПбГТИ)": "Technological Institute (SPbSIT)",
    "ВШЭ (Питер)": "HSE (St. Petersburg)",
}

# ── Литералы aiogram-зависимых модулей (registration/reg_flow/reg_resume/reg_consent/
# user_actions/payment/application_effects/reg_schema) — не импортируются `i18n_sources.py`
# (докстринг `code_literals()`), продублированы там же буквально, здесь тот же приём. ────────
_CODE_LITERALS_EN = {
    # Запасные тексты /start и после подачи — с 09.10 дефолты реестра (start_text, reg_complete_text).
    "Привет! 👋\n\nЭто бот мероприятия. Зарегистрируйся, чтобы получить доступ ко всей информации.": "Hi! 👋\n\nThis is the event bot. Register to get access to all the information.",
    "Поздравляем, твоя заявка принята!\n\nМы рассмотрим её в течение 2-3 дней и напишем сюда. Следи за обновлениями, впереди много интересного.\n\nЕсли у тебя возникнут вопросы — не стесняйся задавать их нам!": "Congratulations, your application has been received!\n\nWe'll review it within 2-3 days and write to you here. Stay tuned — lots of exciting things ahead.\n\nIf you have any questions, don't hesitate to ask us!",
    "С возвращением! Ты уже зарегистрирован(а) — всё нужное в меню ниже 👇": "Welcome back! You're already registered — everything you need is in the menu below 👇",
    "С возвращением! Ты уже был(а) с нами на {season}. Давай обновим анкету — большинство ответов уже заполнено, останется только подтвердить 👇": "Welcome back! You were with us at {season}. Let's update your application — most answers are already filled in, you'll just need to confirm them 👇",
    "Отлично, начинаем регистрацию.": "Great, let's start the registration.",
    # Кнопка «🚀 Обновить анкету» под баннером возвращенца — ядро без эмодзи (приёмка 01.10).
    "Обновить анкету": "Update my application",
    "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию.": "Great, you came by a friend's invite. Let's start the registration.",
    "У тебя есть незаконченная анкета — что дальше?": "You have an unfinished application — what's next?",
    "Изменения отменены — анкета осталась прежней.": "Changes cancelled — your application stayed the same.",
    "Черновик не найден — начни заново с /start.": "Draft not found — start over with /start.",
    "Ты уже зарегистрирован(а) на этот сезон": "You're already registered for this season",
    "Точно отменить регистрацию? Все введённые ответы сотрутся.": "Are you sure you want to cancel the registration? All entered answers will be erased.",
    "Регистрация отменена. Чтобы начать заново, отправь /start.": "Registration cancelled. To start over, send /start.",
    "Принимаются только PDF или DOCX. Прикрепи файл ещё раз.": "Only PDF or DOCX are accepted. Please attach the file again.",
    "❌ Файл слишком большой (максимум 10 МБ). Прикрепи резюме меньшего размера.": "❌ File is too large (max 10 MB). Attach a smaller CV.",
    "Пришли резюме текстом или прикрепи файл (PDF или DOCX).": "Send your CV as text or attach a file (PDF or DOCX).",
    "Отмечай варианты кнопками выше и нажми «Готово».": "Mark your options with the buttons above and tap «Done».",
    "✅ Спасибо! Согласие обновлено.": "✅ Thanks! Your consent has been updated.",
    "Проверь свои ответы:": "Check your answers:",
    "🗓 Дата пока уточняется. Скоро сообщим! 🙂": "🗓 Date is still being confirmed. We'll let you know soon! 🙂",
    "📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!": "📍 The venue is being confirmed. We'll message you as soon as it's ready!",
    "Информация о мероприятии пока заполняется.": "Event information is still being filled in.",
    "Выбери, что тебя интересует:": "Choose what you're interested in:",
    "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», там актуальный список.": "This task was archived — you can no longer submit it. Check «🎯 Tasks» for the current list.",
    "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои задания», там актуальный список.": "This task was archived — you can no longer submit it. Check «🎯 My tasks» for the current list.",
    "Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что это ошибка — напиши менеджеру через «❓ Задать вопрос».": "You've used up your attempts for this task ({limit}). If you think this is a mistake, message a manager via «❓ Ask a question».",
    "⏰ Срок сдачи вышел. Отправить можно, но начислять баллы будет решать менеджер.": "⏰ The deadline has passed. You can still submit, but a manager will decide on the points.",
    "Больше {max_parts} частей в одну сдачу не влезет — нажми «✅ Готово», менеджер уже увидит присланное.": "More than {max_parts} parts won't fit in one submission — tap «✅ Done», the manager will already see what you sent.",
    "Это задание больше не доступно.": "This task is no longer available.",
    "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.": "Already submitted — someone beat you to it by a split second. Refresh the task list.",
    "Оплатили или оплата не требуется.": "Already paid, or payment isn't required.",
    "Пожалуйста, отправь вопрос текстом.": "Please send your question as text.",
    "Это команда для организаторов. Если вы организатор — попросите доступ у руководителя.": (
        "This command is for organizers. If you are an organizer, ask your lead for access."
    ),
    "Не удалось отправить вопрос, попробуйте позже.": "Couldn't send your question, please try again later.",
    "Администраторы не настроены.": "No administrators configured.",
    "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.": "To use the bot, you need to register first. Send the /start command.",
    "❌ Принимается только PDF-документ. Для скриншота используй функцию отправки фото.": "❌ Only a PDF document is accepted. For a screenshot, use the photo-sending option.",
    "❌ Файл слишком большой (максимум 10 МБ). Пришли чек меньшего размера.": "❌ File is too large (max 10 MB). Send a smaller receipt.",
    "❌ Изображение слишком большое (максимум 10 МБ). Пришли чек меньшего размера.": "❌ Image is too large (max 10 MB). Send a smaller receipt.",
    "⏳ Слишком часто. Подожди пару секунд и попробуй снова.": "⏳ Too often. Wait a couple of seconds and try again.",
    "❌ Отправь чек оплаты (PDF-документ или фото).\nИли /start — вернуться в меню (загрузить чек можно будет позже).": "❌ Send your payment receipt (a PDF document or photo).\nOr /start — return to the menu (you can upload the receipt later).",
    "Этот вариант недоступен для твоего трека.": "This option isn't available for your track.",
    "Вариант больше не доступен.": "This option is no longer available.",
    "Некорректный вариант.": "Invalid option.",
    "К сожалению, твоя заявка отклонена.": "Unfortunately, your application was rejected.",
    "Твоя заявка одобрена! Добро пожаловать 🎉": "Your application is approved! Welcome 🎉",
    "Заявка принята ✅ Всё получили — ждём тебя!": "Application accepted ✅ We got everything — see you there!",
    "🎁 Бонус за регистрацию!": "🎁 Registration bonus!",
    # Форум-ночь: подтверждение получения чек-ина по QR (`lit:user_actions.checkin_qr_confirm_receipt`).
    "Отлично, увидимся на форуме!": "Great, see you at the forum!",
    # Форум-ночь п.7 («❗ Важное»): экран делегата «❗ Важное» (`lit:user_actions.important_today_*`).
    "Сегодня важных рассылок не было.": "No important messages today.",
    "❗ Важные рассылки за сегодня:": "❗ Important messages today:",
    # Форум-ночь п.4 (расписание форума в боте): экран делегата «🗓 Программа» (`lit:program.*`).
    "Программа": "Program",
    "Выберите день:": "Choose a day:",
    "Сессий в этот день пока нет.": "No sessions for this day yet.",
    "Программа пока пуста.": "The program is empty for now.",
    "Идёт сейчас": "Happening now",
    "Следующая": "Next up",
    "Зал:": "Hall:",
    "Спикер:": "Speaker:",
    "параллельно": "in parallel",
    "← Дни": "← Days",
    # Форум-ночь п.6 (D-25, идея №14): шаблон «Не пришёл» — дефолт реестра checkin_not_arrived_text
    # (`lit:admin_checkin.not_arrived_go`) + подписи трёх кнопок ответа делегата и подтверждение
    # (`lit:user_actions.checkin_not_arrived_respond`).
    "Мы тебя не видим на форуме 👀 Всё в порядке?": "We don't see you at the forum 👀 Is everything okay?",
    "Уже еду": "On my way",
    "Не смогу прийти": "Can't make it",
    "Я на месте": "I'm here",
    # Кнопки под делегатскими рассылками форума (QR накануне, отзыв о сессии) — ключи БЕЗ
    # ведущего эмодзи, `reg_i18n.tr_text` снимает его до поиска.
    "Сохранил, открывается": "Saved, it opens",
    "Написать": "Write a comment",
    "Покажи этот экран волонтёру на входе.": "Show this screen to the volunteer at the entrance.",
    "Ты уже отмечен(а) на входе в {time} — всё в порядке, приятного форума!":
        "You're already checked in at the entrance at {time} — all good, enjoy the forum!",
    # Ведущий эмодзи "🎟 " отделяется `split_leading_symbols` ДО перевода (см. докстринг
    # `handlers/reg_i18n.py::tr_text`) — ключ здесь БЕЗ эмодзи, тот же, что реально ищет `tr()`.
    "Мой QR": "My QR",
    "Спасибо, передали организаторам!": "Thanks, we let the organizers know!",
    "Жаль! Спасибо, что предупредил.": "Too bad! Thanks for letting us know.",
    # Форум-ночь п.7 (переделка, ревью 470ce5e..3703ba4, п.5): «🔕 Не присылать сегодня» —
    # ключи БЕЗ ведущего эмодзи-префикса (тот же приём, что «Мой QR» выше — `reg_i18n.tr_text`
    # снимает префикс ДО поиска). Предложение отключиться (альбом) без префикса, переносить
    # нечего.
    "Сегодня многовато рассылок? Можно отключить необязательные до завтра:":
        "Getting a lot of messages today? You can turn off the non-essential ones until tomorrow:",
    "Не присылать сегодня": "Don't send today",
    "Присылать всё": "Send everything",
    "Хорошо, сегодня присылаю только важное. Вернуть — кнопка «🔔 Присылать всё».":
        "Okay, sending only important messages today. To undo — tap «🔔 Send everything».",
    "Хорошо, снова присылаю все рассылки.": "Okay, sending all messages again.",
    # D-34 (24.09, решение владельца): реестровые ключи `broadcast_mute_confirm_text`/
    # `broadcast_unmute_confirm_text` (settings_schema.py) хранят ПОЛНЫЙ текст С эмодзи-
    # префиксом (в отличие от пары строк выше, которые ловит именно `reg_i18n.tr_text` после
    # снятия префикса) — сторож `tests/test_i18n_form_manual_coverage_260917.py` сверяет
    # корпус реестровых дефолтов байт-в-байт, без снятия префикса.
    "🔕 Хорошо, сегодня присылаю только важное. Вернуть — кнопка «🔔 Присылать всё».":
        "🔕 Okay, sending only important messages today. To undo — tap «🔔 Send everything».",
    "🔔 Хорошо, снова присылаю все рассылки.": "🔔 Okay, sending all messages again.",
    # D-31 (24.09, «SOS без категорий»): реестровые дефолты режима «дописываю SOS»
    # (group "event", `sos_sent_text`/`sos_done_text`/`sos_collecting_expired_text`) — вопрос
    # «что случилось?» и категорийные подсказки сняты вместе с визардом.
    "🆘 Сигнал отправлен оргкомитету — с тобой свяжутся. Напиши, что случилось, пришли фото "
    "или геопозицию — всё уйдёт им.":
        "🆘 The signal has been sent to the organizers — they'll reach out to you. Type what "
        "happened, send a photo or your location — it'll all go to them.",
    # Он же без «🆘 » — `sos_sent_text` уходит через `reg_i18n.say` -> `tr_text`, который
    # снимает ведущий эмодзи до поиска.
    "Сигнал отправлен оргкомитету — с тобой свяжутся. Напиши, что случилось, пришли фото "
    "или геопозицию — всё уйдёт им.":
        "The signal has been sent to the organizers — they'll reach out to you. Type what "
        "happened, send a photo or your location — it'll all go to them.",
    "Принято. Если понадобится снова — жми «🆘 SOS».":
        "Got it. If you need it again — tap «🆘 SOS».",
    "Оргкомитет отметил вопрос решённым. Снова нужна помощь — жми «🆘 SOS».":
        "The organizers marked your issue as resolved. Need help again — tap «🆘 SOS».",
    "Сессия SOS закрыта по времени. Если всё ещё нужна помощь — нажми «🆘 SOS» снова.":
        "The SOS session closed due to inactivity. If you still need help — tap «🆘 SOS» again.",
    # Подпись кнопки геопозиции (`lit:sos.location_button`) — идёт через tr_kb (кнопки reply-
    # клавиатуры), не видна АСТ-сторожу, см. комментарий у её регистрации в
    # services/i18n_sources.py.
    "📍 Отправить геопозицию": "📍 Send location",
    # Он же без «📍 » — `tr_kb` -> `tr_text` снимает ведущий эмодзи до поиска, и запись выше
    # с эмодзи не находилась никогда: EN-делегат видел кнопку по-русски (приёмка 01.10).
    "Отправить геопозицию": "Send location",
    # Шапки ответа организаторов делегату (приёмка 01.10). «SOS #» в переводе обязателен:
    # по нему реплай делегата на ответ уходит обратно в тред (`handlers/sos.py`).
    "Ответ по SOS #{id}:": "Reply to SOS #{id}:",
    "Ответ от организаторов:": "Reply from the organizers:",
    # Ревью 24.09 (находки 1/3): дефолты трёх новых реестровых текстов SOS — тот же сосед,
    # что остальные sos_* дефолты выше.
    "Не получилось передать SOS оргкомитету. Подойди к стойке регистрации или к любому "
    "человеку в форме оргкомитета.":
        "We couldn't reach the organizers with your SOS. Go to the registration desk or "
        "find anyone wearing organizer clothing.",
    # Стенд 25.09: напоминание взявшему SOS и сигнал менеджерам после трёх напоминаний.
    "⏰ SOS #{id} у тебя в работе {minutes} мин. без отметки «✅ Решено» — если уже "
    "разобрался(лась), не забудь нажать её под карточкой.":
        "⏰ SOS #{id} has been with you for {minutes} min without «✅ Resolved» — if it's "
        "already handled, don't forget to tap it under the card.",
    # Он же без «⏰ » — `sos_claimed_remind_text` уходит через `reg_i18n.tr_fmt` -> `tr_text`,
    # который снимает ведущий эмодзи до поиска (та же пара, что у `sos_sent_text` выше).
    "SOS #{id} у тебя в работе {minutes} мин. без отметки «✅ Решено» — если уже "
    "разобрался(лась), не забудь нажать её под карточкой.":
        "SOS #{id} has been with you for {minutes} min without «✅ Resolved» — if it's "
        "already handled, don't forget to tap it under the card.",
    "⏰ SOS #{id} в работе у {who} уже {minutes} мин. без отметки «✅ Решено» — "
    "три напоминания остались без ответа.":
        "⏰ SOS #{id} has been with {who} for {minutes} min without «✅ Resolved» — "
        "three reminders went unanswered.",
    "Сигнал уже у оргкомитета ({claim_status}). Напиши, что случилось — дополню.":
        "The signal is already with the organizers ({claim_status}). Write what happened — I'll add it.",
    # Часть А (ревью SOS-переводов): `{claim_status}` — сам ярус B, ДВЕ формы шаблона
    # (`services.sos.claim_status_parts`), переводится ОТДЕЛЬНО от подстановки имени
    # (`handlers/sos.py::_recent_followup_text`, тот же порядок «шаблон сначала», что
    # `reg_i18n.tr_fmt` везде в чате). «коллега» — переводимый фолбэк имени, когда у держателя
    # заявки нет отображаемого имени в БД.
    "взял(а) {who}": "picked up by {who}",
    "ещё не взяли": "not picked up yet",
    "коллега": "a colleague",
    # Форум-ночь п.9 (идея №15, D-24): «⭐ Отзыв о сессии одним тапом» — четыре реестровых
    # дефолта экрана оценки (group "event", `session_feedback_prompt_text`/
    # `session_feedback_thanks_text`/`session_feedback_comment_hint_text`/
    # `session_feedback_comment_saved_text`).
    "Как тебе «{title}»?": "How was «{title}»?",
    "Спасибо! Хочешь добавить пару слов?": "Thanks! Want to add a few words?",
    "✍️ Напиши комментарий следующим сообщением.": "✍️ Write your comment as your next message.",
    "Спасибо, записал!": "Thanks, got it!",
    "Эта оценка тебе недоступна.": "This rating isn't available to you.",
    # Идея №32: «↩️ Отменить» на плашке сканера Mini App (волонтёру).
    "↩️ Отменить": "↩️ Undo",
    "Отметка снята — делегат снова не отмечен на этой точке.":
        "Check-in removed — the delegate is no longer checked in at this point.",
    "Отменить уже нельзя. Если отметка ошибочная — попросите менеджера снять её в боте.":
        "It's too late to undo. If the check-in is wrong, ask a manager to remove it in the bot.",
    "Отметка уже изменилась — её перенёс или снял кто-то другой. Ничего не отменено.":
        "The check-in has already changed — someone else moved or removed it. Nothing was undone.",
    "Не получилось отменить — попросите менеджера снять отметку в боте.":
        "Couldn't undo — ask a manager to remove the check-in in the bot.",
    # Идея №11: полоса «сеть медленная» в сканере Mini App (волонтёру).
    "Сеть медленная. Переключитесь на приложение-сканер.":
        "Slow network. Switch to the scanner app.",
    "Как переключиться": "How to switch",
    # Бэклог чек-ина №7: тренировка сканера и лист учебных QR (волонтёру).
    "Тренировка: отметка не записана.": "Training: the check-in was not recorded.",
    "Учебный QR — не пропуск на форум, ничего не записывается.":
        "Training QR — not a pass to the forum, nothing is recorded.",
    "Так работает отмена: на настоящем скане отметка снимется. Сейчас отменять нечего.":
        "This is how undo works: on a real scan the check-in is removed. There is nothing to undo now.",
    "Зелёная плашка «Отмечен». Пропускайте и сканируйте следующего.":
        "Green banner «Checked in». Let them in and scan the next one.",
    "Жёлтая плашка «Уже был в ЧЧ:ММ». Делегат сегодня уже проходил — пропускайте, если QR показывает он сам.":
        "Yellow banner «Already here at HH:MM». The delegate has already come in today — let them in "
        "if they are showing their own QR.",
    "Красная плашка «Заявка ещё на рассмотрении». Не пропускайте — отправьте на стойку проблемных случаев.":
        "Red banner «Application is still under review». Do not let them in — send them to the "
        "problem-cases desk.",
    "Красная плашка «Делегат прошлого сезона». Не пропускайте — отправьте на стойку проблемных случаев.":
        "Red banner «Delegate of a past season». Do not let them in — send them to the problem-cases desk.",
    "Красная плашка «QR другого мероприятия». Попросите открыть «Мой QR» в боте этого форума.":
        "Red banner «QR of another event». Ask them to open «My QR» in this forum's bot.",
    "Учебные QR: тренировка сканера": "Training QR codes: scanner practice",
    "Лист учебных QR. Распечатайте его или откройте на другом экране. В сканере выберите точку «Тренировка» и отсканируйте все пять — плашки те же, что на входе, но ничего не записывается. Учебный QR в любой другой точке тоже ничего не отметит. Для проверки приложения-сканера отсканируйте лист им и пришлите выгрузку в «Загрузить файл сканера» — учебные коды бот посчитает отдельно и не отметит.":
        "A sheet of training QR codes. Print it or open it on another screen. In the scanner choose "
        "the «Training» point and scan all five — the banners are the same as at the entrance, but "
        "nothing is recorded. A training QR at any other point won't check anyone in either. To "
        "test a scanner app, scan the sheet with it and send the export to «Upload scanner file» — "
        "the bot counts training codes separately and doesn't record them.",
}

# ── reg_engine._default_prompt_text/help_default — литералы движка, вычисляемые ДИНАМИЧЕСКИ
# (не лежат ни в одном перечислимом словаре `code_literals()` уже читает — PROMPT_DEFAULTS/
# STEP_HELP), поэтому `services/i18n_sources.py::corpus()` их не видит вообще, а `tr()` их всё
# равно получает на вход при рендере анкеты. Найдено при ревизии `reg_engine.py` (Задача 1
# инструкции — «литералы движка, которые выводятся через tr, но не входят в corpus()»). ─────
_ENGINE_DYNAMIC_EN = {
    # university (mode="text"/select) — дефолт вопроса, КОГДА reg_prompt_university НЕ задан.
    # Строка с двоеточием — код-дефолт; строка БЕЗ двоеточия — реальный оверрайд события
    # (см. `EVENT_TEXTS_260917["Введи название твоего ВУЗа"]` ниже), это разные строки.
    "Введи название твоего ВУЗа:": "Enter the name of your university:",
    "В каком ВУЗе/колледже ты учишься?": "Which university or college do you study at?",
    # resume, reg_resume_mode(city)=="text_only" — дефолт вопроса (не путать со STEP_HELP —
    # это отдельная ветка `_default_prompt_text`, не `help_default`).
    "Опиши вкратце свой опыт участия в проектах / активностях и, если есть, опыт работы. Например: был организатором школьных мероприятий, был куратором в университете и т. п.": (
        "Briefly describe your experience with projects/activities and, if any, work experience. "
        "For example: organized school events, was a mentor at university, etc."
    ),
    # expectations — дефолт вопроса, КОГДА ни event_name_genitive, ни event_name не заданы
    # (get_setting fallback «мероприятия»).
    "Что ты ожидаешь от мероприятия? Что хотел(а) бы узнать или получить?": "What do you expect from the event? What would you like to learn or get out of it?",
    # payment_plan_date — дефолт вопроса, КОГДА payment_deadline не задан (dl_note пустой).
    "Когда планируешь оплатить взнос? Введи дату (ДД.ММ.ГГГГ):": "When are you planning to pay the fee? Enter the date (DD.MM.YYYY):",
    # _DATE_HELP — общая подсказка формата для типа "date" (arrival_date/payment_plan_date;
    # birth_date получает отдельную _BIRTH_DATE_HELP, уже покрыта в FORM_DEFAULT_EN).
    "Формат ДД.ММ.ГГГГ, например «01.09.2026».": "Format DD.MM.YYYY, for example «01.09.2026».",
    # _STEP_HELP_RESUME_TEXT_ONLY_CHAT — подсказка резюме в чате при reg_resume_mode="text_only"
    # (версия для приложения "Коротко, текстом." уже покрыта в FORM_DEFAULT_EN/STEP_HELP).
    "Коротко, текстом в чате.": "Briefly, in text in the chat.",
    # _GENERIC_FALLBACK_LABEL composite: шаги date/select/multi БЕЗ записи в PROMPT_DEFAULTS
    # получают "{label} (ДД.ММ.ГГГГ):"/"{label}:"/"{label} (можно выбрать несколько):" —
    # birth_date/arrival_date/study_field/goal/formats. Оба варианта (с ведущим эмодзи и без)
    # нужны: `tr()` в Mini App получает текст ЦЕЛИКОМ (с эмодзи), `reg_i18n.tr_text` в чате
    # сначала отделяет эмодзи (`split_leading_symbols`) и ищет по остатку.
    "🎂 Дата рождения (ДД.ММ.ГГГГ):": "🎂 Date of birth (DD.MM.YYYY):",
    "Дата рождения (ДД.ММ.ГГГГ):": "Date of birth (DD.MM.YYYY):",
    "📅 Дата приезда (ДД.ММ.ГГГГ):": "📅 Arrival date (DD.MM.YYYY):",
    "Дата приезда (ДД.ММ.ГГГГ):": "Arrival date (DD.MM.YYYY):",
    "🎯 Направление обучения:": "🎯 Field of study:",
    "Направление обучения:": "Field of study:",
    "🎯 Цель участия (можно выбрать несколько):": "🎯 Goal of participation (you can select several):",
    "Цель участия (можно выбрать несколько):": "Goal of participation (you can select several):",
    "📋 Форматы форума (можно выбрать несколько):": "📋 Forum formats (you can select several):",
    "Форматы форума (можно выбрать несколько):": "Forum formats (you can select several):",
    # `reg_engine._v2_texts_for` — `{entity}` в `reg_form_own_option_text`/`reg_form_own_chip_text`
    # (шаблоны в `_FORM_INTRO`, `services/i18n_miniapp_manual.py`) подставляется ДО перевода
    # (`.replace("{entity}", entity)` на сыром русском значении настройки, entity ∈
    # `_LOOKUP_ENTITY_NAMES` — только "ВУЗ"/"город"), а не после — в отличие от большинства
    # других плейсхолдеров проекта. `tr()` поэтому видит уже СОСТАВНУЮ строку с "ВУЗ"/"город"
    # внутри, а не шаблон с "{entity}" — шаблонная запись в `_FORM_INTRO` для этого конкретного
    # места мертва (не находится), нужны обе готовые составные строки отдельно.
    "Впиши ВУЗ сам — менеджер увидит его как есть.": "Type your own university — the manager will see it exactly as you wrote it.",
    "Впиши город сам — менеджер увидит его как есть.": "Type your own city — the manager will see it exactly as you wrote it.",
    "Другой ВУЗ": "Other university",
    "Другой город": "Other city",
    # Трек «региональные форумы → Москва» (25.09, ревью 🔴2 forum-regions-msk): подписи кнопок
    # предложения переноса и короткие ответы бота (`handlers/user_actions.py`, callback_data
    # `rnm_accept`/`rnm_confirm`/`rnm_decline`) — хардкод в коде (та же конвенция, что у кнопок
    # «Уже еду»/«Не смогу прийти»/«Я на месте» выше: подпись кнопки короче фразы, самого текста
    # в реестре нет). Ведущий эмодзи "✅ " отделяется `split_leading_symbols` ДО перевода (тот
    # же приём, что у «🎟 Мой QR» выше) — ключ здесь БЕЗ эмодзи, тот же, что реально ищет `tr()`.
    # `{target_city}` — город назначения (`regional_noshow_target_city`, НИКОГДА не хардкод
    # «Москва») — плейсхолдер переживает перевод (сентинелы глоссария) и подставляется ПОСЛЕ
    # (`reg_i18n.tr_fmt`), RU/EN-ключи здесь намеренно с плейсхолдером внутри.
    "Перенести заявку: {target_city}": "Move application: {target_city}",
    "Нет, спасибо": "No, thanks",
    "Перенести заявку в другой город: {target_city}? Анкету заново заполнять не нужно.":
        "Move your application to another city: {target_city}? No need to fill out the form again.",
    "Да, перенести": "Yes, move it",
    "Готово, твоя заявка теперь здесь: {target_city}. Даты и место — в меню.":
        "Done, your application is now here: {target_city}. Dates and venue — in the menu.",
    "Заявку посмотрят ещё раз.": "Your application will be reviewed again.",
    "Хорошо, до встречи в следующий раз!": "Okay, see you next time!",
    "Уже перенесено.": "Already moved.",
    "Предложение уже отвечено.": "This offer has already been answered.",
    "Заявка уже не в {city}.": "Your application is no longer in {city}.",
    "Не нашли твою заявку — напиши организаторам.":
        "We couldn't find your application — please message the organizers.",
    "Перенос сейчас недоступен — напиши организаторам.":
        "The transfer isn't available right now — please message the organizers.",
    "Не получилось перенести заявку — напиши организаторам.":
        "Couldn't move your application — please message the organizers.",
}

# ── Голые ключи реестра (group=event/reg/game/pay) — хаб/статус/оплата/задания за пределами
# готовых списков выше: подписи Mini App-анкеты, экрана статуса заявки, оплаты, монет, FAQ. ──
_REGISTRY_TEXTS_EN = {
    # reg_engine._STEP_HELP_RESUME_FORK / _STEP_HELP_RESUME_FORK_FILE_CHAT — подсказки развилки
    # резюме (приложение и редактор в админке) и ветки «📎 Загрузить файл» в чате.
    "Файл PDF или DOCX до 10 МБ, ссылка на резюме, текст об опыте или «нет резюме» — как удобнее.": (
        "A PDF or DOCX file up to 10 MB, a link to your CV, a text about your experience, or «no CV» — whatever works for you."
    ),
    "Прикрепи файл резюме — PDF или DOCX до 10 МБ.": "Attach your CV file — PDF or DOCX up to 10 MB.",
    # Делегации вузов — тексты делегату, группа reg (delegation_welcome_text /
    # delegation_welcome_existing_text / delegation_game_off_text). RU-ключ — дефолт из
    # settings_schema.py байт-в-байт; меняется дефолт — меняются оба места одним коммитом.
    "Привет! Ты в списке делегации {university} на форум Юлид в Москве 🎉\n\nАнкету заполнять не нужно — заявка уже одобрена. Ниже меню участника: там будет QR на вход и все новости форума.": "Hi! You're on the {university} delegation list for the YouLead forum in Moscow 🎉\n\nNo need to fill in the questionnaire — your application is already approved. Below is the participant menu: your entry QR and all forum news will be there.",
    "Ты в списке делегации {university} — спасибо, что с нами! Заявка одобрена, меню участника ниже.": "You're on the {university} delegation list — thanks for being with us! Your application is approved, the participant menu is below.",
    "Задания и монеты для делегаций вузов на этом форуме выключены. Всё остальное — программа, QR на вход, новости — работает как обычно.": "Tasks and coins for university delegations are switched off at this forum. Everything else — the programme, the entry QR, the news — works as usual.",
    # Идея №29 бэклога чек-ина («Твой Юлид в цифрах»): дефолт подписи к фото карточки-итога
    # (group "reg", `forum_stats_card_caption_text`) — сами цифры остаются на картинке (её
    # подписи — код-литералы `services/forum_stats_card.py`, читаются ПРЯМО по языку делегата,
    # не через этот корпус), здесь только текст сообщения.
    "🎉 {name}, вот твой Юлид в цифрах! Поделись с друзьями 🧡💙": "🎉 {name}, here's your Юлид in numbers! Share it with friends 🧡💙",
    # Phase 33 (delegate-card admin actions): три опциональных сообщения делегату с карточки
    # /find менеджера (группа "apps" — вне DELEGATE_GROUPS, сторож i18n_form_manual_coverage_
    # 260917 их не требует, добавлены сверх минимума — тексты, которые реально уходят
    # делегату, того же класса, что coins_manual_notify_text).
    "Ваша заявка снова на рассмотрении — менеджер посмотрит её ещё раз.": "Your application is under review again — a manager will take another look.",
    "Вы можете подать заявку заново — отправьте /start.": "You can submit your application again — send /start.",
    "Менеджер разрешил вам ещё раз изменить анкету — нажмите «✏️ Изменить анкету» под этим сообщением.": "A manager has allowed you to edit your application once more — tap «✏️ Edit application» below this message.",
    # Phase 33 (задача 1): «🧹 Сбросить зависшую анкету» — та же группа "apps"/тот же
    # приём "сверх минимума", что у трёх строк выше.
    "Анкета сброшена — начни заново: /start": "Your application has been reset — start again: /start",
    "Москва, 30-31 октября": "Moscow, October 30–31",
    "Санкт-Петербург, 3 октября": "Saint Petersburg, October 3",
    "Тюмень, 3 октября": "Tyumen, October 3",
    "Напиши свой вопрос, и мы передадим его организаторам.": "Type your question, and we'll pass it on to the organizers.",
    "Твой вопрос отправлен!": "Your question has been sent!",
    "📜 <b>История монет</b>": "📜 <b>Coin history</b>",
    "🪙 Баланс: {balance} монет\nМесто в общем рейтинге: {rank} из {total}": "🪙 Balance: {balance} coins\nOverall leaderboard place: {rank} of {total}",
    "🪙 Баланс изменён: {delta} монет.\nПричина: {reason}\nТекущий баланс: {balance}": "🪙 Balance changed: {delta} coins.\nReason: {reason}\nCurrent balance: {balance}",
    "Контакты пока не указаны. Обратитесь к организаторам.": "Contacts haven't been added yet. Please reach out to the organizers.",
    # Техническое значение (URL HTTP-драйвера перевода), не естественный язык — идентичная
    # запись только ради сторожа покрытия (ключ `delegate_lang_http_url` физически лежит в
    # группе `reg`, `i18n_sources.delegate_registry_keys()` его не исключает).
    "http://libretranslate:5000": "http://libretranslate:5000",
    "Не нашёл ответ — спросить менеджера": "Didn't find an answer — ask a manager",
    "Собрали ответы на частые вопросы — может, твой уже здесь:": "We've gathered answers to common questions — yours might already be here:",
    "🏆 <b>Рейтинг по монетам</b>": "🏆 <b>Coin leaderboard</b>",
    "Твоё место: <b>{rank}</b> · баланс: <b>{balance}</b>": "Your place: <b>{rank}</b> · balance: <b>{balance}</b>",
    "👋 Вы начали регистрацию, но не завершили её. Отправьте /start, чтобы продолжить — это займёт пару минут.": "👋 You started your registration but didn't finish it. Send /start to continue — it takes a couple of minutes.",
    "🔄 Бот перезапускался, но твоя анкета сохранена — продолжим?": "🔄 The bot restarted, but your application is saved — shall we continue?",
    "Регистрация на вечеринку сейчас закрыта.": "Party registration is currently closed.",
    "💰 <b>Оплата участия</b>\n\nВариант: {option}\nСумма: {amount} ₽\n\n{requisites}{deadline}{penalties}📎 Загрузи чек оплаты (PDF-документ или скриншот).": "💰 <b>Payment for participation</b>\n\nOption: {option}\nAmount: {amount} ₽\n\n{requisites}{deadline}{penalties}📎 Upload your payment receipt (a PDF document or screenshot).",
    "💳 Выбери вариант участия:": "💳 Choose your participation option:",
    "Кнопка «💳 Оплата» будет в меню, пока чек не отправлен.": "The «💳 Payment» button will stay in the menu until you send the receipt.",
    "Ок! Оплатишь позже.": "Ok! You'll pay later.",
    "✅ Чек получен! Менеджер проверит его в ближайшее время.": "✅ Receipt received! A manager will check it shortly.",
    "⏳ Твоя заявка на рассмотрении. Доступ откроется после одобрения.": "⏳ Your application is under review. Access will open once it's approved.",
    "Отбор не пройден.": "You didn't pass the selection.",
    "Чтобы продолжить, задайте @username в настройках Telegram и снова отправьте /start.": "To continue, set a @username in your Telegram settings and send /start again.",
    "Программа форума ещё не загружена.": "The forum program hasn't been uploaded yet.",
    "<b>{label}</b>\n\nПрошлый ответ: <b>{display}</b>\n\nОставить или изменить?": "<b>{label}</b>\n\nPrevious answer: <b>{display}</b>\n\nKeep it or change it?",
    "У нас есть твоё резюме с прошлой регистрации. Оставить его или прислать новое?": "We have your CV from your previous registration. Keep it or send a new one?",
    "Отправь эту ссылку друзьям, чтобы пригласить их на форум!\n\n{link}": "Send this link to your friends to invite them to the forum!\n\n{link}",
    "Пока никто не зарегистрировался по твоей ссылке.\n\nПоделись ей с друзьями:\n{link}": "No one has registered with your link yet.\n\nShare it with your friends:\n{link}",
    "👥 <b>Твои приглашённые ({count}):</b>": "👥 <b>Your invitees ({count}):</b>",
    "✅ Анкета уже отправлена — мы её получили. Ответ придёт сюда, в чат.": "✅ Application already submitted — we've received it. The answer will come here, in the chat.",
    "Финал — очно на СкиллАп 5, командами по 3–4 человека. Опыт не нужен, важно желание пробовать.": "The final is in person at SkillUp 5, in teams of 3–4. No experience needed — just a willingness to try.",
    "Проверь образование": "Check your education",
    "Исправить": "Fix",
    "🕓 История": "🕓 History",
    "Несохранённые правки пропадут, анкета вернётся к последней отправленной версии. Отменить изменения?": "Unsaved edits will be lost, and the application will return to the last submitted version. Cancel the changes?",
    "↩️ Отменить изменения": "↩️ Cancel changes",
    "Восемь популярных ответов сразу под вопросом": "Eight popular answers right under the question",
    "Чипы частых значений": "Chips with common answers",
    "Регистрация сейчас закрыта. Как только менеджер её откроет — анкета появится здесь снова.": "Registration is currently closed. As soon as a manager opens it, the application will appear here again.",
    "Спасибо! Мы всё получили — подробности пришли в чат с ботом.": "Thank you! We've received everything — details were sent to the bot chat.",
    "Заявка принята": "Application accepted",
    "Ответы обновились из чата — некоторые поля мы обновили автоматически.": "Your answers were updated from the chat — we've updated some fields automatically.",
    "Нужно подтвердить согласия — вернитесь к шагу «Согласия».": "You need to confirm your consents — go back to the «Consents» step.",
    "Продолжить": "Continue",
    "📱 Заполнить в приложении": "📱 Fill in the app",
    "Черновик сохранён": "Draft saved",
    "💾 Отправить изменения": "💾 Submit changes",
    "Изменения отправлены": "Changes submitted",
    "Анкета снова на проверке": "Application is under review again",
    "ВУЗ, курс и программа вместо четырёх вопросов": "University, year, and program instead of four questions",
    "Образование одним экраном": "Education on one screen",
    "Короткий отклик на выбор и на ошибку": "A short vibration on selection and on error",
    "Вибрация": "Haptics",
    "Тема, язык и вибрация — выбирает делегат": "Theme, language, and haptics — chosen by the delegate",
    "Настройки в шапке анкеты": "Settings in the application header",
    "«Навыки 0/3» вместо ошибки при четвёртом выборе": "«Skills 0/3» instead of an error on the fourth pick",
    "Счётчик лимита в заголовке": "Limit counter in the heading",
    "Города и ВУЗы ищутся по первым буквам": "Cities and universities are searched by the first letters",
    "Поиск в справочнике": "Directory search",
    "Выключишь элемент — шаг не сломается, а упростится: вместо чипов останется поиск, вместо поиска — список, вместо списка — обычный ввод.": "Turn off an element and the step won't break, it will just get simpler: chips fall back to search, search falls back to a list, a list falls back to plain input.",
    "Что включено в новой анкете": "What's included in the new application",
    "Посмотреть анкету глазами делегата": "Preview the application as a delegate",
    "Выбери, какой анкетой собираешь заявки. Переключается на ходу, без перезапуска бота.": "Choose which application form you're using to collect applications. Switches on the fly, no bot restart needed.",
    "Анкета мероприятия": "Event application form",
    "Выбери минимум {min}": "Choose at least {min}",
    "Правки не сохранились — откройте поле ещё раз и попробуйте снова.": "The edits weren't saved — open the field again and try once more.",
    "Не заполнено": "Not filled in",
    "необязательно": "optional",
    "из прошлой анкеты": "from your previous application",
    "Шаг {step} из {total}": "Step {step} of {total}",
    "Вопросы анкеты": "Application questions",
    "Заявку отклонили. Можно поправить ответы и отправить ещё раз.": "Your application was rejected. You can fix your answers and submit it again.",
    "«Добавить ещё проект» в опыте и портфолио": "«Add another project» in experience and portfolio",
    "Повторяемые блоки": "Repeatable blocks",
    "Заявка отправлена заново": "Application resubmitted",
    "Здесь резюме принимается текстом — напиши коротко в ответном сообщении.": "Here your CV is accepted as text — write a short reply message.",
    "Файл больше 20 МБ — сожмите или пришлите ссылку в описании.": "File is larger than 20 MB — compress it or send a link in the description.",
    "Не удалось загрузить файл — попробуй ещё раз или пришли резюме текстом.": "Couldn't upload the file — try again or send your CV as text.",
    "Выбери вариант резюме кнопкой ещё раз: приложи файл, дай ссылку или напиши о себе текстом.": "Pick your CV option with a button again: attach a file, share a link or write about yourself as text.",
    "Файл резюме ещё не загрузился — прикрепи его ещё раз.": "Your CV file hasn't uploaded yet — please attach it again.",
    "Выбрать другой способ": "Choose another way",
    "Резюме принимается как PDF или DOCX — другой формат не подойдёт.": "Your CV is accepted as PDF or DOCX — other formats won't work.",
    "📱 Поделиться номером": "📱 Share phone number",
    "Вместо строчки «Анкета: одобрена» в профиле": "Instead of the «Application: approved» line in the profile",
    "Экран статуса заявки во всю ширину": "Full-width application status screen",
    "✅ Отправить анкету": "✅ Submit application",
    "обновлено в чате": "updated in chat",
    "Сломается — вернёшь одним нажатием, ответы делегатов останутся на месте.": "If something breaks, revert with one tap — delegates' answers stay in place.",
    "Новая анкета": "New application form",
    "📱 Анкета сейчас открыта в приложении. Продолжить здесь, в чате?": "📱 The application is currently open in the app. Continue here, in the chat?",
    "✍️ Возвращаемся в чат. Вот твой вопрос:": "✍️ Back to the chat. Here's your question:",
    "📱 Анкета открыта в приложении — заполняй там. Захочешь вернуться в чат, просто напиши мне, и я предложу продолжить здесь.": "📱 The application is open in the app — fill it in there. If you want to come back to the chat, just message me and I'll offer to continue here.",
    "✍️ Продолжить в чате": "✍️ Continue in chat",
    "Выкл": "Off",
    "Вкл": "On",
    "Язык анкеты": "Application language",
    "Меняет только эту анкету у тебя. На ответы и на то, что видит менеджер, не влияет.": "Only changes your own application view. Doesn't affect your answers or what the manager sees.",
    "Как в Телеграме": "Same as Telegram",
    "Тёмная": "Dark",
    "Оформление": "Appearance",
    "Светлая": "Light",
    "Или файлом": "Or as a file",
    "Ссылку сохранили ✅": "Link saved ✅",
    "Профиль узнали — ссылка рабочая": "Profile recognized — the link works",
    "Подойдёт любая открытая ссылка: резюме, портфолио, профиль на сайте вакансий.": "Any public link works: a CV, a portfolio, or a job-site profile.",
    "Ничего не нашли по запросу «{query}»": "Nothing found for «{query}»",
    "Справочник грузит менеджер один раз. Название приходит нормализованным — «СПбГАСУ» и «спбгасу» не попадут в таблицу двумя разными строками.": "The manager loads the directory once. Names are normalized — «SPbGASU» and «spbgasu» won't end up as two separate entries in the table.",
    "Выбрано {selected} из {max}": "Selected {selected} of {max}",
    "Можно выбрать не больше {max} вариантов.": "You can choose no more than {max} options.",
    "Выбрано {n} из {max}. Больше не нужно — сними лишнее, чтобы поменять.": "Selected {n} of {max}. No more needed — deselect one to change your pick.",
    "Выбрано {n} из {max}. Можно добавить ещё {left}.": "Selected {n} of {max}. You can add {left} more.",
    "Можно выбрать до {max}.": "You can choose up to {max}.",
    # Легаси: дефолт `reg_multi_limit_hint_zero_text` ДО коммита «счётчик мультивыбора не
    # обещает…» (main) — обещал необязательность шага, которая не всегда была правдой у
    # обязательных multi-шагов. Держим перевод строки — она могла осесть как оверрайд события
    # или machine-перевод в чужой БД (LANG-05, сид не трогает manual=1 чужого происхождения).
    "Выбрано 0 из {max}. Можно ничего не выбирать — шаг необязательный.": "Selected 0 of {max}. You can choose nothing — this step is optional.",
    # Актуальный дефолт (main, «счётчик мультивыбора не обещает «можно ничего не выбирать» у
    # обязательного шага») — короче, без обещания необязательности; обязательность шага
    # сообщает отдельная подсказка над вариантами (см. `settings_schema.py::reg_multi_limit_hint_zero_text`).
    "Выбрано 0 из {max}.": "Selected 0 of {max}.",
    "📱 Продолжить в приложении": "📱 Continue in the app",
    "💬 Продолжить в чате": "💬 Continue in chat",
    "Сейчас в команде": "Currently on the team",
    "Был(а) в АЙСЕК раньше": "Was in AIESEC before",
    "Пришёл(ла) на форум впервые": "Coming to the forum for the first time",
    "Поделиться номером из Телеграма": "Share number from Telegram",
    "Телеграм подставит номер сам — вводить руками не нужно.": "Telegram will fill in the number automatically — no need to type it.",
    "+ Добавить ещё {noun}": "+ Add another {noun}",
    "Опиши коротко.": "Describe it briefly.",
    "В чате тот же шаг спрашивает «Добавить ещё? Да / Готово» — способ ответить есть на любой поверхности.": "In the chat, the same step asks «Add another? Yes / Done» — there's a way to answer on any surface.",
    "Добавить ещё?": "Add another?",
    "Как называется этот {noun}?": "What's this {noun} called?",
    "Проект {n}": "Project {n}",
    "▶️ Продолжить с шага {step} из {total}": "▶️ Continue from step {step} of {total}",
    "📎 Загрузить файл": "📎 Upload a file",
    "🔗 Дать ссылку": "🔗 Give a link",
    "✍️ Написать текстом": "✍️ Type it instead",
    "Опиши свой опыт текстом: где работаешь или учишься, какие проекты, стажировки или волонтёрство есть, какие задачи решал(а) и каких результатов добился(ась) — коротко.": "Describe your experience in text: where you work or study, what projects, internships or volunteering you've done, what tasks you handled and what results you got — keep it short.",
    "Опиши опыт текстом ответом в чате.": "Describe your experience in a text reply in the chat.",
    "🙅 У меня нет резюме": "🙅 I don't have a CV",
    "Выбери способ кнопкой выше ⬆️ — в том числе если хочешь написать об опыте текстом.": (
        "Choose a way with the button above ⬆️ — including if you want to describe your experience in text."
    ),
    "Пришли ссылку целиком, начиная с http:// или https://": "Send the full link, starting with http:// or https://",
    "Личный сайт — тоже подойдёт": "A personal website also works",
    "{domain} — сайт из списка проверенных": "{domain} — a site from our trusted list",
    "Пропадут уже введённые ответы ({count}). Начать анкету заново?": "Your entered answers ({count}) will be lost. Start the application over?",
    "🔄 Начать заново": "🔄 Start over",
    "Почти всё": "Almost there",
    "Заполнить": "Fill in",
    "О тебе": "About you",
    "Форум": "Forum",
    "Учёба и опыт": "Studies and experience",
    "Пропущено необязательное: {list}": "Skipped optional: {list}",
    "Отправить заявку": "Submit application",
    "Проверь перед отправкой": "Check before submitting",
    "Реквизиты и чек — в одном экране.": "Payment details and receipt — on one screen.",
    "Оплати участие": "Pay for participation",
    "Менеджер подтвердит за день.": "A manager will confirm within a day.",
    "Пришли чек": "Send the receipt",
    "Задания уже открыты.": "Tasks are already open.",
    "Собирай монеты до форума": "Collect coins before the forum",
    "Если чего-то не хватит — напишет в чат.": "If something's missing, we'll message you in the chat.",
    "Менеджер читает анкету": "A manager is reading your application",
    "Одобрение или отказ с причиной.": "Approval or rejection with a reason.",
    "Придёт ответ в чат": "The answer will come to the chat",
    "Тариф зависит от трека, реквизиты пришлём.": "The rate depends on the track, we'll send the payment details.",
    "После одобрения — оплата": "After approval — payment",
    "📲 Подхватил ответы, которые вы ввели в приложении.": "📲 Picked up the answers you entered in the app.",
    "Список спикеров формируется и скоро появится здесь.": "The speaker list is being put together and will appear here soon.",
    "Хочешь участвовать снова? Обновим анкету — прошлые ответы предложу оставить.": "Want to join again? Let's update your application — I'll offer to keep your previous answers.",
    "Твоя заявка в этом сезоне отклонена. Это не навсегда: анкету можно поправить и подать заново 👇": "Your application was rejected this season. It's not final: you can fix your answers and apply again 👇",
    # Квик 260923 (форум-чекин): подпись к личному QR и текст «QR ещё не выдаётся» (кнопка
    # «🎟 Мой QR» главного меню, group "reg" — делегатский текст, checkin_qr_caption_text /
    # checkin_qr_disabled_text).
    "🎟 Твой QR для отметки на форуме.\n\nСохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото из чата открывается и без интернета.": (
        "🎟 Your QR code for check-in at the forum.\n\n"
        "Save it in advance (for example, take a screenshot) — the venue may have no network, "
        "but a photo from the chat opens even without the internet."
    ),
    # Та же подпись БЕЗ «🎟 » — её ищет `reg_i18n.tr_text` после снятия ведущего эмодзи
    # (бот шлёт подпись QR через него); ключ выше нужен сторожу корпуса и `tr()` целиком.
    "Твой QR для отметки на форуме.\n\nСохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото из чата открывается и без интернета.": (
        "Your QR code for check-in at the forum.\n\n"
        "Save it in advance (for example, take a screenshot) — the venue may have no network, "
        "but a photo from the chat opens even without the internet."
    ),
    "QR для чек-ина пока не выдаётся — загляни сюда позже.": "The check-in QR isn't issued yet — check back here later.",
    # Форум-ночь п.3 (D-03, идея №2): текст рассылки QR накануне форума (и утреннего повтора
    # неподтвердившим) — group "reg", checkin_qr_broadcast_text.
    "Завтра форум! Вот твой QR для входа. Открой его сейчас и сделай скриншот — на площадке может не быть сети.": (
        "The forum is tomorrow! Here's your QR code for entry. Open it now and take a "
        "screenshot — the venue may have no network."
    ),
    # Утренний повтор QR в день форума — checkin_qr_morning_text (group "reg").
    "Сегодня форум! Вот твой QR для входа. Открой его сейчас и сделай скриншот — на площадке может не быть сети.": (
        "The forum is today! Here's your QR code for entry. Open it now and take a "
        "screenshot — the venue may have no network."
    ),
    # Форум-ночь (идея №3 бэклога чек-ина): приветствие после первой отметки входа — group
    # "reg", forum_welcome_text. Плейсхолдер `{time}` переживает перевод сентинелами глоссария
    # (services/i18n_glossary.py) — тот же приём, что у остальных плейсхолдеров этого словаря.
    "Ты отмечен на входе в {time} ✅ Добро пожаловать на Юлид!": (
        "You're checked in at {time} ✅ Welcome to YouLead!"
    ),
    # Идея №4 бэклога чек-ина: строка статуса «✅ Ты отмечен» (кнопка «🎟 Мой QR», хаб Mini
    # App) — group "reg", checked_in_status_text. Ключ БЕЗ ведущего «✅ » — `handlers.reg_i18n.
    # tr_text`/`split_leading_symbols` отделяет ведущий эмодзи-префикс ДО перевода и
    # приклеивает его назад нетронутым (докстринг `services/i18n_glossary.py::
    # split_leading_symbols`); словарь с эмодзи внутри ключа НИКОГДА не совпал бы по хешу с
    # уже отделённым остатком (тот же класс бага, что ревью нашло у `{claim_status}`, только
    # для символьного, не прогресс-префикса).
    "Отмечен на входе в {time} · сессий: {sessions}": "Checked in at {time} · sessions: {sessions}",
    # Идея №23 бэклога чек-ина: опрос неявившихся «почему не пришёл» — вопрос, пять подписей
    # кнопок ответа, подсказка после «Другое», ответ-подтверждение (group "reg",
    # forum_noshow_poll_*).
    "Мы не видели тебя на форуме. Расскажешь, что помешало прийти?":
        "We didn't see you at the forum. Would you tell us what got in the way?",
    "Передумал(а)": "Changed my mind",
    "Не смог(ла) по учёбе/работе": "Couldn't make it because of school/work",
    "Далеко ехать": "Too far to travel",
    "Забыл(а)": "Forgot",
    "Другое": "Other",
    "Напиши своими словами, что помешало прийти.": "Tell us in your own words what got in the way.",
    "Спасибо, учтём!": "Thanks, noted!",
    # Идея №5 бэклога чек-ина: приглашение волонтёров ссылкой — приветствие/просрочка ссылки/
    # уже-есть-доступ (group "reg", volunteer_invite_*).
    "Добро пожаловать в команду волонтёров! Тебе выдан доступ к сканеру отметок.":
        "Welcome to the volunteer team! You've been granted access to the check-in scanner.",
    "Ссылка устарела, попроси у организатора новую.": "This link has expired, ask the organizer for a new one.",
    "У тебя уже есть доступ к боту как у менеджера.": "You already have manager-level access to the bot.",
    # D-41 (FORUM-CHECKIN.md): регистрация на месте — тексты человеку у стойки (group "reg",
    # onsite_reg_*) и волонтёру сканера (group "event", onsite_*).
    "Привет! Это короткая регистрация на форум прямо на площадке — всего 3 вопроса: "
    "имя, телефон и вуз.\n\n"
    "Нажимая кнопку ниже, ты соглашаешься на обработку этих данных для участия в форуме.": (
        "Hi! This is a quick on-site registration for the forum — just 3 questions: your name, "
        "phone number and university.\n\n"
        "By tapping the button below, you agree to the processing of this data for taking part "
        "in the forum."
    ),
    "✅ Согласен(на), начать": "✅ I agree, let's start",
    "Как тебя зовут? Напиши фамилию и имя, например: Иванова Мария":
        "What's your name? Write your last and first name, for example: Ivanova Maria",
    "Твой номер телефона? Нажми кнопку ниже или напиши номер, например: +79991234567":
        "Your phone number? Tap the button below or type the number, for example: +79991234567",
    "Где ты учишься? Напиши название вуза или нажми «Пропустить».":
        "Where do you study? Type your university name or tap “Skip”.",
    "Пропустить": "Skip",
    "Готово, {name}! Подойди к волонтёру на стойке регистрации и назови свою фамилию — "
    "он пропустит тебя на форум.": (
        "All set, {name}! Go to the volunteer at the registration desk and say your last name — "
        "they'll let you into the forum."
    ),
    "Регистрация на месте сейчас не открыта. Подойди, пожалуйста, к организаторам.":
        "On-site registration isn't open right now. Please talk to the organizers.",
    "Твоя заявка уже одобрена! Открой «🎟 Мой QR» в меню и покажи его волонтёру на входе.":
        "Your application is already approved! Open “🎟 My QR” in the menu and show it to the "
        "volunteer at the entrance.",
    "У тебя уже есть заявка. Подойди к стойке регистрации — волонтёр найдёт тебя по фамилии.":
        "You already have an application. Go to the registration desk — the volunteer will find "
        "you by your last name.",
    "Добро пожаловать на форум! Тебя зарегистрировали на месте. Ниже — твой QR: он "
    "понадобится на сессиях, сохрани его.": (
        "Welcome to the forum! You've been registered on site. Below is your QR code — you'll "
        "need it at the sessions, so save it."
    ),
    "Не понял имя. Напиши фамилию и имя буквами через пробел, например: Иванова Мария":
        "I didn't get the name. Write your last and first name in letters separated by a space, "
        "for example: Ivanova Maria",
    "Не понял номер. Нажми кнопку «Отправить номер» ниже или напиши номер цифрами, "
    "например: +79991234567": (
        "I didn't get the number. Tap the “Share number” button below or type the number in "
        "digits, for example: +79991234567"
    ),
    "Это не твой номер. Нажми кнопку «Отправить номер» ниже — Телеграм пришлёт твой — "
    "или напиши свой номер цифрами, например: +79991234567": (
        "That's not your number. Tap the “Share number” button below — Telegram will send "
        "yours — or type your number in digits, for example: +79991234567"
    ),
    "✅ Пропустить и одобрить": "✅ Let in and approve",
    "Одобрить {name} на месте и отметить вход? Решение запишется на вас.":
        "Approve {name} on site and check them in? The decision will be recorded under your name.",
    "📝 Зарегистрировать на месте": "📝 Register on site",
    "Пусть человек отсканирует этот QR камерой телефона и ответит на 3 вопроса в боте. "
    "Потом найдите его в списке «Ждут на стойке» и одобрите.": (
        "Have the person scan this QR code with their phone camera and answer 3 questions in "
        "the bot. Then find them in the “Waiting at the desk” list and approve."
    ),
    "📝 Ждут на стойке": "📝 Waiting at the desk",
    "Делегат другого города: {city} — отправьте на стойку своего города или к организаторам":
        "Delegate from another city: {city} — send them to their city's desk or to the organizers",
    "Одобрен(а) на месте": "Approved on site",
    "Не получилось собрать ссылку — откройте сканер заново.":
        "Couldn't build the link — reopen the scanner.",
    "Выберите город вверху экрана — у каждого города своя ссылка.":
        "Choose a city at the top of the screen — each city has its own link.",
    "Заявка отклонена менеджером": "The application was rejected by a manager",
    "Заявка отклонена менеджером. Причина: {reason}":
        "The application was rejected by a manager. Reason: {reason}",
    "⚠️ Пропустить вопреки отказу": "⚠️ Let in despite rejection",
    "🗑 Убрать": "🗑 Remove",
    "Убрать {name} из списка ждущих? Короткая анкета удалится — если человек всё же "
    "придёт, ему нужно будет заполнить её заново.": (
        "Remove {name} from the waiting list? The short form will be deleted — if the person "
        "does come, they will need to fill it in again."
    ),
    "Убрать нельзя — по человеку уже есть решение или это обычная заявка.":
        "Can't remove — this person already has a decision or a regular application.",
    "Слишком длинно — напиши название вуза короче, до 200 символов, например: СПбГУ":
        "That's too long — write a shorter university name, up to 200 characters, for example: "
        "SPbU",
    "Не получилось сохранить согласие — нажми кнопку ещё раз.":
        "Couldn't save your consent — please tap the button again.",
    "Слишком много попыток подряд. Подожди пару минут и открой ссылку снова.":
        "Too many attempts in a row. Wait a couple of minutes and open the link again.",
    "Человек записан на форум в другом городе — после одобрения он переедет в {city}.":
        "This person signed up for the forum in another city — once approved, they will be "
        "moved to {city}.",
    "Заявку {name} отклонил менеджер. Пропустить вопреки отказу и отметить вход? "
    "Отказ будет отменён, решение запишется на вас и попадёт в журнал.": (
        "A manager rejected {name}'s application. Let them in despite the rejection and check "
        "them in? The rejection will be cancelled, and the decision will be recorded under your "
        "name in the log."
    ),
    "Регистрация на месте выключена для этого города — включает менеджер в «🎪 Форум: функции».":
        "On-site registration is off for this city — a manager can turn it on in "
        "“🎪 Forum: features”.",
    # Трек «региональные форумы → Москва» (25.09, ревью 🔴2 forum-regions-msk): предложение
    # переноса неявившегося делегата регионального форума на форум города назначения (group
    # "reg", regional_noshow_offer_text). `{target_city}` заменил хардкод «Москва».
    "Не получилось на форум в {city}? Приезжай на Юлид: {target_city}{dates}":
        "Couldn't make it to the forum in {city}? Come to YouLead: {target_city}{dates}",
    # Phase 33 (delegate-card admin actions): уведомление делегату после перевода в другой
    # город (group "reg", city_move_delegate_notice_text). `{city}` переживает перевод
    # сентинелами глоссария, тот же приём, что у остальных плейсхолдеров этого словаря.
    "Твоя заявка перенесена в другой город: {city}. Программа, чат и все материалы — "
    "теперь по новому городу.": (
        "Your application has been moved to another city: {city}. The program, chat, and "
        "all materials are now for the new city."
    ),
}

# ── Голые ключи реестра группы `game` — амбассадорские волны (291bdd2): хаб амбассадора,
# карточка волны, рейтинг волны, штраф за просроченную сдачу, итоги волны. «Балл» здесь —
# тот же счёт, что уже переведён «points» в `EVENT_TEXTS_260917` («получения Юлид баллов»),
# держим единый термин в обоих словарях. ───────────────────────────────────────────────────
_AMBASSADOR_WAVE_TEXTS_EN = {
    "Задания амбассадоров": "Ambassador tasks",
    "Перестать быть амбассадором": "Stop being an ambassador",
    "Точно хочешь перестать быть амбассадором?\n\nРассылки волны прекратятся, из рейтинга текущей волны ты исчезнешь, а уже набранные баллы останутся.": "Are you sure you want to stop being an ambassador?\n\nWave broadcasts will stop and you'll disappear from the current wave's ranking, but the points you've already earned will stay with you.",
    "Готово — ты больше не амбассадор. Спасибо за работу!": "Done — you're no longer an ambassador. Thanks for your work!",
    "Делаю контент": "Creating content",
    "Зову людей": "Inviting people",
    "Пока не выбрал": "Not chosen yet",
    "Как тебе больше нравится помогать как амбассадору?": "How do you prefer to help as an ambassador?",
    "без срока": "no deadline",
    "После дедлайна ({deadline}) — {penalized} баллов вместо {coins}. Штраф фиксированный, не зависит от того, насколько поздно.": "After the deadline ({deadline}) — {penalized} points instead of {coins}. The penalty is fixed, no matter how late.",
    "Скоро дедлайн ({deadline}), а у тебя ещё не сдано: «{task}» ({coins} баллов). Успей сдать до дедлайна, чтобы получить баллы.": "The deadline ({deadline}) is coming up, and you still haven't submitted: «{task}» ({coins} points). Submit before the deadline to get your points.",
    "Сейчас нет активной волны амбассадоров — рейтинг появится, когда волна начнётся.": "There's no active ambassador wave right now — the ranking will appear once a wave starts.",
    "🏅 <b>Рейтинг волны</b>": "🏅 <b>Wave ranking</b>",
    "Ты {rank}-й из {total}. До призового места ({place}-е) — {gap} баллов.": "You're #{rank} out of {total}. {gap} points to the prize place ({place}).",
    "Волна {wave} завершена. Призёры: {winners} — поздравляем!\nТвоё место — {place}-е из {total}, {points} баллов за волну.": "Wave {wave} is over. Winners: {winners} — congratulations!\nYour place — {place} out of {total}, {points} points for the wave.",
    "Твой приз найдёт тебя отдельно — считай это счастливым билетом 🎟": "Your prize will find you separately — consider it a lucky ticket 🎟",
    "Ты в тройке призёров волны {wave} — {place}-е место, {points} баллов!\n{prize}\nСпасибо за работу в этой волне.": "You're in the top three of wave {wave} — {place} place, {points} points!\n{prize}\nThanks for your work in this wave.",
    "Открыть задания": "Open tasks",
    "Волна {wave} началась и продлится до {ends}.\n{intro}\n\nЗадания волны:\n{tasks}\n\nСдать можно и позже дедлайна — баллов будет меньше.": "Wave {wave} has started and runs until {ends}.\n{intro}\n\nWave tasks:\n{tasks}\n\nYou can still submit after the deadline — you'll just get fewer points.",
    # Phase 32 (32-06, D-36): третья подпись источника в истории монет, рядом с manual/task.
    "за приглашённого": "for a referral",
    # Литералы делегатского хаба амбассадора (`lit:user_actions.*`): кнопка рейтинга волны,
    # отказ не-участнику, подтверждение выхода из амбассадоров.
    "🏅 Рейтинг волны": "🏅 Wave ranking",
    "Рейтинг волны виден только участникам текущей волны амбассадоров.": "The wave ranking is only visible to participants of the current ambassador wave.",
    "Да, выйти": "Yes, leave",
    # Ревью фазы 32 (WR-11): «Ты в тройке призёров» противоречило настраиваемому числу
    # призовых мест (при `prize_places = 5` пятое место тоже получало «тройку») — новый
    # дефолт корректен для любого числа мест, старая строка выше оставлена как исторический
    # ключ ручного перевода (могла остаться в БД у события, которое уже переопределило текст).
    "Ты среди призёров волны {wave} — {place}-е место, {points} баллов!\n{prize}\nСпасибо за работу в этой волне.": "You're among the winners of wave {wave} — {place} place, {points} points!\n{prize}\nThanks for your work in this wave.",
    # 32-FIX-common-2 (хвост IN-09б): экран рейтинга closing-волны без объявленных итогов.
    "Волна закончилась — итоги готовятся.": "The wave has ended — the results are being prepared.",
    # Ступени амбассадоров СкиллАп (квалифицированная амбассадорка): уведомления о ступенях.
    "Первый человек по твоей ссылке прошёл отбор. Теперь ты официально амбассадор СкиллАп, сертификат выдадим после форума. До гарантированного разбора резюме осталось {left}.": "The first person you invited has passed selection. You're now an official SkillUp ambassador — we'll give you a certificate after the forum. {left} more to go until your guaranteed resume review.",
    "Трое твоих прошли отбор. Слот на разбор резюме с рекрутером за тобой, время пришлём ближе к форуму. Следующая ступень на 7: закрытый нетворкинг со спикерами и партнёрами.": "Three of your invitees have passed selection. A resume review slot with a recruiter is yours — we'll send the time closer to the forum. Next level at 7: a closed networking session with speakers and partners.",
    "Трое твоих прошли отбор, спасибо. Слоты на разбор резюме для амбассадоров уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется. Нетворкинг на 7 в силе.": "Three of your invitees have passed selection — thank you! The resume review slots for ambassadors are already taken, so you're on the waiting list and will be the first to get a spot if someone drops out. Networking at 7 still stands.",
    "Семеро по твоей ссылке прошли отбор. Зовём тебя на закрытый нетворкинг со спикерами и партнёрами 21 ноября, детали напишем в личку.": "Seven people you invited have passed selection. We're inviting you to a closed networking session with speakers and partners on November 21 — we'll send the details in a private message.",
    # Обобщённые ступени 1–5: нейтральные дефолты реестра.
    "Твой первый приглашённый прошёл отбор — это первая ступень! До следующей осталось {left}.": "Your first invitee has passed selection — that's the first level! {left} more to go until the next one.",
    "Ты дошёл до второй ступени — твои приглашённые прошли отбор. Награда за тобой, детали пришлём в личку.": "You've reached the second level — your invitees have passed selection. The reward is yours, we'll send the details in a private message.",
    "Ты дошёл до второй ступени, спасибо. Награды этой ступени уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется.": "You've reached the second level, thank you. The rewards for this level have already been taken, so you're on the waiting list and will be the first to get a spot if someone drops out.",
    "Ты дошёл до третьей ступени — спасибо, что приводишь людей на форум! Детали награды напишем в личку.": "You've reached the third level — thank you for bringing people to the forum! We'll send the reward details in a private message.",
    "До ступени 2: {n}": "Until level 2: {n}",
    "До ступени 3: {n}": "Until level 3: {n}",
    "Ты дошёл до ступени 4 — спасибо, что приводишь людей на форум! Детали награды напишем в личку.": "You've reached level 4 — thank you for bringing people to the forum! We'll send the reward details in a private message.",
    "Ты дошёл до ступени 5 — спасибо, что приводишь людей на форум! Детали награды напишем в личку.": "You've reached level 5 — thank you for bringing people to the forum! We'll send the reward details in a private message.",
    "До ступени 4: {n}": "Until level 4: {n}",
    "До ступени 5: {n}": "Until level 5: {n}",
    "Ты дошёл до ступени 1, спасибо. Награды этой ступени уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется.": "You've reached level 1, thank you. The rewards for this level have already been taken, so you're on the waiting list and will be the first to get a spot if someone drops out.",
    "Ты дошёл до ступени 3, спасибо. Награды этой ступени уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется.": "You've reached level 3, thank you. The rewards for this level have already been taken, so you're on the waiting list and will be the first to get a spot if someone drops out.",
    "Ты дошёл до ступени 4, спасибо. Награды этой ступени уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется.": "You've reached level 4, thank you. The rewards for this level have already been taken, so you're on the waiting list and will be the first to get a spot if someone drops out.",
    "Ты дошёл до ступени 5, спасибо. Награды этой ступени уже разобрали, ты в листе ожидания и первым получишь место, если кто-то откажется.": "You've reached level 5, thank you. The rewards for this level have already been taken, so you're on the waiting list and will be the first to get a spot if someone drops out.",
    # Экраны амбассадора: прогресс цифрами и скрытие имён приглашённых.
    "По твоей ссылке: {total}. Прошли отбор: {qualified}. {next_step}": "Joined via your link: {total}. Passed selection: {qualified}. {next_step}",
    "До разбора резюме: {n}": "Until the resume review: {n}",
    "До нетворкинга: {n}": "Until networking: {n}",
    "Все ступени твои": "All levels are yours",
    "Всего по твоей ссылке: {total}\nНа рассмотрении: {pending}\nПрошли отбор: {qualified}": "Total via your link: {total}\nUnder review: {pending}\nPassed selection: {qualified}",
    "Приглашённый №{n}": "Invitee #{n}",
    "Снят с зачёта приглашённый: {name}": "Invitee removed from your count: {name}",
    "Возвращён в зачёт приглашённый: {name}": "Invitee restored to your count: {name}",
    "За приглашённых до вступления: {count}": "For invitees approved before you joined: {count}",
    # Вход в команду амбассадоров: режим отбора, лимит мест, взяли / вывели / отказ.
    "Заявка в команду амбассадоров принята! Менеджер свяжется с тобой. А ссылка уже твоя — можешь делиться ею прямо сейчас.": "Your request to join the ambassador team has been received! A manager will get in touch with you. Your invite link is already yours — you can start sharing it right now.",
    "🎁 Ты в команде амбассадоров — пакет амбассадора за тобой.": "🎁 You're on the ambassador team — the ambassador pack is yours.",
    "Ты в команде амбассадоров. Пакет амбассадора закрепляется за теми, чья заявка на форум одобрена, пока есть места.": "You're on the ambassador team. The ambassador pack goes to those whose forum application is approved, while spots last.",
    "💰 Баллы за приглашённых: {points}": "💰 Points for invitees: {points}",
    "🌊 {wave}: ты на {place} месте из {total}": "🌊 {wave}: you're in place {place} of {total}",
    "🕓 Твоя заявка в команду амбассадоров рассматривается.": "🕓 Your request to join the ambassador team is under review.",
    "Места в команде амбассадоров уже заняты, но мы всё равно тебя очень ждём на форуме — следи за новостями!": "All spots on the ambassador team are already taken, but we're still really looking forward to seeing you at the forum — stay tuned for news!",
    "🎉 Ты в команде амбассадоров! Вот твоя ссылка для приглашений: {link}\nВсё про задания и прогресс — в разделе «Моя ссылка».": "🎉 You're on the ambassador team! Here's your invite link: {link}\nEverything about tasks and progress is in the “My link” section.",
    "Ты больше не в команде амбассадоров этого форума. Спасибо за помощь! Твоя ссылка для приглашений продолжает работать, а баллы остаются за тобой.": "You're no longer on this forum's ambassador team. Thank you for your help! Your invite link keeps working, and your points stay with you.",
    "Спасибо, что откликнулся! Места в команде амбассадоров уже заняты, но мы всё равно тебя очень ждём на форуме — следи за его развитием.": "Thank you for responding! All spots on the ambassador team are already taken, but we're still really looking forward to seeing you at the forum — stay tuned for updates.",
}

# Нейтральные дефолты 09.10: «форум»/«Юлид» в дефолтах реестра уезжали делегату конференции и
# СкиллАпа — тексты переписаны на «мероприятие»/«площадку» и подстановку `{event}` (название из
# «🎪 Название мероприятия», `services.text_fill.event_label`). Прежние RU-ключи выше оставлены:
# у менеджера мог сохраниться старый дефолт дословно — перевод для него не должен пропасть.
_NEUTRAL_DEFAULTS_EN: dict[str, str] = {
    "Программа ещё не загружена.": "The program hasn't been uploaded yet.",
    "Финал — очно на мероприятии, командами по 3–4 человека. Опыт не нужен, важно желание пробовать.":
        "The final is in person at the event, in teams of 3–4. No experience needed — just a willingness to try.",
    "Пришёл(ла) к нам впервые": "Coming for the first time",
    "Мероприятие": "Event",
    "Собирай монеты до старта": "Collect coins before the start",
    "Отправь эту ссылку друзьям, чтобы пригласить их с собой!\n\n{link}":
        "Send this link to your friends to bring them along!\n\n{link}",
    "Ты дошёл до третьей ступени — спасибо, что приводишь к нам людей! Детали награды напишем в личку.":
        "You've reached the third level — thank you for bringing people to us! We'll send the reward details in a private message.",
    "Ты дошёл до ступени 4 — спасибо, что приводишь к нам людей! Детали награды напишем в личку.":
        "You've reached level 4 — thank you for bringing people to us! We'll send the reward details in a private message.",
    "Ты дошёл до ступени 5 — спасибо, что приводишь к нам людей! Детали награды напишем в личку.":
        "You've reached level 5 — thank you for bringing people to us! We'll send the reward details in a private message.",
    "Места в команде амбассадоров уже заняты, но мы всё равно тебя очень ждём — следи за новостями!":
        "All spots on the ambassador team are already taken, but we're still really looking forward to seeing you — stay tuned for news!",
    "Ты в команде амбассадоров. Пакет амбассадора закрепляется за теми, чья заявка одобрена, пока есть места.":
        "You're on the ambassador team. The ambassador pack goes to those whose application is approved, while spots last.",
    "Ты больше не в команде амбассадоров. Спасибо за помощь! Твоя ссылка для приглашений продолжает работать, а баллы остаются за тобой.":
        "You're no longer on the ambassador team. Thank you for your help! Your invite link keeps working, and your points stay with you.",
    "Спасибо, что откликнулся! Места в команде амбассадоров уже заняты, но мы всё равно тебя очень ждём — следи за новостями.":
        "Thank you for responding! All spots on the ambassador team are already taken, but we're still really looking forward to seeing you — stay tuned for news.",
    "Привет! Ты в списке делегации {university} на {event} 🎉\n\nАнкету заполнять не нужно — заявка уже одобрена. Ниже меню участника: там будет QR на вход и все новости мероприятия.":
        "Hi! You're on the {university} delegation list for {event} 🎉\n\nNo need to fill in the questionnaire — your application is already approved. Below is the participant menu: your entry QR and all event news will be there.",
    "Задания и монеты для делегаций вузов здесь выключены. Всё остальное — программа, QR на вход, новости — работает как обычно.":
        "Tasks and coins for university delegations are switched off here. Everything else — the programme, the entry QR, the news — works as usual.",
    "🎟 Твой QR для отметки на входе.\n\nСохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото из чата открывается и без интернета.": (
        "🎟 Your QR code for check-in at the entrance.\n\n"
        "Save it in advance (for example, take a screenshot) — the venue may have no network, "
        "but a photo from the chat opens even without the internet."
    ),
    "Твой QR для отметки на входе.\n\nСохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото из чата открывается и без интернета.": (
        "Your QR code for check-in at the entrance.\n\n"
        "Save it in advance (for example, take a screenshot) — the venue may have no network, "
        "but a photo from the chat opens even without the internet."
    ),
    "Учебный QR — не пропуск на вход, ничего не записывается.":
        "Training QR — not an entry pass, nothing is recorded.",
    "Красная плашка «QR другого мероприятия». Попросите открыть «Мой QR» в боте этого мероприятия.":
        "Red banner «QR of another event». Ask them to open «My QR» in this event's bot.",
    "Завтра встречаемся! Вот твой QR для входа. Открой его сейчас и сделай скриншот — на площадке может не быть сети.": (
        "See you tomorrow! Here's your QR code for entry. Open it now and take a "
        "screenshot — the venue may have no network."
    ),
    "Сегодня встречаемся! Вот твой QR для входа. Открой его сейчас и сделай скриншот — на площадке может не быть сети.": (
        "See you today! Here's your QR code for entry. Open it now and take a "
        "screenshot — the venue may have no network."
    ),
    "Мы тебя не видим на площадке 👀 Всё в порядке?": "We don't see you at the venue 👀 Is everything okay?",
    "Отлично, до встречи!": "Great, see you there!",
    "Ты отмечен на входе в {time} ✅ Добро пожаловать на {event}!": "You're checked in at {time} ✅ Welcome to {event}!",
    "Мы не видели тебя на площадке. Расскажешь, что помешало прийти?":
        "We didn't see you at the venue. Would you tell us what got in the way?",
    "Не получилось на форум в {city}? Приезжай на {event}: {target_city}{dates}":
        "Couldn't make it to the forum in {city}? Come to {event}: {target_city}{dates}",
    "Привет! Это короткая регистрация прямо на площадке — всего 3 вопроса: имя, телефон и вуз.\n\n"
    "Нажимая кнопку ниже, ты соглашаешься на обработку этих данных для участия в мероприятии.": (
        "Hi! This is a quick on-site registration — just 3 questions: your name, "
        "phone number and university.\n\n"
        "By tapping the button below, you agree to the processing of this data for participation "
        "in the event."
    ),
    "Готово, {name}! Подойди к волонтёру на стойке регистрации и назови свою фамилию — "
    "он пропустит тебя внутрь.": (
        "All set, {name}! Go to the volunteer at the registration desk and say your last name — "
        "they'll let you in."
    ),
    "Добро пожаловать! Тебя зарегистрировали на месте. Ниже — твой QR: он "
    "понадобится на сессиях, сохрани его.": (
        "Welcome! You've been registered on site. Below is your QR code — you'll "
        "need it at the sessions, so save it."
    ),
    "Человек записан в другом городе — после одобрения он переедет в {city}.":
        "This person signed up in another city — once approved, they will be moved to {city}.",
    "🎉 {name}, {event} в цифрах — вот твой итог! Поделись с друзьями 🧡💙":
        "🎉 {name}, {event} in numbers — here's your recap! Share it with friends 🧡💙",
}

# Валюта в дефолтах — «баллы» / «points» (10.10, выбор DXP): одно слово на все тексты делегату.
# Прежние RU-ключи с «монетами» выше оставлены — у менеджера мог сохраниться старый дефолт дословно.
_POINTS_261010_EN = {
    "📜 <b>История баллов</b>": "📜 <b>Points history</b>",
    "🪙 Баланс: {balance} баллов\nМесто в общем рейтинге: {rank} из {total}": "🪙 Balance: {balance} points\nOverall leaderboard place: {rank} of {total}",
    "🪙 Баланс изменён: {delta} баллов.\nПричина: {reason}\nТекущий баланс: {balance}": "🪙 Balance changed: {delta} points.\nReason: {reason}\nCurrent balance: {balance}",
    "🏆 <b>Рейтинг по баллам</b>": "🏆 <b>Points leaderboard</b>",
    "Собирай баллы до старта": "Collect points before the start",
    "Задания и баллы для делегаций вузов здесь выключены. Всё остальное — программа, QR на вход, новости — работает как обычно.":
        "Tasks and points for university delegations are switched off here. Everything else — the programme, the entry QR, the news — works as usual.",
}

FORM_DEFAULT_EN: dict[str, str] = {
    **_PROMPT_DEFAULTS_EN, **_STEP_HELP_EN, **_REG_LABELS_EN, **_SUMMARY_LABELS_EN,
    **_OPTIONS_EN, **_CODE_LITERALS_EN, **_ENGINE_DYNAMIC_EN, **_REGISTRY_TEXTS_EN,
    **_AMBASSADOR_WAVE_TEXTS_EN, **_NEUTRAL_DEFAULTS_EN, **_POINTS_261010_EN,
    # Подписи кнопок главного меню (группа menu_labels): дефолты переведены руками в MENU_EN —
    # тот же перевод, второй копии не заводим.
    **MENU_EN,
    "🌐 Язык / Language": "🌐 Язык / Language",  # кнопка языка двуязычна сама по себе
    # --- запись на сессии и тест по компетенциям ---
    "📅 Выбрать сессии": "📅 Choose sessions",
    "Тест сейчас недоступен.": "The quiz is currently unavailable.",
    "🧭 Тест": "🧭 Quiz",
    "📊 Мой результат": "📊 My result",
    "пока без уровня": "no level yet",
    "Ты ещё не проходил тест.": "You haven't taken the quiz yet.",
    "Тест откроется, когда твою заявку одобрят.": "The quiz will open once your application is approved.",
    "Вопрос {n} из {total}": "Question {n} of {total}",
    "Мы обновили тест — начнём сначала.": "We've updated the quiz — let's start over.",
    "Твой результат по компетенциям:": "Your results by competency:",
    "{competency} — {level}": "{competency} — {level}",
    "Продолжаем с вопроса {n} из {total}.": "Continuing from question {n} of {total}.",
    "🔁 Пройти заново": "🔁 Retake",
    "▶️ Начать тест": "▶️ Start the quiz",
    "✏️ Изменить": "✏️ Change",
    "✖️ Снять выбор": "✖️ Clear selection",
    "🔒 запись закрыта": "🔒 enrollment closed",
    "для всех": "for everyone",
    "✅ Подтвердить": "✅ Confirm",
    "Готово, расписание подтверждено! Поменять выбор можно до {deadline}.": "Done, your schedule is confirmed! You can change your choice until {deadline}.",
    "Запись на сессии сейчас недоступна.": "Session enrollment is currently unavailable.",
    "Запись на «{title}» закрыта — выбери другую сессию.": "Enrollment for \"{title}\" is closed — please pick another session.",
    "Запись на сессии закрылась {deadline}. Расписание можно посмотреть, а менять уже нельзя.": "Session enrollment closed on {deadline}. You can view your schedule, but it can no longer be changed.",
    "На «{title}» мест больше нет — выбери другую сессию.": "\"{title}\" has no spots left — please pick another session.",
    "🔒 мест нет": "🔒 no spots left",
    "Выбери трек — его сессии будут отмечены первыми. Можно смешать треки и выбрать сессии из разных.": "Pick a track — its sessions will be shown first. You can also mix tracks and choose sessions from different ones.",
    "📅 Запись на сессии": "📅 Session enrollment",
    "🔀 Смешать треки": "🔀 Mix tracks",
    "Дальше ➡️": "Next ➡️",
    "Сессий для записи пока нет — загляни позже.": "There are no sessions to enroll in yet — check back later.",
    "Запись на сессии откроется, когда твою заявку одобрят.": "Session enrollment will open once your application is approved.",
    "Оставить как было": "Keep as it was",
    "Ты уже записан на «{old}» в это время. Поменять на «{new}»?": "You're already enrolled in \"{old}\" at this time. Switch to \"{new}\"?",
    "Да, поменять": "Yes, switch",
    "Записать на эту": "Enroll in this one",
    "Записали на «{title}»": "Enrolled in \"{title}\"",
    "Не получилось записать — попробуйте ещё раз": "Couldn't enroll — please try again",
    "Не записан на сессию в это время": "Not enrolled in a session at this time",
    "Записан на «{title}»": "Enrolled in \"{title}\"",
    "Перезаписать на эту": "Re-enroll in this one",
    "Ты пока не выбрал ни одной сессии.": "You haven't chosen any sessions yet.",
    "🗓 Моё расписание": "🗓 My schedule",
    "{day}, {time}. Выбери одну сессию:": "{day}, {time}. Pick one session:",
    "Эта сессия больше недоступна — обнови список.": "This session is no longer available — please refresh the list.",
}


# ── Тексты события «Юлид 26/2» на 17.09 (снято с прода и стенда, см. докстринг модуля) ──────
EVENT_TEXTS_260917: dict[str, str] = {
    "30-31 октября": "October 30–31",
    "3 октября": "October 3",
    "Введи ФИО": "Enter your full name",
    "Введи номер телефона в формате +7XXXXXXXXXX": "Enter your phone number in the format +7XXXXXXXXXX",
    "Выбери свой город": "Choose your city",
    "Откуда ты узнал(-а) о форуме?": "How did you hear about the forum?",
    "Введи название твоего ВУЗа": "Enter the name of your university",
    "Уточни номер курса": "Tell us your year of study",
    "Выбери направление обучения": "Choose your field of study",
    "Какова твоя цель участия? (можно выбрать несколько)": "What's your goal for attending? (you can select several)",
    "Какие темы и спикеров ты хочешь увидеть на форуме?": "What topics and speakers would you like to see at the forum?",
    "Сколько тебе полных лет?": "How old are you (full years)?",
    "Какие форматы ты хочешь увидеть на форуме? (можно выбрать несколько)": "Which formats would you like to see at the forum? (you can select several)",
    "Обучаешься на данный момент?": "Are you currently studying?",
    "Прикрепи своё резюме файлом в формате PDF или DOCX.\n\nЕсли резюме пока нет – напиши кратко о своем опыте в свободной форме:\n\n1. Где работаешь сейчас?\n2. В каких проектах, стажировках, волонтёрстве или студенческих активностях участвовал(-а)?\n3. Какие задачи и цели стояли перед тобой, каких результатов удалось достичь?": (
        "Attach your CV as a file in PDF or DOCX format.\n\n"
        "If you don't have a CV yet – write briefly about your experience in free form:\n\n"
        "1. Where do you currently work?\n"
        "2. What projects, internships, volunteering, or student activities have you taken part in?\n"
        "3. What tasks and goals did you have, and what results did you achieve?"
    ),
    "Поздравляем, твоя заявка принята! \n\nМы рассмотрим ее в течение 2-3 дней и напишем сюда. Следи за обновлениями, впереди много интересного 🔥\n\nЕсли у тебя возникнут вопросы – не стесняйся задавать их нам! \n\nС уважением,  \nКоманда YouLead’26 🧡💙": (
        "Congratulations, your application has been accepted! \n\n"
        "We'll review it within 2-3 days and message you here. Stay tuned for updates, "
        "there's a lot of exciting stuff ahead 🔥\n\n"
        "If you have any questions – don't hesitate to ask us! \n\n"
        "Best regards,  \n"
        "The YouLead’26 Team 🧡💙"
    ),
    # Тюмень — новый город сезона YL 26/2, добавленный в `city_options` поверх дефолтного
    # списка (см. `_OPTIONS_EN` выше — остальные пять городов из настройки уже там).
    "Тюмень": "Tyumen",
    "<b>Поздравляем, ты прошёл(-ла) отбор на Юлид | YouLead 2026! 🎊\n</b>\nМы внимательно посмотрели твою заявку и <b>рады пригласить тебя на форум</b>, который пройдёт <b>30-31 октября в Москве</b> <b>\n</b>\n<i>Будь внимателен - учитывается город, который изначально был указан при регистрации, мы ждем тебя именно там! </i>\n\n<b>Что дальше:</b>\n<blockquote>– следи за обновлениями в этом боте;\n– здесь будет появляться важная информация по форуму;\n– ближе к событию ты получишь детали по программе, трекам и возможностям от партнёров;\n– также здесь появятся задания, за которые можно будет получать YouLead coins</blockquote>\n\nЕсли ты прикрепил(-а) резюме, мы сможем использовать его для карьерных возможностей от партнеров форума.\n<b>\nДо встречи на Юлид | YouLead 2026 🧡💙</b>": (
        "<b>Congratulations, you've passed the selection for YouLead 2026! 🎊\n</b>\n"
        "We've carefully reviewed your application and <b>are happy to invite you to the forum</b>, "
        "which will take place <b>on October 30–31 in Moscow</b> <b>\n</b>\n"
        "<i>Please note – the city you originally registered with is the one that counts, "
        "we're waiting for you there! </i>\n\n"
        "<b>What's next:</b>\n"
        "<blockquote>– watch for updates in this bot;\n"
        "– important forum information will appear here;\n"
        "– closer to the event you'll get details on the program, tracks, and partner opportunities;\n"
        "– tasks will also appear here that let you earn YouLead coins</blockquote>\n\n"
        "If you've attached a CV, we'll be able to use it for career opportunities from the forum's partners.\n"
        "<b>\nSee you at YouLead 2026 🧡💙</b>"
    ),
    "<b>Поздравляем, ты прошёл(-ла) отбор на Юлид | YouLead 2026! 🎊\n</b>\nМы внимательно посмотрели твою заявку и <b>рады пригласить тебя на форум</b>, который пройдёт <b>3 октября в Тюмени</b>  <b>\n</b>\n<i>Будь внимателен - учитывается город, который изначально был указан при регистрации, мы ждем тебя именно там! </i>\n\n<b>Что дальше:</b>\n<blockquote>– следи за обновлениями в этом боте;\n– здесь будет появляться важная информация по форуму;\n– ближе к событию ты получишь детали по программе, трекам и возможностям от партнёров;\n– также здесь появятся задания, за которые можно будет получать YouLead coins</blockquote>\n\nЕсли ты прикрепил(-а) резюме, мы сможем использовать его для карьерных возможностей от партнеров форума.\n<b>\nДо встречи на Юлид | YouLead 2026 🧡💙</b>": (
        "<b>Congratulations, you've passed the selection for YouLead 2026! 🎊\n</b>\n"
        "We've carefully reviewed your application and <b>are happy to invite you to the forum</b>, "
        "which will take place <b>on October 3 in Tyumen</b>  <b>\n</b>\n"
        "<i>Please note – the city you originally registered with is the one that counts, "
        "we're waiting for you there! </i>\n\n"
        "<b>What's next:</b>\n"
        "<blockquote>– watch for updates in this bot;\n"
        "– important forum information will appear here;\n"
        "– closer to the event you'll get details on the program, tracks, and partner opportunities;\n"
        "– tasks will also appear here that let you earn YouLead coins</blockquote>\n\n"
        "If you've attached a CV, we'll be able to use it for career opportunities from the forum's partners.\n"
        "<b>\nSee you at YouLead 2026 🧡💙</b>"
    ),
    "Поздравляем с прохождением отбора на Юлид | YouLead 2026’2! \n<a href=\"https://t.me/+3RM9f52FhqBjMWYy\"><b>Скорее присоединяйся к нашему чату участников </b></a><b>- </b><b>https://t.me/+J_C7QQt7Et0zOWZi</b>\n\nОбрати внимание, что <b><u>проживание и проезд форум не предоставляет</u></b>, но мы делаем документы твоей направляющей стороне о том, что ты являешься участником нашего форума для компенсации с их стороны при необходимости 🤍\n\nЧто дальше:\n<blockquote>– следи за новостями в этом боте;\n– скоро получишь детали программы и возможности от партнёров;\n– будут задания для получения Юлид баллов.</blockquote>\n\nДо встречи на Юлид | YouLead 2026’2!🫂": (
        "Congratulations on passing the selection for YouLead 2026’2! \n"
        "<a href=\"https://t.me/+3RM9f52FhqBjMWYy\"><b>Hurry and join our participants' chat </b></a>"
        "<b>– </b><b>https://t.me/+J_C7QQt7Et0zOWZi</b>\n\n"
        "Please note that <b><u>the forum doesn't cover accommodation or travel</u></b>, but we prepare "
        "documents for your sending organization confirming that you're a participant of our forum, "
        "for compensation on their side if needed 🤍\n\n"
        "What's next:\n"
        "<blockquote>– watch for news in this bot;\n"
        "– you'll soon get program details and partner opportunities;\n"
        "– there will be tasks for earning YouLead points.</blockquote>\n\n"
        "See you at YouLead 2026’2!🫂"
    ),
    "<b>Поздравляем, ты прошёл(-ла) отбор на Юлид | YouLead 2026!</b> 🎊\n\nМы внимательно посмотрели твою заявку и рады пригласить тебя на форум, который пройдёт <b>3 октября в Санкт-Петербурге</b>\n\nСкорее добавляйся в <a href=\"https://t.me/+T80F0YywSnw5OGRi\">чат делегатов</a>\n\nБудь внимателен - учитывается город, который изначально был указан при регистрации, мы ждем тебя именно там! \n\n<i>Что дальше:</i>\n– следи за обновлениями в этом боте;\n– здесь будет появляться важная информация по форуму;\n– ближе к событию ты получишь детали по программе, трекам и возможностям от партнёров;\n– также здесь появятся задания, за которые можно будет получать <i>YouLead coins</i>\n\nЕсли ты прикрепил(-а) резюме, мы сможем использовать его для карьерных возможностей от партнеров форума.\n\n<b>До встречи на Юлид | YouLead 2026 </b>🧡💙": (
        "<b>Congratulations, you've passed the selection for YouLead 2026!</b> 🎊\n\n"
        "We've carefully reviewed your application and are happy to invite you to the forum, "
        "which will take place <b>on October 3 in Saint Petersburg</b>\n\n"
        "Hurry and join the <a href=\"https://t.me/+T80F0YywSnw5OGRi\">delegates' chat</a>\n\n"
        "Please note – the city you originally registered with is the one that counts, "
        "we're waiting for you there! \n\n"
        "<i>What's next:</i>\n"
        "– watch for updates in this bot;\n"
        "– important forum information will appear here;\n"
        "– closer to the event you'll get details on the program, tracks, and partner opportunities;\n"
        "– tasks will also appear here that let you earn <i>YouLead coins</i>\n\n"
        "If you've attached a CV, we'll be able to use it for career opportunities from the forum's partners.\n\n"
        "<b>See you at YouLead 2026 </b>🧡💙"
    ),
    "Привет! Я бот <b>Юлид</b> | <b>YouLead 2026</b> – твой помощник на пути к форуму 🙌\n\nФорум пройдёт сразу в нескольких городах России:\n<b>30-31 октября</b> в Москве;\n<b>3 октября</b> в Санкт-Петербурге \n<b>3 октября</b> в Тюмени \n\nЗдесь ты сможешь:\n<blockquote>– подать заявку на участие;\n– отслеживать статус заявки;\n– получать новости форума;\n– выполнять задания и копить YouLead coins;\n– получать возможности от партнёров форума.</blockquote>\n\nЗаявка займёт 5-7 минут. По твоим ответам мы подберём треки, нетворкинг-партнера и персональные рекомендации.\n\n📑 <b>Прежде чем начать:</b> \n• <a href=\"https://drive.google.com/file/d/1RDqdBvmXZJVoYoP35Q2VJkaAXaVQrIyF/view?clckid=089145db\">Политика конфиденциальности</a>\n• <a href=\"https://drive.google.com/file/d/1BUpnBL3M8xNJfxvOw9bTIDVb71dWOEml/view?clckid=5e43940e\">Согласие на обработку ПД</a>": (
        "Hi! I'm the <b>YouLead 2026</b> bot – your helper on the way to the forum 🙌\n\n"
        "The forum will take place in several cities across Russia at once:\n"
        "<b>October 30–31</b> in Moscow;\n"
        "<b>October 3</b> in Saint Petersburg \n"
        "<b>October 3</b> in Tyumen \n\n"
        "Here you can:\n"
        "<blockquote>– apply to participate;\n"
        "– track your application status;\n"
        "– get forum news;\n"
        "– complete tasks and collect YouLead coins;\n"
        "– get opportunities from the forum's partners.</blockquote>\n\n"
        "The application takes 5-7 minutes. Based on your answers, we'll pick tracks, a networking "
        "partner, and personal recommendations for you.\n\n"
        "📑 <b>Before you start:</b> \n"
        "• <a href=\"https://drive.google.com/file/d/1RDqdBvmXZJVoYoP35Q2VJkaAXaVQrIyF/view?clckid=089145db\">"
        "Privacy Policy</a>\n"
        "• <a href=\"https://drive.google.com/file/d/1BUpnBL3M8xNJfxvOw9bTIDVb71dWOEml/view?clckid=5e43940e\">"
        "Personal Data Processing Consent</a>"
    ),
}


async def seed(lang: str = "en") -> dict:
    """Пишет `FORM_DEFAULT_EN` и `EVENT_TEXTS_260917` в `translations` с `manual=1` —
    идемпотентно, зовётся на каждом старте бота рядом с `i18n_miniapp_manual.seed()`
    (`main.py`). Возвращает `{"applied": N, "skipped_manager_edit": M}` суммарно по обоим
    словарям — для лога старта."""
    # Не перетирает `manual=1` перевод с ЧУЖИМ `origin_key` (менеджер отредактировал строку
    # сам через экран правки, план 27-06) — правило внутри `seed_manual_translations`.
    default_result, event_result = await seed_manual_translations(
        lang, [(FORM_DEFAULT_EN, ORIGIN_DEFAULT), (EVENT_TEXTS_260917, ORIGIN_EVENT)], src_hash,
    )
    total = {
        "applied": default_result["applied"] + event_result["applied"],
        "skipped_manager_edit": default_result["skipped_manager_edit"] + event_result["skipped_manager_edit"],
    }
    if total["skipped_manager_edit"]:
        logger.info(
            "i18n_form_manual.seed: пропущено %d строк — уже отредактированы менеджером",
            total["skipped_manager_edit"],
        )
    return total
