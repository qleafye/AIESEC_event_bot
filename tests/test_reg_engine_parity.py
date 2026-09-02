"""Phase 21 (21-01, FORM-SYNC-01) — golden-снимок анкеты СНЯТ ДО переноса в reg_engine.py.

Зачем: `reg_engine.py`/`reg_options.py` переносят «какие шаги, какой текст вопроса, какие
варианты ответа, что подставить возвращенцу» из aiogram-хендлеров `handlers/registration.py`
в aiogram-free ядро, которое позже (план 21-10) обслужит и Mini App. Перенос обязан быть
«byte-for-byte unchanged» — делегат не должен заметить ничего. «Не заметил ничего» проверяется
машиной, а не глазами: этот файл снимает `GOLDEN` — литеральный снимок поведения ТЕКУЩЕГО кода
(`SOURCE = handlers.registration`, Task 1) — и после переноса (Task 3, `SOURCE = reg_engine`)
сравнивает тот же снимок с тем же литералом. Любое расхождение — регресс переноса, а не повод
поправить `GOLDEN` (единственное легитимное исключение — намеренное изменение поведения,
описанное отдельным планом и явно упомянутое в коммите).

`GOLDEN` получен ПРОГОНОМ `_collect_snapshot(SOURCE)` против чистой временной БД (только
дефолты реестра, ни одного override) — не вписан на глаз. `_collect_snapshot` сама по себе
знает, как читать оба интерфейса (до и после переноса): `prompt`/`_prompt`,
`options`/`_get_options`+клавиатуры builders, `enabled_steps`/`_get_enabled_steps`,
`prior_answers_for`+`has_prior_resume`/эквивалентное выражение из `_ask_step_or_recall`.

БД поднимается по образцу `_ready(tmp_path)` из `tests/test_reg_resume_ttl_260820.py`
(`config.DB_PATH = tmp_path/...` + `asyncio.run(init_db())`), pytest-asyncio недоступен —
async идёт через `asyncio.run()` (правило проекта).
"""
import asyncio

from config import config
from database.db import init_db
from reg_engine import REG_FLOW
from reg_labels import REG_LABELS

# Task 3: SOURCE переключён на reg_engine — GOLDEN не тронут ни одним символом (см. докстринг
# выше). До переноса (Task 1) здесь стояло `import handlers.registration as SOURCE`.
import reg_engine as SOURCE  # noqa: E402


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_reg_engine_parity.db")
    asyncio.run(init_db())


# ── Раздел prompts ───────────────────────────────────────────────────────────────────────────

