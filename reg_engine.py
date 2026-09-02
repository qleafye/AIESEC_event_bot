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
"""
from datetime import datetime

from config import config
from database.db import get_setting
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from cities import cities_module_on, enabled_cities
from reg_labels import REG_LABELS
import reg_options as _opts

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
    ("goal", "reg_q_goal", "multi"),
    ("formats", "reg_q_formats", "multi"),
    ("expectations", "reg_q_expectations", "text"),
    ("source", "reg_q_source", "text"),
    ("ambassador", "reg_q_ambassador", "ambassador"),
    ("resume", "reg_q_resume", "text"),
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
# is_ambassador_candidate. "resume" stays in the map as an identity entry (three real columns:
# resume_file_id/resume_text/resume_url) but is excluded from RECALLABLE_STEPS — a raw
# file_id/URL is meaningless to a human (Pitfall 3).
STEP_TO_COLUMN = {step_key: step_key for step_key, _sk, _t in REG_FLOW}
STEP_TO_COLUMN["vk"] = "vk_username"
STEP_TO_COLUMN["ambassador"] = "is_ambassador_candidate"
# ФИО спрашивается ВНЕ REG_FLOW (_ask_full_name, до движка шагов) — колонка совпадает с ключом.
STEP_TO_COLUMN["full_name"] = "full_name"
RECALLABLE_STEPS = {k for k in STEP_TO_COLUMN if k != "resume"}

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
}


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
    if step_key == "university":
        mode = await get_setting_typed("reg_university_mode")
        if mode == "text":
            return []
        uni_opts = await get_setting("university_options")
        if uni_opts and uni_opts.strip():
            return [line.strip() for line in uni_opts.splitlines() if line.strip()]
        return list(config.UNIVERSITIES)
    return list(_LITERAL_OPTIONS.get(step_key, []))


# ── Гейт «включён ли шаг для трека» + список включённых шагов ──────────────────────────────

async def is_step_enabled_for_track(setting_key: str, participant_type: str | None) -> bool:
    """D-03/D-04: tri-state per-track gate. `__party` is ONE namespace shared by
    party_overnight and party_noovernight. Key-absence means "inherit the global reg_q_<step>
    value"; key-presence means the explicit on/off wins. Full track (or None) never reads the
    __party key.

    Phase 7 (SHORT-01/SHORT-04): the short track reads `{setting_key}__short` the same
    tri-state way but does NOT fall through to the global value on key-absence — short is a
    curated subset of the full form, so key-absence means "do not ask this question"."""
    if _is_party_track(participant_type):
        override = await get_setting(f"{setting_key}__party")
        if override is not None:
            return override == "on"
    elif _is_short_track(participant_type):
        override = await get_setting(f"{setting_key}__short")
        if override is not None:
            return override == "on"
        return False
    return await _is_step_enabled(setting_key)


async def enabled_steps(data: dict) -> list[str]:
    """Список step_key, которые нужно спросить/показать для текущих `data` — единая точка
    правды для условных шагов и пресетов (D-03/FORM-SYNC-01). Перенос дословный из
    handlers/registration.py::_get_enabled_steps."""
    enabled = []
    edu_conditional = await get_setting_typed("edu_conditional") == "on"
    studying = str(data.get("education_status", "")).startswith("Да")
    participant_type = data.get("participant_type") or "full"
    for step_key, setting_key, *_rest in REG_FLOW:
        if not await is_step_enabled_for_track(setting_key, participant_type):
            continue
        if step_key == "informal_day" and data.get("attendance_format") == "Online":
            continue
        if step_key == "source" and data.get("_source_from_tag"):
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
}

_GENERIC_FALLBACK_LABEL = {"select": "Выбери вариант", "multi": "Выбери варианты", "date": "Дата"}


async def _default_prompt_text(step_key: str, participant_type: str | None) -> str:
    """Вычисляет дефолтный текст вопроса — то же самое, что раньше собирал `_ask_step` перед
    вызовом `_prompt(step_key, default, participant_type)`. university/expectations/
    payment_plan_date зависят от реестра (режим ВУЗа, event_name, дедлайн оплаты); остальные
    типизированные (date/select/multi) — от REG_LABELS; всё прочее — статический литерал."""
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


async def prompt(step_key: str, participant_type: str | None = None) -> str:
    """D-05: admin override reg_prompt_<step_key> (party track checks __party first, truthy
    wins) else the computed default. Перенос `_prompt` из handlers/registration.py — с той
    разницей, что `default` теперь считается ВНУТРИ (см. `_default_prompt_text`), а не
    приходит аргументом от вызывающего: бот и веб зовут одну и ту же резолюцию."""
    default = await _default_prompt_text(step_key, participant_type)
    if _is_party_track(participant_type):
        override = await get_setting(f"reg_prompt_{step_key}__party")
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
    `meta` — {event_city, is_registered, party_track, recovered_track}, все опциональны."""
    meta = meta or {}
    screens = list(await get_consent_steps())
    is_registered = bool(meta.get("is_registered"))
    event_city = meta.get("event_city") or answers.get("event_city")
    if await should_show_city_fork(event_city, is_registered):
        screens.append("city_fork")
    if await should_show_fork(meta.get("party_track"), meta.get("recovered_track"), is_registered):
        screens.append("party_fork")
    return screens


