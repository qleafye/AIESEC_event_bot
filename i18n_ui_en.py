"""Phase 27 (27-02, LANG-02/LANG-08 — ярус A) — рукописный английский служебных слов анкеты,
текстов ошибок валидации и подсказок «Пропустить»/«Другое». Корневой модуль-словарь, сосед
`reg_labels.py`/`reg_options.py` — литералы и никакой логики, ни одного импорта проекта.

Почему руками, а не машиной. Эти строки участвуют в жёстких сравнениях aiogram-фильтров
(`F.text == "Отмена"`, `F.text.in_({"Отмена", "/cancel"})`) и в проверках `reg_engine.py`
(`_MEMBERSHIP_STEPS`, `_SKIP_TEXT_ERRORS`, `_BESPOKE_CHOICE`). Машинный перевод недетерминирован
между версиями модели движка (argos-translate-lt) — после апдейта движка перевод той же русской
строки может выйти другим словом, и фильтр «Cancel»/«Skip» в коде расклеится молча (делегат
нажмёт кнопку, а бот её не узнает). Этих строк ~50, они не меняются годами — машинный перевод
здесь ничего не экономит, а риск создаёт постоянный.

Почему латиница здесь законна. `tests/test_ru_brand_wording_260824.py` сканирует
`SETTINGS_SCHEMA`, `REG_LABELS`, `reg_options`, `miniapp/static/js/**` и три человеческих дока
на латинские «AIESEC»/«YouLead» (правило владельца, закон РФ о рекламе). Этот модуль — ИСТОЧНИК
английских текстов фазы 27, а не текст ДЛЯ РУССКОГО делегата — он вне скана намеренно, и
ослаблять `_EXEMPT_RE`/`LATIN_OWNER_BRAND_SUBSTRINGS` сторожа ради него — неправильный ход
(это ломало бы саму защиту бренда для всех остальных случаев).

Ярус B (контент менеджера: тексты вопросов, варианты ответов, экраны анкеты) сюда не входит —
он живёт в `translations` (план 27-02, `database/db.py`) и переводится машиной фоново (план 27-03).
`services/i18n.py::tr()` смотрит сначала этот словарь (`UI_EN`), потом карту `translations`,
потом отдаёт русский как есть (fail-soft, D-04).

Литералы сверены копированием из `reg_engine.py` (`_CHOICE_EMPTY_ERROR`, `_CHOICE_OTHER_PROMPT`,
`_BESPOKE_CHOICE`, `_SKIP_TEXT_ERRORS`, `_MEMBERSHIP_STEPS`, `_GENERIC_FALLBACK_LABEL`,
`validate_date_range`), `handlers/reg_flow.py` (кнопки подтверждения/отмены/сохранения) и
`keyboards/builders.py` (`get_cancel_kb`/`get_confirm_kb`/`get_phone_kb`/`get_skip_kb`) на дату
плана (2026-09-06) — несовпадение хотя бы на один символ (кавычку-ёлочку, точку) означает, что
`services/i18n.py::tr()` не найдёт строку в этом словаре и молча пропустит её в машинный ярус
(fail-soft, не падение — но тихий пробел, который `tests/test_i18n_core_27.py` ловит там, где
может интроспекцией, а где не может — держит явным списком с пометкой «сверено на дату плана»).
"""