# Дефолтные тексты вопросов, вырезанные из handlers/registration.py::_ask_step (Task 1
# read_first). Скопированы дословно из веток if/elif — это ЕДИНСТВЕННОЕ место в тесте, где
# текст «вписан руками», и он существует именно для того, чтобы дать `_prompt(step_key,
# default, track)` тот же `default`, что видит текущий код. Шаги, для которых `_ask_step` не
# заводит отдельную ветку (select/multi/date generic), вычисляются ниже через REG_LABELS —
# те же данные, что читает сам бот, а не переизобретённые тестом.
_EXPLICIT_DEFAULTS = {
    "age": "Напиши свой возраст числом:",
    "email": "Укажи свой email:",
    "phone": "Укажи номер телефона:",
    "vk": "Введи свой ник в ВК в формате @username:",
    "city": "Из какого ты города?",
    "source": "Откуда ты узнал(а) о нас?",
    "local_committee": "Локальный комитет:",
    "position": "Твоя позиция:",
    "education_status": "Учишься ли ты сейчас?",
    # reg_university_mode дефолт = "text" (settings_schema.py) -> текстовая ветка _ask_step.
    "university": "Введи название твоего ВУЗа:",
    "course": "На каком ты курсе?",
    "specialty": "Какая у тебя специальность?",
    "work_status": "Работаешь ли ты сейчас?",
    "work_sphere": "В какой сфере ты работаешь?",
    "missing_skills": "Каких навыков тебе сейчас не хватает?",
    # event_name дефолт (не задан в реестре tmp-БД) = "мероприятия" (registration.py::_ask_step).
    "expectations": "Что ты ожидаешь от мероприятия? Что хотел(а) бы узнать или получить?",
    "informal_day": "Планируете ли вы посетить второй неформальный день (пройдёт загородом)?",
    "attendance_format": "В каком формате ты будешь присутствовать?",
    "comments": "Любые вопросы/комментарии/пожелания:",
    "department": "Твой департамент:",
    "aiesec_role": "Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):",
    "needs_certificate": "Нужна справка в ВУЗ?",
    "alumni_status": "Ты аламни или айсекер?",
    "english_level": "Уровень английского:",
    "allergies": "Есть ли у тебя аллергии на продукты/запахи? (если нет — поставь «-»)",
    "food_pref": "Особенности питания? Напиши, если ты веган/вегетарианец (иначе — обычное):",
    "arrival": "Когда приедешь?",
    "housing": "Где будешь жить?",
    "bed_sharing": "На площадке много двуспальных кроватей. Готов(а) спать с кем-то на одной кровати?",
    "bed_partner": "С кем хотел(а) бы делить кровать? Напиши имя или «без разницы».",
    "transport": "Как добираешься до площадки?",
    # payment_deadline не задан в tmp-БД -> dl_note="" (registration.py::_ask_step).
    "payment_plan_date": "Когда планируешь оплатить взнос? Введи дату (ДД.ММ.ГГГГ):",
    "cc_shop": "Что бы ты хотел(а) видеть в CC-shop?",
    "exp_organizers": "Ожидания от команды организаторов?",
    "exp_content": "Ожидания от контента?",
    "volunteer": "Хочешь быть волонтёром?",
    "resume": "Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.",
    "ambassador": "Хочешь стать амбассадором форума?",
}

_GENERIC_FALLBACK_LABEL = {"select": "Выбери вариант", "multi": "Выбери варианты", "date": "Дата"}

TRACKS = ["full", "short", "party_overnight"]


def _default_for(step_key: str, step_type: str) -> str:
    if step_key in _EXPLICIT_DEFAULTS:
        return _EXPLICIT_DEFAULTS[step_key]
    label = REG_LABELS.get(f"reg_q_{step_key}", _GENERIC_FALLBACK_LABEL.get(step_type, "Вопрос"))
    if step_type == "date":
        return f"{label} (ДД.ММ.ГГГГ):"
    if step_type == "select":
        return f"{label}:"
    if step_type == "multi":
        return f"{label} (можно выбрать несколько):"
    return label


async def _collect_prompts(mod) -> dict:
    prompt_fn = getattr(mod, "prompt", None)
    out = {}
    for step_key, _setting_key, step_type in REG_FLOW:
        default = _default_for(step_key, step_type)
        per_track = {}
        for track in TRACKS:
            if prompt_fn is not None:
                per_track[track] = await prompt_fn(step_key, track)
            else:
                per_track[track] = await mod._prompt(step_key, default, track)
        out[step_key] = per_track
    return out


# ── Раздел options ───────────────────────────────────────────────────────────────────────────

# Шаги, у которых есть варианты ответа кнопками (university исключён: дефолтный
# reg_university_mode="text" -> свободный ввод, вариантов нет).
OPTION_STEPS = [
    "city", "source", "education_status", "course", "study_field", "goal", "formats",
    "local_committee", "position", "department", "aiesec_role", "english_level", "arrival",
    "housing", "attendance_format", "informal_day", "alumni_status", "transport", "bed_sharing",
    "ambassador", "work_status", "needs_certificate", "volunteer",
]

# Литералы, которые сегодня живут ТОЛЬКО внутри handlers/registration.py::_ask_step (не в
# keyboards/builders.py) — списаны дословно оттуда (Task 1 read_first). Task 2 переносит их в
# reg_options.py как именованные константы; до этого момента здесь единственное место, где их
# можно прочитать программно без запуска полного FSM.
_INLINE_LITERALS = {
    "alumni_status": ["Аламни", "Айсекер", "Ни то, ни другое"],
    "bed_sharing": ["Да", "Нет"],
    "transport": ["Трансфер до площадки", "Самостоятельно"],
    "ambassador": ["Да!", "Пока нет"],
}

