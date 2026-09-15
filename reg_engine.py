"""Phase 21 (21-01, FORM-SYNC-01): reg_engine.py — корневое ядро анкеты без зависимости на
бот-фреймворк. Одна функция обслуживает и текстовый чат бота (`handlers/registration.py`),
и (с плана 21-10) Mini App — «второго движка» с копией схемы анкеты быть не должно.

Behavior перенесённых кусков byte-for-byte unchanged от кода ДО этого плана — паритет снят
тестом `tests/test_reg_engine_parity.py` ДО переноса (`GOLDEN`, задача 1) и сравнивается с
ним же после (задача 3). Где отличие неизбежно (например, `prompt()` теперь сама вычисляет
дефолтный текст вместо приёма его аргументом) — снимок всё равно сходится побайтово, потому
что вычисление дефолта перенесено, а не переписано.

ВАЖНО (не нарушать): этот модуль не должен импортировать НИЧЕГО из пакета `handlers` — любой
`import handlers.x` исполняет `handlers/__init__.py`, который тянет `registration, user_actions,
admin, payment` (полный бот на бот-фреймворке, см. докстринг `handlers/__init__.py` и
`reg_labels.py`). Разрешённые импорты «наверх»: `settings_schema`, `database.db` (только
функции без бот-фреймворка), `reg_labels`, `reg_options`, `cities`, `config`. Поэтому `REG_FLOW` и его ближайшие зависимости
(`_is_party_track`/`_is_short_track`/`REG_DEFAULTS`/`_is_step_enabled`/`_is_module_enabled`),
раньше жившие в `handlers/reg_schema.py`, переехали СЮДА; `handlers/reg_schema.py` теперь
реэкспортирует их обратно (тот же приём, каким она уже реэкспортирует `REG_LABELS` из
корневого `reg_labels.py` — комментарий там же).

Phase 27 (27-04, LANG-06): к разрешённым импортам «наверх» добавляются `services.i18n` (сама
функция `tr()` — чистая, зависит только от `database.db`/`i18n_ui_en`/`settings_schema`, из
`handlers` не импортирует ничего, цикл невозможен) и корневой `i18n_ui_en` (ярус A — обратный
индекс `EN_TO_RU` для служебных слов «Other»/«Skip»/«Yes»/«No», тот же класс модуля-словаря без
проекта импортов, что `reg_labels`/`reg_options`, уже входящие в этот список). Оба используются
ТОЛЬКО в `option_pairs`/`canonical_option` ниже — ни одна из существующих функций движка
(`options`/`prompt`/`help_text`/`step_spec`/`form_spec`/`validate_answer`) языков не знает и не
меняет поведения ни при включённом, ни при выключенном модуле (A-03, 27-CONTEXT.md): перевод
врезается в воронки вывода поверхностей (Mini App — план 27-04, чат бота — план 27-05), а не в
ядро — золотые снимки (`tests/test_reg_engine_parity.py`, `tests/test_refac_snapshot_260816.py`)
эту фазу не видят вообще.
"""
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from config import config
from database.db import get_setting, get_user, RESUME_RECALL_COLUMNS
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from cities import (
    ALL_CITIES, cities_module_on, city_codes, city_label, enabled_cities,
    get_setting_typed_for_city, is_city_enabled, normalize_city, per_city_key,
)
from reg_labels import REG_LABELS
import reg_options as _opts
from services.i18n import tr as _tr
from i18n_ui_en import EN_TO_RU as _EN_TO_RU

# ── Registration Flow Engine: REG_FLOW + непосредственные зависимости ──────────────────────
# Перенесено дословно из handlers/reg_schema.py (было там с Phase 13 REFAC 13-02) — только
# ЭТОТ модуль и его непосредственные зависимости, не весь файл (REG_PRESETS/_apply_*_preset/
# REG_CATEGORIES — админские bulk-writer'ы настроек, не часть чтения анкеты, остаются в
# handlers/reg_schema.py и просто читают REG_FLOW отсюда).

# Phase 4 (D-07): each entry is (step_key, setting_key, type). type is "text" (default
# free-text handler), "date" (ДД.ММ.ГГГГ validation), "select"/"multi" (configurable option
# list), or "ambassador" (its own literal-yesno branch). "consent" is injected dynamically,
# never declared statically here.
REG_FLOW = [
    # YL'26 launch order (Tatiana). Consent + ФИО run before this list (see
    # _start_registration_flow). Order here IS the ask order for the enabled steps.
    ("age", "reg_q_age", "text"),
    ("phone", "reg_q_phone", "text"),
    ("alumni_status", "reg_q_alumni_status", "text"),  # аламни / айсекер / ни то, ни другое
    ("vk", "reg_q_vk", "text"),
    ("city", "reg_q_city", "text"),
    ("education_status", "reg_q_education", "text"),
    ("course", "reg_q_course", "text"),
    ("university", "reg_q_university", "text"),
    ("study_field", "reg_q_study_field", "select"),
    # Phase 28 (28-01, SU-01, СкиллАп 5): стек/опыт/готовность — default OFF, тумблеры
    # reg_q_stack/reg_q_experience/reg_q_readiness. Место в потоке — сразу после «Направление
    # обучения», как задано планом; у выключенных шагов порядок ни на что не влияет (D-06).
    ("stack", "reg_q_stack", "multi"),
    ("experience", "reg_q_experience", "select"),
    ("readiness", "reg_q_readiness", "select"),
    ("goal", "reg_q_goal", "multi"),
    ("formats", "reg_q_formats", "multi"),
    ("expectations", "reg_q_expectations", "text"),
    ("source", "reg_q_source", "text"),
    ("ambassador", "reg_q_ambassador", "ambassador"),
    ("resume", "reg_q_resume", "text"),
    # Phase 28 (28-01, SU-04, СкиллАп 5): развилка резюме R2b/R2c — default OFF, включаются
    # условно в enabled_steps (Задача 3) через resume_type. case_optin — без доп. условия
    # (место в потоке = позиция здесь, A-02 CONTEXT).
    ("resume_link", "reg_q_resume_link", "text"),
    ("mini_projects", "reg_q_mini_projects", "text"),
    ("mini_portfolio", "reg_q_mini_portfolio", "text"),
    ("mini_direction", "reg_q_mini_direction", "text"),
    ("case_optin", "reg_q_case_optin", "text"),
    # Remaining steps — default OFF, kept for other events (RusCo/Summit).
    ("email", "reg_q_email", "text"),
    ("local_committee", "reg_q_lc", "text"),
    ("position", "reg_q_position", "text"),
    ("specialty", "reg_q_specialty", "text"),
    ("work_status", "reg_q_work", "text"),
    ("work_sphere", "reg_q_work_sphere", "text"),
    ("missing_skills", "reg_q_skills", "text"),
    ("attendance_format", "reg_q_attendance", "text"),
    ("informal_day", "reg_q_informal_day", "text"),
    ("comments", "reg_q_comments", "text"),
    ("department", "reg_q_department", "text"),
    ("aiesec_role", "reg_q_aiesec_role", "text"),
    ("needs_certificate", "reg_q_certificate", "text"),
    ("english_level", "reg_q_english", "text"),
    ("allergies", "reg_q_allergies", "text"),
    ("food_pref", "reg_q_food", "text"),
    ("arrival", "reg_q_arrival", "text"),
    ("housing", "reg_q_housing", "text"),
    ("bed_sharing", "reg_q_bed_sharing", "text"),   # конфа: делить двуспальную кровать?
    ("bed_partner", "reg_q_bed_partner", "text"),   # конфа: с кем (условно на «Да»)
    ("transport", "reg_q_transport", "text"),
    ("cc_shop", "reg_q_cc_shop", "text"),
    ("exp_organizers", "reg_q_exp_organizers", "text"),
    ("exp_content", "reg_q_exp_content", "text"),
    ("volunteer", "reg_q_volunteer", "text"),
    ("arrival_date", "reg_q_arrival_date", "date"),
    ("birth_date", "reg_q_birth_date", "date"),
    ("payment_plan_date", "reg_q_payment_date", "date"),
]

# Phase 5 (TRACK-01/03): participant track vocabulary + deep-link parsing (D-10).
def _is_party_track(participant_type: str | None) -> bool:
    """The single predicate every later Phase 5 plan imports; do not duplicate elsewhere."""
    return participant_type in ("party_overnight", "party_noovernight")


# Phase 7 (SHORT-04): short-form track vocabulary.
SHORT_TRACK = "short"


def _is_short_track(participant_type: str | None) -> bool:
    """Exact-literal predicate for the short-form track — deliberately separate from
    _is_party_track, never merged into it."""
    return participant_type == SHORT_TRACK


# REG-01/D-06 (06-04): REG_DEFAULTS is DERIVED from settings_schema.SETTINGS_SCHEMA (every
# registered "toggle"-type entry), not a hand-maintained literal — the registry is the single
# source of truth for reg_q_* defaults.
REG_DEFAULTS = {
    k: v["default"] for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"
}