# ── Веб-контракт: allowlist колонок, спека шага, спека формы ────────────────────────────────

_EXTRA_ANSWER_COLUMNS = ["resume_file_id", "resume_file_name", "resume_text"]


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
}
_TEXTAREA_STEPS = {"expectations", "comments"}


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


# Права/безопасность формы (RESEARCH § «Права / безопасность формы»): ФИО 200,
# expectations/comments/resume(текст) 4000, остальной текст 1000. Бот сегодня лимита не
# применяет (summary режется по 4096 Telegram-лимиту отдельно) — эти константы предназначены
# для будущего веб-валидатора PATCH-черновика (план 21-03), step_spec их только публикует.
MAX_LEN_DEFAULT = 1000
MAX_LEN_LONG = 4000
MAX_LEN_FULL_NAME = 200
_LONG_TEXT_STEPS = {"expectations", "comments", "resume"}


def _max_len_for(step_key: str, ui_type: str) -> int | None:
    if step_key == "full_name":
        return MAX_LEN_FULL_NAME
    if step_key in _LONG_TEXT_STEPS:
        return MAX_LEN_LONG
    if ui_type in ("text", "textarea", "phone", "email"):
        return MAX_LEN_DEFAULT
    return None


# Шаги, у которых бот сегодня показывает клавиатуру "Пропустить" (get_skip_kb) — required=False.
_SKIP_ALLOWED_STEPS = {
    "specialty", "work_sphere", "missing_skills", "expectations", "comments", "food_pref",
    "bed_partner", "cc_shop", "exp_organizers", "exp_content", "allergies",
}
# Шаги, у которых клавиатура бота сегодня включает кнопку "Другое" (свободный текст поверх
# списка) — city/study_field через _reply_kb(options, add_other=True) в _ask_step,
# local_committee/position/department/aiesec_role через builders.py.
_OTHER_ALLOWED_STEPS = {"city", "study_field", "local_committee", "position", "department", "aiesec_role"}


async def step_spec(step_key: str, participant_type: str | None = None,
                     event_city: str | None = None) -> dict:
    """Спека одного шага по контракту UI-SPEC — бот берёт из неё текст/варианты по отдельности
    (`prompt()`/`options()`), Mini App (план 21-04a/b) — эту функцию целиком. `event_city`
    принят для единообразия с будущими per-city полями формы; сегодня спека от него не зависит."""
    step_type = REG_STEP_TYPES.get(step_key, "text")
    ui_type = _ui_type_for(step_key, step_type)
    label = label_for(step_key)
    spec = {
        "key": step_key,
        "column": STEP_TO_COLUMN.get(step_key, step_key),
        "type": ui_type,
        "label": label,
        "prompt": await prompt(step_key, participant_type),
        "options": None,
        "other_allowed": step_key in _OTHER_ALLOWED_STEPS,
        "skip_allowed": step_key in _SKIP_ALLOWED_STEPS,
        "required": step_key not in _SKIP_ALLOWED_STEPS,
        "max_len": _max_len_for(step_key, ui_type),
    }
    if ui_type in ("choice-chips", "select", "multi", "yesno"):
        spec["options"] = await options(step_key)
    return spec