_KB_BUILDER_NAME = {
    "education_status": "get_education_status_kb",
    "course": "get_course_kb",
    "local_committee": "get_local_committee_kb",
    "position": "get_position_kb",
    "department": "get_department_kb",
    "aiesec_role": "get_aiesec_role_kb",
    "english_level": "get_english_level_kb",
    "arrival": "get_arrival_kb",
    "housing": "get_housing_kb",
    "attendance_format": "get_attendance_format_kb",
    "informal_day": "get_informal_day_kb",
}


def _kb_texts(markup) -> list[str]:
    return [btn.text for row in markup.keyboard for btn in row]


async def _legacy_step_options(mod, step_key: str) -> list[str]:
    if step_key in _INLINE_LITERALS:
        return list(_INLINE_LITERALS[step_key])
    if step_key == "city":
        opt_key, default = mod.SELECT_CONFIG["city"]
        return await mod._get_options(opt_key, default)
    if step_key == "study_field":
        opt_key, default = mod.SELECT_CONFIG["study_field"]
        return await mod._get_options(opt_key, default)
    if step_key == "goal":
        opt_key, default = mod.MULTI_CONFIG["goal"]
        return await mod._get_options(opt_key, default)
    if step_key == "formats":
        opt_key, default = mod.MULTI_CONFIG["formats"]
        return await mod._get_options(opt_key, default)
    if step_key == "source":
        return _kb_texts(await mod.get_source_kb())
    if step_key in ("work_status", "needs_certificate", "volunteer"):
        return _kb_texts(mod.get_yes_no_kb())
    return _kb_texts(getattr(mod, _KB_BUILDER_NAME[step_key])())


# Эти 4 шага сегодня строят клавиатуру через builders.py, которая допечатывает литеральную
# кнопку "Другое" БЕЗУСЛОВНО (не через флаг add_other) -- она попала в golden как часть
# плоского списка подписей кнопок задачи 1. reg_engine.options() отдаёт только сам список
# (без служебных слов -- Pitfall 10, Task 2 read_first); "Другое" для этих шагов реконструируем
# из отдельного флага other_allowed (reg_engine.step_spec()), чтобы не трогать GOLDEN и не
# продолжать хранить "Другое" в самом списке вариантов.
_OTHER_APPENDED_IN_GOLDEN = {"local_committee", "position", "department", "aiesec_role"}


async def _collect_options(mod) -> dict:
    options_fn = getattr(mod, "options", None)
    out = {}
    for step_key in OPTION_STEPS:
        if options_fn is not None:
            value = list(await options_fn(step_key))
            if step_key in _OTHER_APPENDED_IN_GOLDEN:
                value = value + ["Другое"]
            out[step_key] = value
        else:
            out[step_key] = await _legacy_step_options(mod, step_key)
    return out


# ── Раздел enabled ───────────────────────────────────────────────────────────────────────────

# Матрица входов (Task 1 action): пустой data, education_status Да/Нет, work_status Да/Нет,
# participant_type каждого трека, source_from_tag установлен/нет, housing=«Заранее»,
# attendance_format=«Онлайн».
ENABLED_SCENARIOS = {
    "empty": {},
    "edu_yes": {"education_status": "Да, в ВУЗе или колледже"},
    "edu_no": {"education_status": "Нет, завершил(а) обучение"},
    "work_yes": {"work_status": "Да"},
    "work_no": {"work_status": "Нет"},
    "track_full": {"participant_type": "full"},
    "track_short": {"participant_type": "short"},
    "track_party_overnight": {"participant_type": "party_overnight"},
    "track_party_noovernight": {"participant_type": "party_noovernight"},
    "source_from_tag_set": {"_source_from_tag": True},
    "source_from_tag_absent": {},
    "housing_zaranee": {"arrival": "Заранее", "housing": "Хост"},
    "attendance_online": {"attendance_format": "Online"},
}