async def _is_step_enabled(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    if val is None:
        return REG_DEFAULTS.get(setting_key, "on") == "on"
    return val == "on"


async def _is_module_enabled(key: str) -> bool:
    """Phase 4 module flag check — None/absent/'off'/anything-but-'on' → False (D-15 fail-safe)."""
    return await get_setting_typed(key) == "on"


# ── Step type / column maps (перенос из handlers/registration.py, дословно) ────────────────

REG_STEP_TYPES = {step_key: step_type for step_key, _sk, step_type in REG_FLOW}

# Phase 07.3 (04, RET-02): step_key -> users column name. EXPLICIT map — most REG_FLOW keys
# match their column 1:1, but "vk" writes vk_username and "ambassador" writes
# is_ambassador_candidate. "resume" stays in the map, but is excluded from RECALLABLE_STEPS —
# a raw file_id/URL is meaningless to a human (Pitfall 3).
STEP_TO_COLUMN = {step_key: step_key for step_key, _sk, _t in REG_FLOW}
STEP_TO_COLUMN["vk"] = "vk_username"
STEP_TO_COLUMN["ambassador"] = "is_ambassador_candidate"
# Quick 260904-aup (D6): БЫЛА идентити-запись "resume" -> "resume" — колонки с таким именем в
# `users` нет (там resume_file_id/resume_text/resume_url), поэтому apply_answer клал текстовый
# ответ шага «резюме» из Mini App в reg_drafts.answers["resume"], а add_user такую колонку не
# знал — значение молча пропадало на финализации. Бот этой дыры не имел: он пишет resume_text
# напрямую (handlers/reg_flow.py::process_resume_text), минуя STEP_TO_COLUMN. Теперь совпадают.
STEP_TO_COLUMN["resume"] = "resume_text"
# ФИО спрашивается ВНЕ REG_FLOW (_ask_full_name, до движка шагов) — колонка совпадает с ключом.
STEP_TO_COLUMN["full_name"] = "full_name"
RECALLABLE_STEPS = {k for k in STEP_TO_COLUMN if k != "resume"}

# UAT 07.09 (T-d6t-04): маркер «все включённые шаги отвечены» в reg_drafts.step — это НЕ
# шаг. Он никогда не встречается в REG_FLOW, enabled_steps, column_to_step, STEP_TO_COLUMN,
# поэтому и выбран вид, который не может совпасть ни с одним ключом шага.
STEP_DONE = "__done__"

# Phase 21 (gap closure, FORM-SYNC-01): ключ подписи шага в REG_LABELS — это setting_key из
# тройки REG_FLOW (так бот подписывает шаг в `handlers/admin_reg_config.py`:
# `REG_LABELS.get(setting_key, setting_key)`), а НЕ `reg_q_{step_key}`. Для девяти шагов
# (education_status→reg_q_education, local_committee→reg_q_lc, work_status→reg_q_work,
# missing_skills→reg_q_skills, attendance_format→reg_q_attendance, needs_certificate→
# reg_q_certificate, english_level→reg_q_english, food_pref→reg_q_food, payment_plan_date→
# reg_q_payment_date) эти ключи расходятся — префиксный поиск откатывался на сырой step_key
# (deferred-items 21-11). Единственный источник подписи для бота, мастера и профиля — здесь.
SETTING_KEY_BY_STEP = {step_key: setting_key for step_key, setting_key, _t in REG_FLOW}


def label_key_for(step_key: str) -> str:
    """Ключ REG_LABELS для шага: setting_key из REG_FLOW; для ключей вне REG_FLOW (например
    `full_name` — ФИО спрашивается до движка шагов) — префиксный `reg_q_{step_key}`."""
    return SETTING_KEY_BY_STEP.get(step_key, f"reg_q_{step_key}")


def label_for(step_key: str) -> str:
    """Человеческая подпись шага — та же строка, что у бота в админке; фоллбэк на step_key
    остаётся только для ключей, которых нет ни в REG_FLOW, ни в REG_LABELS."""
    return REG_LABELS.get(label_key_for(step_key), step_key)

# Phase 21 (21-11, D-13): шаги, не редактируемые при правке уже поданной анкеты («город/трек/
# согласия — другая заявка»). Согласия — pre-flow, не запись REG_FLOW, поэтому их сюда
# заводить не нужно (обзор правки их вовсе не запрашивает). "city" — единственный REG_FLOW-шаг
# из этого списка; трек (party) не является отдельным шагом step_spec (выбирается вилкой
# pre-flow, participant_type), поэтому в списке колонок анкеты его тоже нет.
EDIT_LOCKED_STEPS = {"city"}

# Configurable single-select steps: step_key → (options_setting_key, default options).
SELECT_CONFIG = {
    "city": ("city_options", [
        "Москва и МО", "Санкт-Петербург", "Новосибирск", "Екатеринбург",
        "Казань", "Нижний Новгород", "Красноярск", "Уфа",
    ]),
    "study_field": ("study_field_options", [
        "Бизнес и управление", "IT и технологии",
        "Социальные и гуманитарные науки", "Математические и естественные науки",
    ]),
    # Phase 28 (28-01, SU-01, СкиллАп 5): default OFF (reg_q_experience/reg_q_readiness) —
    # подписи-варианты, не коды (D-02, ТЗ §1).
    "experience": ("experience_options", [
        "Нет опыта", "Пет-проекты", "Стажировка",
        "Коммерческий опыт до года", "Коммерческий опыт больше года",
    ]),
    "readiness": ("readiness_options", [
        "Готов(а) выйти сейчас", "Через 3–6 месяцев", "После выпуска", "Пока не ищу работу",
    ]),
}

# Configurable multi-select steps: step_key → (options_setting_key, default options).
MULTI_CONFIG = {
    "goal": ("goal_options", [
        "Найти возможность трудоустройства",
        "Прокачать свои hard и soft skills",
        "Пообщаться с людьми из моей сферы, нетворкинг",
        "Получить карьерную консультацию от HR",
        "Узнать о деятельности компаний",
    ]),
    "formats": ("formats_options", [
        "Панельные дискуссии", "Мастер-классы", "Сессии со спикерами",
        "Нетворкинг-сессии", "Ярмарка открытых вакансий",
    ]),
    # Phase 28 (28-01, SU-01, СкиллАп 5): default OFF (reg_q_stack) — двенадцать направлений
    # ТЗ §1, подписями (D-02).
    "stack": ("stack_options", [
        "Python", "JavaScript / TypeScript", "Java / Kotlin", "C# / .NET", "Go",
        "SQL и базы данных", "Аналитика и данные", "Дизайн и UX",
        "Мобильная разработка", "DevOps и облака", "Тестирование",
        "Информационная безопасность",
    ]),
}


async def multi_max(step_key: str) -> int | None:
    """Phase 28 (28-03, SU-02, RESEARCH Pattern 2): читает `reg_multi_max_{step_key}` — int
    больше нуля либо `None` (без лимита, D-06 «дефолт пусто = прежнее поведение байт-в-байт»).
    Не `per_city` (28-UI-SPEC.md таблица новых ключей): менеджер задаёт число одно на все
    города события. Ключ, не заведённый в SETTINGS_SCHEMA (любой будущий multi-шаг без своего
    лимита), безопасно резолвится в `None` через тот же fail-soft `_parse_setting`."""
    return await get_setting_typed(f"reg_multi_max_{step_key}")


async def option_list_for(setting_key: str, defaults: list[str]) -> list[str]:
    """Admin-editable option list (newline text) with a hardcoded fallback. Verbatim
    behaviour of the pre-move handlers/registration.py::_get_options — kept as a generic
    2-arg helper (setting_key, defaults), because handlers/reg_flow.py::_multi_options calls
    it directly with an (opt_key, default) pair from MULTI_CONFIG."""
    raw = await get_setting(setting_key)
    if raw:
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        if items:
            return items
    return list(defaults)


# Литеральные списки без реестрового override — reg_options.py (перенесены туда планом 21-01
# Task 2). university не входит: у него собственная ветка (см. options() ниже) — список
# зависит от reg_university_mode.
_LITERAL_OPTIONS = {
    "education_status": _opts.EDUCATION_STATUS_OPTIONS,
    "course": _opts.COURSE_OPTIONS,
    "local_committee": _opts.LOCAL_COMMITTEE_OPTIONS,
    "position": _opts.POSITION_OPTIONS,
    "department": _opts.DEPARTMENT_OPTIONS,
    "aiesec_role": _opts.AIESEC_ROLE_OPTIONS,
    "english_level": _opts.ENGLISH_LEVEL_OPTIONS,
    "arrival": _opts.ARRIVAL_OPTIONS,
    "housing": _opts.HOUSING_OPTIONS,
    "attendance_format": _opts.ATTENDANCE_FORMAT_OPTIONS,
    "informal_day": _opts.INFORMAL_DAY_OPTIONS,
    "alumni_status": _opts.ALUMNI_STATUS_OPTIONS,
    "transport": _opts.TRANSPORT_OPTIONS,
    "bed_sharing": _opts.BED_SHARING_OPTIONS,
    "ambassador": _opts.AMBASSADOR_OPTIONS,
    "work_status": _opts.YES_NO_OPTIONS,
    "needs_certificate": _opts.YES_NO_OPTIONS,
    "volunteer": _opts.YES_NO_OPTIONS,
    # Phase 28 (28-01, SU-01, СкиллАп 5): жёсткие «Да»/«Нет» (не редактируемый список) —
    # храним ПОДПИСЬ, не bool (см. _MEMBERSHIP_STEPS ниже, D-02).
    "case_optin": _opts.YES_NO_OPTIONS,
}


async def options(step_key: str) -> list[str]:
    """Плоский список подписей кнопок для `step_key` — единая точка правды для клавиатур
    бота (через `keyboards/builders.py` -> reg_options) и Mini App (`step_spec()` ниже).
    Реестровые override (source/city/study_field/goal/formats) резолвятся точно так же, как
    делал pre-move `_get_options`; university зависит от reg_university_mode (текстовый режим
    — вариантов нет, список — свободный ввод не подходит)."""
    if step_key in SELECT_CONFIG:
        opt_key, default = SELECT_CONFIG[step_key]
        return await option_list_for(opt_key, default)
    if step_key in MULTI_CONFIG:
        opt_key, default = MULTI_CONFIG[step_key]
        return await option_list_for(opt_key, default)
    if step_key == "source":
        return await option_list_for("source_options", _opts.DEFAULT_SOURCE_OPTIONS)
    if step_key == "education_status":
        # Phase 28 (28-01, R-A3 CONTEXT): editable list, пустой реестр -> прежние три
        # литерала байт-в-байт (D-06) — единственное расхождение с прежним прямым
        # `_LITERAL_OPTIONS["education_status"]`.
        return await option_list_for("education_status_options", _opts.EDUCATION_STATUS_OPTIONS)
    if step_key == "university":
        mode = await get_setting_typed("reg_university_mode")
        if mode == "text":
            return []
        uni_opts = await get_setting("university_options")
        if uni_opts and uni_opts.strip():
            return [line.strip() for line in uni_opts.splitlines() if line.strip()]
        return list(config.UNIVERSITIES)
    return list(_LITERAL_OPTIONS.get(step_key, []))


# ── Перевод вариантов ответа (Phase 27, 27-04, LANG-06) ─────────────────────────────────────
# Единственное место в проекте, где подпись варианта расходится с каноном. Ядро само языков не
# знает (A-03) — эти две функции существуют РЯДОМ с ним как явный, узкий вход: воронки вывода
# поверхностей (Mini App здесь, план 27-05 — чат бота) зовут `option_pairs` перед рендером и
# `canonical_option` перед `validate_answer`, само ядро их не вызывает нигде внутри себя.

async def option_pairs(step_key: str, lang: str, tr_map: dict[str, str]) -> list[tuple[str, str]]:
    """`[(канон_ru, подпись_для_показа), ...]` — общий источник для Mini App (план 27-04) и
    чата бота (план 27-05). При `lang == "ru"` подпись — ТОТ ЖЕ объект, что канон (`tr()`
    отдаёт `text` по `is`, не копию — обязательное условие снимков `test_miniapp_labels_drift`/
    `test_reg_engine_parity`). Для шага без вариантов (`options()` вернула `[]`) — пустой
    список; `canonical_option` на пустом списке безопасен (падает сразу в ярус A/`None`)."""
    return [(opt, _tr(opt, lang, tr_map)) for opt in await options(step_key)]


def canonical_option(pairs: list[tuple[str, str]], text) -> str | None:
    """Подпись любого языка -> русский канон. Порядок разрешения:
    1) точное совпадение по ПОДПИСИ (обычный путь — делегат выбрал вариант на своём языке);
    2) точное совпадение по КАНОНУ (подпись совпала с русским текстом — `lang == "ru"`, или
       перевод этого конкретного варианта ещё не готов и `tr()` fail-soft вернул русский же
       текст, см. `services/i18n.py::tr`);
    3) служебные слова яруса A (`i18n_ui_en.EN_TO_RU`: «Other» -> «Другое», «Skip» ->
       «Пропустить», «Yes»/«No» -> «Да»/«Нет», …) — покрывает generic-подсказки движка, которые
       не входят в `options()` этого шага, но встречаются в его допустимых литералах
       (`_CHOICE_STEPS`/`_MEMBERSHIP_STEPS`);
    4) `None` — это НЕ ошибка, это сигнал «свободный ввод»: вызывающий сохраняет `text` как
       есть (шаги с `other_allowed`/`_BESPOKE_CHOICE` разрешают делегату написать свой вариант).

    Сравнение — по `strip()`-нутой строке; регистр НЕ игнорируется НАМЕРЕННО (варианты могут
    отличаться регистром осмысленно, например `Offline`/`online` были бы разными ответами) —
    не «чинить» на предположении, что это опечатка."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    for canon, label in pairs:
        if label == stripped:
            return canon
    for canon, _label in pairs:
        if canon == stripped:
            return canon
    return _EN_TO_RU.get(stripped)


# ── Гейт «включён ли шаг для трека» + список включённых шагов ──────────────────────────────

async def _city_override(setting_key: str, city_code: str | None) -> str | None:
    """Городской слой поверх УЖЕ СОБРАННОГО ключа (с трековым суффиксом, если он есть) —
    Phase 25 (CITYQ-01). Лестница ранних выходов ровно как в `cities.get_setting_for_city`,
    но БЕЗ похода в `is_per_city`/`get_setting_for_city`: те делают точный lookup ключа в
    `SETTINGS_SCHEMA` и не увидят суффиксный `reg_q_goal__party` (25-RESEARCH Pitfall 1) —
    он никогда не будет отдельной записью реестра."""
    if city_code is None:
        return None
    if city_code == ALL_CITIES:
        return None
    if not await cities_module_on():
        return None
    code = normalize_city(city_code)
    key = per_city_key(setting_key, code)
    if key is None:
        return None
    return await get_setting(key) or None


async def is_step_enabled_for_track(
    setting_key: str, participant_type: str | None, city_code: str | None = None
) -> bool:
    """D-03/D-04: tri-state per-track gate. `__party` is ONE namespace shared by
    party_overnight and party_noovernight. Key-absence means "inherit the global reg_q_<step>
    value"; key-presence means the explicit on/off wins. Full track (or None) never reads the
    __party key.

    Phase 7 (SHORT-01/SHORT-04): the short track reads `{setting_key}__short` the same
    tri-state way but does NOT fall through to the global value on key-absence — short is a
    curated subset of the full form, so key-absence means "do not ask this question".

    Phase 25 (CITYQ-01): `city_code` добавляет городской слой поверх трекового, порядок
    резолюции зафиксирован в CONTEXT «Оси переопределения». ВАЖНО (не «улучшать»): композит
    `{setting_key}__city__{code}` (БЕЗ трекового суффикса) читается ТОЛЬКО для full-трека
    (T=full) — party/short без своего трекового переопределения наследуют ГЛОБАЛЬНЫЙ базовый
    ключ, как сегодня, а не городской слой над ним."""
    if _is_party_track(participant_type):
        track_key = f"{setting_key}__party"
        override = await _city_override(track_key, city_code)
        if override is None:
            override = await get_setting(track_key)
        if override is not None:
            return override == "on"
        # ни городского, ни трекового переопределения для party — общий хвост ниже.
    elif _is_short_track(participant_type):
        track_key = f"{setting_key}__short"
        override = await _city_override(track_key, city_code)
        if override is None:
            override = await get_setting(track_key)
        if override is not None:
            return override == "on"
        return False
    else:
        override = await _city_override(setting_key, city_code)
        if override is not None:
            return override == "on"
    return await _is_step_enabled(setting_key)


# Phase 28 (28-01, SU-03, R-A3 CONTEXT): множество «учащихся» статусов образования настраивается
# из админки (edu_studying_statuses) — до этого плана условие было захардкожено как
# `startswith("Да")` (все три литерала EDUCATION_STATUS_OPTIONS начинаются с «Да»).
def is_studying(value, statuses: list[str] | None = None) -> bool:
    """Чистый предикат «считается учащимся» — при непустом `statuses` это точное вхождение
    `str(value)` в список (менеджер настроил семь статусов ТЗ, любой из «учащихся» вариантов);
    при `statuses is None`/пустом списке — ПРЕЖНЕЕ правило `str(value).startswith("Да")`
    байт-в-байт (D-06). Синхронная функция — вызывается и из `enabled_steps`/`apply_answer`
    (уже в async-контексте, но сам предикат данных не читает), и потенциально из тестов без
    event loop."""
    if statuses:
        return str(value) in statuses
    return str(value).startswith("Да")


async def studying_statuses() -> list[str]:
    """Реестровый список «учащихся» статусов (`edu_studying_statuses`, type: list) — пусто
    (дефолт) означает «множество не настроено», и `is_studying` откатывается на прежнее
    правило `startswith("Да")`. Список из реестра сюда приходит уже в виде отдельных строк
    (`get_setting` + построчный парсинг), без литерального дефолта — старое поведение живёт
    в `is_studying`, а не здесь."""
    raw = await get_setting("edu_studying_statuses") or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


async def enabled_steps(data: dict, city_code: str | None = None) -> list[str]:
    """Список step_key, которые нужно спросить/показать для текущих `data` — единая точка
    правды для условных шагов и пресетов (D-03/FORM-SYNC-01). Перенос дословный из
    handlers/registration.py::_get_enabled_steps.

    Phase 25 (CITYQ-01): `city_code`, если передан явно, побеждает `data["event_city"]` (FSM
    бота уже кладёт его туда — handlers/registration.py:1341-1344); ни один сегодняшний
    вызывающий не обязан меняться (`city_code=None` → берём из `data`, а не глобально).

    Phase 28 (28-01, SU-01/SU-03/SU-04, СкиллАп 5): множество «учусь» резолвится ОДИН раз за
    вызов (`studying_statuses()`), studying считается через `is_studying` — пустое множество
    воспроизводит прежнее правило байт-в-байт. Плюс три новых условия: resume_link/mini_* —
    по `resume_type` (развилка резюме, план 28-04 кладёт значение в data), case_optin — без
    доп. условия (A-02 CONTEXT)."""
    enabled = []
    edu_conditional = await get_setting_typed("edu_conditional") == "on"
    edu_studying_set = await studying_statuses()
    studying = is_studying(data.get("education_status", ""), edu_studying_set)
    resume_type = data.get("resume_type")
    participant_type = data.get("participant_type") or "full"
    city = city_code if city_code is not None else data.get("event_city")
    # Phase 28 (28-06, SU-06): пропуск «Источника» у пришедших по реф-ссылке (`amb_<id>`/
    # числовой формат — обоим уже дан один и тот же `referrer_id`) — дефолт off (YL/РилТолк
    # байт-в-байт), `source` при этом уже получает «Реферальная ссылка» из `with_defaults`.
    skip_source_for_referred = await get_setting_typed("reg_skip_source_for_referred") == "on"
    for step_key, setting_key, *_rest in REG_FLOW:
        if not await is_step_enabled_for_track(setting_key, participant_type, city):
            continue
        if step_key == "informal_day" and data.get("attendance_format") == "Online":
            continue
        if step_key == "source" and data.get("_source_from_tag"):
            continue
        if step_key == "source" and skip_source_for_referred and data.get("referrer_id"):
            continue
        if step_key == "housing" and "arrival" in data and data.get("arrival") != "Заранее":
            continue
        if step_key == "bed_partner" and not str(data.get("bed_sharing", "")).startswith("Да"):
            continue
        if step_key in ("housing", "bed_sharing", "bed_partner") and _is_party_track(participant_type) \
                and participant_type != "party_overnight":
            continue
        if edu_conditional and step_key == "university" and not studying:
            continue
        if edu_conditional and step_key == "course" and not studying:
            continue
        if edu_conditional and step_key == "specialty" and not studying:
            continue
        if edu_conditional and step_key == "study_field" and not studying:
            continue
        if step_key == "work_sphere" and not data.get("work_status"):
            continue
        # Phase 28 (28-01, SU-04, СкиллАп 5): развилка резюме — resume_link только при
        # resume_type == "link", мини-профиль (mini_*) только при resume_type == "mini".
        # resume_type кладёт в FSM/черновик план 28-04; до него условие никогда не истинно —
        # это и есть «выключено по умолчанию».
        if step_key == "resume_link" and resume_type != "link":
            continue
        if step_key in ("mini_projects", "mini_portfolio", "mini_direction") and resume_type != "mini":
            continue
        enabled.append(step_key)
    return enabled


# ── Тексты вопросов ──────────────────────────────────────────────────────────────────────

# Дефолтные тексты, вырезанные из handlers/registration.py::_ask_step дословно (перенос, не
# переписывание). university/expectations/payment_plan_date вычисляются отдельно (зависят от
# реестра); select/multi/date-generic (study_field, goal, formats, arrival_date, birth_date)
# вычисляются по REG_LABELS ниже — так же, как это делал сам _ask_step.
PROMPT_DEFAULTS = {
    "age": "Напиши свой возраст числом:",
    "phone": "Укажи номер телефона:",
    "alumni_status": "Ты аламни или айсекер?",
    "vk": "Введи свой ник в ВК в формате @username:",
    "city": "Из какого ты города?",
    "education_status": "Учишься ли ты сейчас?",
    "course": "На каком ты курсе?",
    "source": "Откуда ты узнал(а) о нас?",
    "ambassador": "Хочешь стать амбассадором форума?",
    "resume": "Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.",
    "email": "Укажи свой email:",
    "local_committee": "Локальный комитет:",
    "position": "Твоя позиция:",
    "specialty": "Какая у тебя специальность?",
    "work_status": "Работаешь ли ты сейчас?",
    "work_sphere": "В какой сфере ты работаешь?",
    "missing_skills": "Каких навыков тебе сейчас не хватает?",
    "attendance_format": "В каком формате ты будешь присутствовать?",
    "informal_day": "Планируете ли вы посетить второй неформальный день (пройдёт загородом)?",
    "comments": "Любые вопросы/комментарии/пожелания:",
    "department": "Твой департамент:",
    "aiesec_role": "Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):",
    "needs_certificate": "Нужна справка в ВУЗ?",
    "english_level": "Уровень английского:",
    "allergies": "Есть ли у тебя аллергии на продукты/запахи? (если нет — поставь «-»)",
    "food_pref": "Особенности питания? Напиши, если ты веган/вегетарианец (иначе — обычное):",
    "arrival": "Когда приедешь?",
    "housing": "Где будешь жить?",
    "bed_sharing": "На площадке много двуспальных кроватей. Готов(а) спать с кем-то на одной кровати?",
    "bed_partner": "С кем хотел(а) бы делить кровать? Напиши имя или «без разницы».",
    "transport": "Как добираешься до площадки?",
    "cc_shop": "Что бы ты хотел(а) видеть в CC-shop?",
    "exp_organizers": "Ожидания от команды организаторов?",
    "exp_content": "Ожидания от контента?",
    "volunteer": "Хочешь быть волонтёром?",
    # Phase 28 (28-01, SU-01/SU-04, СкиллАп 5): default OFF — стек/опыт/готовность и
    # развилка резюме R2b/R2c. Тексты resume_link/mini_*/case_optin изначально пришли из
    # «Copywriting Contract» 28-UI-SPEC.md на «вы» (D-07 fix, follow-up 28-10): бот везде на
    # «ты» — переписаны здесь и в самом UI-SPEC, GOLDEN["prompts"] обновлён тем же коммитом.
    "stack": "Что из этого пробовал(а)? Можно выбрать несколько.",
    "experience": "Какой у тебя опыт работы?",
    "readiness": "Когда готов(а) выйти на работу?",
    "resume_link": (
        "Пришли ссылку на резюме, портфолио или профиль — например hh.ru, GitHub, LinkedIn"
    ),
    "mini_projects": (
        "Расскажи о своих проектах — учебных, пет-, рабочих. Что делал(а), какую роль играл(а)?"
    ),
    "mini_portfolio": "Есть ссылка на портфолио, GitHub или соцсети с работами? Можно пропустить.",
    "mini_direction": "В каком направлении хочешь развиваться?",
    "case_optin": (
        "Кейс-чемпионат — это возможность решить бизнес-кейс от партнёров и получить "
        "обратную связь. Участвуешь?"
    ),
}

# ── Подсказки формата (D1, quick 260904-de4) ────────────────────────────────────────────────

# Подсказка формата под вопросом веб-анкеты — дословно согласована с веткой валидатора шага в
# `_validate_answer_core` (подсказка обязана описывать то, что валидатор ПРИНИМАЕТ, а не то, что
# человеку кажется удобным). Единственный источник подсказок: экран Mini App её не дублирует,
# бот в чат её не шлёт (менять `prompt()` = ломать golden-снимки текстов бота, а текст ошибки
# валидации в чате и так называет формат).
STEP_HELP = {
    "full_name": "Фамилия и имя минимум, например «Иванова Мария».",
    "age": "Число от 10 до 120, например «19».",
    "email": "Формат имя@домен, например «ivanova@example.com».",
    "phone": "Цифры, можно с плюсом впереди, например «+79161234567».",
    "vk": "Ник в ВК начинается с «@» и без пробелов, например «@ivanova_maria».",
    "resume": "Файл PDF или DOCX до 10 МБ, либо текст ответом в чате.",
}

# Пример-значение для каждого шага из STEP_HELP — ровно то, что названо в подсказке. Карта
# существует ради сторожа «подсказка не врёт»: пример, не проходящий собственный валидатор
# шага, — баг, который иначе видит только делегат.
STEP_HELP_EXAMPLES = {
    "full_name": "Иванова Мария",
    "age": "19",
    "email": "ivanova@example.com",
    "phone": "+79161234567",
    "vk": "@ivanova_maria",
    "resume": "Резюме текстом: 2 года опыта в маркетинге.",
}

_DATE_HELP = "Формат ДД.ММ.ГГГГ, например «01.09.2026»."


async def help_default(step_key: str, city_code: str | None = None) -> str | None:
    """Единственный расчёт СТАНДАРТНОЙ подсказки формата — те же три ветки, в том же порядке,
    что раньше считал `help_text` сам (квик 260906-7zv, HELP-01): resume/text_only -> общий
    `_DATE_HELP` для шагов типа `date` -> словарь `STEP_HELP`. Аргумента `participant_type` здесь
    нет: трековой оси у подсказки нет (D-1) — лишний неиспользуемый аргумент её бы подразумевал.
    `None` означает «у шага нет подсказки формата вовсе» (не «оверрайд ещё не задан»)."""
    if step_key == "resume" and await resume_mode(city_code) == "text_only":
        return "Коротко, текстом в чате."
    if REG_STEP_TYPES.get(step_key) == "date":
        return _DATE_HELP
    return STEP_HELP.get(step_key)


def has_help(step_key: str) -> bool:
    """Синхронный предикат «у шага есть подсказка формата» — существует ради клавиатуры
    админки (квик 260906-7zv): рисовать кнопку 💡 или нет — вопрос без похода в БД, экран и так
    делает по одному `get_setting` на каждый из ~44 шагов «✏️ Тексты вопросов». Обязан сходиться
    с `help_default` (тест `test_reg_help_editor_260906.py` проверяет это по всем шагам сразу) —
    иначе кнопка появится там, где показывать нечего, или наоборот."""
    return step_key in STEP_HELP or REG_STEP_TYPES.get(step_key) == "date"


async def help_text(
    step_key: str, participant_type: str | None = None, city_code: str | None = None
) -> str | None:
    """Подсказка формата под вопросом веб-анкеты (D1). Дефолт считает `help_default` (единый
    расчёт, квик 260906-7zv) — для шагов типа `date` он же отдаёт общую `_DATE_HELP` (одна
    константа, не копия на каждый шаг). Нет дефолта → `None` СРАЗУ, без похода в `bot_settings`:
    `form_spec` зовёт `step_spec` на ~43 шага, лишний `get_setting` на каждый шаг без подсказки
    не нужен. Есть дефолт → оверрайд `reg_help_{step_key}` (динамический ключ, как `reg_prompt_*`;
    в `SETTINGS_SCHEMA` НЕ заводится — иначе реестр вырастет на десяток ключей ради шести
    подсказок), пустая строка в оверрайде = «дефолт» (та же семантика `or default`, что у
    `prompt()`).

    Phase 25 (CITYQ-01): шаг `resume` в режиме `reg_resume_mode(city_code) == "text_only"`
    получает свой дефолт («Коротко, текстом в чате.») вместо общего `STEP_HELP["resume"]` —
    сам литерал `STEP_HELP` не меняется, оверрайд `reg_help_resume` остаётся глобальным."""
    default = await help_default(step_key, city_code)
    if default is None:
        return None
    return await get_setting(f"reg_help_{step_key}") or default


_GENERIC_FALLBACK_LABEL = {"select": "Выбери вариант", "multi": "Выбери варианты", "date": "Дата"}


async def resume_mode(city_code: str | None = None) -> str:
    """Режим приёма резюме (Phase 25, CITYQ-01; Phase 28-04, SU-04): `file_or_text` (дефолт,
    как всегда было), `text_only` или `fork` (развилка «файл / ссылка / нет резюме», R-A3
    CONTEXT). `reg_resume_mode` — обычный реестровый ключ БЕЗ трекового суффикса, поэтому
    общий резолвер `cities.get_setting_typed_for_city` уместен напрямую (в отличие от
    `_city_override`, который существует ради суффиксных `reg_q_*__party`/`__short`)."""
    value = await get_setting_typed_for_city("reg_resume_mode", city_code)
    if value not in ("file_or_text", "text_only", "fork"):
        return "file_or_text"
    return value


# Phase 28 (28-04, SU-04): вайтлист доменов ссылки на резюме — редактируемый список (D-01),
# дефолт — пять сайтов ТЗ §3.3. Чужой домен НЕ ошибка (D-04) — принимается с `link_verified=0`.
RESUME_LINK_WHITELIST_DEFAULT = ["hh.ru", "github.com", "gitlab.com", "linkedin.com", "notion.so"]


async def resume_link_whitelist() -> list[str]:
    """Editable-list вайтлист доменов для развилки резюме (SU-04) — правится из админки тем же
    приёмом, что `stack_options`/`goal_options` (`option_list_for`); пусто = пять сайтов ТЗ."""
    return await option_list_for("reg_resume_link_whitelist", RESUME_LINK_WHITELIST_DEFAULT)


# Дефолт-текст ошибки формата ссылки — используется, если вызывающий не резолвил реестровый
# `reg_resume_link_invalid_text` сам (та же защита, что `_DEFAULT_MULTI_LIMIT_ERROR_TEXT`,
# 28-03): движок синхронный, в БД не ходит, дословно совпадает с дефолтом ключа реестра.
_RESUME_LINK_INVALID_TEXT_DEFAULT = "Пришли ссылку целиком, начиная с http:// или https://"


def validate_resume_link(raw, whitelist) -> tuple[str | None, bool, str | None]:
    """Чистая функция проверки ссылки на резюме (SU-04, T-28-04-02): обрезает пробелы; без
    схемы `http(s)://` или без хоста — ошибка (текст дословно `reg_resume_link_invalid_text`,
    сам реестровый оверрайд резолвит вызывающий — эта функция синхронная, в БД не ходит, тот
    же приём, что `_DEFAULT_MULTI_LIMIT_ERROR_TEXT`); домен достаётся `urllib.parse` (без
    регулярок-самоделок), сравнение — по ПОЛНОМУ хосту без ведущего `www.` (не `endswith` —
    поддомен/похожий домен чужого сайта, например `hh.ru.evil.com`, не выдаёт себя за
    вайтлист, T-28-04-02). Домен не из списка — НЕ ошибка (ТЗ §3.3, D-04): `(url, False,
    None)`, ссылка принимается как есть."""
    text = (raw or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, False, _RESUME_LINK_INVALID_TEXT_DEFAULT
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    allowed = {d.strip().lower() for d in (whitelist or []) if d and d.strip()}
    return text, host in allowed, None


async def _default_prompt_text(
    step_key: str, participant_type: str | None, city_code: str | None = None
) -> str:
    """Вычисляет дефолтный текст вопроса — то же самое, что раньше собирал `_ask_step` перед
    вызовом `_prompt(step_key, default, participant_type)`. university/expectations/
    payment_plan_date зависят от реестра (режим ВУЗа, event_name, дедлайн оплаты); остальные
    типизированные (date/select/multi) — от REG_LABELS; всё прочее — статический литерал.

    Phase 25 (CITYQ-01): `resume` в режиме `text_only` — единственная новая ветка (литерал из
    CONTEXT); остальные ветки не тронуты — golden-снимки текстов бота обязаны сойтись."""
    if step_key == "resume" and await resume_mode(city_code) == "text_only":
        return (
            "Опиши вкратце свой опыт участия в проектах / активностях и, если есть, опыт "
            "работы. Например: был организатором школьных мероприятий, был куратором в "
            "университете и т. п."
        )
    if step_key == "university":
        mode = await get_setting_typed("reg_university_mode")
        if mode == "text":
            return "Введи название твоего ВУЗа:"
        return "В каком ВУЗе/колледже ты учишься?"
    if step_key == "expectations":
        event_name = await get_setting("event_name") or "мероприятия"
        return f"Что ты ожидаешь от {event_name}? Что хотел(а) бы узнать или получить?"
    if step_key == "payment_plan_date":
        deadline = await get_setting("payment_deadline")
        dl_date = deadline.split()[0] if deadline else ""
        dl_note = f" Крайний срок: {dl_date}." if dl_date else ""
        return f"Когда планируешь оплатить взнос?{dl_note} Введи дату (ДД.ММ.ГГГГ):"
    if step_key in PROMPT_DEFAULTS:
        return PROMPT_DEFAULTS[step_key]
    step_type = REG_STEP_TYPES.get(step_key)
    # Ключ подписи — setting_key из REG_FLOW (label_key_for), не reg_q_{step_key}: для шагов,
    # доходящих сюда, оба совпадают, поэтому golden-снимок prompts не меняется.
    label = REG_LABELS.get(label_key_for(step_key), _GENERIC_FALLBACK_LABEL.get(step_type, "Вопрос"))
    if step_type == "date":
        return f"{label} (ДД.ММ.ГГГГ):"
    if step_type == "select":
        return f"{label}:"
    if step_type == "multi":
        return f"{label} (можно выбрать несколько):"
    return label


async def prompt(
    step_key: str, participant_type: str | None = None, city_code: str | None = None
) -> str:
    """D-05: admin override reg_prompt_<step_key> (party track checks __party first, truthy
    wins) else the computed default. Перенос `_prompt` из handlers/registration.py — с той
    разницей, что `default` теперь считается ВНУТРИ (см. `_default_prompt_text`), а не
    приходит аргументом от вызывающего: бот и веб зовут одну и ту же резолюцию.

    Phase 25 (CITYQ-01): порядок резолюции — `reg_prompt_{step}__party__city__C` →
    `reg_prompt_{step}__party` (обе ветки ТОЛЬКО для party-трека) → `reg_prompt_{step}__city__C`
    (только full/None) → `reg_prompt_{step}` → вычисленный дефолт. Городской слой — через
    `_city_override`, короткий трек здесь как и раньше не имеет своей ветки (это НЕ новый
    пробел — у `prompt()` его не было и до этого плана)."""
    default = await _default_prompt_text(step_key, participant_type, city_code)
    if _is_party_track(participant_type):
        track_key = f"reg_prompt_{step_key}__party"
        override = await _city_override(track_key, city_code)
        if override:
            return override
        override = await get_setting(track_key)
        if override:
            return override
    elif not _is_short_track(participant_type):
        # short не получал city-слой ни разу до этого плана — new global city composite
        # доступен только full/None, ровно как зафиксировано в CONTEXT.
        override = await _city_override(f"reg_prompt_{step_key}", city_code)
        if override:
            return override
    return await get_setting(f"reg_prompt_{step_key}") or default


# ── Pre-flow: согласия, вилка города, вилка трека ───────────────────────────────────────────

# Fallback when consent_enabled is on but consent_list is empty.
DEFAULT_CONSENTS = [("Согласие на обработку персональных данных", "personal_data")]


async def consent_entries() -> list[tuple[str, str]]:
    """Parse consent_list ('Видимое название | ключ' per line) → [(label, key)]. Accepts ';'
    as a line separator too (mobile Telegram Enter=send trap)."""
    raw = await get_setting("consent_list") or ""
    entries: list[tuple[str, str]] = []
    for line in raw.replace(";", "\n").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, consent_key = line.split("|", 1)
        consent_key = consent_key.strip()
        if consent_key:
            entries.append((label.strip(), consent_key))
    return entries or DEFAULT_CONSENTS


async def get_consent_steps() -> list[str]:
    """Consent step keys (consent:<key>) when consent_enabled is on, else []."""
    if not await _is_module_enabled("consent_enabled"):
        return []
    return [f"consent:{key}" for _label, key in await consent_entries()]


async def should_show_fork(party_track: str | None, recovered_track: str | None,
                            is_registered: bool) -> bool:
    """D-10: pre-flow party-track fork. ALL conditions must hold before the fork is shown."""
    if party_track:
        return False
    if recovered_track:
        return False
    if is_registered:
        return False
    if await get_setting_typed("party_fork_question") != "on":
        return False
    if await get_setting_typed("party_enabled") != "on":
        return False
    return True


async def should_show_city_fork(event_city: str | None, is_registered: bool) -> bool:
    """Pure(ish) gating helper for the city pre-flow screen, mirrors should_show_fork."""
    if event_city:
        return False
    if is_registered:
        return False
    if not await cities_module_on():
        return False
    enabled = await enabled_cities()
    if len(enabled) < 2:
        return False
    return True


async def pre_flow(answers: dict, meta: dict | None = None) -> list[str]:
    """Список экранов до анкеты (согласия, вилка города, вилка трека), в том же порядке, в
    каком их сегодня показывает бот (D-02: общий движок гейтов, одинаково для бота и веба).
    `meta` — {event_city, is_registered, party_track, recovered_track, pending_consent_keys},
    все опциональны.

    UAT 21-12 находка 1: `pending_consent_keys` (если передан, не None) сужает согласия до
    ключей, которых в нём НЕТ ни одного отфильтрованного — то есть до реально несогласованных;
    вызывающий (`form_spec`) считает список тем же фильтром версий, что и гейт пересогласия
    (`services.consent.outstanding_consents`), не второй копией правила. `None` (по умолчанию,
    бот сюда не заглядывает — pre_flow вообще не вызывается из handlers/*) — старое поведение,
    все согласия модуля."""
    meta = meta or {}
    consent_steps = await get_consent_steps()
    pending_consent_keys = meta.get("pending_consent_keys")
    if pending_consent_keys is not None:
        pending = set(pending_consent_keys)
        consent_steps = [step for step in consent_steps if step.split(":", 1)[1] in pending]
    screens = list(consent_steps)
    is_registered = bool(meta.get("is_registered"))
    event_city = meta.get("event_city") or answers.get("event_city")
    if await should_show_city_fork(event_city, is_registered):
        screens.append("city_fork")
    if await should_show_fork(meta.get("party_track"), meta.get("recovered_track"), is_registered):
        screens.append("party_fork")
    return screens


# ── Pre-flow: выбор города / трека (gap closure фазы 21, D-01/FORM-SYNC-02) ─────────────────
# Тап по кнопке развилки в боте (`handlers/reg_flow.py::party_pick`/`city_pick`) и PATCH из
# приложения (`miniapp/routers/form.py::draft_patch`) проходят ОДНИ и те же проверки и тот же
# `resolve_track` — здесь, а не в двух копиях. Тексты ошибок — прежние литералы бота,
# переехавшие сюда как единый источник (не новый текст).

# Fixed 2-entry map — exact-match only (T-05-01-01: no prefix/startswith/regex matching, so
# no crafted payload can produce a track value outside this closed vocabulary).
PARTY_TAG_MAP = {"party_over": "party_overnight", "party_noover": "party_noovernight"}
# Закрытый словарь кодов, которые может прислать пикер формата участия (веб).
PARTY_TRACK_CODES = ("full", "party_overnight", "party_noovernight")

# Один текст на тап по устаревшей кнопке в боте и на PATCH из приложения.
CITY_CHOICE_INVALID_TEXT = "Некорректный выбор."
CITY_CLOSED_TEXT = "Регистрация на этот город закрыта."
PARTY_CLOSED_TEXT = "Регистрация на вечеринку уже закрыта."


# ── Amb деп-линк (Phase 28, 28-06, SU-05/SU-06/SU-07) ───────────────────────────────────────
# Шестой экстрактор — та же строгость, что `handlers.registration._extract_referrer_id`
# (ASCII-цифры, «сам себя» -> None), но свой префикс "amb_" делает его exact-match
# взаимоисключающим с числовым referrer_id/"src_"-тегом/party-токенами/"city_"-токенами/
# "continue"/"edit" (A-05 CONTEXT, тест-матрица tests/test_cities_phase71.py). Живёт здесь,
# рядом с PARTY_TAG_MAP/_city_tag_map-соседями (handlers/registration.py) — тем же движком,
# что уже обслуживает и бот, и Mini App.
def extract_ambassador_ref(command_args: str | None, current_user_id: int) -> int | None:
    if not command_args:
        return None
    arg = command_args.strip()
    if not (arg.startswith("amb_") and len(arg) > 4):
        return None
    digits = arg[4:]
    if not (digits.isascii() and digits.isdigit()):
        return None
    referrer_id = int(digits)
    if referrer_id == current_user_id:
        return None
    return referrer_id


async def resolve_referrer(referrer_id: int | None) -> int | None:
    """SU-05/SU-07 (T-28-06-01/05): реферер обязан реально существовать в `users` —
    несуществующий/незарегистрированный id идёт по ОБЫЧНОМУ пути без единого сообщения об
    ошибке делегату (ТЗ §3.2: «реферер не найден -> обычный путь»). Тумблер
    `reg_referrer_must_be_ambassador` (дефолт off) ужесточает проверку до `is_ambassador OR
    is_ambassador_candidate` (OQ-2: разная семантика — «стал амбассадором кнопкой после
    анкеты» и «ответил "да" на вопрос-шаг анкеты внутри неё»; любое из двух засчитывается)."""
    if not referrer_id:
        return None
    user = await get_user(referrer_id)
    if not user:
        return None
    if await get_setting_typed("reg_referrer_must_be_ambassador") == "on":
        if not (user.get("is_ambassador") or user.get("is_ambassador_candidate")):
            return None
    return referrer_id


async def resolve_track(candidate: str | None, city_code: str | None = None) -> str:
    """Phase 7 (SHORT-01/CONTEXT.md): resolves the effective track a fresh/resumed
    registration should run under, given a candidate track (deep-link arg or a track already
    recorded in FSM/`reg_started`).

    1. A party track is authoritative no matter what `registration_mode` says — party has
       its own master gate (`party_enabled`) and deep-link vocabulary; this phase must not
       touch that.
    2. Otherwise, `registration_mode == "short"` is a GLOBAL override (user decision,
       CONTEXT.md: "7-10 августа краткая форма для всех новых заявок, полная недоступна").
       It wins even over a `candidate` of "full" recovered from a stale `reg_started` row —
       a delegate who started under the old full form and returns mid-promo gets funneled
       into the promo track too.
    3. Otherwise fall back to whatever `candidate` already says, defaulting to "full". This
       is what makes the promo reversible without stranding in-flight delegates: someone who
       started under "short" and finishes AFTER the manager flips the toggle back keeps
       "short" (step 3 sees a non-None candidate and never reaches step 2's mode check).

    Phase 09.2-04 (CITY-04/CONTEXT B): step 2 now reads `registration_mode` through
    `cities.get_setting_typed_for_city`, so a manager can flip the short/full toggle for one
    city without affecting every other city — `city_code=None` (the default) collapses to
    the exact global read this function always used. The caller resolves the city, not this
    function (RESEARCH Pitfall 3) — by the time `_resolve_track` runs, the delegate's city is
    already known by construction (07.1: welcome -> CITY -> track fork), so re-deriving it
    here would risk a second resolution disagreeing with the caller's own."""
    if _is_party_track(candidate):
        return candidate
    if await get_setting_typed_for_city("registration_mode", city_code) == SHORT_TRACK:
        return SHORT_TRACK
    return candidate or "full"


async def party_track_options() -> list[dict]:
    """Варианты пикера формата участия — тот же список и те же подписи, что у кнопок
    `_party_fork_kb` бота (`reg_options.PARTY_TRACK_OPTIONS`)."""
    return [{"code": code, "label": label} for code, label in _opts.PARTY_TRACK_OPTIONS]


async def city_fork_options() -> list[dict]:
    """Варианты пикера города — по включённым городам в порядке CITIES, подпись из
    `city_label` (per-city override менеджера): ровно то, что строит `_city_fork_kb` бота."""
    return [{"code": c["code"], "label": await city_label(c["code"])} for c in await enabled_cities()]


async def validate_city_choice(code) -> tuple[str | None, str | None]:
    """Порядок проверок — как в `city_pick`: закрытый словарь CITIES, затем `is_city_enabled`
    (окно «нарисовали — выключили»)."""
    if code not in city_codes():
        return None, CITY_CHOICE_INVALID_TEXT
    if not await is_city_enabled(code):
        return None, CITY_CLOSED_TEXT
    return code, None


async def validate_track_choice(code, city_code: str | None = None) -> tuple[str | None, str | None]:
    """Ровно то, что делает `party_pick` -> `_start_registration_flow` -> `resolve_track`:
    закрытый словарь кодов; вечеринка — только при `party_enabled == on`; «Полная регистрация»
    (`full`) означает «кандидата нет» и уходит в `resolve_track(None, city)` — так глобальный
    `registration_mode == short` перебивает выбор, как у бота."""
    if code not in PARTY_TRACK_CODES:
        return None, CITY_CHOICE_INVALID_TEXT
    if _is_party_track(code) and await get_setting_typed("party_enabled") != "on":
        return None, PARTY_CLOSED_TEXT
    return await resolve_track(None if code == "full" else code, city_code), None


# ── Веб-контракт: allowlist колонок, спека шага, спека формы ────────────────────────────────

_EXTRA_ANSWER_COLUMNS = ["resume_file_id", "resume_file_name", "resume_text"]

# Quick 260910-wb6: колонки анкеты, у которых НЕТ колонки в `users` — существуют только чтобы
# доехать до `post_finalize` (расширение файла резюме для Некстклауда, `services/reg_finalize.py`),
# ни `add_user`, ни `update_user_answers` их не пишут (тот же факт уже задокументирован
# `tests/test_skillup_core_28.py::_NOT_IN_ADD_USER`). `diff()` ниже обязан их пропускать —
# иначе правка анкеты с файловым резюме считает их «изменившимися» и ловит
# `sqlite3.OperationalError: no such column: resume_file_name` на узком UPDATE финала.
DRAFT_ONLY_COLUMNS = ("resume_file_name",)


def columns_for_step(step_key: str | None) -> list[str]:
    """Колонки-компаньоны шага анкеты — набор, который `_sync_draft_in`/`_sync_draft_out`
    (`handlers/registration.py`) обязаны читать/писать ОДНИМ патчем, не одной колонкой.

    Прод-баг (с 05.09, квик 260910-wb6): `STEP_TO_COLUMN["resume"] == "resume_text"` — при
    ответе файлом в чате в черновик уходила ровно эта колонка (`{"resume_text": None}"`), а
    `resume_file_id`/`resume_file_name` жили только в FSM и терялись на финале, который читает
    `reg_drafts`, а не FSM. Для шага «resume» набор — все три колонки резюме сразу
    (`_EXTRA_ANSWER_COLUMNS`, тот же список, без второй копии литералами: делегат мог ответить
    файлом ИЛИ текстом, черновик обязан унести оба варианта). Для остальных шагов — как и
    раньше, ровно одна колонка `STEP_TO_COLUMN[step_key]`. Неизвестный/пустой `step_key` ->
    пустой список (нечего синхронизировать)."""
    if not step_key:
        return []
    if step_key == "resume":
        return list(_EXTRA_ANSWER_COLUMNS)
    column = STEP_TO_COLUMN.get(step_key)
    return [column] if column else []


def answer_columns() -> list[str]:
    """Allowlist колонок анкеты — веб-процесс валидирует PATCH-запросы черновика по этому
    списку (RESEARCH Pattern 2); бот его не использует."""
    seen: list[str] = []
    for col in STEP_TO_COLUMN.values():
        if col not in seen:
            seen.append(col)
    for col in _EXTRA_ANSWER_COLUMNS:
        if col not in seen:
            seen.append(col)
    return seen


_COLUMN_TO_STEP: dict[str, str] = {}
for _step_key, _col in STEP_TO_COLUMN.items():
    _COLUMN_TO_STEP.setdefault(_col, _step_key)
# Quick 260904-aup (D6): legacy-алиас для делегата, у которого приложение уже открыто со
# старой спекой шага (колонка "resume") в момент деплоя фикса выше — PATCH со старым именем
# колонки не должен ловить 400 bad_field. Новые ответы всегда идут в "resume_text".
_COLUMN_TO_STEP.setdefault("resume", "resume")


def column_to_step(column: str) -> str | None:
    return _COLUMN_TO_STEP.get(column)


# UI-SPEC § «Form Components → По типу»: text/textarea/phone/email/int/date/choice-chips/
# select/multi/yesno/file/consent. REG_STEP_TYPES само по себе слишком грубое (много
# "text"-шагов на деле рисуются chip-кнопками в боте) — здесь считаем настоящий UI-тип.
_UI_TYPE_OVERRIDES = {
    "age": "int",
    "email": "email",
    "phone": "phone",
    "resume": "file",
    "work_status": "yesno",
    "needs_certificate": "yesno",
    "volunteer": "yesno",
    "ambassador": "choice-chips",
    # Phase 28 (28-01, SU-01, СкиллАп 5): case_optin — жёсткие «Да»/«Нет», как work_status.
    "case_optin": "yesno",
    # Phase 28 (28-04, SU-04): R2b — новый под-тип поля `url` (Component Contracts §2
    # 28-UI-SPEC.md), соседствует с существующими text/phone/email.
    "resume_link": "url",
}
_TEXTAREA_STEPS = {"expectations", "comments", "mini_projects"}


def _ui_type_for(step_key: str, step_type: str) -> str:
    if step_key in _UI_TYPE_OVERRIDES:
        return _UI_TYPE_OVERRIDES[step_key]
    if step_type == "date":
        return "date"
    if step_type == "select":
        return "select"
    if step_type == "multi":
        return "multi"
    if step_key in _LITERAL_OPTIONS or step_key in SELECT_CONFIG or step_key == "source":
        return "choice-chips"
    if step_key in _TEXTAREA_STEPS:
        return "textarea"
    return "text"


# Phase 30 (30-01, A2-01): вторая ось типа шага — «Анкета 2.0». `_ui_type_for` выше отвечает
# «каким HTML-контролом рисовать поле» (text/select/choice-chips/...), эта ось отвечает «какой
# ЦЕЛЬНЫЙ тип шага это с точки зрения нового дизайна» (select/lookup/composite/link/multi/
# repeatable/text, A2-01) — то, чем управляют девять тумблеров «📝 Анкета» ниже. Обе оси живут
# в spec ОДНОВРЕМЕННЕ (`spec["type"]` не тронут ни одним символом, Pitfall 1 30-RESEARCH.md) —
# `step_type_v2()`/`spec["kind"]` читает только новый фронт (план 30-03+) и `degrade_kind()`;
# сегодняшний бот и сегодняшний Mini App продолжают читать `spec["type"]` как раньше.
_STEP_TYPE_V2_OVERRIDES = {
    # ВУЗ/город — свой справочник с поиском (A2-03), не общий choice-chips список.
    "university": "lookup",
    "city": "lookup",
    # Ссылки — карточка с иконкой сервиса и распознаванием формата (A2-06).
    "vk": "link",
    "resume_link": "link",
    # Опыт и проекты — единственный потребитель repeatable-блоков в этой фазе (A2-05).
    "mini_portfolio": "repeatable",
    # Явный override, а не «упало через default»: `phone` в новой анкете получает встроенную
    # кнопку `requestContact` (30-UI-SPEC.md §7), но остаётся типом `text`, а не отдельным
    # типом — решение оркестратора 12.09 (30-CONTEXT.md «Пять тумблероподобных подписей»).
    "phone": "text",
}

# Единственная composite-группа фазы — «Образование» (условная развилка reg_engine.py:467-624,
# читать `enabled_steps`/`_is_step_enabled` для контекста веток). Формат — на случай, если
# будущая фаза заведёт вторую композитную группу: имя группы -> список входящих step_key.
_COMPOSITE_GROUPS: dict[str, list[str]] = {
    "education": ["education_status", "university", "course", "study_field"],
}


def composite_group_of(step_key: str) -> str | None:
    """Имя composite-группы шага (сегодня только `"education"`) — `None`, если шаг ни в какой
    группе не состоит. `university` состоит и в группе, и в `_STEP_TYPE_V2_OVERRIDES` — это НЕ
    противоречие: `step_type_v2("university")` всё равно вернёт `"lookup"` (override проверяется
    первым), а эта функция отдельно отвечает на вопрос «чьей карточки часть», не «как рисовать»
    (нужно `composite`-рендеру, чтобы найти под-поле ВУЗ внутри карточки «Образование»)."""
    for group_name, group_steps in _COMPOSITE_GROUPS.items():
        if step_key in group_steps:
            return group_name
    return None


# Шаг-тумблер группы («Сейчас учусь здесь» — часть карточки, не отдельный вопрос снаружи неё).
# `<interfaces>` 30-04-PLAN.md называет его `toggle_step` в `spec["composite"]`.
_COMPOSITE_TOGGLE_STEP: dict[str, str] = {"education": "education_status"}


def composite_card_group(step_key: str, flags: dict[str, bool]) -> str | None:
    """Имя группы, которую НОВАЯ анкета показывает ОДНОЙ карточкой (и потому проходит целиком
    за один ответ), либо `None` — когда карточка выключена (`reg_form_edu_card`/мастер-тумблер)
    или шаг вообще не состоит в группе. Правило деградации не переписывается — спрашиваем
    ту же `degrade_kind`, что и обе поверхности (T-30-02)."""
    if degrade_kind("composite", flags) != "composite":
        return None
    return composite_group_of(step_key)


def composite_absorbed_steps(flags: dict[str, bool]) -> dict[str, str]:
    """Приёмка 15.09 (п.3б «дальше почему-то пошёл вопрос про вуз», п.6 «сбита нумерация»):
    шаги, которые карточка рисует ВНУТРИ себя, — `step_key -> step_key карточки`. Карточка
    «Образование» показывает ВУЗ, курс и программу своими строками, поэтому те же шаги не
    должны появляться в мастере ещё раз отдельными экранами (и попадать в знаменатель «шаг N
    из M»). Тумблер группы (`education_status`) — и есть шаг-карточка, он остаётся.

    Пустой словарь, когда карточка выключена: тогда все четыре шага идут как сегодня, по
    одному (30-UI-SPEC.md § «3. composite» → «Деградация»)."""
    if degrade_kind("composite", flags) != "composite":
        return {}
    absorbed: dict[str, str] = {}
    for group_name, group_steps in _COMPOSITE_GROUPS.items():
        toggle = _COMPOSITE_TOGGLE_STEP.get(group_name)
        if toggle is None or toggle not in group_steps:
            continue
        for step_key in group_steps:
            if step_key != toggle:
                absorbed[step_key] = toggle
    return absorbed


def advance_anchor(step_key: str, enabled: list[str], flags: dict[str, bool]) -> str:
    """Шаг, ОТ которого искать следующий вопрос после ответа на `step_key`. Обычно это сам
    `step_key`; для карточки-композита — её ПОСЛЕДНЯЯ включённая часть: одним ответом делегат
    закрыл всю группу разом, и следующий вопрос — тот, что идёт за группой, а не её же ВУЗ
    (приёмка 15.09, п.3б). Карточка выключена -> `step_key` без изменений."""
    group = composite_card_group(step_key, flags)
    if group is None:
        return step_key
    members = [key for key in enabled if key in _COMPOSITE_GROUPS.get(group, [])]
    return members[-1] if members else step_key


async def composite_parts(
    group: str, participant_type: str | None = None, event_city: str | None = None,
) -> list[str]:
    """Список step_key группы, реально включённых у СОБЫТИЯ (контракт `<interfaces>`
    30-04-PLAN.md), в порядке `REG_FLOW`. Карточка рисуется всегда, из включённых частей
    (30-CONTEXT.md реш. 2) — «включён» здесь значит «тумблер `reg_q_*` этого шага включён у
    трека/города», а НЕ «делегат ответил на тумблер образования утвердительно»: composite_parts
    вызывается из `step_spec()`, у которого нет доступа к текущим ответам делегата (`answers`),
    только к треку/городу (T-30-04-01) — ветвление по значению «учусь/не учусь» делает СОБСТВЕННО
    composite-карточка на клиенте (скрывает/показывает уже включённые части), это её работа, не
    этой функции. Поэтому composite_parts НЕ зовёт `enabled_steps()` целиком (та ветвится по
    `education_status`/`edu_conditional`) — только структурный гейт `is_step_enabled_for_track`,
    тот же, каким сегодня фильтруется весь REG_FLOW."""
    group_steps = _COMPOSITE_GROUPS.get(group)
    if not group_steps:
        return []
    group_set = set(group_steps)
    out = []
    for step_key, setting_key, _step_type in REG_FLOW:
        if step_key not in group_set:
            continue
        if await is_step_enabled_for_track(setting_key, participant_type, event_city):
            out.append(step_key)
    return out


async def validate_composite(
    group: str, answers: dict, *, participant_type: str | None = None, event_city: str | None = None,
) -> dict[str, str]:
    """Валидация КАРТОЧКИ по частям (30-UI-SPEC.md § «3. composite» → «Состояния»): словарь
    `step_key -> текст ошибки`, пустой словарь значит «карточка валидна целиком». Ошибка одной
    части не мешает проверить остальные (в отличие от `validate_answer`, который возвращает
    ПЕРВУЮ ошибку и останавливается) — каждая часть карточки должна светить СВОЮ ошибку
    независимо от соседних.

    Выключенный тумблер (`education_status` не «учится», `is_studying`) исключает под-шаги
    ВУЗ/курс/программа из проверки целиком — как если бы их не было у события (тот же принцип,
    что `enabled_steps`/`apply_answer` уже применяют для этой группы, T-30-04-01: композит не
    заводит второе правило «когда спрашивать образование», использует то же самое)."""
    parts = await composite_parts(group, participant_type, event_city)
    toggle_step = _COMPOSITE_TOGGLE_STEP.get(group)
    studying = True
    if toggle_step and toggle_step in parts:
        studying = is_studying(answers.get(toggle_step, ""), await studying_statuses())
    errors: dict[str, str] = {}
    for step_key in parts:
        if step_key == toggle_step:
            continue
        if not studying:
            continue
        _value, error = validate_answer(step_key, answers.get(step_key), participant_type=participant_type)
        if error:
            errors[step_key] = error
    return errors


def step_type_v2(step_key: str) -> str:
    """Тип шага по новой оси (A2-01) — одно из `select`/`lookup`/`composite`/`link`/`multi`/
    `repeatable`/`text`. Порядок вывода дословно из таблицы 30-RESEARCH.md Pattern 1 (verified
    чтением REG_FLOW/`_ui_type_for`, не догадка): override -> composite-группа -> `_ui_type_for`
    (choice-chips/select/yesno -> select, multi -> multi) -> text. Чистая функция (без похода в
    БД) — дефолт по сегодняшнему поведению шага, ничего не спрашивает у тумблеров (это отдельно
    делает `degrade_kind` ниже, читая `form_v2_flags()`)."""
    if step_key in _STEP_TYPE_V2_OVERRIDES:
        return _STEP_TYPE_V2_OVERRIDES[step_key]
    if composite_group_of(step_key) is not None:
        return "composite"
    step_type = REG_STEP_TYPES.get(step_key, "text")
    ui_type = _ui_type_for(step_key, step_type)
    if ui_type in ("choice-chips", "select", "yesno"):
        return "select"
    if ui_type == "multi":
        return "multi"
    return "text"


# ── repeatable: формат хранения + двуформатное чтение (Phase 30, 30-04, A2-05) ──────────────
# Владелец 12.09 (30-CONTEXT.md § «Решения по итогам 30-RESEARCH.md», п. 1) зафиксировал формат
# блока `{"title": …, "description": …}` — JSON-список в ТОЙ ЖЕ колонке (`mini_portfolio`), без
# миграции данных: старый свободный текст читается как legacy и оборачивается в один блок без
# заголовка. Единственная точка правды для ОБОИХ форматов — четыре функции ниже; ни один
# читатель (лист/карточка заявки/сводка чата/обзор Mini App) не получает сырой JSON на глаза
# человеку (T-30-09 threat register).
_REPEATABLE_TITLE_MAX_LEN = 200
# Консервативный дефолт барьера DoS (T-30-10), если вызывающий (async-контекст) не передал
# реальный `await repeatable_max(step_key)` в `validate_answer` — второй барьер не должен
# исчезать только потому, что кто-то забыл прокинуть лимит из реестра (тот же приём, каким
# `_DEFAULT_MULTI_LIMIT_ERROR_TEXT` подстраховывает multi-лимит).
_REPEATABLE_MAX_FALLBACK = 20


def _normalize_repeatable_item(item) -> dict:
    """Один блок к каноническому виду `{"title": str, "description": str}` — не-словарь (битый
    JSON-элемент/старый мусор) считается блоком без заголовка, тот же приём, что и legacy-текст
    целиком ниже."""
    if isinstance(item, dict):
        return {
            "title": str(item.get("title") or ""),
            "description": str(item.get("description") or ""),
        }
    return {"title": "", "description": str(item)}


def parse_repeatable(raw) -> list[dict]:
    """Разбор ОБОИХ форматов колонки `mini_portfolio` (контракт 30-04-PLAN.md `<interfaces>`):
    JSON-список -> список блоков как есть; НЕ JSON-список (в т.ч. битый JSON, обычный старый
    текст делегата) -> один блок без заголовка (`{"title": "", "description": raw}`); пусто/`-`
    -> пустой список. `raw` может прийти уже готовым списком (Mini App шлёт `onChange` массивом
    объектов, `validate_answer` ниже пропускает его сюда же, не через `json.loads`) — второй
    ветки разбора для этого случая не заводим, просто нормализуем элементы."""
    if isinstance(raw, list):
        return [_normalize_repeatable_item(item) for item in raw]
    text = str(raw or "").strip()
    if not text or text == "-":
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [_normalize_repeatable_item(item) for item in parsed]
    # Legacy: свободный текст ДО этой фазы (или ответ из чата, план 30-06, который присылает
    # голую строку) — один блок без заголовка, без миграции данных (30-CONTEXT.md).
    return [{"title": "", "description": text}]


def dump_repeatable(items: list[dict]) -> str:
    """Единственная точка сериализации (контракт `<interfaces>`) — JSON-список, кириллица не
    экранируется (`ensure_ascii=False`, читаемость сырой колонки в БД/логах). Пустой список ->
    `"[]"` (не `"-"`) — `parse_repeatable("[]")` даёт обратно пустой список, круглая совместимость
    важнее сохранения старого литерала прочерка для НОВОГО формата (прочерк остаётся только у
    легаси-пути, `validate_answer` ниже)."""
    return json.dumps([_normalize_repeatable_item(item) for item in (items or [])], ensure_ascii=False)


def repeatable_display(items: list[dict]) -> str:
    """Человекочитаемая строка для листа Гугл и карточки заявки менеджера (30-UI-SPEC.md §6):
    «Название — описание; Название — описание» — разделитель «;», та же конвенция, что
    `handlers/admin_settings_lists.py::split_list_items` (ловушка Enter=send в Телеграме).
    Блок без описания не роняется (title-only строка); блок без заголовка отдаёт голое
    описание — это ЛЕГАСИ-БЛОК (свободный текст до этой фазы), тот же текст, что видел
    менеджер до плана 30-04."""
    parts = []
    for item in items or []:
        title = str((item or {}).get("title") or "").strip()
        description = str((item or {}).get("description") or "").strip()
        if title and description:
            parts.append(f"{title} — {description}")
        elif title:
            parts.append(title)
        elif description:
            parts.append(description)
    return "; ".join(parts)


async def repeatable_max(step_key: str) -> int:
    """Максимум блоков (контракт `<interfaces>`) — ЧИСЛО в реестре (`reg_repeatable_max_<step>`),
    не тумблер (30-CONTEXT.md A2-05). В отличие от `multi_max` (дефолт `None` = без лимита)
    здесь дефолт ОБЯЗАН быть конкретным числом (`SETTINGS_SCHEMA["default"]`) — «без лимита» на
    повторяемый JSON-список делегата открыл бы T-30-10 (DoS бесконечными блоками), а не просто
    менял бы визуал."""
    return await get_setting_typed(f"reg_repeatable_max_{step_key}")


# Обратная карта «колонка -> это repeatable-формат» — считается из `step_type_v2()`, а не
# хардкодит имя `mini_portfolio` литералом: второй repeatable-шаг будущей фазы подхватится
# сюда автоматически, без правки мест чтения ниже (`summary_fields`/`_published_value`).
_REPEATABLE_COLUMNS: set[str] = {
    STEP_TO_COLUMN[step_key] for step_key, _sk, _t in REG_FLOW if step_type_v2(step_key) == "repeatable"
}

# Подстановка `{noun}` в общий шаблон `reg_repeatable_add_button_text` (30-UI-SPEC.md §
# «composite "Образование"»: «общий шаблон, {noun} задаёт конкретный шаг») — по step_key,
# единственный repeatable-потребитель этой фазы: «Опыт и проекты».
_REPEATABLE_NOUNS: dict[str, str] = {"mini_portfolio": "проект"}


# Phase 30 (30-01, A2-08): девять тумблеров группы «📝 Анкета» — имена без префикса `reg_form_`
# (сам префикс добавляет `form_v2_flags` при чтении реестра), порядок — как в артборде 13
# (мастер первым). Единственное место, откуда обе поверхности (Mini App/чат) читают набор имён —
# `settings_schema.py`/`handlers/admin_reg_form.py` строят свои списки из тех же девяти строк.
FORM_V2_TOGGLE_KEYS = (
    "v2_enabled", "chips", "lookup_search", "edu_card", "repeatable",
    "limit_counter", "status_screen", "header_settings", "haptics",
)


async def form_v2_flags(event_city: str | None = None) -> dict[str, bool]:
    """Девять тумблеров «📝 Анкета» одним словарём `{имя_без_префикса: bool}` — контракт для
    `degrade_kind` ниже и для планов 30-03..30-08, которые ничего не резолвят из реестра сами
    (единая точка чтения). `reg_form_*` — обычные реестровые ключи БЕЗ трекового суффикса (как
    `reg_resume_mode` выше), поэтому общий резолвер `get_setting_typed_for_city` уместен
    напрямую. Дефолт каждого ключа — `"off"` (SETTINGS_SCHEMA) — пустой реестр отдаёт девять
    `False`, а `degrade_kind` при всех `False` отдаёт `"legacy"` для любого типа: делегат не
    видит ничего нового, пока менеджер явно не включит хотя бы мастер-тумблер (acceptance этого
    плана — GOLDEN не сдвинут)."""
    return {
        name: await get_setting_typed_for_city(f"reg_form_{name}", event_city) == "on"
        for name in FORM_V2_TOGGLE_KEYS
    }


def degrade_kind(kind: str, flags: dict[str, bool]) -> str:
    """Единственное место с правилами деградации типа при выключенных тумблерах (T-30-02 threat
    register — обе поверхности зовут эту функцию и не копируют правила). Таблицы — дословно из
    `30-UI-SPEC.md` § «По типу шага» → «Деградация» каждого типа. Возвращает канонический тип
    (шаг рисуется как есть) либо `"legacy"` («рисуй как сегодня» — весь путь до-фазы-30, чат и
    Mini App это уже умеют без единой новой строчки кода)."""
    if not flags.get("v2_enabled"):
        # 30-UI-SPEC.md §1 «select» / общее правило: выключенный master switch откатывает
        # ЛЮБОЙ тип к сегодняшнему поведению обеих поверхностей — единственная деградация,
        # применяющаяся раньше любого более узкого тумблера.
        return "legacy"
    if kind == "lookup":
        # 30-UI-SPEC.md §2: оба тумблера выключены -> голое текстовое поле («Впиши {entity}»).
        if not flags.get("chips") and not flags.get("lookup_search"):
            return "text"
        return "lookup"
    if kind == "multi":
        # 30-UI-SPEC.md §5: без чипов остаётся только свободный ввод с лимитом — фактический
        # откат к `text` (лимит остаётся подсказкой, не структурой).
        if not flags.get("chips"):
            return "text"
        return "multi"
    if kind == "composite":
        # 30-UI-SPEC.md §3: без карточки — 4 классических шага (архитектура шага меняется
        # целиком, не только визуал) — четыре шага, а не «урезанная карточка».
        if not flags.get("edu_card"):
            return "legacy"
        return "composite"
    if kind == "repeatable":
        # 30-UI-SPEC.md §6: без повторяемости — один блок максимум (сегодняшнее поведение).
        if not flags.get("repeatable"):
            return "text"
        return "repeatable"
    # select/link/text (30-UI-SPEC.md §1/§4/§7): собственного под-тумблера нет — деградация
    # только через master switch, уже обработанный выше.
    return kind


# Phase 30 (30-01, A2-01): паритет проекций — ЯВНАЯ таблица «тип -> модуль, который умеет
# нарисовать этот шаг ЦЕЛИКОМ», а не grep произвольного токена (T-30-02 threat register).
# Для «легаси»-типов (select/multi/link/text — сегодня уже отрисованы существующим
# агрегатором inline-кнопками бота/полями Mini App) значение — сегодняшний агрегатор, а не
# новый шов: это письменная фиксация факта, а не предположение (нет отдельного файла для
# того, что уже работает). `lookup`/`composite`/`repeatable` получают выделенный шов — они не
# существовали до этой фазы вообще.
CHAT_PROJECTION: dict[str, str] = {
    "select": "handlers.registration",
    "multi": "handlers.registration",
    "text": "handlers.registration",
    "link": "handlers.registration",
    "lookup": "handlers.reg_types_lookup",
    "composite": "handlers.reg_types_composite",
    "repeatable": "handlers.reg_types_repeatable",
}

# Правка плана 30-03 (задача 4) к решению 30-01: черновик 30-01 предполагал, что рестайл
# select/multi/link/text приземлится ВНУТРИ существующего `form.js::buildControl` (новыми
# ветками того же switch). Интерфейс, зафиксированный 30-03-PLAN.md (`buildV2Control` с
# литеральными `case "select"`/`"multi"`/`"link"`/`"text"`), кладёт эти ветки в НОВЫЙ модуль
# `form_types.js` — `form.js::buildControl` делает только ранний выход на `buildV2Control`,
# сам литерал `case "<тип>"` живёт в form_types.js. Обновляем проекцию под фактическую
# реализацию (единственный источник правды для сторожа паритета — этот файл, не то, что
# предполагалось на момент 30-01).
APP_PROJECTION: dict[str, str] = {
    "select": "miniapp/static/js/form_types.js",
    "multi": "miniapp/static/js/form_types.js",
    "text": "miniapp/static/js/form_types.js",
    "link": "miniapp/static/js/form_types.js",
    "lookup": "miniapp/static/js/form_types.js",
    "composite": "miniapp/static/js/form_types.js",
    "repeatable": "miniapp/static/js/form_types.js",
}

# Phase 30 (30-01, A2-01): временные заглушки паритета — тип, у которого проекция из таблиц
# выше ещё физически не написана (модуль не существует / в файле фронта нет ветки `case`).
# Сторож `tests/test_reg_step_type_v2_260912.py::test_step_type_v2_has_both_projections`
# пропускает импорт/проверку файла для типов из этого списка — КАЖДАЯ запись обязана называть
# план, который её снимает (сам сторож это проверяет: пустой комментарий — красный тест).
# Пусто с плана 30-06 (задача 4): `handlers/reg_types_lookup.py`/`reg_types_composite.py`/
# `reg_types_repeatable.py` заведены, `form_types.js` уже нёс их `case` с плана 30-04 — сторож
# паритета (`test_step_type_v2_has_both_projections`) теперь требует обе проекции для ВСЕХ
# семи типов, ни одного пропуска не осталось.
PENDING_PROJECTIONS: dict[str, str] = {}


# Phase 30 (30-03, A2-02, 30-UI-SPEC.md § «1. select»): пояснения к вариантам плитки select —
# вторая строка под названием варианта. Явная карта (значение -> ключ реестра), НЕ генерация
# имени ключа шаблоном из сырого значения: значения `_LITERAL_OPTIONS`/`SELECT_CONFIG`
# содержат пробелы/запятые («Ни то, ни другое»), небезопасные как суффикс имени настройки.
# Заведена сегодня ТОЛЬКО для `alumni_status` — макет (`30-UI-SPEC.md` § Copywriting Contract
# «select») рисует пояснение именно для него; тиражировать этот приём на остальные девятнадцать
# select-шагов (~70 пар значение/пояснение) означало бы завести ключи под текст, которого
# макет никогда не рисовал — пустые дефолты без визуального образца не проверить глазами.
# Расширение на другой шаг — просто новая запись здесь + ключ в SETTINGS_SCHEMA.
_OPTION_HINT_KEYS: dict[str, dict[str, str]] = {
    "alumni_status": {
        "Аламни": "reg_option_hint__alumni_status__alumni",
        "Айсекер": "reg_option_hint__alumni_status__aiesecer",
        "Ни то, ни другое": "reg_option_hint__alumni_status__neither",
    },
}


async def option_hints_for(step_key: str) -> dict[str, str]:
    """Словарь `{значение_варианта: пояснение}` для плитки select — пустой для любого шага
    вне `_OPTION_HINT_KEYS` (fail-soft: `form_types.js` просто не рисует вторую строку
    плитки, `KeyError` невозможен ни на клиенте, ни здесь). Пустой текст (менеджер стёр
    пояснение через настройки) тоже не попадает в результат — та же логика, что у любого
    optional-хинта в этом файле."""
    mapping = _OPTION_HINT_KEYS.get(step_key)
    if not mapping:
        return {}
    result: dict[str, str] = {}
    for value, setting_key in mapping.items():
        text = await get_setting_typed(setting_key)
        if text:
            result[value] = text
    return result


# Phase 30 (30-03, A2-02/A2-03/A2-06, 30-UI-SPEC.md § Copywriting Contract): `{entity}` —
# человеческое имя справочника lookup, подставляется в `reg_form_own_option_text`/
# `reg_form_own_chip_text` (тексты объявлены «переиспользуемыми» в UI-SPEC — единственные
# сегодняшние потребители lookup-типа). Явная карта, а не производная от `spec.label`
# (`label_for()` отдаёт формулировку вопроса целиком — «Из какого ты города?», не короткое
# существительное для подстановки в середину фразы).
_LOOKUP_ENTITY_NAMES: dict[str, str] = {"university": "ВУЗ", "city": "город"}


async def _v2_texts_for(degraded_kind: str, step_key: str, event_city: str | None) -> dict:
    """Тексты новых типов шага (30-UI-SPEC.md § Copywriting Contract), публикуются В
    `spec["v2_texts"]` — ОДИН словарь, а не двадцать полей верхнего уровня (30-03-PLAN.md
    задача 4). Читает реестр ТОЛЬКО для типа, который реально рисуется (`degraded_kind`) —
    легаси-шаг (`degraded_kind == "legacy"`) не тратит ни одного похода в БД на тексты, которые
    всё равно не попадут на экран. Ключ словаря — короткое имя без `reg_..._text`-обвязки
    (`form_types.js` читает `texts.pick_option`, не `texts.reg_form_pick_option_text`)."""
    if degraded_kind == "legacy":
        return {}
    texts: dict[str, str] = {}
    if degraded_kind == "select":
        texts["pick_option"] = await get_setting_typed("reg_form_pick_option_text")
        texts["selection_visible_note"] = await get_setting_typed(
            "reg_form_selection_visible_note_text"
        )
    elif degraded_kind == "lookup":
        entity = _LOOKUP_ENTITY_NAMES.get(step_key, "")
        own_option = await get_setting_typed("reg_form_own_option_text")
        own_chip = await get_setting_typed("reg_form_own_chip_text")
        texts["own_option"] = own_option.replace("{entity}", entity)
        texts["own_chip"] = own_chip.replace("{entity}", entity)
        texts["hint_default"] = await get_setting_typed("reg_lookup_hint_default_text")
        texts["empty_title"] = await get_setting_typed("reg_lookup_empty_title_text")
        texts["normalized_note"] = await get_setting_typed("reg_lookup_normalized_note_text")
    elif degraded_kind == "multi":
        texts["skip_button"] = await get_setting_typed("reg_form_skip_button_text")
        texts["continue_button"] = await get_setting_typed("reg_form_continue_button_text")
        texts["limit_hint_zero"] = await get_setting_typed_for_city(
            "reg_multi_limit_hint_zero_text", event_city
        )
        texts["limit_hint_mid"] = await get_setting_typed_for_city(
            "reg_multi_limit_hint_mid_text", event_city
        )
        texts["limit_hint_max"] = await get_setting_typed_for_city(
            "reg_multi_limit_hint_max_text", event_city
        )
        # `reg_form_own_option_text` НЕ публикуется здесь: это целое предложение с `{entity}`
        # для пустого состояния lookup (см. ветку выше), не короткий placeholder поля «свой
        # вариант» multi — Copywriting Contract не заводит отдельного ключа под это поле
        # (30-UI-SPEC.md § «5. multi» не перечисляет его текстом, только иконку `+`).
    elif degraded_kind == "link":
        texts["recognized"] = await get_setting_typed_for_city(
            "reg_link_recognized_text", event_city
        )
        if step_key == "resume_link":
            texts["resume_hint"] = await get_setting_typed_for_city(
                "reg_link_resume_hint_text", event_city
            )
    elif degraded_kind == "text" and step_key == "phone":
        # 30-UI-SPEC.md § «7. text»: кнопка «Поделиться номером» — постоянное поведение шага
        # `phone` в новой анкете (решение оркестратора 12.09, без отдельного тумблера);
        # рестайл (`screens/form.js::shareContactButton`) читает эти два текста, только когда
        # `degraded_kind != "legacy"` — сегодняшний `reg_form_share_contact_text` не трогаем.
        texts["phone_share_button"] = await get_setting_typed_for_city(
            "reg_phone_share_button_text", event_city
        )
        texts["phone_share_hint"] = await get_setting_typed_for_city(
            "reg_phone_share_hint_text", event_city
        )
    elif degraded_kind == "composite":
        texts["subtitle"] = await get_setting_typed_for_city(
            "reg_composite_edu_subtitle_text", event_city
        )
        texts["toggle_on_label"] = await get_setting_typed_for_city(
            "reg_composite_edu_toggle_on_label", event_city
        )
        texts["toggle_off_label"] = await get_setting_typed_for_city(
            "reg_composite_edu_toggle_off_label", event_city
        )
        texts["toggle_on_hint"] = await get_setting_typed_for_city(
            "reg_composite_edu_toggle_on_hint", event_city
        )
        texts["toggle_off_hint"] = await get_setting_typed_for_city(
            "reg_composite_edu_toggle_off_hint", event_city
        )
        texts["done_hint"] = await get_setting_typed_for_city(
            "reg_composite_edu_done_hint_text", event_city
        )
        texts["toggle_note"] = await get_setting_typed("reg_composite_toggle_note_text")
    elif degraded_kind == "repeatable":
        texts["item_label"] = await get_setting_typed_for_city(
            "reg_repeatable_item_label_text", event_city
        )
        texts["edit_action"] = await get_setting_typed("reg_repeatable_edit_action_text")
        texts["chat_parity_note"] = await get_setting_typed("reg_repeatable_chat_parity_note_text")
        add_button = await get_setting_typed_for_city("reg_repeatable_add_button_text", event_city)
        texts["add_button"] = add_button.replace("{noun}", _REPEATABLE_NOUNS.get(step_key, ""))
    return texts


# Права/безопасность формы (RESEARCH § «Права / безопасность формы»): ФИО 200,
# expectations/comments/resume(текст) 4000, остальной текст 1000. Бот сегодня лимита не
# применяет (summary режется по 4096 Telegram-лимиту отдельно) — эти константы предназначены
# для будущего веб-валидатора PATCH-черновика (план 21-03), step_spec их только публикует.
MAX_LEN_DEFAULT = 1000
MAX_LEN_LONG = 4000
MAX_LEN_FULL_NAME = 200
_LONG_TEXT_STEPS = {"expectations", "comments", "resume"}

# Phase 28 (28-01, SU-04, СкиллАп 5): персональные лимиты длины — читается ПЕРВЫМ в
# _max_len_for (лимиты действуют и в боте, validate_answer применяет _max_len_for уже сегодня).
_MAX_LEN_OVERRIDES = {"mini_projects": 500, "mini_direction": 200}


def _max_len_for(step_key: str, ui_type: str) -> int | None:
    if step_key in _MAX_LEN_OVERRIDES:
        return _MAX_LEN_OVERRIDES[step_key]
    if step_key == "full_name":
        return MAX_LEN_FULL_NAME
    if step_key in _LONG_TEXT_STEPS:
        return MAX_LEN_LONG
    # Phase 28 (28-04, SU-04, T-28-04-04): "url" (resume_link) делит лимит с "text" — тот же
    # DoS-барьер, что был у него ДО смены ui_type на "url" (28-01 регистрировал resume_link как
    # обычный "text"-шаг); смена типа поля не должна снимать существовавшую защиту длины.
    if ui_type in ("text", "textarea", "phone", "email", "url"):
        return MAX_LEN_DEFAULT
    return None


# Шаги, у которых бот сегодня показывает клавиатуру "Пропустить" (get_skip_kb) — required=False.
_SKIP_ALLOWED_STEPS = {
    "specialty", "work_sphere", "missing_skills", "expectations", "comments", "food_pref",
    "bed_partner", "cc_shop", "exp_organizers", "exp_content", "allergies",
    # Phase 28 (28-01, SU-04, СкиллАп 5): «Пропустить» у второго мини-профильного подшага.
    "mini_portfolio",
}
# Шаги, у которых клавиатура бота сегодня включает кнопку "Другое" (свободный текст поверх
# списка) — city/study_field через _reply_kb(options, add_other=True) в _ask_step,
# local_committee/position/department/aiesec_role через builders.py.
# Phase 30 (30-06, A2-03, deviation Rule 2): "university" добавлен — chat-проекция lookup
# (handlers/reg_types_lookup.py) обязана предлагать «Другое» для ВУЗа так же, как для города
# (30-UI-SPEC.md § «2. lookup»: «до 5 совпадений + кнопка «Другое»» — без разделения по шагу);
# `spec["other_allowed"]` для legacy-ветки university не читался (список строится безусловно
# через `_reply_kb(options, add_other=True)`), поэтому добавление сюда не двигает GOLDEN.
_OTHER_ALLOWED_STEPS = {"city", "study_field", "local_committee", "position", "department", "aiesec_role", "university"}

# Приёмка 15.09 (п.5 «Другое: писать негде»): литерал варианта «свой ответ». Уже живёт
# отдельными ветками в `_validate_answer_core` (там он значит «делегат нажал кнопку, но текста
# не прислал — переспроси») и в клавиатурах бота (`keyboards/builders.py`). Здесь он нужен
# спеке шага: новая анкета рисует плитки из `spec["options"]` и без явного маркера не знает,
# КАКАЯ плитка обязана раскрыть поле «впиши свой вариант» (у старого рендера это была
# отдельная кнопка-карандаш `spec.other_allowed`, у плиток её нет). Константа, а не литерал по
# месту — чтобы совпадение с ветками валидатора было видно грепом.
OTHER_OPTION = "Другое"

# Phase 30 (30-07, задача 4, A2-03, 30-CONTEXT.md § «Решения оркестратора», п.2): атрибут
# «свой вариант» конкретного списка-справочника — источник правды для `other_allowed` у ШАГОВ
# ТИПА `lookup` (university/city), заменяет `_OTHER_ALLOWED_STEPS` выше ТОЛЬКО для них; для
# остальных choice-шагов множество выше не трогается (city/university тоже в нём остаются —
# `_OTHER_ALLOWED_STEPS` продолжает обслуживать легаси-ветку при выключенной новой анкете,
# 30-CONTEXT.md: «Атрибуты влияют только на новую анкету»). `университет`/`city` — единственные
# сегодняшние lookup-шаги (reg_engine.step_type_v2), второй карты имён не заводится —
# `_LOOKUP_LIST_KEY` дословно совпадает с `services.lookup._SEASON_COLUMN`/`_STEP_TO_LOOKUP_KIND`
# (miniapp/routers/form.py), но живёт здесь отдельной картой: это связка step_key -> ключ
# СПИСКА реестра (`*_options`), а не step_key -> kind справочника.
_LOOKUP_LIST_KEY = {"university": "university_options", "city": "city_options"}


async def lookup_other_allowed(step_key: str) -> bool:
    """«Свой вариант» для шага типа `lookup` — читает атрибут списка (`<list_key>_other_
    allowed`, план 30-07 задача 4, дефолт `"on"` = сегодняшнее поведение не меняется, пока
    менеджер явно не выключит). Для любого другого step_key (не lookup) — старое поведение,
    `_OTHER_ALLOWED_STEPS` НЕ подменяется."""
    list_key = _LOOKUP_LIST_KEY.get(step_key)
    if list_key is None:
        return step_key in _OTHER_ALLOWED_STEPS
    return await get_setting_typed(f"{list_key}_other_allowed") == "on"


async def lookup_render_flags(step_key: str, flags: dict[str, bool]) -> dict[str, bool]:
    """spec['lookup'] (30-08 задача A): атрибуты списка-справочника `<list_key>_chips_enabled`/
    `<list_key>_search_enabled` (план 30-07, заведены, но до этой задачи ни на что не влияли —
    30-07-SUMMARY.md Known Stubs) управляют РЕНДЕРОМ типа `lookup` СВЕРХ глобальных тумблеров
    `reg_form_chips`/`reg_form_lookup_search` — правило задачи A дословно: глобальный `off` →
    `off` (без похода в реестр списка вовсе — экономия чтения на шаге, где список всё равно
    ничего не решает), глобальный `on` → решает атрибут конкретного списка. Единственная точка
    правды для ОБЕИХ поверхностей — `form_types.js` читает `spec.lookup` (`reg_engine.step_
    spec`), чат (`handlers/reg_types_lookup.py`) зовёт эту же функцию напрямую (там `step_spec`
    целиком не строится — чат ведёт шаг без полной спеки, тем же приёмом, что и `lookup_other_
    allowed` выше).

    `degrade_kind()` (A2-01, единственный источник правды для САМОГО ТИПА шага) НЕ трогается —
    он видит только девять глобальных тумблеров, не атрибуты списков (30-CONTEXT.md: «Атрибуты
    влияют только на новую анкету», не на классификацию типа). Если оба вычисленных здесь флага
    ложны, а `degraded_kind` шага всё ещё `"lookup"` (менеджер выключил ОБА атрибута списка при
    включённых глобальных тумблерах) — обе поверхности рисуют голое поле сами (тот же приём,
    что уже применяет `degrade_kind` на уровне глобальных тумблеров), эта функция только
    публикует флаги, решение о развороте — за вызывающим."""
    list_key = _LOOKUP_LIST_KEY.get(step_key)
    if list_key is None:
        return {"chips_enabled": False, "search_enabled": False}
    chips_enabled = bool(flags.get("chips")) and (
        await get_setting_typed(f"{list_key}_chips_enabled") == "on"
    )
    search_enabled = bool(flags.get("lookup_search")) and (
        await get_setting_typed(f"{list_key}_search_enabled") == "on"
    )
    return {"chips_enabled": chips_enabled, "search_enabled": search_enabled}

# Phase 28 (28-04, SU-04, A-03 CONTEXT): три записи развилки резюме R1 — code (не показывается
# делегату, только Mini App/бот решают, куда вести дальше) / реестровый ключ подписи / иконка
# Lucide-подсета (28-UI-SPEC.md §Component Contracts 1, upload/link/x).
_RESUME_FORK_OPTIONS = [
    ("file", "reg_resume_fork_file_label", "upload"),
    ("link", "reg_resume_fork_link_label", "link"),
    ("mini", "reg_resume_fork_none_label", "x"),
]


async def resume_fork_options() -> list[dict]:
    """Спека трёх кнопок развилки резюме (SU-04) — код/человеческая подпись из реестра
    (Copywriting Contract 28-UI-SPEC.md)/иконка; менеджер меняет подписи, коды и порядок
    закрыты (D-01/D-02 — коду делегат не видит, порядок — часть UX-контракта развилки)."""
    return [
        {"code": code, "label": await get_setting_typed(setting_key), "icon": icon}
        for code, setting_key, icon in _RESUME_FORK_OPTIONS
    ]


async def _composite_spec_for(
    group: str | None, participant_type: str | None, event_city: str | None,
    flags: dict[str, bool],
) -> dict:
    """`spec["composite"]` контракт (`<interfaces>` 30-04-PLAN.md): `{group, parts, toggle_step}`
    — `parts` собираются РЕКУРСИВНЫМ вызовом `step_spec(..., _in_composite=True)` (защита от
    рекурсии — докстринг `step_spec` выше) для каждого шага, реально включённого у события
    (`composite_parts()`, T-30-04-01). Карточка с единственной включённой частью — валидный
    результат (30-CONTEXT.md реш. 2: «карточка всегда, из включённых частей»), не откат к
    легаси-шагу."""
    part_keys = await composite_parts(group or "", participant_type, event_city)
    parts = [
        await step_spec(part_key, participant_type, event_city, flags=flags, _in_composite=True)
        for part_key in part_keys
    ]
    # Task 260915-skg (P1, решение владельца «две плитки»): тумблер группы должен писать
    # СТРОКУ education_status, не bool (form_types.js больше не хардкодит статус) — источник
    # правды остаётся один и тот же `is_studying`/`studying_statuses` (D-06 фазы 28), клиент
    # своего списка «учащихся» не держит (T-skg-03). `studying_option` — первый вариант
    # `options("education_status")`, для которого `is_studying` истинно; `not_studying_options`
    # — все остальные варианты (пустой список опций -> `studying_option is None`, T-skg-03).
    edu_opts = await options("education_status")
    edu_statuses = await studying_statuses()
    studying_option = None
    studying_idx = None
    for idx, opt in enumerate(edu_opts):
        if is_studying(opt, edu_statuses):
            studying_option = opt
            studying_idx = idx
            break
    if studying_idx is None:
        not_studying_options = list(edu_opts)
    else:
        not_studying_options = edu_opts[:studying_idx] + edu_opts[studying_idx + 1:]
    return {
        "group": group, "parts": parts, "toggle_step": _COMPOSITE_TOGGLE_STEP.get(group),
        "studying_option": studying_option, "not_studying_options": not_studying_options,
    }


async def step_spec(step_key: str, participant_type: str | None = None,
                     event_city: str | None = None, flags: dict[str, bool] | None = None,
                     _in_composite: bool = False) -> dict:
    """Спека одного шага по контракту UI-SPEC — бот берёт из неё текст/варианты по отдельности
    (`prompt()`/`options()`), Mini App (план 21-04a/b) — эту функцию целиком.

    Phase 25 (CITYQ-02): `event_city` теперь используется — `prompt`/`help_text` резолвятся по
    городу делегата, а шаг `resume` в режиме `reg_resume_mode(event_city) == "text_only"`
    рисуется текстовым полем вместо дропзоны (`_UI_TYPE_OVERRIDES` не трогается — оверрайд
    режима резюме считается здесь, где уже известен город). Для всех прочих шагов вывод не
    меняется.

    Phase 28-04 (SU-04): `reg_resume_mode(event_city) == "fork"` рисует развилку («Файл» /
    «Ссылка» / «Нет резюме», R1 UI-SPEC) вместо дропзоны/текстового поля — `type` становится
    `"resume-fork"`, спека получает `fork_options` (см. `resume_fork_options()`). Экранов
    делегат для этого режима ещё не видит (план 28-05) — здесь только контракт спеки.

    Phase 30 (30-03, A2-08): `flags` — девять тумблеров «📝 Анкета» (`form_v2_flags()`),
    ОПЦИОНАЛЬНЫЙ параметр. `form_spec()` считает их ОДИН раз на всю форму и передаёт сюда —
    девять чтений реестра на КАЖДЫЙ из ~43 шагов было бы явным overkill (30-01 докстринг
    `form_v2_flags` уже называет её «единой точкой чтения», не «читай на каждый шаг заново»).
    Прямые вызовы `step_spec()` в обход `form_spec()` (сегодня таких нет, `grep` подтверждает
    единственный call site) остаются рабочими — `None` считает флаги сам, тем же вызовом.

    Phase 30 (30-04, A2-04): `_in_composite` — ВНУТРЕННИЙ параметр (не часть публичного
    контракта, не документирован в `<interfaces>` 30-04-PLAN.md), защита от рекурсии. Карточка
    «Образование» строит свои под-спеки РЕКУРСИВНЫМ вызовом `step_spec()` для КАЖДОЙ части
    группы (см. `_composite_spec_for` ниже) — без этого флага под-спека `course`/`study_field`/
    `education_status` (все трое сами по себе имеют `kind == "composite"`, T-30-04-02) снова
    попыталась бы построить СВОЙ `spec["composite"]`, снова рекурсивно вызывая `step_spec()` —
    бесконечная рекурсия. `university` в группе состоит, но её `kind == "lookup"` (override
    сильнее группы) — флаг её не касается вовсе, сохраняет собственную деградацию lookup."""
    step_type = REG_STEP_TYPES.get(step_key, "text")
    ui_type = _ui_type_for(step_key, step_type)
    label = label_for(step_key)
    resume_mode_value = None
    if step_key == "resume":
        resume_mode_value = await resume_mode(event_city)
        if resume_mode_value == "text_only":
            ui_type = "textarea"
        elif resume_mode_value == "fork":
            ui_type = "resume-fork"
    spec = {
        "key": step_key,
        "column": STEP_TO_COLUMN.get(step_key, step_key),
        "type": ui_type,
        "label": label,
        "prompt": await prompt(step_key, participant_type, event_city),
        "help": await help_text(step_key, participant_type, event_city),
        "options": None,
        "other_allowed": step_key in _OTHER_ALLOWED_STEPS,
        "skip_allowed": step_key in _SKIP_ALLOWED_STEPS,
        "required": step_key not in _SKIP_ALLOWED_STEPS,
        "max_len": _max_len_for(step_key, ui_type),
        # Phase 30 (30-01, A2-01): новая ось «Анкета 2.0» — публикуется РЯДОМ с `spec["type"]`
        # (старая ось `_ui_type_for`, выше), не заменяя его ни одним символом (Pitfall 1
        # 30-RESEARCH.md). GOLDEN (tests/test_reg_engine_parity.py) собирает снимок через
        # prompt()/options()/enabled_steps() по отдельности, а не через step_spec() целиком —
        # добавление этих двух ключей не меняет ни одного байта GOLDEN.
        "kind": step_type_v2(step_key),
        "composite_group": composite_group_of(step_key),
    }
    # Phase 30 (30-03, A2-08, задача 4): деградация/тексты/флаги для НОВОГО рендера —
    # `spec["kind"]` (выше) сам по себе НЕ признак «рисуй по-новому» (публикуется всегда с
    # 30-01), признак — `degraded_kind` (при выключенном мастер-тумблере ВСЕГДА `"legacy"`,
    # см. `degrade_kind()`). `v2_texts`/`option_hints` считаются ТОЛЬКО когда реально нужны
    # (не `"legacy"`/не `"select"` соответственно) — легаси-шаг не платит лишним походом в
    # реестр за тексты, которые всё равно не попадут на экран.
    resolved_flags = flags if flags is not None else await form_v2_flags(event_city)
    degraded_kind = degrade_kind(spec["kind"], resolved_flags)
    spec["flags"] = resolved_flags
    spec["degraded_kind"] = degraded_kind
    spec["v2_texts"] = await _v2_texts_for(degraded_kind, step_key, event_city)
    if degraded_kind == "select":
        spec["option_hints"] = await option_hints_for(step_key)
    if degraded_kind == "lookup":
        # 30-08 задача A: атрибуты списка-справочника сверх глобальных тумблеров — см.
        # докстринг `lookup_render_flags`.
        spec["lookup"] = await lookup_render_flags(step_key, resolved_flags)
    if degraded_kind == "composite" and not _in_composite:
        spec["composite"] = await _composite_spec_for(
            spec["composite_group"], participant_type, event_city, resolved_flags,
        )
    if degraded_kind == "repeatable":
        # Phase 30 (30-04, A2-05, задача 3): клиенту нужно число, чтобы скрыть «+ Добавить»
        # по достижении лимита (T-30-10) — второй, серверный барьер того же числа уже стоит в
        # `validate_answer(..., repeatable_max_items=...)`, здесь только публикация для UI.
        spec["repeatable_max"] = await repeatable_max(step_key)
    # УАТ 10-11.09 (квик 260911-2kb, пункт 4): набор колонок-компаньонов шага — та же функция,
    # что уже синхронизирует черновик (`columns_for_step`, квик 260910-wb6), второй копии
    # правила здесь не заводим. Для резюме — три колонки разом, у всех остальных шагов —
    # список из одной `spec["column"]` (columns_for_step на неизвестном/legacy ключе отдаёт
    # пустой список — `or [spec["column"]]` не даёт шагу остаться вовсе без набора).
    spec["columns"] = columns_for_step(step_key) or [spec["column"]]
    if step_key == "resume":
        spec["resume_mode"] = resume_mode_value
        if resume_mode_value == "fork":
            spec["fork_options"] = await resume_fork_options()
    # Phase 28 (28-04, SU-04): ссылка на резюме публикует вайтлист доменов — сверка домена
    # происходит на клиенте БЕЗ отдельного запроса (28-UI-SPEC.md §2), финальное решение
    # `link_verified` всё равно пересчитывает сервер на финале (T-28-04-01).
    # Phase 28 (28-05, SU-04, deviation Rule 3): шаблоны нейтрального маркера домена
    # (`{domain}` подставляет клиент) — без них form.js не может нарисовать текст маркера,
    # только иконку; те же реестровые ключи, что уже читает бот в R2b (handlers/reg_resume_fork.py).
    if step_key == "resume_link":
        spec["link_whitelist"] = await resume_link_whitelist()
        spec["whitelist_hint_text"] = await get_setting_typed("reg_resume_link_whitelist_hint_text")
        spec["other_hint_text"] = await get_setting_typed("reg_resume_link_other_hint_text")
        spec["invalid_hint_text"] = await get_setting_typed("reg_resume_link_invalid_text")
    if ui_type in ("choice-chips", "select", "multi", "yesno"):
        spec["options"] = await options(step_key)
    # Приёмка 15.09 (п.5): какая ПЛИТКА раскрывает поле «впиши свой вариант». Два источника,
    # оба уже существуют: шаг с разрешённым свободным ответом (`other_allowed` — старый рендер
    # рисовал для него отдельную кнопку-карандаш) и шаг, у которого «Другое» и так лежит
    # ВАРИАНТОМ в списке («Откуда узнал»: `reg_options.DEFAULT_SOURCE_OPTIONS`). В обоих
    # случаях сервер валидатором отвечает «Напиши свой вариант:» — значит поле для этого текста
    # обязано быть на экране, а не только в тексте ошибки.
    if degraded_kind == "select" and (
        spec["other_allowed"] or OTHER_OPTION in (spec["options"] or [])
    ):
        spec["other_option"] = OTHER_OPTION
    # Phase 28 (28-03, SU-02, A-06): лимит мультивыбора — публикуется ВСЕГДА для multi-шага
    # (значение `None` = без лимита, существующие мультивыборы `work_format`/`formats`/`goal`
    # без настроенного `reg_multi_max_<step>` не меняют поведения). При заданном лимите — ещё
    # счётчик (текст с подставленным {max}, {selected} остаётся для фронта) и подсказка формата
    # ДОписывается в существующий узел `help` (Reuse Contract 28-UI-SPEC §4 — отдельного узла
    # на фронте не заводим).
    if ui_type == "multi":
        limit = await multi_max(step_key)
        spec["max_select"] = limit
        if limit is not None:
            counter_text = await get_setting_typed("reg_multi_limit_counter_text")
            spec["limit_counter_text"] = counter_text.replace("{max}", str(limit))
            hint_text = await get_setting_typed_for_city("reg_multi_limit_hint_text", event_city)
            hint_text = hint_text.replace("{max}", str(limit))
            spec["help"] = f"{spec['help']}\n{hint_text}" if spec.get("help") else hint_text
    # Phase 28 (28-02, SU-08, СкиллАп 5): пояснение под заголовком экрана кейс-чемпионата
    # (28-UI-SPEC.md §5, Body-абзац) — публикуется в спеку, чтобы Mini App нарисовало его
    # существующим узлом подсказки, без нового компонента (Reuse Contract).
    if step_key == "case_optin":
        spec["description"] = await get_setting_typed_for_city(
            "reg_case_optin_description_text", event_city
        )
    # УАТ 10-11.09 (квик 260911-2kb, пункт 3): D-06 («остальные одиннадцать skip-шагов
    # обходятся пустым «Дальше») снят — на живом стенде пустой ввод на этих шагах не проходит
    # validate_answer (нужен буквально «-»), а слово «Пропустить» из UI отправить нечем: делегат
    # упирался в тупик там, где в чате бота кнопка «Пропустить» есть всегда. Подпись теперь
    # публикуется для ЛЮБОГО шага из `_SKIP_ALLOWED_STEPS` — `mini_portfolio` оставляет СВОЙ
    # реестровый ключ (он уже в админке с фазы 28, переименование стало бы регрессом для
    # менеджера), остальные одиннадцать делят один общий `reg_form_skip_cta_text`.
    if step_key in _SKIP_ALLOWED_STEPS:
        spec["skip_label"] = await get_setting_typed(
            "reg_mini_portfolio_skip_label" if step_key == "mini_portfolio"
            else "reg_form_skip_cta_text"
        )
    return spec


def _is_yes_no_value(value) -> bool:
    """«Это ответ да/нет, а не текст» — условие первой ветки `_display_value` отдельной
    функцией: приёмка 15.09 (п.8) добавила второе место, которое обязано принимать то же
    решение (`form_spec` публикует подпись), а два одинаковых условия по месту разъезжаются.
    Булево из черновика (JSON) и 0/1 из строки `users` (SQLite) — одно и то же значение."""
    return isinstance(value, bool) or (isinstance(value, int) and value in (0, 1))


def _display_value(value) -> str:
    """Человекочитаемое представление прошлого ответа — та же логика, что
    `handlers/registration.py::_recall_display`, БЕЗ HTML-экранирования (T-21-03: движок
    отдаёт сырой текст, экранирование — забота поверхности)."""
    if _is_yes_no_value(value):
        return "Да" if value else "Нет"
    return str(value)


# УАТ 10-11.09 (квик 260911-2kb, пункт 4): колонки, у которых значение — не для показа
# человеку (сырой Telegram file_id). Тот же принцип, что уже применяет `has_prior_resume`
# (Pitfall 3): наличие проверяем, само значение наружу не отдаём. Сегодня одна колонка —
# `resume_file_id`; `resume_file_name`/`resume_text` — человекочитаемые, в набор не входят.
_OPAQUE_COLUMNS = {"resume_file_id"}


def _published_value(column: str, value):
    """Значение колонки, которое реально уезжает клиенту в спеке шага. Для обычной колонки —
    как есть; для `_OPAQUE_COLUMNS` (сырой Telegram file_id) — только признак «непусто»
    (`True`/`None`), само значение на сервере и наружу не отдаётся. Клиенту
    (`screens/form.js::stepAnswered`) нужен именно этот признак — набор `spec.columns` не
    меняется, а PATCH с такой колонкой всё равно поймает `400 bad_field`
    (`_COLUMN_TO_STEP` её не знает, см. квик 260911-6i9 пункт 3), поэтому признак физически не
    может доехать обратно до БД.

    Phase 30 (30-04, A2-05): `_REPEATABLE_COLUMNS` публикуется УЖЕ РАЗОБРАННЫМ списком
    (`parse_repeatable`) — единственная точка чтения формата, второй копии правила «не JSON ->
    legacy» на клиенте нет (30-CONTEXT.md п.1). Проверка стоит РАНЬШЕ `_OPAQUE_COLUMNS`:
    множества не пересекаются (repeatable-колонка никогда не опаковая), порядок значения не
    имеет, но так читается как «сначала спрашиваем про формат, потом про опаковость»."""
    if column in _REPEATABLE_COLUMNS:
        return parse_repeatable(value)
    if column not in _OPAQUE_COLUMNS:
        return value
    return True if value not in (None, "", "-") else None


async def form_spec(answers: dict, participant_type: str | None = None,
                     event_city: str | None = None, prior: dict | None = None,
                     pending_consent_keys: list[str] | None = None) -> dict:
    """Контракт формы для Mini App (RESEARCH Pattern 2): `{pre, steps, progress}`. `prior` —
    результат `prior_answers_for(user_row)` для возвращенца (D-07); движок НИКУДА prior не
    пишет и не логирует — вызывающий передаёт его на каждый запрос заново (Pitfall 5). Шаг без
    собственного ответа, но с непустым prior, получает `value_source == "prior"`; отвеченный
    шаг — `"answer"`; шаг без ответа и без prior — `None`.

    `pending_consent_keys` (UAT 21-12 находка 1) — см. докстринг `pre_flow`: `None` (дефолт)
    не фильтрует согласия вовсе."""
    answers = answers or {}
    prior = prior or {}
    # Живой баг владельца (03.09): `enabled_steps` сам решает трек только по
    # `data.get("participant_type")` -- у Mini App трек живёт в ОТДЕЛЬНОМ аргументе этой
    # функции (`answers` черновика ключа "participant_type" не содержит вовсе, см.
    # `miniapp/routers/form.py::_load_context`), поэтому раньше сюда всегда уходил "answers"
    # без трека и `enabled_steps` тихо падал на дефолт "full" -- каждый веб-делегат видел все
    # 43 вопроса вместо своего трека. Явно подмешиваем track в копию для этого одного вызова;
    # сам `answers` ниже (value/prior по шагам) остаётся нетронутым.
    # Quick 260904-3vm (D16, УАТ владельца 04.09): "or 'full'" тут раньше означало, что
    # глобальный/по-городу registration_mode=short веб просто ИГНОРИРУЕТ, когда в черновике
    # нет трека (старт из приложения, kind=new, без party-форка) -- каждый веб-делегат на
    # промо-окне получал полную форму. `resolve_track` зовётся ТОЛЬКО с пустым кандидатом
    # (raw_track is None) -- вызов с непустым кандидатом означал бы, что промо-окно
    # перебивает уже зафиксированный трек 'full' (шаг 2 функции выиграл бы у шага 3) и
    # перевело бы делегатов, начавших полную анкету, на короткую в середине заполнения.
    raw_track = participant_type or answers.get("participant_type")
    track = raw_track or await resolve_track(None, event_city)
    # Phase 25 (CITYQ-02): город делегата в набор шагов — без этого веб-форма считала набор
    # глобально даже для делегата города с выключенными вопросами (enabled_steps сама умеет
    # брать event_city из data, но form_spec раньше его не передавал).
    enabled = await enabled_steps({**answers, "participant_type": track, "event_city": event_city})
    # Phase 30 (30-03, A2-08): девять тумблеров читаются ОДИН раз на всю форму, не по разу на
    # каждый из ~43 шагов (`form_v2_flags()` — «единая точка чтения», не «читай на каждый
    # шаг заново», см. её докстринг 30-01) — экономит ~9×N походов в реестр на один запрос.
    v2_flags = await form_v2_flags(event_city)
    # Приёмка 15.09 (п.3б/п.6): части карточки-композита не идут вторым экраном и не считаются
    # отдельными шагами в «шаг N из M» — карточка уже спрашивает их у себя внутри. Поглощение
    # действует, ТОЛЬКО пока сам шаг-карточка включён у события: выключил менеджер вопрос
    # «Учишься сейчас?» — ВУЗ/курс/программа остаются обычными шагами, иначе делегат потерял бы
    # их вовсе.
    absorbed = {
        part: owner for part, owner in composite_absorbed_steps(v2_flags).items()
        if owner in enabled
    }
    steps_out = []
    done = 0
    for step_key in enabled:
        if step_key in absorbed:
            continue
        spec = await step_spec(step_key, participant_type, event_city, flags=v2_flags)
        column = spec["column"]
        # УАТ 10-11.09 (пункт 4): «отвечен» решает НАБОР колонок шага, не одна главная —
        # резюме файлом уходит в resume_file_id/resume_file_name, а не в spec["column"]
        # (resume_text), поэтому проверка одной колонки видела «не заполнено» на реально
        # загруженном файле.
        # Квик 260911-6i9 (пункт 3): значения читаем один раз в raw_values — has_answer и
        # выбор display_col решаются на СЫРЫХ значениях (поведение W1 не меняется), а наружу
        # (spec["values"]) публикуется результат _published_value: опаковая колонка
        # (`_OPAQUE_COLUMNS`, сырой file_id) уезжает клиенту признаком True/None, а не строкой.
        raw_values = {col: answers.get(col) for col in spec["columns"]}
        spec["values"] = {col: _published_value(col, val) for col, val in raw_values.items()}
        has_answer = any(v not in (None, "", "-") for v in raw_values.values())
        prior_value = prior.get(step_key)
        if prior_value not in (None, "", "-"):
            # Phase 30 (30-04, A2-05): «отображение прошлого ответа» для repeatable-колонки —
            # ТА ЖЕ пара функций, что у листа/карточки заявки (repeatable_display+parse_
            # repeatable), не generic `_display_value` (который просто str()-ит значение и
            # показал бы делегату сырой JSON прошлого сезона).
            prior_display = (
                repeatable_display(parse_repeatable(prior_value)) if column in _REPEATABLE_COLUMNS
                else _display_value(prior_value)
            )
            spec["prior"] = {"value": prior_value, "display": prior_display}
        else:
            spec["prior"] = None
        if has_answer:
            spec["value"] = _published_value(column, answers.get(column))
            spec["value_source"] = "answer"
            done += 1
            # Пункт 4: основная колонка пуста (файл резюме), а набор отвечен — подпись
            # берём с первой непустой НЕ-опасной колонки набора (для резюме это
            # resume_file_name). Опасную (`_OPAQUE_COLUMNS`, сырой file_id) и пустые
            # колонки пропускаем; если показываемой колонки нет вовсе (легаси-строка с
            # одним file_id) — display не выставляем, шаг всё равно считается отвеченным.
            if spec["value"] in (None, "", "-"):
                display_col = next(
                    (
                        col for col in spec["columns"]
                        if col not in _OPAQUE_COLUMNS
                        and raw_values.get(col) not in (None, "", "-")
                    ),
                    None,
                )
                if display_col is not None:
                    spec["display"] = _display_value(raw_values[display_col])
            elif _is_yes_no_value(spec["value"]):
                # Приёмка 15.09 (п.8 «на итоговом просмотре анкеты амбассадор false»): ответы
                # «да/нет» лежат в БД булевыми (`validate_answer` для ambassador/work_status/
                # needs_certificate/volunteer/case_optin отдаёт bool) — экран печатал их
                # `String(value)`, то есть «false». Подпись считает ТА ЖЕ функция, что уже
                # переводит прошлый ответ возвращенца (`_display_value`), второго правила
                # «как показать булево» в проекте не заводим.
                spec["display"] = _display_value(spec["value"])
        elif spec["prior"] is not None:
            spec["value"] = _published_value(column, prior_value)
            spec["value_source"] = "prior"
            if _is_yes_no_value(spec["value"]):
                spec["display"] = _display_value(spec["value"])
        else:
            spec["value"] = None
            spec["value_source"] = None
        # Phase 30 (30-05, задача 0б, хвост 30-04): `step_spec()`/`_composite_spec_for` строят
        # части карточки БЕЗ доступа к `answers` (T-30-04-01, докстринг `composite_parts`) —
        # значения делегата подмешиваются здесь, тем же способом, что верхний уровень спеки
        # выше (`_published_value`/`prior`), но по СВОЕЙ колонке каждой части (`columns_for_step`,
        # тот же приём, что `spec["columns"]` строки 1746). Известное ограничение 30-04-SUMMARY.md
        # («Composite не получает текущие/прошлые значения делегата») закрывается здесь.
        composite = spec.get("composite")
        if composite:
            toggle_key = composite.get("toggle_step")
            toggle_raw_for_studying = None
            for part in composite["parts"]:
                part_column = part.get("column")
                part_columns = columns_for_step(part["key"]) or ([part_column] if part_column else [])
                part_raw = {col: answers.get(col) for col in part_columns}
                part_has_answer = any(v not in (None, "", "-") for v in part_raw.values())
                part_prior_value = prior.get(part["key"])
                if part_has_answer:
                    part["value"] = _published_value(part_column, answers.get(part_column))
                elif part_prior_value not in (None, "", "-"):
                    part["value"] = _published_value(part_column, part_prior_value)
                else:
                    part["value"] = None
                if part_prior_value not in (None, "", "-"):
                    part["prior"] = {"value": part_prior_value, "display": _display_value(part_prior_value)}
                else:
                    part["prior"] = None
                if part["key"] == toggle_key:
                    toggle_raw_for_studying = part["value"] if part["value"] not in (None, "", "-") \
                        else part_prior_value
            # Дефолт тумблера — «учится» (30-CONTEXT.md реш. 6: у делегата без ответа карточка
            # стартует со всеми частями открытыми), как и клиентский дефолт `compositeCard`.
            if toggle_key and toggle_raw_for_studying not in (None, "", "-"):
                composite["studying"] = is_studying(toggle_raw_for_studying, await studying_statuses())
            elif toggle_key:
                composite["studying"] = True
        steps_out.append(spec)
    # Вилка трека не показывается, когда трек уже известен (deep-link или выбор в приложении) —
    # паритет с `should_show_fork` в боте. D-27 (гейт владельца, 02.09): `is_registered` в
    # пре-флоу зовётся ЖЁСТКО False — ровно то, что передаёт бот на КАЖДОМ вызове
    # `_should_show_city_fork`/`_should_show_fork` (handlers/registration.py::
    # _city_fork_then_continue/_continue_after_city, тела которых пишут это прямым текстом:
    # «is_registered stays hardcoded False» / «is_registered is always False here»). У бота
    # прошлый сезон/`prior` НИКОГДА не подавляет развилку — подавляет только уже известный
    # город/трек (deep-link, восстановленный `reg_started`). `prior` — ранее подменял собой
    # это булево и прятал развилку у возвращенца; это была не «та же развилка, что у бота», а
    # своё правило.
    pre = await pre_flow(answers, {
        "event_city": event_city, "is_registered": False, "party_track": participant_type,
        "pending_consent_keys": pending_consent_keys,
    })
    return {"pre": pre, "steps": steps_out, "progress": {"done": done, "total": len(steps_out)}}


# ── Префилл возвращенца (D-07, FORM-SYNC-01) ────────────────────────────────────────────────

def prior_answers_for(user_row: dict | None) -> dict:
    """Прошлые ответы возвращенца: step_key -> значение, для шагов из RECALLABLE_STEPS, чьё
    значение в строке `users` не пустое, не None и не прочерк. Чистая функция — ни обращений к
    БД, ни к FSM, ни логов со значениями (T-21-11). Ровно то же выражение, что раньше стояло в
    handlers/registration.py::_ask_step_or_recall."""
    if not user_row:
        return {}
    return {
        step: user_row.get(col)
        for step, col in STEP_TO_COLUMN.items()
        if step != "resume" and user_row.get(col) not in (None, "", "-")
    }


def answers_from_user_row(user_row: dict | None) -> dict:
    """Текущие ответы уже поданной анкеты — колонки `users`, а не шаги (в отличие от
    `prior_answers_for`): `form_spec` индексирует `answers` по колонке (`STEP_TO_COLUMN.values()`),
    не по `step_key`). Источник — та же карта `STEP_TO_COLUMN`, вторая копия не заводится:
    ключи `prior_answers_for` просто перекладываются в свою колонку. Нужна веб-обзору правки
    уже поданной анкеты (`miniapp/routers/form.py::_load_context`, D-26) для делегата без
    строки `reg_drafts` — тем же способом, каким чат-recall уже читает `users` напрямую
    (UAT 21-12 находка 4: обзор показывал «Не заполнено» вместо реальных значений).

    УАТ 10-11.09 (квик 260911-2kb, пункт 4): `prior_answers_for` резюме исключает намеренно
    (Pitfall 3) — здесь дополняем результат колонками шага резюме (`columns_for_step("resume")`)
    НАПРЯМУЮ из `user_row`, только непустые значения. Иначе у одобренного делегата БЕЗ строки
    `reg_drafts` (обзор правки читает эту функцию, `miniapp/routers/form.py::_load_context`)
    резюме-файл не был виден в обзоре даже с исправленной спекой шага (пункт 4 выше) — снимка,
    из которого спека берёт `values`, просто не было. `prior_answers_for` не трогается — чат-recall
    остаётся байт-в-байт. Ловушка: `resume_url` (Nextcloud) НЕ входит в `columns_for_step`/
    `answer_columns()` — в снимок его не класть, иначе узкий UPDATE финала поймает
    `no such column`."""
    out = {STEP_TO_COLUMN[step]: value for step, value in prior_answers_for(user_row).items()}
    if user_row:
        for col in columns_for_step("resume"):
            value = user_row.get(col)
            if value not in (None, "", "-"):
                out[col] = value
    return out


def has_prior_resume(user_row: dict | None) -> bool:
    """Карвинг resume (Pitfall 3): наличие любой из resume_file_id/resume_text/resume_url;
    само значение наружу не отдаётся — показывать raw file_id человеку бессмысленно.

    Набор колонок (`RESUME_RECALL_COLUMNS`) приезжает из `database.db` — квик 260911-0fh
    завёл там единственный источник правды о том, что считается «резюме». Это НЕ то же
    самое, что `db.RESUME_COLUMNS` фильтра рассылки: там есть ещё `resume_link` (ссылка на
    профиль, СкиллАп 5) — для менеджера это тоже «резюме есть», но recall не станет
    переиспользовать эту ссылку как артефакт на шаге резюме, поэтому здесь её нет.
    Поведение этой функции квиком не меняется."""
    if not user_row:
        return False
    return any(
        user_row.get(col) not in (None, "", "-")
        for col in RESUME_RECALL_COLUMNS
    )


def has_submitted_anketa(user_row: dict | None, season: str | None) -> bool:
    """Quick 260904-3vm (D15): «анкета реально подана в ТЕКУЩЕМ сезоне» — намеренно НЕ то же
    самое, что «есть строка `users`». Экран «Изменение анкеты» обязан появляться только у
    того, кто анкету подал; пустой `full_name`/`registration_date` при статусе approved —
    артефакт импорта (например, предзаведённая строка амбассадора/менеджера), а не поданная
    анкета — вот почему признак «подана» = непустой `registration_date`, а не факт наличия
    строки или статус ≠ rejected (это раньше и давало D15: делегат без единого ответа видел
    пустой экран правки поверх реально заполненного черновика).

    True только когда ВСЕ условия разом: строка есть, её `season` совпадает с переданным
    `season`, статус ≠ 'rejected' и `registration_date` непустой."""
    if not user_row:
        return False
    if season and user_row.get("season") != season:
        return False
    status = user_row.get("status") or "approved"
    if status == "rejected":
        return False
    return bool(user_row.get("registration_date"))


def is_returning_row(user: dict | None, event_season: str | None) -> bool:
    """«прошлый делегат» = has a users row AND (status is 'rejected' OR their row's `season`
    is set and differs from the currently configured `event_season`). Перенос дословный из
    handlers/registration.py::_is_returning_row (Phase 07.3, RET-02/CONTEXT A)."""
    if not user:
        return False
    status = user.get("status") or "approved"
    if status == "rejected":
        return True
    season = user.get("season")
    if event_season and season and season != event_season:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Phase 21 (21-06, FORM-SYNC-01/03) — validate_answer / apply_answer / merge_answers: вторая
# половина движка. Судья ввода теперь один — и для чата бота, и (план 21-10) для Mini App
# (T-21-05: «второго валидатора» быть не должно). Тексты ошибок и побочные правила перенесены
# byte-for-byte из тел `process_*` (handlers/reg_steps.py, handlers/reg_flow.py) и вспомогательных
# функций (`_parse_age`/`_is_allowed_resume`/`_resume_too_large`/`_validate_date_range`,
# ранее handlers/registration.py и handlers/reg_flow.py) — паритет снят
# `tests/test_reg_engine_parity.py` (VALIDATION_GOLDEN/APPLY_GOLDEN, Task 1) ДО переноса.
#
# Какую клавиатуру приложить к тексту ошибки (get_cancel_kb() для «Другое», ничего для обычной
# ошибки) — решает вызывающий хендлер (handlers/registration.py::_err_kb), не движок: движок не
# знает про aiogram/HTML (T-21-03). validate_answer возвращает голый текст в обоих случаях.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def parse_age(raw: int | str | None) -> int | None:
    """CR-8: ASCII-digit-safe age parse. Перенос дословный из
    handlers/registration.py::_parse_age. Task 260915-skg (P3, T-skg-01): Mini App
    `<input type="number">` шлёт JSON-число, не строку — `int`/`float` принимаются наравне со
    строкой; `bool` отсекается первой строкой (bool — подкласс int, `parse_age(True)` иначе
    молча прошёл бы как `1`)."""
    if isinstance(raw, bool):
        return None
    raw = "" if raw is None else str(raw).strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    age = int(raw)
    return age if 10 <= age <= 120 else None


# P0 audit T-dw1-01: resume size guard. Перенос дословный из handlers/registration.py.
RESUME_MAX_BYTES = 10 * 1024 * 1024


def is_allowed_resume(file_name: str | None) -> bool:
    """QW-03: accept only PDF/DOCX by extension (case-insensitive). Перенос дословный из
    handlers/registration.py::_is_allowed_resume."""
    if not file_name:
        return False
    name = file_name.lower()
    return name.endswith(".pdf") or name.endswith(".docx")


def resume_too_large(file_size) -> bool:
    """Перенос дословный из handlers/registration.py::_resume_too_large."""
    return bool(file_size) and file_size > RESUME_MAX_BYTES


def validate_date_range(step_key: str, dt: datetime) -> str | None:
    """LOW: sanity range check for a parsed date step. Перенос дословный из
    handlers/reg_flow.py::_validate_date_range."""
    today = datetime.now()
    if step_key == "birth_date":
        if dt > today:
            return "Дата рождения не может быть в будущем. Проверь и введи ещё раз."
        if dt.year < today.year - 100 or dt.year > today.year - 10:
            return "Проверь дату рождения (год выглядит неправдоподобно) и введи ещё раз."
    elif step_key == "arrival_date":
        if dt.date() < today.date():
            return "Дата приезда не может быть в прошлом. Введи корректную дату."
        if dt.year > today.year + 2:
            return "Проверь дату приезда (слишком далеко в будущем) и введи ещё раз."
    return None


# ── validate_answer: единая точка проверки ответа ────────────────────────────────────────────

# _store_choice-паттерн (handlers/reg_steps.py): непустой текст, «Другое» -> свободный ввод,
# текст ошибки/подсказки ОДИН на все шаги набора (не свой у каждого).
_CHOICE_STEPS = {
    "department", "aiesec_role", "needs_certificate", "english_level",
    "alumni_status", "arrival", "housing", "bed_sharing", "transport", "volunteer",
}
_CHOICE_EMPTY_ERROR = "Выбери вариант на клавиатуре или напиши ответ."
_CHOICE_OTHER_PROMPT = "Напиши свой вариант:"

# Шаги с собственным (не generic) текстом ошибки/«Другое»-подсказки — отдельные ветки в
# handlers/reg_steps.py (не через _store_choice): step_key -> (empty_error, other_prompt).
_BESPOKE_CHOICE = {
    "city": ("Выбери город на клавиатуре или напиши свой.", "Напиши название своего города:"),
    "source": ("Выбери один из вариантов или напиши свой.", "Напиши свой вариант:"),
    "local_committee": (
        "Выбери локальный комитет из списка или напиши свой.", "Напиши название своего ЛК:",
    ),
    "position": ("Выбери позицию из списка или напиши свою.", "Напиши свою позицию:"),
    "university": ("Выбери ВУЗ из списка или напиши свой.", "Напиши название своего ВУЗа:"),
}

# _store_text-паттерн: непустое поле, «Пропустить» -> "-". work_sphere — единственный шаг с
# нестандартным текстом ошибки (process_work_sphere); остальные делят один и тот же текст.
_SKIP_TEXT_ERRORS = {
    "specialty": "Напиши или нажми «Пропустить».",
    "work_sphere": "Напиши сферу работы или нажми «Пропустить».",
    "missing_skills": "Напиши или нажми «Пропустить».",
    "expectations": "Напиши или нажми «Пропустить».",
    "comments": "Напиши или нажми «Пропустить».",
    "allergies": "Напиши или нажми «Пропустить».",
    "food_pref": "Напиши или нажми «Пропустить».",
    "bed_partner": "Напиши или нажми «Пропустить».",
    "cc_shop": "Напиши или нажми «Пропустить».",
    "exp_organizers": "Напиши или нажми «Пропустить».",
    "exp_content": "Напиши или нажми «Пропустить».",
    # Rule 2 (28-01): без записи здесь _SKIP_ALLOWED_STEPS дал бы кнопку «Пропустить», но
    # validate_answer хранил бы литеральный текст "Пропустить" вместо "-" — тот же приём, что
    # у всех остальных skip-allowed шагов выше.
    "mini_portfolio": "Напиши или нажми «Пропустить».",
}

# Жёсткая проверка ровно допустимых литералов (без «Другое», без «Пропустить»).
# step_key -> (допустимые варианты, текст ошибки, конвертировать «Да»->True в bool).
_MEMBERSHIP_STEPS = {
    "work_status": (("Да", "Нет"), "Выбери «Да» или «Нет».", True),
    "informal_day": (("Да", "Нет", "Буду только в онлайне"), "Выбери один из вариантов.", False),
    "attendance_format": (("Offline", "Online"), "Выбери «Offline» или «Online».", False),
    # Phase 28 (28-01, SU-01, СкиллАп 5): case_optin — храним ПОДПИСЬ («Да»/«Нет»), не bool
    # (as_bool=False) — колонка листа и карточка печатают человеческое слово (D-02).
    "case_optin": (("Да", "Нет"), "Выбери «Да» или «Нет».", False),
}


def _validate_answer_core(step_key: str, raw, participant_type: str | None) -> tuple:
    # Task 260915-skg (P1/P3, T-skg-01): Mini App PATCH может прислать не-строковый JSON —
    # тумблер шлёт bool, `<input type="number">` шлёт число, до этого guard'а нижние ветки делают
    # голый `(raw or "").strip()`/`.startswith(...)` и падают AttributeError -> 500 (возраст,
    # тумблер «Образование»). Guard узкий: bool -> "" (ПЕРВЫМ, bool — подкласс int), int/float ->
    # str(raw), контейнер -> "" ТОЛЬКО для не-multi шага (multi легитимно получает список —
    # ветка ниже, строка 2337; repeatable свой список уже разобрал раньше, в `validate_answer`).
    # `None` не трогаем — нижние ветки уже пишут `(raw or "").strip()`.
    if isinstance(raw, bool):
        raw = ""
    elif isinstance(raw, (int, float)):
        raw = str(raw)
    elif isinstance(raw, (list, tuple, dict, set)) and REG_STEP_TYPES.get(step_key) != "multi":
        raw = ""
    if step_key == "full_name":
        text = (raw or "").strip()
        if len(text.split()) < 2:
            return None, "Укажи ФИО полностью (минимум фамилию и имя)."
        return text, None
    if step_key == "age":
        value = parse_age(raw)
        if value is None:
            return None, "Укажи корректный возраст числом от 10 до 120."
        return value, None
    if step_key == "email":
        text = (raw or "").strip()
        if not text or "@" not in text or "." not in text:
            return None, "Укажи корректный email (например, name@example.com)."
        return text, None
    if step_key == "phone":
        text = (raw or "").strip()
        if text == "Пропустить":
            return "-", None
        cleaned = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not cleaned:
            return None, "Укажи номер телефона или нажми «Пропустить»."
        if not (cleaned.startswith("+") and cleaned[1:].isdigit()) and not cleaned.isdigit():
            return None, "Укажи корректный номер телефона или нажми «Пропустить»."
        return text, None
    if step_key == "vk":
        vk = (raw or "").strip()
        if not vk.startswith("@") or len(vk) < 2 or " " in vk:
            return None, "Укажи ник в ВК в формате @username (начинается с @, без пробелов)."
        return vk, None
    if step_key == "resume":
        # Только текстовый вариант (process_resume_text) — файл (process_resume) идёт через
        # is_allowed_resume/resume_too_large напрямую, там другая форма входа (имя файла/размер,
        # не «сырой текст»), сюда не попадает.
        text = (raw or "").strip()
        if not text:
            return None, "Напиши резюме текстом или прикрепи файл (PDF или DOCX)."
        return text, None
    if step_key == "education_status":
        text = (raw or "").strip()
        if not text:
            return None, "Выбери один из вариантов."
        return text, None
    if step_key == "course":
        text = (raw or "").strip()
        if not text:
            return None, "Выбери курс."
        return text, None
    if step_key == "ambassador":
        text = (raw or "").strip()
        if not text:
            return None, "Выбери «Да!» или «Пока нет»."
        return text.lower().startswith("да"), None
    if step_key in _BESPOKE_CHOICE:
        empty_err, other_prompt = _BESPOKE_CHOICE[step_key]
        text = (raw or "").strip()
        if not text:
            return None, empty_err
        if text == "Другое":
            return None, other_prompt
        return text, None
    if step_key in _CHOICE_STEPS:
        text = (raw or "").strip()
        if not text:
            return None, _CHOICE_EMPTY_ERROR
        if text == "Другое":
            return None, _CHOICE_OTHER_PROMPT
        return text, None
    if step_key in _SKIP_TEXT_ERRORS:
        text = (raw or "").strip()
        if not text:
            return None, _SKIP_TEXT_ERRORS[step_key]
        return ("-" if text == "Пропустить" else text), None
    if step_key in _MEMBERSHIP_STEPS:
        allowed, err, as_bool = _MEMBERSHIP_STEPS[step_key]
        text = (raw or "").strip()
        if text not in allowed:
            return None, err
        return ((text == "Да") if as_bool else text), None
    step_type = REG_STEP_TYPES.get(step_key)
    if step_type == "select":
        text = (raw or "").strip()
        if not text:
            return None, "Выбери вариант на клавиатуре или напиши свой."
        if text == "Другое":
            return None, "Напиши свой вариант:"
        return text, None
    if step_type == "multi":
        chosen = list(raw) if raw else []
        if not chosen:
            return None, "Выбери хотя бы один вариант."
        return ", ".join(chosen), None
    if step_type == "date":
        text = (raw or "").strip()
        try:
            dt = datetime.strptime(text, "%d.%m.%Y")
        except (ValueError, TypeError):
            return None, "Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз."
        range_err = validate_date_range(step_key, dt)
        if range_err:
            return None, range_err
        return text, None
    # Неизвестный/будущий шаг без собственной ветки — та же терпимость, что была у бота для
    # любого текстового шага без явной проверки (просто сохраняет то, что прислали).
    return (raw or "").strip(), None


# Plan 21-10 (FORM-SYNC-03, Pitfall 10): Mini App шлёт `null`, когда делегат нажал
# «Пропустить» — эквивалент литерала «Пропустить», который бот сегодня получает текстом.
# Подставлять литерал безопасно ТОЛЬКО туда, где сам валидатор уже понимает «Пропустить» как
# сигнал очистки поля (иначе, например, "multi" получил бы строку вместо списка, а "resume"
# принял бы «Пропустить» как настоящий текст резюме, хотя своего литерала-скипа не имеет).
_NULL_SKIP_STEPS = _SKIP_ALLOWED_STEPS | {"phone"}

# Phase 28 (28-03, SU-02): дефолт текста ошибки лимита — используется, только если вызывающий
# не резолвил `reg_multi_limit_error_text` из реестра сам (движок синхронный, в БД не ходит).
# Подстановка `{max}` — ТОЛЬКО `.replace`, не `.format` (текст менеджера может содержать
# посторонние фигурные скобки, T-073-03-05).
_DEFAULT_MULTI_LIMIT_ERROR_TEXT = "Можно выбрать не больше {max} вариантов."


def validate_answer(
    step_key: str, raw, *, participant_type: str | None = None,
    max_select: int | None = None, limit_error_text: str | None = None,
    whitelist: list[str] | None = None, repeatable_max_items: int | None = None,
) -> tuple:
    """Единая точка проверки ответа — и для текста из чата бота, и (план 21-10) для JSON из
    Mini App (T-21-05). Возвращает `(value, error_text)`; `error_text is None` значит `value`
    готово класть в состояние/черновик. Для choice-шагов с `other_allowed` литерал «Другое»
    тоже возвращается через `error_text` — это ТА ЖЕ подсказка «напиши свой вариант», что бот
    сегодня шлёт тем же `message.answer(...)`; какую клавиатуру приложить к этому тексту решает
    вызывающий (движок не знает про aiogram, T-21-03).

    `raw is None` (план 21-10, Mini App) — эквивалент «Пропустить» для шагов, где это реальный
    skip-литерал (`_NULL_SKIP_STEPS`); для остальных шагов `None` остаётся «пустой ввод» и
    получает обычную ошибку «поле обязательно» — конверсия НЕ в роутере (RESEARCH Pattern 2).

    `max_len` (T-21-04, DoS) — НОВАЯ проверка фазы 21 (см. `VALIDATION_GOLDEN`).

    `max_select`/`limit_error_text` (Phase 28-03, SU-02, A-06) — НОВЫЕ keyword-only параметры,
    дефолт `None`: при `None` (никто не передал лимит) поведение multi-шага байт-в-байт прежнее
    (`VALIDATION_GOLDEN` не двигается). Второй барьер — веб-PATCH мимо клиентского дизейбла и
    гонка «клавиатура рассинхронизировалась с настройкой, изменённой посреди анкеты» (Anti-
    Pattern RESEARCH: второго валидатора нет, ЭТА ветка — единственное место, где multi-лимит
    проверяется по-настоящему). Проверка — по количеству выбранных ДО joins-а в строку (core
    хранит multi-ответ как строку через `", ".join`, считать по запятым после — хрупко).

    `whitelist` (Phase 28-04, SU-04) — НОВЫЙ keyword-only параметр, дефолт `None`: применяется
    ТОЛЬКО к шагу `resume_link` (единая проверка ссылки — `validate_resume_link`, тот же приём
    keyword-only-с-безопасным-дефолтом, что `max_select`/`limit_error_text` выше). При `None`
    ссылка проверяется только на схему `http(s)://` — домен ни с чем не сверяется (пустой
    вайтлист), `VALIDATION_GOLDEN` не двигается, т.к. `resume_link` в нём не участвует.

    `repeatable_max_items` (Phase 30, 30-04, A2-05, T-30-09/T-30-10) — НОВЫЙ keyword-only
    параметр, тот же приём, что `max_select`/`limit_error_text`: вызывающий (async-контекст,
    Mini App PATCH/чат) резолвит `await repeatable_max(step_key)` сам и передаёт сюда; при
    `None` действует консервативный `_REPEATABLE_MAX_FALLBACK` (второй барьер DoS не должен
    исчезать только потому, что вызывающий забыл его прокинуть). Работает ТОЛЬКО для шагов
    `step_type_v2(step_key) == "repeatable"` — сегодня это `mini_portfolio`. Raw может прийти
    ЛИБО списком объектов (Mini App шлёт `onChange` уже массивом, `<interfaces>` 30-04-PLAN.md),
    ЛИБО голой строкой (чат бота/легаси-текст) — обе формы идут через `parse_repeatable`, ветка
    ниже НЕ дублирует его правило «не JSON -> legacy» (T-30-09: клиент не может подменить формат
    колонки произвольным JSON — сервер сам собирает итоговую строку через `dump_repeatable` из
    уже нормализованных блоков, не сохраняет присланный текст как есть)."""
    if raw is None and step_key in _NULL_SKIP_STEPS:
        raw = "Пропустить"
    if max_select is not None and REG_STEP_TYPES.get(step_key) == "multi":
        chosen = list(raw) if raw else []
        if len(chosen) > max_select:
            text = limit_error_text or _DEFAULT_MULTI_LIMIT_ERROR_TEXT
            return None, text.replace("{max}", str(max_select))
    # Phase 30 (30-04, A2-05): ветка входит ТОЛЬКО когда `raw` уже пришёл списком (Mini App
    # repeatable-контрол шлёт `onChange` массивом объектов, form_types.js докстринг) — голая
    # строка (сегодняшний бот, `handlers/reg_extra_steps.py::process_mini_portfolio`, ИЛИ
    # легаси-Mini App при выключенном `reg_form_repeatable`) идёт СТАРЫМ путём ниже
    # (`_SKIP_TEXT_ERRORS["mini_portfolio"]`) БЕЗ единого изменения байта — иначе включение
    # этого плана само по себе начало бы JSON-оборачивать текущий свободный ответ делегата,
    # хотя ни один тумблер ещё не включён (инвариант «выключенный v2 = поведение прежнее»).
    if step_type_v2(step_key) == "repeatable" and isinstance(raw, list):
        items = parse_repeatable(raw)
        limit = repeatable_max_items if repeatable_max_items is not None else _REPEATABLE_MAX_FALLBACK
        if len(items) > limit:
            return None, f"Слишком много блоков (максимум {limit})."
        for item in items:
            if len(item["title"]) > _REPEATABLE_TITLE_MAX_LEN:
                return None, f"Слишком длинное название (максимум {_REPEATABLE_TITLE_MAX_LEN} символов)."
            if len(item["description"]) > MAX_LEN_DEFAULT:
                return None, f"Слишком длинное описание (максимум {MAX_LEN_DEFAULT} символов)."
        return (dump_repeatable(items) if items else "-"), None
    if step_key == "resume_link":
        url, _verified, error = validate_resume_link(raw, whitelist)
        value = url
    else:
        value, error = _validate_answer_core(step_key, raw, participant_type)
    if error is None and isinstance(value, str):
        ui_type = _ui_type_for(step_key, REG_STEP_TYPES.get(step_key, "text"))
        max_len = _max_len_for(step_key, ui_type)
        if max_len and len(value) > max_len:
            return None, f"Слишком длинный ответ (максимум {max_len} символов)."
    return value, error


# ── apply_answer: побочные правила при ответе (APPLY_GOLDEN) ─────────────────────────────────

def apply_answer(
    answers: dict, step_key: str, value, *, studying_statuses: list[str] | None = None,
) -> dict:
    """Кладёт `value` шага в свою колонку (`STEP_TO_COLUMN`) + применяет побочные правила
    (`APPLY_GOLDEN`: не учится -> ВУЗ/курс/специальность/направление обучения прочерком; не
    работает -> сфера работы прочерком). Возвращает НОВЫЙ dict, входной `answers` не мутирует —
    вызывающий (FSM-хендлер бота или веб-роутер) сам решает, как сохранить результат.

    Phase 28 (28-01, SU-03, R-A3 CONTEXT): `studying_statuses` — keyword-only, дефолт `None`
    воспроизводит прежнее правило `startswith("Да")` байт-в-байт (APPLY_GOLDEN не двигается);
    вызывающий в async-контексте (handlers/reg_steps.py, miniapp/routers/form.py) передаёт
    `await reg_engine.studying_statuses()`."""
    result = dict(answers)
    column = STEP_TO_COLUMN.get(step_key, step_key)
    result[column] = value
    if step_key == "education_status" and not is_studying(value, studying_statuses):
        result["university"] = "-"
        result["course"] = "-"
        result["specialty"] = "-"
        result["study_field"] = "-"
    if step_key == "work_status" and not value:
        result["work_sphere"] = "-"
    return result


def apply_answers(
    answers: dict, patch: dict, *, studying_statuses: list[str] | None = None,
) -> dict:
    """Применить несколько ответов подряд (Mini App PATCH нескольких полей за один запрос) —
    тот же `apply_answer` в цикле, в порядке `patch` (обычный dict сохраняет порядок вставки).
    `studying_statuses` — тот же keyword-only параметр, что у `apply_answer` (дефолт `None`)."""
    result = dict(answers)
    for step_key, value in patch.items():
        result = apply_answer(result, step_key, value, studying_statuses=studying_statuses)
    return result


# ── merge_answers / conflicts: пофилевый last-write-wins (D-19, FORM-SYNC-03) ────────────────

def merge_answers(current: dict, field_versions: dict, base_version: int, patch: dict,
                   new_version: int) -> tuple:
    """`patch` побеждает ВСЕГДА (его только что набрал человек); `conflicts` — колонки, которые
    кто-то другой менял ПОСЛЕ `base_version` (версии, с которой клиент рисовал форму) и чьё
    текущее значение отличается от присланного — список для информирования, не для отката
    (T-21-21: потеря ввода при гонке двух окон — patch никогда не откатывается)."""
    conflicts_out = [
        col for col, val in patch.items()
        if field_versions.get(col, 0) > base_version and current.get(col) != val
    ]
    merged = {**current, **patch}
    versions = {**field_versions, **{col: new_version for col in patch}}
    return merged, versions, conflicts_out


def conflicts(field_versions: dict, base_version: int, columns) -> list:
    """Список колонок из `columns`, которые кто-то менял после `base_version` — для случая,
    когда нужно только УЗНАТЬ, что изменилось (например `GET draft` при возврате фокуса), не
    сливая целиком ответ."""
    return [col for col in columns if field_versions.get(col, 0) > base_version]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Phase 21 (21-06, Task 3) — with_defaults / summary_fields / decide_status / diff: дефолты
# финала, данные сводки, расчёт статуса модерации и вычисление diff — перенос дословный из
# handlers/registration.py (setdefault-блок finalize_registration, _build_summary/_esc,
# _decide_status). `finalize_registration` САМА в этом плане не переписывается (план 21-08) —
# отсюда только исчезает дублирующая логика, движок становится единственным источником правды
# для обеих поверхностей.
#
# T-21-03 (XSS): движок НЕ собирает HTML — `summary_fields` отдаёт голые (label, value), теги и
# экранирование добавляет вызывающий (бот своим `_esc`, Mini App своим `h()`/DOM). В этом файле
# намеренно нет ни одного HTML-тега и ни одного вызова экранирующей функции стандартной библиотеки.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def with_defaults(answers: dict) -> dict:
    """Дефолты финала анкеты — прежний setdefault-блок `finalize_registration` (~20 полей),
    перенесён дословно, без изменения значений по умолчанию. Возвращает НОВЫЙ dict (копия
    `answers` + дефолты) — чистая функция, в отличие от прежнего `data.setdefault(...)` на живом
    FSM-словаре; единая для бота и будущего веб-финала (план 21-08).
    Квик 260912: подпись подстановки `source` — `_opts.SOURCE_NOT_ASKED`, а не «Самостоятельно»,
    — прежняя подпись читалась менеджером на дашборде как осознанный ответ делегата."""
    result = dict(answers)
    result.setdefault("email", "-")
    result.setdefault("phone", "-")
    result.setdefault("city", "-")
    result.setdefault("is_aiesec_member", False)
    result.setdefault(
        "source", "Реферальная ссылка" if result.get("referrer_id") else _opts.SOURCE_NOT_ASKED,
    )
    result.setdefault("source_details", f"Referrer ID: {result.get('referrer_id', '-')}")
    result.setdefault("education_status", "-")
    result.setdefault("university", "-")
    result.setdefault("course", "-")
    result.setdefault("specialty", "-")
    result.setdefault("work_status", False)
    result.setdefault("work_sphere", "-")
    result.setdefault("missing_skills", "-")
    result.setdefault("expectations", "-")
    result.setdefault("local_committee", "-")
    result.setdefault("position", "-")
    # IN-01: expectations_ar остаётся мёртвым дефолтом не заведённым намеренно (ни один шаг его
    # не заполняет) — колонка листа остаётся, повторное заведение сломало бы ширину листа.
    result.setdefault("informal_day", "-")
    result.setdefault("attendance_format", "-")
    result.setdefault("comments", "-")
    result.setdefault("resume_file_id", None)
    result.setdefault("resume_text", None)
    result.setdefault("resume_url", None)
    return result


# Поля сводки (QW-01) — перенос дословный из handlers/registration.py::_build_summary: тот же
# список, тот же порядок, то же условие фильтрации (None/пустая строка не попадают в сводку).
_SUMMARY_FIELD_LABELS = [
    ("ФИО", "full_name"),
    ("Возраст", "age"),
    ("Дата приезда", "arrival_date"),
    ("Дата рождения", "birth_date"),
    ("Email", "email"),
    ("Телефон", "phone"),
    ("ВК", "vk_username"),
    ("Город", "city"),
    ("Источник", "source"),
    ("Лок. комитет", "local_committee"),
    ("Позиция", "position"),
    ("Образование", "education_status"),
    ("ВУЗ", "university"),
    ("Курс", "course"),
    ("Специальность", "specialty"),
    ("Направление обучения", "study_field"),
    # "Работа" и "Амбассадор" вычисляются отдельно ниже (bool -> «Да»/None, не прямой .get).
    ("Сфера работы", "work_sphere"),
    ("Навыки", "missing_skills"),
    ("Ожидания", "expectations"),
    ("Неформальный день", "informal_day"),
    ("Формат", "attendance_format"),
    ("Комментарии", "comments"),
    ("Департамент", "department"),
    ("Позиция АЙСЕК", "aiesec_role"),
    ("Аламни/айсекер", "alumni_status"),
    ("Справка в ВУЗ", "needs_certificate"),
    ("Английский", "english_level"),
    ("Аллергии", "allergies"),
    ("Питание", "food_pref"),
    ("Приезд", "arrival"),
    ("Проживание", "housing"),
    ("Общая кровать", "bed_sharing"),
    ("Сосед по кровати", "bed_partner"),
    ("Трансфер", "transport"),
    ("Дата план. оплаты", "payment_plan_date"),
    ("CC-shop", "cc_shop"),
    ("Ожидания от орг", "exp_organizers"),
    ("Ожидания от контента", "exp_content"),
    ("Волонтёр", "volunteer"),
    ("Цель участия", "goal"),
    ("Форматы форума", "formats"),
    # Phase 28 (28-01, SU-01/SU-04, СкиллАп 5) — восемь строк в порядке шагов REG_FLOW.
    ("Стек", "stack"),
    ("Опыт", "experience"),
    ("Готовность", "readiness"),
    ("Резюме (ссылка)", "resume_link"),
    ("Проекты", "mini_projects"),
    ("Портфолио", "mini_portfolio"),
    ("Направление развития", "mini_direction"),
    ("Кейс-чемпионат", "case_optin"),
]


def summary_fields(answers: dict) -> list:
    """QW-01: данные сводки анкеты БЕЗ разметки (T-21-03 — HTML собирает вызывающий, не
    движок). Перенос дословный из handlers/registration.py::_build_summary — тот же список
    полей в том же порядке, то же условие фильтрации пустых значений, то же условие резюме
    (файл побеждает текст).

    Phase 30 (30-04, A2-05): repeatable-колонка (`mini_portfolio`) — «сводка анкеты в чате»
    показывает её ЧЕЛОВЕКУ (QW-01 confirm-экран перед отправкой), поэтому идёт через
    `repeatable_display(parse_repeatable(...))`, ту же пару функций, что лист/карточка заявки —
    без этого делегат увидел бы в подтверждении сырой JSON-список своих блоков."""
    answers = answers or {}
    fields = [
        (
            label,
            repeatable_display(parse_repeatable(answers.get(column)))
            if column in _REPEATABLE_COLUMNS else answers.get(column),
        )
        for label, column in _SUMMARY_FIELD_LABELS
    ]
    fields.append(("Работа", "Да" if answers.get("work_status") else "Нет"))
    fields.append(("Амбассадор", "Да" if answers.get("is_ambassador_candidate") else None))
    out = [(label, value) for label, value in fields if not (value is None or str(value) == "")]
    if answers.get("resume_file_id"):
        out.append(("Резюме", "прикреплено файлом"))
    elif answers.get("resume_text"):
        out.append(("Резюме", answers.get("resume_text")))
    return out


# ── Автоскоринг заявки (Phase 28-07, SU-08, A-04) ───────────────────────────────────────────
# Формула ТЗ §3.6: веса жёсткие (2/2/1/1/1, максимум 7), множества/пороги — реестровые
# (менеджер решает, какие направления считать IT и какой курс считать старшим, D-01). Пустое
# множество/невыставленный порог = ЭТОТ пункт никому не начисляется (не «всем») — выключенный
# по умолчанию скоринг не должен раздавать баллы на других событиях (D-06). Чистая функция —
# та же форма, что `decide_status` ниже: тестируется без БД и без aiogram.

async def scoring_rules() -> dict:
    """Один поход в реестр за все семь скоринговых значений (T-28-07-03: ни одного лишнего
    запроса на кандидата — вызывающий (`services/reg_finalize.py`) зовёт это один раз и
    передаёт готовый `rules` в чистый `compute_score`). Списки — через `option_list_for` с
    ПУСТЫМ дефолтом (не путать с дефолтами вариантов вопросов): невыставленное множество
    значит «правило не срабатывает», а не «стандартный набор» (D-06)."""
    return {
        "it_fields": await option_list_for("score_it_fields", []),
        "senior_statuses": await option_list_for("score_senior_statuses", []),
        "readiness_counts": await option_list_for("score_readiness_counts", []),
        "experience_counts": await option_list_for("score_experience_counts", []),
        "course_from": await get_setting_typed("score_course_from"),
        "stack_from": await get_setting_typed("score_stack_from"),
    }


def course_number(value) -> int | None:
    """Ведущее число из подписи варианта курса (R-A3b CONTEXT): «3» -> 3, «5+» -> 5.
    «Магистратура/Аспирантура» числа не имеет -> `None` — курс НЕ засчитывается порогом
    «курс от N», это направление берёт только множество «старшие статусы» образования."""
    match = re.match(r"\d+", str(value or "").strip())
    return int(match.group()) if match else None


def compute_score(answers: dict, rules: dict) -> tuple[int, bool]:
    """Чистая формула ТЗ §3.6 — без БД, без aiogram, синхронная (тот же класс функции, что
    `decide_status`). `rules` — заранее собранный словарь `scoring_rules()`; `answers` — плоский
    словарь ответов анкеты (те же ключи, что кладёт `with_defaults`/`add_user`, включая
    `resume_file_id`/`resume_url`, если они там есть).

    `is_it_3plus` — ОБЕ половины формулы одновременно (направление/стек И курс/статус), а не
    просто «сумма набрала порог» — читается прямо из формулы ТЗ, не выводится из `score`."""
    answers = answers or {}
    it_fields = rules.get("it_fields") or []
    senior_statuses = rules.get("senior_statuses") or []
    readiness_counts = rules.get("readiness_counts") or []
    experience_counts = rules.get("experience_counts") or []
    course_from = rules.get("course_from")
    stack_from = rules.get("stack_from")

    stack_raw = str(answers.get("stack") or "")
    stack_selected = [item.strip() for item in stack_raw.split(", ") if item.strip()]
    is_it_direction = bool(it_fields) and answers.get("study_field") in it_fields
    is_it_stack = stack_from is not None and len(stack_selected) >= stack_from
    direction_condition = is_it_direction or is_it_stack

    course_num = course_number(answers.get("course"))
    is_senior_course = (
        course_from is not None and course_num is not None and course_num >= course_from
    )
    is_senior_status = bool(senior_statuses) and answers.get("education_status") in senior_statuses
    course_condition = is_senior_course or is_senior_status

    has_resume = (
        answers.get("resume_type") in ("file", "link")
        or bool(answers.get("resume_file_id"))
        or bool(answers.get("resume_url"))
    )
    has_readiness = bool(readiness_counts) and answers.get("readiness") in readiness_counts
    has_experience = bool(experience_counts) and answers.get("experience") in experience_counts

    score = 0
    if direction_condition:
        score += 2
    if course_condition:
        score += 2
    if has_resume:
        score += 1
    if has_readiness:
        score += 1
    if has_experience:
        score += 1

    return score, bool(direction_condition and course_condition)


def decide_status(reg_mode: str, full_setting: str, short_setting: str,
                   participant_type: str = "full", party_setting: str | None = None) -> str:
    """Form type x per-form moderation setting -> 'pending' | 'approved'. Перенос дословный из
    handlers/registration.py::_decide_status (Phase 2, D-01..D-03).

    Phase 5 (D-13): party tracks resolve status from party_approval alone, completely
    independent of full_approval/short_approval — this branch never falls through to the
    reg_mode logic below it, and never reads full_setting/short_setting. `party_setting`
    of None (an unconfigured party_approval) resolves to "manual": a party track must be
    moderated by default, never silently auto-approved (T-05-04-02).

    Phase 7 (SHORT-05): the short track resolves status from `short_setting` (short_approval)
    alone, keyed off the persisted `participant_type`, and — crucially — WITHOUT reading
    `reg_mode` at all."""
    if _is_party_track(participant_type):
        setting = party_setting or "manual"
        return "pending" if setting == "manual" else "approved"
    if _is_short_track(participant_type):
        return "pending" if short_setting == "manual" else "approved"
    setting = full_setting if reg_mode == "full" else short_setting
    return "pending" if setting == "manual" else "approved"


def diff(old_row: dict | None, new_answers: dict) -> list:
    """Только колонки анкеты (`answer_columns()`); значения, равные с точностью до
    `str().strip()`, изменением не считаются — пустой результат означает «правки не было»
    (D-14: пометка «изменена» в карточке заявки ставится только при непустом diff).

    Quick 260910-wb6: `DRAFT_ONLY_COLUMNS` (колонки без пары в `users`) исключены — иначе узкий
    UPDATE финала правки ловит `no such column` (см. докстринг `DRAFT_ONLY_COLUMNS`)."""
    old_row = old_row or {}
    new_answers = new_answers or {}
    changes = []
    for column in answer_columns():
        if column in DRAFT_ONLY_COLUMNS:
            continue
        old_value = old_row.get(column)
        new_value = new_answers.get(column)
        old_str = "" if old_value is None else str(old_value).strip()
        new_str = "" if new_value is None else str(new_value).strip()
        if old_str == new_str:
            continue
        changes.append({"column": column, "old": old_value, "new": new_value})
    return changes