def _display_value(value) -> str:
    """Человекочитаемое представление прошлого ответа — та же логика, что
    `handlers/registration.py::_recall_display`, БЕЗ HTML-экранирования (T-21-03: движок
    отдаёт сырой текст, экранирование — забота поверхности)."""
    if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
        return "Да" if value else "Нет"
    return str(value)


async def form_spec(answers: dict, participant_type: str | None = None,
                     event_city: str | None = None, prior: dict | None = None) -> dict:
    """Контракт формы для Mini App (RESEARCH Pattern 2): `{pre, steps, progress}`. `prior` —
    результат `prior_answers_for(user_row)` для возвращенца (D-07); движок НИКУДА prior не
    пишет и не логирует — вызывающий передаёт его на каждый запрос заново (Pitfall 5). Шаг без
    собственного ответа, но с непустым prior, получает `value_source == "prior"`; отвеченный
    шаг — `"answer"`; шаг без ответа и без prior — `None`."""
    answers = answers or {}
    prior = prior or {}
    enabled = await enabled_steps(answers)
    steps_out = []
    done = 0
    for step_key in enabled:
        spec = await step_spec(step_key, participant_type, event_city)
        column = spec["column"]
        has_answer = column in answers and answers.get(column) not in (None, "", "-")
        prior_value = prior.get(step_key)
        if prior_value not in (None, "", "-"):
            spec["prior"] = {"value": prior_value, "display": _display_value(prior_value)}
        else:
            spec["prior"] = None
        if has_answer:
            spec["value"] = answers.get(column)
            spec["value_source"] = "answer"
            done += 1
        elif spec["prior"] is not None:
            spec["value"] = prior_value
            spec["value_source"] = "prior"
        else:
            spec["value"] = None
            spec["value_source"] = None
        steps_out.append(spec)
    pre = await pre_flow(answers, {"event_city": event_city, "is_registered": bool(prior)})
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


def has_prior_resume(user_row: dict | None) -> bool:
    """Карвинг resume (Pitfall 3): наличие любой из resume_file_id/resume_text/resume_url;
    само значение наружу не отдаётся — показывать raw file_id человеку бессмысленно."""
    if not user_row:
        return False
    return any(
        user_row.get(col) not in (None, "", "-")
        for col in ("resume_file_id", "resume_text", "resume_url")
    )


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

def parse_age(raw: str | None) -> int | None:
    """CR-8: ASCII-digit-safe age parse. Перенос дословный из
    handlers/registration.py::_parse_age."""
    raw = (raw or "").strip()
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
}

# Жёсткая проверка ровно допустимых литералов (без «Другое», без «Пропустить»).
# step_key -> (допустимые варианты, текст ошибки, конвертировать «Да»->True в bool).
_MEMBERSHIP_STEPS = {
    "work_status": (("Да", "Нет"), "Выбери «Да» или «Нет».", True),
    "informal_day": (("Да", "Нет", "Буду только в онлайне"), "Выбери один из вариантов.", False),
    "attendance_format": (("Offline", "Online"), "Выбери «Offline» или «Online».", False),
}


def _validate_answer_core(step_key: str, raw, participant_type: str | None) -> tuple:
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