async def _collect_enabled(mod) -> dict:
    enabled_fn = getattr(mod, "enabled_steps", None) or mod._get_enabled_steps
    return {name: list(await enabled_fn(dict(data))) for name, data in ENABLED_SCENARIOS.items()}


# ── Раздел recall (D-07: префилл возвращенца) ───────────────────────────────────────────────

# Синтетическая строка `users`: заполненные, пустая строка, прочерк, None и resume_file_id —
# чтобы зафиксировать и правило «пусто -> вопрос задаётся заново», и карвинг resume (Task 1
# action, Pitfall 3).
RECALL_ROW = {
    "age": 25,
    "phone": "+79991234567",
    "city": "Москва и МО",
    "education_status": "Да, в ВУЗе или колледже",
    "course": "2",
    "university": "МГУ",
    "vk_username": "@ivan",
    "source": "Другое",
    "full_name": "Иван Иванов",
    "is_ambassador_candidate": 1,
    "specialty": "",       # пустая строка -> не должно попасть в recall
    "work_sphere": "-",    # прочерк -> не должно попасть в recall
    "missing_skills": None,  # None -> не должно попасть в recall
    "resume_file_id": "file123",
}


async def _collect_recall(mod) -> dict:
    prior_fn = getattr(mod, "prior_answers_for", None)
    has_resume_fn = getattr(mod, "has_prior_resume", None)
    if prior_fn is not None:
        answers = prior_fn(RECALL_ROW)
        has_resume = has_resume_fn(RECALL_ROW)
    else:
        # Ровно то же выражение, что стоит в handlers/registration.py::_ask_step_or_recall.
        answers = {
            step: RECALL_ROW.get(col)
            for step, col in mod.STEP_TO_COLUMN.items()
            if step != "resume" and RECALL_ROW.get(col) not in (None, "", "-")
        }
        has_resume = any(
            RECALL_ROW.get(col) not in (None, "", "-")
            for col in ("resume_file_id", "resume_text", "resume_url")
        )
    return {"answers": dict(answers), "has_resume": bool(has_resume)}


# ── Сборка снимка ────────────────────────────────────────────────────────────────────────────

async def _collect_snapshot_async(mod) -> dict:
    return {
        "prompts": await _collect_prompts(mod),
        "options": await _collect_options(mod),
        "enabled": await _collect_enabled(mod),
        "recall": await _collect_recall(mod),
    }


def _collect_snapshot(mod) -> dict:
    return asyncio.run(_collect_snapshot_async(mod))