# Ключ — точный русский литерал из кода (aiogram-фильтр или текст ошибки reg_engine.py),
# значение — рукописный английский. Порядок — как встречается в reg_engine.py/reg_flow.py/
# keyboards/builders.py: сначала служебные слова, потом тексты ошибок валидации.
UI_EN: dict[str, str] = {
    # ── Служебные слова (aiogram-фильтры + инлайн-кнопки мастера регистрации) ──────────────
    "Готово": "Done",
    "Отмена": "Cancel",
    "Пропустить": "Skip",
    "Другое": "Other",
    "Всё верно": "Looks good",
    "Изменить": "Edit",
    "Да, отменить": "Yes, cancel",
    "Нет, продолжить": "No, continue",
    "Да": "Yes",
    "Нет": "No",
    "Сохранено": "Saved",
    "✅ Принято": "✅ Accepted",
    # attendance_format: значения УЖЕ латиницей в русской анкете — тождественная запись, чтобы
    # обратный индекс (EN_TO_RU, план 27-05) находил канонический русский вариант при вводе.
    "Offline": "Offline",
    "Online": "Online",
    # informal_day (_MEMBERSHIP_STEPS) — третий допустимый вариант ответа, не «Да»/«Нет».
    "Буду только в онлайне": "Online only",

    # ── _CHOICE_STEPS: общий текст ошибки/подсказки «Другое» на 10 choice-шагов ────────────
    "Выбери вариант на клавиатуре или напиши ответ.": "Choose an option on the keyboard or type your answer.",
    "Напиши свой вариант:": "Type your own answer:",

    # ── _BESPOKE_CHOICE: свой текст ошибки/подсказки на каждый из 5 шагов ──────────────────
    "Выбери город на клавиатуре или напиши свой.": "Choose a city on the keyboard or type your own.",
    "Напиши название своего города:": "Type the name of your city:",
    "Выбери один из вариантов или напиши свой.": "Choose one of the options or type your own.",
    "Выбери локальный комитет из списка или напиши свой.": "Choose a Local Committee from the list or type your own.",
    "Напиши название своего ЛК:": "Type the name of your LC:",
    "Выбери позицию из списка или напиши свою.": "Choose a position from the list or type your own.",
    "Напиши свою позицию:": "Type your position:",
    "Выбери ВУЗ из списка или напиши свой.": "Choose your university from the list or type your own.",
    "Напиши название своего ВУЗа:": "Type the name of your university:",

    # ── _SKIP_TEXT_ERRORS: общий текст на 10 из 11 шагов + свой у work_sphere ──────────────
    "Напиши или нажми «Пропустить».": "Type your answer or tap «Skip».",
    "Напиши сферу работы или нажми «Пропустить».": "Type your field of work or tap «Skip».",

    # ── _MEMBERSHIP_STEPS: жёсткая проверка допустимых литералов ───────────────────────────
    "Выбери «Да» или «Нет».": "Choose «Yes» or «No».",
    "Выбери один из вариантов.": "Choose one of the options.",
    "Выбери «Offline» или «Online».": "Choose «Offline» or «Online».",

    # ── _GENERIC_FALLBACK_LABEL: подпись вопроса без записи в REG_LABELS ───────────────────
    "Выбери вариант": "Choose an option",
    "Выбери варианты": "Choose options",
    "Дата": "Date",

    # ── Остальные тексты ошибок _validate_answer_core (по одному на явную ветку шага) ──────
    "Укажи ФИО полностью (минимум фамилию и имя).": "Enter your full name (at least last and first name).",
    "Укажи корректный возраст числом от 10 до 120.": "Enter a valid age as a number from 10 to 120.",
    "Укажи корректный email (например, name@example.com).": "Enter a valid email (e.g. name@example.com).",
    "Укажи номер телефона или нажми «Пропустить».": "Enter your phone number or tap «Skip».",
    "Укажи корректный номер телефона или нажми «Пропустить».": "Enter a valid phone number or tap «Skip».",
    "Укажи ник в ВК в формате @username (начинается с @, без пробелов).": "Enter your VK handle as @username (starts with @, no spaces).",
    "Напиши резюме текстом или прикрепи файл (PDF или DOCX).": "Type your resume as text or attach a file (PDF or DOCX).",
    "Выбери курс.": "Choose your year of study.",
    "Выбери «Да!» или «Пока нет».": "Choose «Yes!» or «Not yet».",
    "Выбери хотя бы один вариант.": "Choose at least one option.",
    "Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.": "Date format: DD.MM.YYYY. Try again.",

    # ── validate_date_range: тексты зашиты внутрь тела функции, недоступны интроспекцией
    # извне (см. tests/test_i18n_core_27.py — там перечислены явным списком, «сверено с
    # reg_engine.py на дату плана», как и требует план) ────────────────────────────────────
    "Дата рождения не может быть в будущем. Проверь и введи ещё раз.": "Date of birth can't be in the future. Please check and re-enter.",
    "Проверь дату рождения (год выглядит неправдоподобно) и введи ещё раз.": "Please check your date of birth (the year looks implausible) and re-enter.",
    "Дата приезда не может быть в прошлом. Введи корректную дату.": "Arrival date can't be in the past. Enter a valid date.",
    "Проверь дату приезда (слишком далеко в будущем) и введи ещё раз.": "Please check your arrival date (too far in the future) and re-enter.",
}

# Обратный индекс, построенный из UI_EN (не второй словарь руками) — нужен плану 27-05 для
# канонизации ввода (LANG-06): делегат мог получить английскую кнопку, а сохранить в БД
# обязаны русский канон. Коллизия (два разных русских литерала дали один и тот же английский)
# должна упасть здесь, на импорте модуля, а не тихо потерять один из вариантов в 27-05 —
# `assert` ниже перечисляет конфликтующие пары, чтобы разработчик сразу увидел, что
# исправлять в UI_EN, а не искал баг в канонизации.
EN_TO_RU: dict[str, str] = {}
_collisions: dict[str, list[str]] = {}
for _ru, _en in UI_EN.items():
    if _en in EN_TO_RU and EN_TO_RU[_en] != _ru:
        _collisions.setdefault(_en, [EN_TO_RU[_en]]).append(_ru)
    else:
        EN_TO_RU[_en] = _ru
if _collisions:
    raise AssertionError(
        "UI_EN: несколько русских литералов дали один английский (обратный индекс "
        f"неоднозначен): {_collisions!r}"
    )
del _collisions, _ru, _en

# Множества для фильтров aiogram — выведены из словаря, а не выписаны заново (несовпадение
# c UI_EN тут же стало бы невозможным по построению).
CANCEL_WORDS = frozenset({"Отмена", UI_EN["Отмена"], "/cancel"})
CONFIRM_WORDS = frozenset({"Всё верно", UI_EN["Всё верно"]})
EDIT_WORDS = frozenset({"Изменить", UI_EN["Изменить"]})
DONE_WORDS = frozenset({"Готово", UI_EN["Готово"]})
SKIP_WORDS = frozenset({"Пропустить", UI_EN["Пропустить"]})
OTHER_WORDS = frozenset({"Другое", UI_EN["Другое"]})
YES_WORDS = frozenset({"Да", UI_EN["Да"]})
NO_WORDS = frozenset({"Нет", UI_EN["Нет"]})