def validate_answer(step_key: str, raw, *, participant_type: str | None = None) -> tuple:
    """Единая точка проверки ответа — и для текста из чата бота, и (план 21-10) для JSON из
    Mini App (T-21-05). Возвращает `(value, error_text)`; `error_text is None` значит `value`
    готово класть в состояние/черновик. Для choice-шагов с `other_allowed` литерал «Другое»
    тоже возвращается через `error_text` — это ТА ЖЕ подсказка «напиши свой вариант», что бот
    сегодня шлёт тем же `message.answer(...)`; какую клавиатуру приложить к этому тексту решает
    вызывающий (движок не знает про aiogram, T-21-03).

    `raw is None` (план 21-10, Mini App) — эквивалент «Пропустить» для шагов, где это реальный
    skip-литерал (`_NULL_SKIP_STEPS`); для остальных шагов `None` остаётся «пустой ввод» и
    получает обычную ошибку «поле обязательно» — конверсия НЕ в роутере (RESEARCH Pattern 2).

    `max_len` (T-21-04, DoS) — единственная НОВАЯ проверка этой фазы: у бота сегодня лимита нет
    (см. `VALIDATION_GOLDEN`, помечено отдельным комментарием — не перенос, а новое правило)."""
    if raw is None and step_key in _NULL_SKIP_STEPS:
        raw = "Пропустить"
    value, error = _validate_answer_core(step_key, raw, participant_type)
    if error is None and isinstance(value, str):
        ui_type = _ui_type_for(step_key, REG_STEP_TYPES.get(step_key, "text"))
        max_len = _max_len_for(step_key, ui_type)
        if max_len and len(value) > max_len:
            return None, f"Слишком длинный ответ (максимум {max_len} символов)."
    return value, error


# ── apply_answer: побочные правила при ответе (APPLY_GOLDEN) ─────────────────────────────────

def apply_answer(answers: dict, step_key: str, value) -> dict:
    """Кладёт `value` шага в свою колонку (`STEP_TO_COLUMN`) + применяет побочные правила
    (`APPLY_GOLDEN`: не учится -> ВУЗ/курс/специальность/направление обучения прочерком; не
    работает -> сфера работы прочерком). Возвращает НОВЫЙ dict, входной `answers` не мутирует —
    вызывающий (FSM-хендлер бота или веб-роутер) сам решает, как сохранить результат."""
    result = dict(answers)
    column = STEP_TO_COLUMN.get(step_key, step_key)
    result[column] = value
    if step_key == "education_status" and not str(value).startswith("Да"):
        result["university"] = "-"
        result["course"] = "-"
        result["specialty"] = "-"
        result["study_field"] = "-"
    if step_key == "work_status" and not value:
        result["work_sphere"] = "-"
    return result


def apply_answers(answers: dict, patch: dict) -> dict:
    """Применить несколько ответов подряд (Mini App PATCH нескольких полей за один запрос) —
    тот же `apply_answer` в цикле, в порядке `patch` (обычный dict сохраняет порядок вставки)."""
    result = dict(answers)
    for step_key, value in patch.items():
        result = apply_answer(result, step_key, value)
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
    FSM-словаре; единая для бота и будущего веб-финала (план 21-08)."""
    result = dict(answers)
    result.setdefault("email", "-")
    result.setdefault("phone", "-")
    result.setdefault("city", "-")
    result.setdefault("is_aiesec_member", False)
    result.setdefault(
        "source", "Реферальная ссылка" if result.get("referrer_id") else "Самостоятельно",
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
    ("Позиция AIESEC", "aiesec_role"),
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
]


def summary_fields(answers: dict) -> list:
    """QW-01: данные сводки анкеты БЕЗ разметки (T-21-03 — HTML собирает вызывающий, не
    движок). Перенос дословный из handlers/registration.py::_build_summary — тот же список
    полей в том же порядке, то же условие фильтрации пустых значений, то же условие резюме
    (файл побеждает текст)."""
    answers = answers or {}
    fields = [(label, answers.get(column)) for label, column in _SUMMARY_FIELD_LABELS]
    fields.append(("Работа", "Да" if answers.get("work_status") else "Нет"))
    fields.append(("Амбассадор", "Да" if answers.get("is_ambassador_candidate") else None))
    out = [(label, value) for label, value in fields if not (value is None or str(value) == "")]
    if answers.get("resume_file_id"):
        out.append(("Резюме", "прикреплено файлом"))
    elif answers.get("resume_text"):
        out.append(("Резюме", answers.get("resume_text")))
    return out


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
    (D-14: пометка «изменена» в карточке заявки ставится только при непустом diff)."""
    old_row = old_row or {}
    new_answers = new_answers or {}
    changes = []
    for column in answer_columns():
        old_value = old_row.get(column)
        new_value = new_answers.get(column)
        old_str = "" if old_value is None else str(old_value).strip()
        new_str = "" if new_value is None else str(new_value).strip()
        if old_str == new_str:
            continue
        changes.append({"column": column, "old": old_value, "new": new_value})
    return changes