# ── GOLDEN ───────────────────────────────────────────────────────────────────────────────────
# Снят прогоном `_collect_snapshot(SOURCE)` (SOURCE = handlers.registration) против чистой
# временной БД -- см. докстринг файла. НЕ редактировать руками; расхождение после переноса
# в reg_engine — регресс, а не повод поправить константу.
GOLDEN = {'prompts': {'age': {'full': 'Напиши свой возраст числом:',
                     'short': 'Напиши свой возраст числом:',
                     'party_overnight': 'Напиши свой возраст числом:'},
             'phone': {'full': 'Укажи номер телефона:',
                       'short': 'Укажи номер телефона:',
                       'party_overnight': 'Укажи номер телефона:'},
             'alumni_status': {'full': 'Ты аламни или айсекер?',
                               'short': 'Ты аламни или айсекер?',
                               'party_overnight': 'Ты аламни или айсекер?'},
             'vk': {'full': 'Введи свой ник в ВК в формате @username:',
                    'short': 'Введи свой ник в ВК в формате @username:',
                    'party_overnight': 'Введи свой ник в ВК в формате @username:'},
             'city': {'full': 'Из какого ты города?',
                      'short': 'Из какого ты города?',
                      'party_overnight': 'Из какого ты города?'},
             'education_status': {'full': 'Учишься ли ты сейчас?',
                                  'short': 'Учишься ли ты сейчас?',
                                  'party_overnight': 'Учишься ли ты сейчас?'},
             'course': {'full': 'На каком ты курсе?',
                        'short': 'На каком ты курсе?',
                        'party_overnight': 'На каком ты курсе?'},
             'university': {'full': 'Введи название твоего ВУЗа:',
                            'short': 'Введи название твоего ВУЗа:',
                            'party_overnight': 'Введи название твоего ВУЗа:'},
             'study_field': {'full': '🎯 Направление обучения:',
                             'short': '🎯 Направление обучения:',
                             'party_overnight': '🎯 Направление обучения:'},
             'goal': {'full': '🎯 Цель участия (можно выбрать несколько):',
                      'short': '🎯 Цель участия (можно выбрать несколько):',
                      'party_overnight': '🎯 Цель участия (можно выбрать несколько):'},
             'formats': {'full': '📋 Форматы форума (можно выбрать несколько):',
                         'short': '📋 Форматы форума (можно выбрать несколько):',
                         'party_overnight': '📋 Форматы форума (можно выбрать несколько):'},
             'expectations': {'full': 'Что ты ожидаешь от мероприятия? Что хотел(а) бы узнать или '
                                      'получить?',
                              'short': 'Что ты ожидаешь от мероприятия? Что хотел(а) бы узнать или '
                                       'получить?',
                              'party_overnight': 'Что ты ожидаешь от мероприятия? Что хотел(а) бы '
                                                 'узнать или получить?'},
             'source': {'full': 'Откуда ты узнал(а) о нас?',
                        'short': 'Откуда ты узнал(а) о нас?',
                        'party_overnight': 'Откуда ты узнал(а) о нас?'},
             'ambassador': {'full': 'Хочешь стать амбассадором форума?',
                            'short': 'Хочешь стать амбассадором форума?',
                            'party_overnight': 'Хочешь стать амбассадором форума?'},
             'resume': {'full': 'Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.',
                        'short': 'Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.',
                        'party_overnight': 'Прикрепи резюме файлом (PDF или DOCX) или напиши его '
                                           'текстом.'},
             'email': {'full': 'Укажи свой email:',
                       'short': 'Укажи свой email:',
                       'party_overnight': 'Укажи свой email:'},
             'local_committee': {'full': 'Локальный комитет:',
                                 'short': 'Локальный комитет:',
                                 'party_overnight': 'Локальный комитет:'},
             'position': {'full': 'Твоя позиция:',
                          'short': 'Твоя позиция:',
                          'party_overnight': 'Твоя позиция:'},
             'specialty': {'full': 'Какая у тебя специальность?',
                           'short': 'Какая у тебя специальность?',
                           'party_overnight': 'Какая у тебя специальность?'},
             'work_status': {'full': 'Работаешь ли ты сейчас?',
                             'short': 'Работаешь ли ты сейчас?',
                             'party_overnight': 'Работаешь ли ты сейчас?'},
             'work_sphere': {'full': 'В какой сфере ты работаешь?',
                             'short': 'В какой сфере ты работаешь?',
                             'party_overnight': 'В какой сфере ты работаешь?'},
             'missing_skills': {'full': 'Каких навыков тебе сейчас не хватает?',
                                'short': 'Каких навыков тебе сейчас не хватает?',
                                'party_overnight': 'Каких навыков тебе сейчас не хватает?'},
             'attendance_format': {'full': 'В каком формате ты будешь присутствовать?',
                                   'short': 'В каком формате ты будешь присутствовать?',
                                   'party_overnight': 'В каком формате ты будешь присутствовать?'},
             'informal_day': {'full': 'Планируете ли вы посетить второй неформальный день (пройдёт '
                                      'загородом)?',
                              'short': 'Планируете ли вы посетить второй неформальный день '
                                       '(пройдёт загородом)?',
                              'party_overnight': 'Планируете ли вы посетить второй неформальный '
                                                 'день (пройдёт загородом)?'},
             'comments': {'full': 'Любые вопросы/комментарии/пожелания:',
                          'short': 'Любые вопросы/комментарии/пожелания:',
                          'party_overnight': 'Любые вопросы/комментарии/пожелания:'},
             'department': {'full': 'Твой департамент:',
                            'short': 'Твой департамент:',
                            'party_overnight': 'Твой департамент:'},
             'aiesec_role': {'full': 'Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):',
                             'short': 'Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):',
                             'party_overnight': 'Твоя позиция '
                                                '(Member/TL/Manager/VP/LCP/Coordinator):'},
             'needs_certificate': {'full': 'Нужна справка в ВУЗ?',
                                   'short': 'Нужна справка в ВУЗ?',
                                   'party_overnight': 'Нужна справка в ВУЗ?'},
             'english_level': {'full': 'Уровень английского:',
                               'short': 'Уровень английского:',
                               'party_overnight': 'Уровень английского:'},
             'allergies': {'full': 'Есть ли у тебя аллергии на продукты/запахи? (если нет — '
                                   'поставь «-»)',
                           'short': 'Есть ли у тебя аллергии на продукты/запахи? (если нет — '
                                    'поставь «-»)',
                           'party_overnight': 'Есть ли у тебя аллергии на продукты/запахи? (если '
                                              'нет — поставь «-»)'},
             'food_pref': {'full': 'Особенности питания? Напиши, если ты веган/вегетарианец (иначе '
                                   '— обычное):',
                           'short': 'Особенности питания? Напиши, если ты веган/вегетарианец '
                                    '(иначе — обычное):',
                           'party_overnight': 'Особенности питания? Напиши, если ты '
                                              'веган/вегетарианец (иначе — обычное):'},
             'arrival': {'full': 'Когда приедешь?',
                         'short': 'Когда приедешь?',
                         'party_overnight': 'Когда приедешь?'},
             'housing': {'full': 'Где будешь жить?',
                         'short': 'Где будешь жить?',
                         'party_overnight': 'Где будешь жить?'},
             'bed_sharing': {'full': 'На площадке много двуспальных кроватей. Готов(а) спать с '
                                     'кем-то на одной кровати?',
                             'short': 'На площадке много двуспальных кроватей. Готов(а) спать с '
                                      'кем-то на одной кровати?',
                             'party_overnight': 'На площадке много двуспальных кроватей. Готов(а) '
                                                'спать с кем-то на одной кровати?'},
             'bed_partner': {'full': 'С кем хотел(а) бы делить кровать? Напиши имя или «без '
                                     'разницы».',
                             'short': 'С кем хотел(а) бы делить кровать? Напиши имя или «без '
                                      'разницы».',
                             'party_overnight': 'С кем хотел(а) бы делить кровать? Напиши имя или '
                                                '«без разницы».'},
             'transport': {'full': 'Как добираешься до площадки?',
                           'short': 'Как добираешься до площадки?',
                           'party_overnight': 'Как добираешься до площадки?'},
             'cc_shop': {'full': 'Что бы ты хотел(а) видеть в CC-shop?',
                         'short': 'Что бы ты хотел(а) видеть в CC-shop?',
                         'party_overnight': 'Что бы ты хотел(а) видеть в CC-shop?'},
             'exp_organizers': {'full': 'Ожидания от команды организаторов?',
                                'short': 'Ожидания от команды организаторов?',
                                'party_overnight': 'Ожидания от команды организаторов?'},
             'exp_content': {'full': 'Ожидания от контента?',
                             'short': 'Ожидания от контента?',
                             'party_overnight': 'Ожидания от контента?'},
             'volunteer': {'full': 'Хочешь быть волонтёром?',
                           'short': 'Хочешь быть волонтёром?',
                           'party_overnight': 'Хочешь быть волонтёром?'},
             'arrival_date': {'full': '📅 Дата приезда (ДД.ММ.ГГГГ):',
                              'short': '📅 Дата приезда (ДД.ММ.ГГГГ):',
                              'party_overnight': '📅 Дата приезда (ДД.ММ.ГГГГ):'},
             'birth_date': {'full': '🎂 Дата рождения (ДД.ММ.ГГГГ):',
                            'short': '🎂 Дата рождения (ДД.ММ.ГГГГ):',
                            'party_overnight': '🎂 Дата рождения (ДД.ММ.ГГГГ):'},
             'payment_plan_date': {'full': 'Когда планируешь оплатить взнос? Введи дату '
                                           '(ДД.ММ.ГГГГ):',
                                   'short': 'Когда планируешь оплатить взнос? Введи дату '
                                            '(ДД.ММ.ГГГГ):',
                                   'party_overnight': 'Когда планируешь оплатить взнос? Введи дату '
                                                      '(ДД.ММ.ГГГГ):'}},
 'options': {'city': ['Москва и МО',
                      'Санкт-Петербург',
                      'Новосибирск',
                      'Екатеринбург',
                      'Казань',
                      'Нижний Новгород',
                      'Красноярск',
                      'Уфа'],
             'source': ['Соцсети Юлид',
                        'Соцсети АЙСЕК',
                        'Университетские каналы',
                        'Рассказал друг/знакомый',
                        'Узнал от амбассадора',
                        'Узнал от блогера',
                        'Другое'],
             'education_status': ['Да, в ВУЗе или колледже',
                                  'Нет, завершил(а) обучение',
                                  'Нет, не получал(а) образование'],
             'course': ['1', '2', '3', '4', '5+', 'Магистратура/Аспирантура'],
             'study_field': ['Бизнес и управление',
                             'IT и технологии',
                             'Социальные и гуманитарные науки',
                             'Математические и естественные науки'],
             'goal': ['Найти возможность трудоустройства',
                      'Прокачать свои hard и soft skills',
                      'Пообщаться с людьми из моей сферы, нетворкинг',
                      'Получить карьерную консультацию от HR',
                      'Узнать о деятельности компаний'],
             'formats': ['Панельные дискуссии',
                         'Мастер-классы',
                         'Сессии со спикерами',
                         'Нетворкинг-сессии',
                         'Ярмарка открытых вакансий'],
             'local_committee': ['EG',
                                 'SPUEF',
                                 'Moscow',
                                 'Tyumen',
                                 'Ufa',
                                 'Ekaterinburg',
                                 'Другое'],
             'position': ['AIESECer', 'Alumni', 'AIESEC Friend', 'Другое'],
             'department': ['OGV', 'OGT', 'MKT', 'F&L', 'BD', 'LCP', 'EwA', 'Другое'],
             'aiesec_role': ['Member', 'TL', 'Manager', 'VP', 'LCP', 'Coordinator', 'Другое'],
             'english_level': ['Начальный', 'Средний', 'Продвинутый', 'Свободный'],
             'arrival': ['В дни конфы', 'Заранее', 'После'],
             'housing': ['Хост', 'Сам(а)', 'Не нужно'],
             'attendance_format': ['Offline', 'Online'],
             'informal_day': ['Да', 'Нет', 'Буду только в онлайне'],
             'alumni_status': ['Аламни', 'Айсекер', 'Ни то, ни другое'],
             'transport': ['Трансфер до площадки', 'Самостоятельно'],
             'bed_sharing': ['Да', 'Нет'],
             'ambassador': ['Да!', 'Пока нет'],
             'work_status': ['Да', 'Нет'],
             'needs_certificate': ['Да', 'Нет'],
             'volunteer': ['Да', 'Нет']},
 'enabled': {'empty': ['age',
                       'vk',
                       'education_status',
                       'expectations',
                       'source',
                       'work_status',
                       'missing_skills'],
             'edu_yes': ['age',
                         'vk',
                         'education_status',
                         'course',
                         'university',
                         'study_field',
                         'expectations',
                         'source',
                         'work_status',
                         'missing_skills'],
             'edu_no': ['age',
                        'vk',
                        'education_status',
                        'expectations',
                        'source',
                        'work_status',
                        'missing_skills'],
             'work_yes': ['age',
                          'vk',
                          'education_status',
                          'expectations',
                          'source',
                          'work_status',
                          'work_sphere',
                          'missing_skills'],
             'work_no': ['age',
                         'vk',
                         'education_status',
                         'expectations',
                         'source',
                         'work_status',
                         'work_sphere',
                         'missing_skills'],
             'track_full': ['age',
                            'vk',
                            'education_status',
                            'expectations',
                            'source',
                            'work_status',
                            'missing_skills'],
             'track_short': [],
             'track_party_overnight': ['age',
                                       'vk',
                                       'education_status',
                                       'expectations',
                                       'source',
                                       'work_status',
                                       'missing_skills'],
             'track_party_noovernight': ['age',
                                         'vk',
                                         'education_status',
                                         'expectations',
                                         'source',
                                         'work_status',
                                         'missing_skills'],
             'source_from_tag_set': ['age',
                                     'vk',
                                     'education_status',
                                     'expectations',
                                     'work_status',
                                     'missing_skills'],
             'source_from_tag_absent': ['age',
                                        'vk',
                                        'education_status',
                                        'expectations',
                                        'source',
                                        'work_status',
                                        'missing_skills'],
             'housing_zaranee': ['age',
                                 'vk',
                                 'education_status',
                                 'expectations',
                                 'source',
                                 'work_status',
                                 'missing_skills'],
             'attendance_online': ['age',
                                   'vk',
                                   'education_status',
                                   'expectations',
                                   'source',
                                   'work_status',
                                   'missing_skills']},
 'recall': {'answers': {'age': 25,
                        'phone': '+79991234567',
                        'vk': '@ivan',
                        'city': 'Москва и МО',
                        'education_status': 'Да, в ВУЗе или колледже',
                        'course': '2',
                        'university': 'МГУ',
                        'source': 'Другое',
                        'ambassador': 1,
                        'full_name': 'Иван Иванов'},
            'has_resume': True}}


def test_engine_matches_golden(tmp_path):
    _ready(tmp_path)
    snapshot = _collect_snapshot(SOURCE)
    assert snapshot == GOLDEN


def test_golden_covers_all_reg_flow_steps():
    assert set(GOLDEN["prompts"]) == {step for step, _sk, _t in REG_FLOW}


def test_golden_recall_has_no_empty_or_dash_values():
    for value in GOLDEN["recall"]["answers"].values():
        assert value not in (None, "", "-")
    assert GOLDEN["recall"]["answers"]  # непустой раздел


# ── form_spec: префилл возвращенца (D-07, Task 3 acceptance criteria) ──────────────────────

def test_form_spec_prefills_returning_delegate(tmp_path):
    """Для строки users возвращенца и пустого черновика form_spec помечает шаг с прошлым
    ответом value_source == "prior" и кладёт непустой `prior`; для новичка (prior=None)
    поле `prior` пустое у ВСЕХ шагов и value_source нигде не "prior"."""
    _ready(tmp_path)

    async def go():
        prior = SOURCE.prior_answers_for(RECALL_ROW)
        assert prior  # синтетическая строка задачи 1 непустая

        spec_returning = await SOURCE.form_spec({}, participant_type="full", prior=prior)
        by_key = {s["key"]: s for s in spec_returning["steps"]}
        prior_steps = [s for s in spec_returning["steps"] if s["value_source"] == "prior"]
        assert prior_steps, "ни один шаг не получил value_source == 'prior'"
        for step_spec_row in prior_steps:
            assert step_spec_row["prior"] is not None
            assert step_spec_row["prior"]["value"] == prior[step_spec_row["key"]]
            assert step_spec_row["value"] == prior[step_spec_row["key"]]
        # Шаг "age" точно есть в RECALL_ROW и в enabled по умолчанию (empty-сценарий golden).
        if "age" in by_key:
            assert by_key["age"]["value_source"] == "prior"
            assert by_key["age"]["prior"]["value"] == 25

        spec_newcomer = await SOURCE.form_spec({}, participant_type="full", prior=None)
        assert spec_newcomer["steps"], "у новичка форма не должна быть пустой"
        for step_spec_row in spec_newcomer["steps"]:
            assert step_spec_row["prior"] is None
            assert step_spec_row["value_source"] != "prior"

    asyncio.run(go())
